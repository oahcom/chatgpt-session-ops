"""Tests for SO WorkItem persistence (T154-19 assertions 1-12)."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.session_ops.work_items import (  # noqa: E402
    DEFAULT_DATABASE_PATH,
    InvalidSessionId,
    InvalidTransition,
    InvalidWorkItemId,
    SessionAlreadyLinked,
    WorkItemNotFound,
    WorkItemStore,
    new_work_item_id,
    validate_session_id,
    validate_work_item_id,
)


def raised_by(callable_, *args, **kwargs):
    """Return the exception raised by ``callable_``, or ``None``.

    Guard tests capture the exception here instead of using ``assertRaises``
    so that a removed guard surfaces as an AssertionError on the type/message
    assertion, not as an error raised from deeper code.
    """
    try:
        callable_(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - asserted by the caller
        return exc
    return None


class WorkItemTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="so-work-items-")
        self.db_path = Path(self.tmpdir) / "work-items.sqlite3"
        self.store = WorkItemStore(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class WorkItemIdentityTestCase(WorkItemTestCase):
    def test_work_item_id_is_canonical_uuid4(self) -> None:
        # Assertion 1
        item = self.store.create_work_item()
        self.assertTrue(item.work_item_id.startswith("wi_"))
        self.assertEqual(len(item.work_item_id), 39)
        suffix = item.work_item_id[3:]
        parsed = uuid.UUID(suffix)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), suffix)
        self.assertEqual(validate_work_item_id(item.work_item_id), item.work_item_id)
        self.assertEqual(item.status, "active")
        self.assertEqual(item.created_at, item.updated_at)
        self.assertNotEqual(new_work_item_id(), new_work_item_id())

    def test_invalid_uuid_versions_are_rejected(self) -> None:
        # Assertion 4
        rejected = [
            "wi_" + str(uuid.uuid1()),
            "wi_" + str(uuid.uuid3(uuid.NAMESPACE_DNS, "x")),
            "wi_" + str(uuid.uuid5(uuid.NAMESPACE_DNS, "x")),
            "wi_" + str(uuid.uuid4()).upper(),
            "wi_" + str(uuid.uuid4()).replace("-", ""),
            "wi_not-a-uuid",
            "wi_",
            "xx_" + str(uuid.uuid4()),
            "",
        ]
        for bad in rejected:
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidWorkItemId):
                    validate_work_item_id(bad)
                with self.assertRaises(InvalidWorkItemId):
                    self.store.link_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", bad)
                with self.assertRaises(InvalidWorkItemId):
                    self.store.set_status(bad, "paused")

    def test_non_string_work_item_id_is_rejected(self) -> None:
        # Issue #153: pin the isinstance guard by type AND exact message.
        # Without it, None/123 reach .startswith() and raise AttributeError.
        bad_values = (
            None,
            123,
            b"wi_" + uuid.uuid4().bytes,
            ["wi_" + str(uuid.uuid4())],
        )
        for bad in bad_values:
            with self.subTest(bad=bad):
                exc = raised_by(validate_work_item_id, bad)
                self.assertIs(type(exc), InvalidWorkItemId)
                self.assertEqual(str(exc), "work_item_id must be a string")

    def test_session_id_must_be_canonical_uuid(self) -> None:
        # Issue #109: the loose "session-*" literal must no longer be accepted.
        rejected = [
            "session-a",
            "",
            "   ",
            "Quarterly planning call",
            str(uuid.uuid4()).upper(),
            str(uuid.uuid4()).replace("-", ""),
            None,
        ]
        item = self.store.create_work_item()
        for bad in rejected:
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSessionId):
                    validate_session_id(bad)
                with self.assertRaises(InvalidSessionId):
                    self.store.link_session(bad, item.work_item_id)


class WorkItemPersistenceTestCase(WorkItemTestCase):
    def test_work_item_survives_reopen(self) -> None:
        # Assertion 2
        created = self.store.create_work_item()
        reopened = WorkItemStore(self.db_path)
        reopened.initialize()
        loaded = reopened.get_work_item(created.work_item_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded, created)
        self.assertIsNone(reopened.get_work_item("wi_" + str(uuid.uuid4())))

    def test_work_item_id_cannot_be_updated(self) -> None:
        # Assertion 3
        created = self.store.create_work_item()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.DatabaseError) as ctx:
                conn.execute(
                    "UPDATE work_items SET work_item_id = ?"
                    " WHERE work_item_id = ?",
                    ("wi_" + str(uuid.uuid4()), created.work_item_id),
                )
            self.assertIn("immutable", str(ctx.exception).lower())
        finally:
            conn.close()
        self.assertEqual(
            self.store.get_work_item(created.work_item_id), created
        )


class SessionLinkTestCase(WorkItemTestCase):
    def test_unknown_work_item_cannot_receive_session_link(self) -> None:
        # Assertion 5
        unknown = "wi_" + str(uuid.uuid4())
        with self.assertRaises(WorkItemNotFound):
            self.store.link_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", unknown)
        self.assertIsNone(self.store.get_active_link("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70"))
        self.assertEqual(self.store.list_sessions(unknown), [])

    def test_multiple_sessions_link_one_work_item(self) -> None:
        # Assertion 6
        item = self.store.create_work_item()
        first = self.store.link_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", item.work_item_id)
        second = self.store.link_session("9a8b7c6d-1e2f-4a3b-8c4d-5e6f7a8b9c0d", item.work_item_id)
        self.assertNotEqual(first.link_id, second.link_id)
        self.assertEqual(first.work_item_id, item.work_item_id)
        self.assertEqual(second.work_item_id, item.work_item_id)
        self.assertIsNone(first.unlinked_at)
        links = self.store.list_sessions(item.work_item_id)
        self.assertEqual(
            sorted(link.session_id for link in links),
            ["6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", "9a8b7c6d-1e2f-4a3b-8c4d-5e6f7a8b9c0d"],
        )

    def test_session_cannot_have_two_active_links(self) -> None:
        # Assertion 7
        first_item = self.store.create_work_item()
        second_item = self.store.create_work_item()
        session_id = "6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70"
        self.store.link_session(session_id, first_item.work_item_id)
        # Issue #153: pin the explicit pre-check by type AND exact message.
        # Without it the INSERT hits the partial UNIQUE index and a raw
        # sqlite3.IntegrityError escapes instead of SessionAlreadyLinked.
        exc = raised_by(
            self.store.link_session, session_id, second_item.work_item_id
        )
        self.assertIs(type(exc), SessionAlreadyLinked)
        self.assertEqual(
            str(exc),
            "session %s already has an active work item link" % session_id,
        )
        active = self.store.get_active_link(session_id)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.work_item_id, first_item.work_item_id)
        self.assertEqual(self.store.list_sessions(second_item.work_item_id), [])
        # Linking a Session elsewhere must not auto-unlink it.
        self.assertIsNone(
            self.store.get_active_link("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70").unlinked_at
        )

    def test_unlink_preserves_history_row(self) -> None:
        # Assertion 8
        item = self.store.create_work_item()
        linked = self.store.link_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", item.work_item_id)
        unlinked = self.store.unlink_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70")
        self.assertIsNotNone(unlinked)
        assert unlinked is not None
        self.assertEqual(unlinked.link_id, linked.link_id)
        self.assertIsNotNone(unlinked.unlinked_at)
        self.assertGreaterEqual(unlinked.unlinked_at, linked.linked_at)
        self.assertIsNone(self.store.get_active_link("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70"))
        self.assertEqual(self.store.list_sessions(item.work_item_id), [])
        history = self.store.list_sessions(
            item.work_item_id, include_unlinked=True
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].link_id, linked.link_id)
        self.assertIsNone(self.store.unlink_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70"))

    def test_relink_creates_new_link_id(self) -> None:
        # Assertion 9
        item = self.store.create_work_item()
        first = self.store.link_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", item.work_item_id)
        self.store.unlink_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70")
        second = self.store.link_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", item.work_item_id)
        self.assertNotEqual(first.link_id, second.link_id)
        history = self.store.list_sessions(
            item.work_item_id, include_unlinked=True
        )
        self.assertEqual(len(history), 2)
        self.assertEqual(
            {link.link_id for link in history},
            {first.link_id, second.link_id},
        )

    def test_terminal_work_item_rejects_new_session_link(self) -> None:
        # Assertion 10
        terminal_sessions = {
            "completed": "c1c1c1c1-1111-4111-8111-111111111111",
            "cancelled": "c2c2c2c2-2222-4222-8222-222222222222",
        }
        for terminal, session_id in terminal_sessions.items():
            with self.subTest(terminal=terminal):
                item = self.store.create_work_item()
                self.store.set_status(item.work_item_id, terminal)
                with self.assertRaises(InvalidTransition):
                    self.store.link_session(session_id, item.work_item_id)
                self.assertIsNone(self.store.get_active_link(session_id))


class WorkItemLifecycleTestCase(WorkItemTestCase):
    def test_invalid_lifecycle_transitions_are_rejected(self) -> None:
        # Assertion 11
        allowed = {
            ("active", "paused"),
            ("active", "completed"),
            ("active", "cancelled"),
            ("paused", "active"),
            ("paused", "completed"),
            ("paused", "cancelled"),
        }
        for source in ("active", "paused", "completed", "cancelled"):
            for target in ("active", "paused", "completed", "cancelled"):
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    item = self.store.create_work_item()
                    if source != "active":
                        self.store.set_status(item.work_item_id, source)
                    if (source, target) in allowed:
                        updated = self.store.set_status(
                            item.work_item_id, target
                        )
                        self.assertEqual(updated.status, target)
                    else:
                        # Issue #153: every rejected pair here has a terminal
                        # source, so assert the terminal message exactly. A
                        # type-only assertion cannot tell the terminal guard
                        # apart from the generic transition guard.
                        with self.assertRaisesRegex(
                            InvalidTransition,
                            r"^terminal work item %s cannot transition to %s$"
                            % (item.work_item_id, target),
                        ):
                            self.store.set_status(item.work_item_id, target)
                        stored = self.store.get_work_item(item.work_item_id)
                        assert stored is not None
                        self.assertEqual(stored.status, source)

    def test_terminal_status_cannot_transition_to_any_other_status(self) -> None:
        # Issue #153: pin the terminal guard for every terminal source/target.
        for terminal in ("completed", "cancelled"):
            for target in ("active", "paused", "completed", "cancelled"):
                if target == terminal:
                    continue
                with self.subTest(terminal=terminal, target=target):
                    item = self.store.create_work_item()
                    self.store.set_status(item.work_item_id, terminal)
                    exc = raised_by(
                        self.store.set_status, item.work_item_id, target
                    )
                    self.assertIs(type(exc), InvalidTransition)
                    self.assertEqual(
                        str(exc),
                        "terminal work item %s cannot transition to %s"
                        % (item.work_item_id, target),
                    )
                    stored = self.store.get_work_item(item.work_item_id)
                    assert stored is not None
                    self.assertEqual(stored.status, terminal)

    def test_unknown_status_is_rejected(self) -> None:
        # Issue #153: pin the STATUSES guard by type AND exact message. A
        # type-only assertion cannot distinguish this from the SQLite CHECK
        # constraint violation that fires once the guard is gone.
        item = self.store.create_work_item()
        exc = raised_by(self.store.set_status, item.work_item_id, "frozen")
        self.assertIs(type(exc), InvalidTransition)
        self.assertEqual(str(exc), "unknown status: 'frozen'")
        stored = self.store.get_work_item(item.work_item_id)
        assert stored is not None
        self.assertEqual(stored.status, "active")
        self.assertEqual(stored.updated_at, item.updated_at)

    def test_unknown_work_item_status_change_rejected(self) -> None:
        with self.assertRaises(WorkItemNotFound):
            self.store.set_status("wi_" + str(uuid.uuid4()), "paused")

    def test_idempotent_status_repeat_keeps_updated_at(self) -> None:
        item = self.store.create_work_item()
        same = self.store.set_status(item.work_item_id, "active")
        self.assertEqual(same.updated_at, item.updated_at)
        self.assertEqual(same.created_at, item.created_at)

    def test_status_change_advances_updated_at(self) -> None:
        item = self.store.create_work_item()
        paused = self.store.set_status(item.work_item_id, "paused")
        self.assertEqual(paused.status, "paused")
        self.assertGreater(paused.updated_at, item.updated_at)
        self.assertEqual(paused.created_at, item.created_at)


class ExistingPersistenceTestCase(WorkItemTestCase):
    def test_default_database_path_is_the_documented_location(self) -> None:
        self.assertEqual(
            DEFAULT_DATABASE_PATH,
            Path.home()
            / ".workbuddy"
            / "session-ops"
            / "work-items.sqlite3",
        )

    def test_existing_json_persistence_is_unchanged(self) -> None:
        # Assertion 12: WorkItem work must not touch SO's JSON state files.
        scripts = ROOT / "scripts"
        watched = [scripts / "chatgpt_send.py"]
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in watched
            if path.exists()
        }
        before_listing = sorted(p.name for p in scripts.iterdir())
        self.assertTrue(before, "chatgpt_send.py must exist")

        item = self.store.create_work_item()
        self.store.link_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", item.work_item_id)
        self.store.set_status(item.work_item_id, "paused")
        self.store.unlink_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70")

        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in watched
            if path.exists()
        }
        self.assertEqual(before, after)
        self.assertEqual(
            before_listing, sorted(p.name for p in scripts.iterdir())
        )
        # All WorkItem state lives in the temporary database only.
        self.assertTrue(
            all(
                name.startswith("work-items.sqlite3")
                for name in os.listdir(self.tmpdir)
            ),
            "unexpected files in temp dir: %s" % os.listdir(self.tmpdir),
        )


if __name__ == "__main__":
    unittest.main()
