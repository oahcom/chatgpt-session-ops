"""Regression tests for the rollback guards in ``session_ops.authority``.

Issue #115: a mutation sweep found that 42 of the 71 ``raise`` statements in
``src/session_ops/authority.py`` survive deletion.  The dangerous population is
the bare ``raise`` statements inside ``except BaseException:`` rollback blocks.
Replacing one with ``pass`` swallows the database exception and lets the caller
believe the operation succeeded (the issue demonstrates this on
``unlink_session``, which then returns ``None`` -- documented as "absent and
unauthorized links both return None" -- while ``unlinked_at`` is still NULL).

Every test here therefore asserts two things:

1. the database exception reaches the caller, and
2. the row is unchanged, read back with a raw ``sqlite3`` connection rather
   than through the API under test.

AST enumeration of the bare raises (``ast.Raise`` with ``node.exc is None``),
with the enclosing class/function:

    660   SecureWorkItemStore.initialize            (inner migration txn)
    694   SecureWorkItemStore.initialize            (outer guard)
    853   SecureWorkItemStore.create_work_item
    1012  SecureWorkItemStore.set_status
    1049  SecureWorkItemStore.grant
    1082  SecureWorkItemStore.revoke
    1165  SecureWorkItemStore._record_trusted_landing
    1255  SecureWorkItemStore.link_session.reject_after_rollback
    1391  SecureWorkItemStore.link_session            (UNREACHABLE, see below)
    1397  SecureWorkItemStore.link_session            (duplicate link)
    1416  SecureWorkItemStore.link_session            (non-duplicate IntegrityError)
    1421  SecureWorkItemStore.link_session            (generic guard)
    1486  SecureWorkItemStore.unlink_session
    1537  SecureWorkItemStore.list_sessions
    1585  SecureWorkItemStore.get_active_link

Line 1391 (``except SessionAlreadyLinked: ... if selected_event_id is None:
raise``) is dead code and has no test.  ``SessionAlreadyLinked`` is raised in
exactly one place (the deterministic active-link conflict check) and that check
runs *after* ``selected_event_id = landing["event_id"]`` has been assigned, so
the ``None`` branch can never be taken.  Deleting that guard changes no
behaviour; a test for it would pass for the wrong reason.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from src.session_ops.authority import (
    Principal,
    PrincipalRecord,
    SecureWorkItemStore,
    StaticPrincipalDirectory,
)

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"

# Distinct SQL fragments used to inject a database failure at a chosen
# statement.  Each fragment occurs exactly once in the code path under test.
MARKER_MIGRATION_INDEX = "ix_authority_landing_pending_identity"
MARKER_LANDING_INSERT = "INSERT INTO authority_landing_events"
MARKER_REJECT_UPDATE = "SET rejected_at = ?, rejection_reason = ?"
MARKER_COMMIT = "COMMIT"


class _FailingConnection:
    """Delegating proxy that fails the first matching statement.

    ``sqlite3.Connection`` instances are C objects and do not accept attribute
    assignment, so the failure has to be injected by wrapping the connection
    returned from ``SecureWorkItemStore._connect``.
    """

    def __init__(self, conn: sqlite3.Connection, marker: str) -> None:
        self._conn = conn
        self._marker = marker

    def execute(self, sql: str, *args, **kwargs):
        if self._marker in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class RollbackPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="so-rollback-")
        self.db_path = Path(self.tempdir.name) / "work-items.sqlite3"
        self.store = SecureWorkItemStore(
            self.db_path,
            principal_directory=StaticPrincipalDirectory(
                [PrincipalRecord(principal_id="grantee", tenant_id="tenant-a")]
            ),
        )
        self.store.initialize()
        self.principal = Principal(tenant_id="tenant-a", principal_id="owner-a")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    # -- helpers ---------------------------------------------------------

    def _raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _raw_scalar(self, sql: str, params=()):
        conn = self._raw()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def _raw_row(self, sql: str, params=()):
        conn = self._raw()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _failing_connection(self, marker: str):
        real_connect = self.store._connect

        def connect():
            return _FailingConnection(real_connect(), marker)

        return mock.patch.object(self.store, "_connect", connect)

    def _link(self, item, session_id: str):
        attempt_id = "att_" + str(uuid.uuid4())
        self.store._record_trusted_landing(
            session_id,
            match="confirmed",
            evidence_complete=True,
            work_item_id=item.work_item_id,
            attempt_id=attempt_id,
        )
        link = self.store.link_session(
            self.principal, session_id, item.work_item_id, attempt_id
        )
        return link, attempt_id

    def _record_landing(self, item, session_id: str) -> str:
        attempt_id = "att_" + str(uuid.uuid4())
        self.store._record_trusted_landing(
            session_id,
            match="confirmed",
            evidence_complete=True,
            work_item_id=item.work_item_id,
            attempt_id=attempt_id,
        )
        return attempt_id

    # -- initialize (660 inner migration guard, 694 outer guard) ---------

    def _legacy_landing_schema(self) -> None:
        """Replace the landing table with the pre-migration (legacy) shape."""
        conn = self._raw()
        try:
            conn.execute("DROP TABLE authority_landing_events")
            conn.execute(
                "CREATE TABLE authority_landing_events ("
                "event_id TEXT PRIMARY KEY NOT NULL, "
                "session_id TEXT NOT NULL, "
                "match TEXT NOT NULL "
                "CHECK (match IN ('confirmed', 'mismatch', 'ambiguous')), "
                "evidence_complete INTEGER NOT NULL "
                "CHECK (evidence_complete IN (0, 1)), "
                "created_at TEXT NOT NULL, "
                "consumed_at TEXT)"
            )
            conn.execute(
                "INSERT INTO authority_landing_events VALUES "
                "('le_legacy', ?, 'confirmed', 1, '2026-01-01T00:00:00.000000Z', NULL)",
                (SESSION_A,),
            )
            conn.commit()
        finally:
            conn.close()

    def _landing_columns(self) -> set:
        conn = self._raw()
        try:
            return {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(authority_landing_events)"
                ).fetchall()
            }
        finally:
            conn.close()

    def test_initialize_migration_failure_propagates_and_rolls_back_schema(
        self,
    ) -> None:
        """Line 660: a failure inside the landing migration transaction.

        A legacy landing table is installed first, so the four
        ``ALTER TABLE ... ADD COLUMN`` statements really execute.  The failure
        is injected at the CREATE INDEX that runs after them: a working
        rollback leaves the legacy schema and the legacy row untouched.
        """
        self._legacy_landing_schema()
        self.assertNotIn("work_item_id", self._landing_columns())

        with self._failing_connection(MARKER_MIGRATION_INDEX):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.initialize()

        columns = self._landing_columns()
        self.assertNotIn("work_item_id", columns)
        self.assertNotIn("attempt_id", columns)
        self.assertNotIn("rejected_at", columns)
        self.assertNotIn("rejection_reason", columns)
        self.assertEqual(
            self._raw_scalar(
                "SELECT match FROM authority_landing_events WHERE event_id = 'le_legacy'"
            ),
            "confirmed",
        )

        # The database is still usable: the migration can be retried.
        self.store.initialize()
        columns = self._landing_columns()
        self.assertIn("work_item_id", columns)
        self.assertEqual(
            self._raw_scalar(
                "SELECT match FROM authority_landing_events WHERE event_id = 'le_legacy'"
            ),
            "ambiguous",
        )

    def test_initialize_reconciliation_failure_propagates_and_keeps_link(
        self,
    ) -> None:
        """Line 694: audit failure during terminal-link reconciliation.

        The work item is moved to a terminal status with raw SQL so that an
        active link is left behind for ``initialize`` to reconcile.
        """
        item = self.store.create_work_item(self.principal)
        self._link(item, SESSION_A)

        conn = self._raw()
        try:
            conn.execute(
                "UPDATE work_items SET status = 'completed' WHERE work_item_id = ?",
                (item.work_item_id,),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(
            SecureWorkItemStore,
            "_audit",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.initialize()

        row = self._raw_row(
            "SELECT unlinked_at FROM session_links WHERE session_id = ?",
            (SESSION_A,),
        )
        self.assertIsNone(row["unlinked_at"])
        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM authority_audit_events "
                "WHERE action = 'session.auto_unlink_terminal'"
            ),
            0,
        )

    # -- create_work_item (853) -----------------------------------------

    def test_create_work_item_propagates_audit_failure_and_leaves_no_rows(
        self,
    ) -> None:
        with mock.patch.object(
            SecureWorkItemStore,
            "_audit",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.create_work_item(self.principal)

        self.assertEqual(
            self._raw_scalar("SELECT COUNT(*) FROM work_items"), 0
        )
        self.assertEqual(
            self._raw_scalar("SELECT COUNT(*) FROM work_item_owners"), 0
        )

    # -- set_status (1012) ----------------------------------------------

    def test_set_status_propagates_audit_failure_and_keeps_old_status(
        self,
    ) -> None:
        item = self.store.create_work_item(self.principal)

        with mock.patch.object(
            SecureWorkItemStore,
            "_audit",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.set_status(self.principal, item.work_item_id, "paused")

        row = self._raw_row(
            "SELECT status, updated_at FROM work_items WHERE work_item_id = ?",
            (item.work_item_id,),
        )
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["updated_at"], item.updated_at)

    # -- grant (1049) ----------------------------------------------------

    def test_grant_propagates_audit_failure_and_leaves_no_grant(self) -> None:
        item = self.store.create_work_item(self.principal)

        with mock.patch.object(
            SecureWorkItemStore,
            "_audit",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.grant(self.principal, item.work_item_id, "grantee", "read")

        self.assertEqual(
            self._raw_scalar("SELECT COUNT(*) FROM work_item_grants"), 0
        )

    # -- revoke (1082) ---------------------------------------------------

    def test_revoke_propagates_audit_failure_and_keeps_grant(self) -> None:
        item = self.store.create_work_item(self.principal)
        self.store.grant(self.principal, item.work_item_id, "grantee", "read")

        with mock.patch.object(
            SecureWorkItemStore,
            "_audit",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.revoke(
                    self.principal, item.work_item_id, "grantee", "read"
                )

        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM work_item_grants "
                "WHERE work_item_id = ? AND principal_id = 'grantee' "
                "AND scope = 'read'",
                (item.work_item_id,),
            ),
            1,
        )

    # -- _record_trusted_landing (1165) ---------------------------------

    def test_record_trusted_landing_propagates_and_leaves_no_event(self) -> None:
        item = self.store.create_work_item(self.principal)

        with self._failing_connection(MARKER_LANDING_INSERT):
            with self.assertRaises(sqlite3.OperationalError):
                self._record_landing(item, SESSION_A)

        self.assertEqual(
            self._raw_scalar("SELECT COUNT(*) FROM authority_landing_events"), 0
        )

    # -- link_session: reject_after_rollback (1255) ----------------------

    def test_duplicate_link_rejection_failure_propagates(self) -> None:
        """Line 1255: persisting the REJECTED state must not fail silently.

        A duplicate active link is a deterministic rejection, but the rejection
        is written in its own transaction after the link transaction rolls
        back.  If that write fails, the caller must see the database error
        rather than a plain ``SessionAlreadyLinked``.
        """
        first = self.store.create_work_item(self.principal)
        second = self.store.create_work_item(self.principal)
        self._link(first, SESSION_A)
        attempt_id = self._record_landing(second, SESSION_A)

        with self._failing_connection(MARKER_REJECT_UPDATE):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.link_session(
                    self.principal, SESSION_A, second.work_item_id, attempt_id
                )

        row = self._raw_row(
            "SELECT rejected_at, consumed_at FROM authority_landing_events "
            "WHERE work_item_id = ?",
            (second.work_item_id,),
        )
        self.assertIsNone(row["rejected_at"])
        self.assertIsNone(row["consumed_at"])
        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM session_links WHERE work_item_id = ?",
                (second.work_item_id,),
            ),
            0,
        )

    # -- link_session: duplicate conflict re-raise (1397) ----------------

    def test_duplicate_link_conflict_still_raises_session_already_linked(
        self,
    ) -> None:
        """Line 1397: the original ``SessionAlreadyLinked`` must escape.

        With the guard removed, ``link_session`` falls through to
        ``return SessionLink(...)`` for a link row that was rolled back -- a
        success report for an operation that never happened.
        """
        first = self.store.create_work_item(self.principal)
        second = self.store.create_work_item(self.principal)
        self._link(first, SESSION_A)
        attempt_id = self._record_landing(second, SESSION_A)

        from src.session_ops.work_items import SessionAlreadyLinked

        with self.assertRaises(SessionAlreadyLinked):
            self.store.link_session(
                self.principal, SESSION_A, second.work_item_id, attempt_id
            )

        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM session_links WHERE work_item_id = ?",
                (second.work_item_id,),
            ),
            0,
        )
        row = self._raw_row(
            "SELECT rejected_at, consumed_at FROM authority_landing_events "
            "WHERE work_item_id = ?",
            (second.work_item_id,),
        )
        self.assertIsNotNone(row["rejected_at"])
        self.assertIsNone(row["consumed_at"])

    # -- link_session: non-duplicate IntegrityError (1416) --------------

    def test_link_session_preserves_unrelated_integrity_error(self) -> None:
        """Line 1416: an IntegrityError that is not a duplicate link survives.

        ``_audit`` is patched to raise ``sqlite3.IntegrityError``.  No active
        link exists for the session, so the handler must not reclassify the
        error as ``SessionAlreadyLinked``; the original exception propagates.
        """
        item = self.store.create_work_item(self.principal)
        attempt_id = self._record_landing(item, SESSION_B)

        with mock.patch.object(
            SecureWorkItemStore,
            "_audit",
            side_effect=sqlite3.IntegrityError("UNIQUE constraint failed"),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.link_session(
                    self.principal, SESSION_B, item.work_item_id, attempt_id
                )

        self.assertEqual(
            self._raw_scalar("SELECT COUNT(*) FROM session_links"), 0
        )
        row = self._raw_row(
            "SELECT consumed_at FROM authority_landing_events WHERE work_item_id = ?",
            (item.work_item_id,),
        )
        self.assertIsNone(row["consumed_at"])

    # -- link_session: generic guard (1421) -----------------------------

    def test_link_session_propagates_generic_failure_and_leaves_no_link(
        self,
    ) -> None:
        item = self.store.create_work_item(self.principal)
        attempt_id = self._record_landing(item, SESSION_B)

        with mock.patch.object(
            SecureWorkItemStore,
            "_audit",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.link_session(
                    self.principal, SESSION_B, item.work_item_id, attempt_id
                )

        self.assertEqual(
            self._raw_scalar("SELECT COUNT(*) FROM session_links"), 0
        )
        row = self._raw_row(
            "SELECT consumed_at FROM authority_landing_events WHERE work_item_id = ?",
            (item.work_item_id,),
        )
        self.assertIsNone(row["consumed_at"])

    # -- unlink_session (1486) -----------------------------------------

    def test_unlink_session_propagates_audit_failure_and_keeps_link_active(
        self,
    ) -> None:
        """The exact fail-open from issue #115.

        ``unlink_session`` documents that absent and unauthorized links both
        return ``None``.  If the guard at 1486 is replaced with ``pass``, a
        disk I/O error becomes indistinguishable from "nothing to unlink"
        while the row still has ``unlinked_at IS NULL``.
        """
        item = self.store.create_work_item(self.principal)
        link, _ = self._link(item, SESSION_A)

        with mock.patch.object(
            SecureWorkItemStore,
            "_audit",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.unlink_session(self.principal, SESSION_A)

        row = self._raw_row(
            "SELECT unlinked_at FROM session_links WHERE link_id = ?",
            (link.link_id,),
        )
        self.assertIsNone(row["unlinked_at"])
        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM session_links "
                "WHERE link_id = ? AND unlinked_at IS NULL",
                (link.link_id,),
            ),
            1,
        )

    # -- list_sessions (1537) ------------------------------------------

    def test_list_sessions_propagates_commit_failure(self) -> None:
        """Line 1537: a failed COMMIT must not yield a page of rows."""
        item = self.store.create_work_item(self.principal)
        self._link(item, SESSION_A)

        with self._failing_connection(MARKER_COMMIT):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.list_sessions(self.principal, item.work_item_id)

    # -- get_active_link (1585) ----------------------------------------

    def test_get_active_link_propagates_commit_failure(self) -> None:
        """Line 1585: a failed COMMIT must not yield the link."""
        item = self.store.create_work_item(self.principal)
        link, _ = self._link(item, SESSION_A)

        with self._failing_connection(MARKER_COMMIT):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.get_active_link(self.principal, SESSION_A)

        row = self._raw_row(
            "SELECT unlinked_at FROM session_links WHERE link_id = ?",
            (link.link_id,),
        )
        self.assertIsNone(row["unlinked_at"])


if __name__ == "__main__":
    unittest.main()
