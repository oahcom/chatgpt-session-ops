"""Transactional authority-layer tests."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.session_ops.authority import (  # noqa: E402
    AuthorityError,
    DenyAllPrincipalDirectory,
    InvalidIdentifier,
    InvalidPrincipal,
    NotAuthorized,
    Principal,
    PrincipalDirectoryUnavailable,
    PrincipalRecord,
    QuarantineNotCleared,
    SecureWorkItemStore,
    StaticPrincipalDirectory,
    UnconfirmedSession,
    validate_conversation_id,
    validate_link_id,
    validate_work_item_id,
)
from src.session_ops.work_items import InvalidTransition, SessionAlreadyLinked  # noqa: E402


def conversation_id() -> str:
    return str(uuid.uuid4())


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "wi.sqlite3"
        self.directory = StaticPrincipalDirectory(
            [
                PrincipalRecord("alice", "t1"),
                PrincipalRecord("bob", "t1"),
                PrincipalRecord("carol", "t1"),
                PrincipalRecord("mallory", "t2"),
                PrincipalRecord("disabled", "t1", False),
            ]
        )
        self.store = SecureWorkItemStore(
            self.db, principal_directory=self.directory
        )
        self.store.initialize()
        self.alice = Principal("t1", "alice")
        self.bob = Principal("t1", "bob")
        self.mallory = Principal("t2", "mallory")

    def record_landing(
        self,
        session_id: str,
        match: str = "confirmed",
        complete: bool = True,
    ) -> str:
        # This simulates the trusted Session Ops landing/send component.
        return self.store._record_trusted_landing(session_id, match, complete)

    def create_link(self, work_item_id: str) -> str:
        sid = conversation_id()
        self.record_landing(sid)
        link = self.store.link_session(self.alice, sid, work_item_id)
        return link.link_id


class PrincipalValidationTests(unittest.TestCase):
    def test_accepts_valid_principal(self) -> None:
        principal = Principal("tenant-1", "user@example.com")
        self.assertEqual(principal.principal_id, "user@example.com")

    def test_rejects_empty_and_control_characters(self) -> None:
        for tenant, principal in (("", "alice"), ("t1", ""), ("t1", "a\nb")):
            with self.subTest(tenant=tenant, principal=principal):
                with self.assertRaises(InvalidPrincipal):
                    Principal(tenant, principal)

    def test_rejects_trailing_newline(self) -> None:
        with self.assertRaises(InvalidPrincipal):
            Principal("t1", "acct3\n")

    def test_default_directory_denies_all(self) -> None:
        self.assertIsNone(DenyAllPrincipalDirectory().resolve("anyone"))


class IdentifierValidationTests(unittest.TestCase):
    def test_session_id_requires_canonical_uuid(self) -> None:
        sid = conversation_id()
        self.assertEqual(validate_conversation_id(sid), sid)

    def test_rejects_non_ids_and_trailing_newline(self) -> None:
        bad_values = (
            "",
            "acct3",
            "acct3\n",
            "A chat title",
            "6b043328-81dc-83ee-be43-d3f4ce732e8",
            "6B043328-81DC-83EE-BE43-D3F4CE732E8B",
            str(uuid.uuid4()).replace("-", ""),
        )
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(UnconfirmedSession):
                    validate_conversation_id(value)

    def test_work_item_id_rejects_trailing_newline(self) -> None:
        value = "wi_" + str(uuid.uuid4())
        with self.assertRaises(InvalidIdentifier):
            validate_work_item_id(value + "\n")

    def test_link_id_rejects_trailing_newline(self) -> None:
        value = "sl_" + str(uuid.uuid4())
        with self.assertRaises(InvalidIdentifier):
            validate_link_id(value + "\n")


class OwnershipAndAtomicityTests(Base):
    def test_creator_is_owner(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.assertEqual(
            self.store.get_work_item(self.alice, item.work_item_id), item
        )

    def test_owner_record_and_work_item_created_together(self) -> None:
        item = self.store.create_work_item(self.alice)
        conn = sqlite3.connect(str(self.db))
        try:
            work_item_count = conn.execute(
                "SELECT count(*) FROM work_items WHERE work_item_id = ?",
                (item.work_item_id,),
            ).fetchone()[0]
            owner_count = conn.execute(
                "SELECT count(*) FROM work_item_owners WHERE work_item_id = ?",
                (item.work_item_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(work_item_count, 1)
        self.assertEqual(owner_count, 1)

    def test_audit_failure_rolls_back_work_item_and_owner(self) -> None:
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(
                "CREATE TRIGGER fail_authority_audit "
                "BEFORE INSERT ON authority_audit_events "
                "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_work_item(self.alice)
        conn = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM work_items").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM work_item_owners").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM authority_audit_events"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_audit_orphans_reports_but_does_not_delete(self) -> None:
        raw = self.store._connect()
        orphan_id = "wi_" + str(uuid.uuid4())
        now = "2026-01-01T00:00:00.000000Z"
        try:
            raw.execute(
                "INSERT INTO work_items VALUES (?, 'active', ?, ?)",
                (orphan_id, now, now),
            )
        finally:
            raw.close()
        report = self.store.audit_orphans()
        self.assertIn(orphan_id, report.work_item_ids)
        self.assertGreaterEqual(report.count, 1)
        self.assertIsNotNone(self.store._store.get_work_item(orphan_id))

    def test_owner_row_cannot_be_deleted_or_changed(self) -> None:
        item = self.store.create_work_item(self.alice)
        conn = self.store._connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE work_item_owners SET owner_principal_id='bob' "
                    "WHERE work_item_id=?",
                    (item.work_item_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM work_item_owners WHERE work_item_id=?",
                    (item.work_item_id,),
                )
        finally:
            conn.close()


class AuthorizationTests(Base):
    def test_same_tenant_stranger_cannot_read(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(NotAuthorized):
            self.store.get_work_item(self.bob, item.work_item_id)

    def test_other_tenant_cannot_read(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(NotAuthorized):
            self.store.get_work_item(self.mallory, item.work_item_id)

    def test_missing_cross_tenant_and_no_grant_are_identical(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(NotAuthorized) as no_grant:
            self.store.get_work_item(self.bob, item.work_item_id)
        with self.assertRaises(NotAuthorized) as cross_tenant:
            self.store.get_work_item(self.mallory, item.work_item_id)
        with self.assertRaises(NotAuthorized) as missing:
            self.store.get_work_item(
                self.bob, "wi_" + str(uuid.uuid4())
            )
        exceptions = [
            no_grant.exception,
            cross_tenant.exception,
            missing.exception,
        ]
        self.assertEqual({type(exc) for exc in exceptions}, {NotAuthorized})
        self.assertEqual(
            {str(exc) for exc in exceptions},
            {"resource is not available"},
        )
        self.assertTrue(all(exc.details == {} for exc in exceptions))

    def test_read_grant_allows_read_but_not_write(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        self.assertEqual(self.store.get_work_item(self.bob, item.work_item_id), item)
        with self.assertRaises(NotAuthorized):
            self.store.set_status(self.bob, item.work_item_id, "paused")

    def test_write_grant_allows_status_update(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "write")
        changed = self.store.set_status(self.bob, item.work_item_id, "paused")
        self.assertEqual(changed.status, "paused")

    def test_expected_updated_at_is_compare_and_swap(self) -> None:
        item = self.store.create_work_item(self.alice)
        changed = self.store.set_status(
            self.alice,
            item.work_item_id,
            "paused",
            expected_updated_at=item.updated_at,
        )
        with self.assertRaises(AuthorityError):
            self.store.set_status(
                self.alice,
                item.work_item_id,
                "active",
                expected_updated_at=item.updated_at,
            )
        self.assertEqual(
            self.store.get_work_item(self.alice, item.work_item_id), changed
        )

    def test_owner_can_make_terminal_transition_only_once(self) -> None:
        item = self.store.create_work_item(self.alice)
        changed = self.store.set_status(
            self.alice, item.work_item_id, "completed"
        )
        self.assertEqual(changed.status, "completed")
        with self.assertRaises(InvalidTransition):
            self.store.set_status(
                self.alice, item.work_item_id, "active"
            )


class GrantTests(Base):
    def test_unknown_target_is_rejected_by_default_directory(self) -> None:
        store = SecureWorkItemStore(self.db)
        store.initialize()
        item = store.create_work_item(self.alice)
        with self.assertRaises(InvalidPrincipal):
            store.grant(self.alice, item.work_item_id, "bob", "read")

    def test_target_must_exist_be_enabled_and_be_same_tenant(self) -> None:
        item = self.store.create_work_item(self.alice)
        for target in ("unknown", "disabled", "mallory"):
            with self.subTest(target=target):
                with self.assertRaises(InvalidPrincipal):
                    self.store.grant(
                        self.alice, item.work_item_id, target, "read"
                    )

    def test_same_tenant_unregistered_target_is_rejected(self) -> None:
        item = self.store.create_work_item(self.alice)
        # A same-tenant identifier is not an identity assertion. The directory
        # must resolve it to an enabled principal before a grant can be stored.
        with self.assertRaises(InvalidPrincipal):
            self.store.grant(
                self.alice,
                item.work_item_id,
                "unregistered-user",
                "read",
            )

    def test_cross_tenant_target_is_rejected_before_grant_is_written(self) -> None:
        item = self.store.create_work_item(self.alice)
        # Cross-tenant grants are invalid at grant time, not merely ineffective
        # at read time. Do not persist a grant row that can never authorize.
        with self.assertRaises(InvalidPrincipal):
            self.store.grant(
                self.alice, item.work_item_id, "mallory", "read"
            )
        conn = self.store._connect()
        try:
            count = conn.execute(
                "SELECT count(*) FROM work_item_grants "
                "WHERE work_item_id=? AND principal_id=?",
                (item.work_item_id, "mallory"),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_directory_error_fails_closed(self) -> None:
        class BrokenDirectory:
            def resolve(self, principal_id: str):
                raise RuntimeError("directory offline")

        store = SecureWorkItemStore(
            self.db, principal_directory=BrokenDirectory()
        )
        item = store.create_work_item(self.alice)
        with self.assertRaises(PrincipalDirectoryUnavailable):
            store.grant(self.alice, item.work_item_id, "bob", "read")

    def test_write_grantee_cannot_grant_or_revoke(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "write")
        with self.assertRaises(NotAuthorized):
            self.store.grant(self.bob, item.work_item_id, "carol", "read")
        with self.assertRaises(NotAuthorized):
            self.store.revoke(self.bob, item.work_item_id, "bob", "write")

    def test_owner_can_grant_and_revoke(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        self.assertEqual(
            self.store.get_work_item(self.bob, item.work_item_id), item
        )
        self.store.revoke(self.alice, item.work_item_id, "bob", "read")
        with self.assertRaises(NotAuthorized):
            self.store.get_work_item(self.bob, item.work_item_id)

    def test_grant_does_not_cross_tenants(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(InvalidPrincipal):
            self.store.grant(
                self.alice, item.work_item_id, "mallory", "read"
            )


class LandingEvidenceTests(Base):
    def test_caller_supplied_evidence_is_rejected(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        fake = {
            "conversation_id": sid,
            "match": "confirmed",
            "quarantine_evidence_complete": True,
        }
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(self.alice, sid, item.work_item_id, fake)

    def test_link_requires_server_recorded_landing(self) -> None:
        item = self.store.create_work_item(self.alice)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.alice, conversation_id(), item.work_item_id
            )

    def test_confirmed_complete_landing_can_be_consumed_once(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.record_landing(sid)
        link = self.store.link_session(self.alice, sid, item.work_item_id)
        self.assertEqual(link.session_id, sid)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(self.alice, sid, item.work_item_id)

    def test_mismatch_ambiguous_and_incomplete_landing_fail_closed(self) -> None:
        item = self.store.create_work_item(self.alice)
        for match, complete in (
            ("mismatch", True),
            ("ambiguous", True),
            ("confirmed", False),
        ):
            sid = conversation_id()
            self.record_landing(sid, match, complete)
            with self.subTest(match=match, complete=complete):
                with self.assertRaises(QuarantineNotCleared):
                    self.store.link_session(
                        self.alice, sid, item.work_item_id
                    )

    def test_session_link_requires_write_authority(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        sid = conversation_id()
        self.record_landing(sid)
        with self.assertRaises(NotAuthorized):
            self.store.link_session(self.bob, sid, item.work_item_id)

    def test_duplicate_active_link_is_rejected(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.record_landing(sid)
        self.store.link_session(self.alice, sid, item.work_item_id)
        other = self.store.create_work_item(self.alice)
        self.record_landing(sid)
        with self.assertRaises(SessionAlreadyLinked):
            self.store.link_session(self.alice, sid, other.work_item_id)

    def test_terminal_work_item_cannot_receive_link(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.set_status(self.alice, item.work_item_id, "completed")
        sid = conversation_id()
        self.record_landing(sid)
        with self.assertRaises(InvalidTransition):
            self.store.link_session(self.alice, sid, item.work_item_id)


class UnlinkAndVisibilityTests(Base):
    def test_unlink_absent_and_unauthorized_are_indistinguishable(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.record_landing(sid)
        self.store.link_session(self.alice, sid, item.work_item_id)
        unauthorized_result = self.store.unlink_session(self.bob, sid)
        absent_result = self.store.unlink_session(self.bob, conversation_id())
        self.assertIsNone(unauthorized_result)
        self.assertIsNone(absent_result)
        self.assertIsNotNone(self.store.get_active_link(self.alice, sid))

    def test_owner_can_unlink_and_link_disappears(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.record_landing(sid)
        link = self.store.link_session(self.alice, sid, item.work_item_id)
        removed = self.store.unlink_session(
            self.alice, sid, expected_link_id=link.link_id
        )
        self.assertIsNotNone(removed)
        self.assertIsNotNone(removed.unlinked_at)
        self.assertIsNone(self.store.get_active_link(self.alice, sid))

    def test_stale_expected_link_id_does_not_unlink(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.record_landing(sid)
        link = self.store.link_session(self.alice, sid, item.work_item_id)
        result = self.store.unlink_session(
            self.alice,
            sid,
            expected_link_id="sl_" + str(uuid.uuid4()),
        )
        self.assertIsNone(result)
        self.assertIsNotNone(self.store.get_active_link(self.alice, sid))
        self.assertTrue(link.link_id.startswith("sl_"))

    def test_active_link_is_hidden_from_stranger(self) -> None:
        item = self.store.create_work_item(self.alice)
        sid = conversation_id()
        self.record_landing(sid)
        self.store.link_session(self.alice, sid, item.work_item_id)
        self.assertIsNotNone(self.store.get_active_link(self.alice, sid))
        self.assertIsNone(self.store.get_active_link(self.bob, sid))


class PaginationTests(Base):
    def test_work_item_keyset_pages_without_duplicates(self) -> None:
        items = [
            self.store.create_work_item(self.alice) for _ in range(5)
        ]
        first = self.store.list_work_items(self.alice, limit=2)
        self.assertEqual(len(first), 2)
        self.assertIsNotNone(first.next_cursor)
        second = self.store.list_work_items(
            self.alice, limit=2, cursor=first.next_cursor
        )
        third = self.store.list_work_items(
            self.alice, limit=2, cursor=second.next_cursor
        )
        ids = [
            item.work_item_id
            for page in (first, second, third)
            for item in page
        ]
        self.assertEqual(len(ids), 5)
        self.assertEqual(
            set(ids), {item.work_item_id for item in items}
        )
        self.assertIsNone(third.next_cursor)

    def test_owner_self_grants_do_not_duplicate_rows(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "alice", "read")
        self.store.grant(self.alice, item.work_item_id, "alice", "write")
        ids = [
            value.work_item_id
            for value in self.store.list_work_items(self.alice, limit=50)
        ]
        self.assertEqual(ids.count(item.work_item_id), 1)

    def test_session_keyset_pages(self) -> None:
        item = self.store.create_work_item(self.alice)
        session_ids = [conversation_id() for _ in range(5)]
        for sid in session_ids:
            self.record_landing(sid)
            self.store.link_session(self.alice, sid, item.work_item_id)
        first = self.store.list_sessions(
            self.alice, item.work_item_id, limit=2
        )
        self.assertEqual(len(first), 2)
        self.assertIsNotNone(first.next_cursor)
        second = self.store.list_sessions(
            self.alice,
            item.work_item_id,
            limit=2,
            cursor=first.next_cursor,
        )
        third = self.store.list_sessions(
            self.alice,
            item.work_item_id,
            limit=2,
            cursor=second.next_cursor,
        )
        ids = [
            link.session_id
            for page in (first, second, third)
            for link in page
        ]
        self.assertEqual(len(ids), 5)
        self.assertEqual(set(ids), set(session_ids))
        self.assertIsNone(third.next_cursor)

    def test_page_size_and_cursor_bounds(self) -> None:
        for bad in (0, -1, 51):
            with self.subTest(limit=bad):
                with self.assertRaises(AuthorityError):
                    self.store.list_work_items(self.alice, limit=bad)
        with self.assertRaises(AuthorityError):
            self.store.list_work_items(
                self.alice, cursor="x" * 2049
            )
        with self.assertRaises(AuthorityError):
            self.store.list_sessions(
                self.alice,
                "wi_" + str(uuid.uuid4()),
                cursor="not-a-cursor",
            )

    def test_status_filter_is_validated(self) -> None:
        with self.assertRaises(AuthorityError):
            self.store.list_work_items(
                self.alice, status="unknown"
            )


class AuditTests(Base):
    def test_all_successful_mutations_are_audited(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.set_status(self.alice, item.work_item_id, "paused")
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        self.store.revoke(self.alice, item.work_item_id, "bob", "read")
        sid = conversation_id()
        self.record_landing(sid)
        link = self.store.link_session(self.alice, sid, item.work_item_id)
        self.store.unlink_session(
            self.alice, sid, expected_link_id=link.link_id
        )
        actions = [
            event["action"]
            for event in self.store.list_audit_events(item.work_item_id)
        ]
        self.assertEqual(
            actions,
            [
                "work_item.create",
                "work_item.update",
                "work_item.grant",
                "work_item.revoke",
                "session.link",
                "session.unlink",
            ],
        )
        events = self.store.list_audit_events(item.work_item_id)
        self.assertTrue(
            all(event["outcome"] == "success" for event in events)
        )
        self.assertTrue(
            all(event["principal_id"] == "alice" for event in events)
        )
        self.assertTrue(all(event["created_at"] for event in events))

    def test_audit_failure_rolls_back_status_mutation(self) -> None:
        item = self.store.create_work_item(self.alice)
        conn = self.store._connect()
        try:
            conn.execute(
                "CREATE TRIGGER fail_update_audit "
                "BEFORE INSERT ON authority_audit_events "
                "WHEN NEW.action = 'work_item.update' "
                "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
            )
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.set_status(
                self.alice, item.work_item_id, "paused"
            )
        self.assertEqual(
            self.store.get_work_item(self.alice, item.work_item_id).status,
            "active",
        )

    def test_audit_rows_are_append_only(self) -> None:
        item = self.store.create_work_item(self.alice)
        conn = self.store._connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE authority_audit_events SET outcome='tampered' "
                    "WHERE work_item_id=?",
                    (item.work_item_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM authority_audit_events WHERE work_item_id=?",
                    (item.work_item_id,),
                )
        finally:
            conn.close()


class PersistenceTests(Base):
    def test_authority_and_grants_survive_reopen(self) -> None:
        item = self.store.create_work_item(self.alice)
        self.store.grant(self.alice, item.work_item_id, "bob", "read")
        reopened = SecureWorkItemStore(
            self.db, principal_directory=self.directory
        )
        reopened.initialize()
        self.assertEqual(
            reopened.get_work_item(self.alice, item.work_item_id), item
        )
        self.assertEqual(
            reopened.get_work_item(self.bob, item.work_item_id), item
        )


if __name__ == "__main__":
    unittest.main()
