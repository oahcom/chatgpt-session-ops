"""Authority-layer tests: ownership, grants, confirmed identity, F-13 gating."""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.session_ops.authority import (  # noqa: E402
    InvalidPrincipal,
    LandingEvidence,
    NotAuthorized,
    Principal,
    QuarantineNotCleared,
    SecureWorkItemStore,
    UnconfirmedSession,
    validate_conversation_id,
)


def conversation_id() -> str:
    return str(uuid.uuid4())


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "wi.sqlite3"
        self.store = SecureWorkItemStore(self.db)
        self.store.initialize()
        self.alice = Principal(tenant_id="t1", principal_id="alice")
        self.bob = Principal(tenant_id="t1", principal_id="bob")
        self.mallory = Principal(tenant_id="t2", principal_id="mallory")

    def confirmed(self, session_id: str) -> LandingEvidence:
        return LandingEvidence(
            conversation_id=session_id,
            match="confirmed",
            quarantine_evidence_complete=True,
        )


class PrincipalValidationTests(unittest.TestCase):
    def test_rejects_empty(self) -> None:
        for bad in ("", "   "):
            with self.assertRaises(InvalidPrincipal):
                Principal(tenant_id=bad, principal_id="a")

    def test_rejects_control_characters(self) -> None:
        with self.assertRaises(InvalidPrincipal):
            Principal(tenant_id="t1", principal_id="a\nb")

    def test_accepts_email_like(self) -> None:
        p = Principal(tenant_id="t1", principal_id="user@example.com")
        self.assertEqual(p.principal_id, "user@example.com")


class ConversationIdentityTests(unittest.TestCase):
    def test_accepts_canonical_uuid(self) -> None:
        sid = conversation_id()
        self.assertEqual(validate_conversation_id(sid), sid)

    def test_rejects_non_conversation_identities(self) -> None:
        # Each of these is an identity the contract forbids accepting.
        for bad in (
            "",
            "acct3",
            "My chat about MCP",
            "6b043328-81dc-83ee-be43-d3f4ce732e8",     # short
            "6b043328-81dc-83ee-be43-d3f4ce732e8bXX",   # long
            "6B043328-81DC-83EE-BE43-D3F4CE732E8B",     # uppercase
            str(uuid.uuid4()).replace("-", ""),          # unhyphenated
        ):
            with self.assertRaises(UnconfirmedSession, msg=bad):
                validate_conversation_id(bad)


class OwnershipTests(Base):
    def test_creator_is_owner(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.assertEqual(self.store.get_work_item(self.alice, item.work_item_id),
                         item)

    def test_same_tenant_stranger_is_refused(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(NotAuthorized):
            self.store.get_work_item(self.bob, item.work_item_id)

    def test_other_tenant_is_refused(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(NotAuthorized):
            self.store.get_work_item(self.mallory, item.work_item_id)

    def test_missing_and_forbidden_are_indistinguishable(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(NotAuthorized) as forbidden:
            self.store.get_work_item(self.bob, item.work_item_id)
        with self.assertRaises(NotAuthorized) as absent:
            self.store.get_work_item(self.bob, "wi_" + str(uuid.uuid4()))
        self.assertEqual(str(forbidden.exception).split("'")[0],
                         str(absent.exception).split("'")[0])

    def test_owner_can_transition(self) -> None:
        item = self.store.create_work_item(self.alice)
        updated = self.store.set_status(self.alice, item.work_item_id, "paused")
        self.assertEqual(updated.status, "paused")

    def test_stranger_cannot_transition(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(NotAuthorized):
            self.store.set_status(self.bob, item.work_item_id, "paused")


class GrantTests(Base):
    def test_read_grant_allows_read_only(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        self.assertEqual(self.store.get_work_item(self.bob, item.work_item_id),
                         item)
        with self.assertRaises(NotAuthorized):
            self.store.set_status(self.bob, item.work_item_id, "paused")

    def test_write_grant_allows_transition(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "write")
        updated = self.store.set_status(self.bob, item.work_item_id, "completed")
        self.assertEqual(updated.status, "completed")

    def test_grant_does_not_cross_tenants(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "mallory", "write")
        # A grant row for another tenant must not widen access: the tenant check
        # runs before the grant lookup.
        with self.assertRaises(NotAuthorized):
            self.store.get_work_item(self.mallory, item.work_item_id)

    def test_grant_requires_write_on_target(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        with self.assertRaises(NotAuthorized):
            self.store.grant(self.bob, item.work_item_id, "carol", "read")

    def test_revoke_removes_access(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        self.store.revoke(self.alice, item.work_item_id, "bob", "read")
        with self.assertRaises(NotAuthorized):
            self.store.get_work_item(self.bob, item.work_item_id)


class ListingTests(Base):
    def test_owner_sees_only_own(self) -> None:
        a = self.store.create_work_item(self.alice)
        self.store.create_work_item(self.bob)
        self.assertEqual([i.work_item_id for i in self.store.list_work_items(self.alice)],
                         [a.work_item_id])

    def test_granted_item_appears_once(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        self.store.grant(self.alice, item.work_item_id, "bob", "write")
        ids = [i.work_item_id for i in self.store.list_work_items(self.bob)]
        # read+write grants must not duplicate the row.
        self.assertEqual(ids, [item.work_item_id])

    def test_other_tenant_sees_nothing(self) -> None:
        self.store.create_work_item(self.alice)
        self.assertEqual(self.store.list_work_items(self.mallory), [])

    def test_status_filter(self) -> None:
        a = self.store.create_work_item(self.alice)
        b = self.store.create_work_item(self.alice)
        self.store.set_status(self.alice, b.work_item_id, "completed")
        ids = [i.work_item_id for i in
               self.store.list_work_items(self.alice, status="completed")]
        self.assertEqual(ids, [b.work_item_id])
        self.assertNotIn(a.work_item_id, ids)

    def test_limit_bounds(self) -> None:
        from src.session_ops.authority import AuthorityError
        for bad in (0, -1, 201):
            with self.assertRaises(AuthorityError):
                self.store.list_work_items(self.alice, limit=bad)


class SessionLinkPreconditionTests(Base):
    def test_missing_evidence_fails_closed(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(self.alice, conversation_id(),
                                    item.work_item_id, None)
        self.assertEqual(self.store.list_sessions(self.alice, item.work_item_id), [])

    def test_mismatch_fails_closed(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        evidence = LandingEvidence(sid, "mismatch", True)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(self.alice, sid, item.work_item_id, evidence)
        self.assertEqual(self.store.list_sessions(self.alice, item.work_item_id), [])

    def test_ambiguous_fails_closed(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        evidence = LandingEvidence(sid, "ambiguous", True)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(self.alice, sid, item.work_item_id, evidence)

    def test_incomplete_quarantine_evidence_fails_closed(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        evidence = LandingEvidence(sid, "confirmed", False)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(self.alice, sid, item.work_item_id, evidence)

    def test_evidence_must_cover_the_linked_session(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(self.alice, conversation_id(),
                                    item.work_item_id,
                                    self.confirmed(conversation_id()))

    def test_confirmed_evidence_links(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        link = self.store.link_session(self.alice, sid, item.work_item_id,
                                       self.confirmed(sid))
        self.assertEqual(link.session_id, sid)
        self.assertEqual(len(self.store.list_sessions(self.alice, item.work_item_id)), 1)

    def test_link_requires_write_on_work_item(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        with self.assertRaises(NotAuthorized):
            self.store.link_session(self.bob, sid, item.work_item_id,
                                    self.confirmed(sid))

    def test_unconfirmed_session_rejected_before_any_write(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(UnconfirmedSession):
            self.store.link_session(self.alice, "acct3", item.work_item_id,
                                    LandingEvidence("acct3", "confirmed", True))
        self.assertEqual(self.store.list_sessions(self.alice, item.work_item_id), [])

    def test_active_link_hidden_from_stranger(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.store.link_session(self.alice, sid, item.work_item_id,
                                self.confirmed(sid))
        self.assertIsNotNone(self.store.get_active_link(self.alice, sid))
        self.assertIsNone(self.store.get_active_link(self.bob, sid))

    def test_unlink_requires_write(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.store.link_session(self.alice, sid, item.work_item_id,
                                self.confirmed(sid))
        with self.assertRaises(NotAuthorized):
            self.store.unlink_session(self.bob, sid)
        self.assertIsNotNone(self.store.unlink_session(self.alice, sid))

    def test_second_active_link_refused(self) -> None:
        item = self.store.create_work_item(self.alice)
        other = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.store.link_session(self.alice, sid, item.work_item_id,
                                self.confirmed(sid))
        from src.session_ops.work_items import SessionAlreadyLinked
        with self.assertRaises(SessionAlreadyLinked):
            self.store.link_session(self.alice, sid, other.work_item_id,
                                    self.confirmed(sid))

    def test_terminal_item_cannot_receive_link(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.set_status(self.alice, item.work_item_id, "completed")
        sid = conversation_id()
        from src.session_ops.work_items import InvalidTransition
        with self.assertRaises(InvalidTransition):
            self.store.link_session(self.alice, sid, item.work_item_id,
                                    self.confirmed(sid))


class PersistenceReopenTests(Base):
    def test_ownership_survives_reopen(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        reopened = SecureWorkItemStore(self.db)
        reopened.initialize()
        self.assertEqual(reopened.get_work_item(self.alice, item.work_item_id),
                         item)
        self.assertEqual(reopened.get_work_item(self.bob, item.work_item_id), item)
        # The raw store returns None for an unknown id; only the authority layer
        # turns that into a refusal.
        self.assertIsNone(reopened._store.get_work_item("wi_" + str(uuid.uuid4())))
        with self.assertRaises(NotAuthorized):
            reopened.get_work_item(self.alice, "wi_" + str(uuid.uuid4()))


if __name__ == "__main__":
    unittest.main()
