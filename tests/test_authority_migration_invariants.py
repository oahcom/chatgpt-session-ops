"""Invariant tests for the authority migration and link-session guards (#115).

Issue #115 tracked ``raise`` statements in ``src/session_ops/authority.py``
whose deletion no test noticed.  This module closes the last of them.

Reachability, measured rather than assumed (each guard was mutated to ``pass``
by AST line span and the whole suite re-run; see the table in the commit
message):

Reachable through the public API
    L1333  ``link_session`` -- a same-session active link is a deterministic
           rejection.  Deleting the guard does NOT change the exception type
           or message, because the INSERT that follows then trips
           ``ux_session_links_one_active_work_item_per_session`` and the
           ``except sqlite3.IntegrityError`` handler reclassifies it to the
           same ``SessionAlreadyLinked``.  The observable difference is
           *ordering*: the guard rejects before the landing evidence is
           consumed.  ``test_active_link_conflict_is_rejected_before_consuming``
           fails the consume UPDATE and asserts the conflict still wins.

Reachable only after stubbing the upstream authorization check
    L1290  ``link_session`` -- ``_require`` already rejects an unknown
           ``work_item_id`` (it joins ``work_item_owners`` to ``work_items``,
           and the FK is ``ON DELETE RESTRICT``), so the second lookup can
           only return ``None`` if a future refactor weakens ``_require``.
           The guard is still pinned, with ``_require`` stubbed, because its
           fall-through is a ``TypeError`` rather than ``NotAuthorized``.

Unreachable: post-hoc invariants of a single transaction
    L564 L578 L597 L622 L633 L651
        ``initialize`` re-verifies its own landing-evidence migration inside
        the same ``BEGIN IMMEDIATE``.  Each guard restates a query that ran
        microseconds earlier on the same connection:

        L564  ``changed`` (``.rowcount`` of the downgrade UPDATE) vs
              ``legacy_pending_count`` (a COUNT with a byte-identical WHERE).
        L578  a re-COUNT of that same WHERE immediately after the UPDATE.
        L597  ``COUNT(*)`` of the table: the migration only runs ALTER TABLE
              ADD COLUMN, one UPDATE, and CREATE INDEX -- never INSERT/DELETE.
        L622  the distribution map, adjusted by ``legacy_pending_count`` --
              the same number the UPDATE moved.
        L633  ``consumed_at`` / ``linked_at`` correspondence: neither column
              is written by the migration.
        L651  ``invalid_legacy_rows`` is a strict SUPERSET of the rows the
              UPDATE just touched (it also matches pre-existing ambiguous
              rows), so it can only be ``< legacy_pending_count`` if the
              UPDATE failed to apply.

        No caller can break any of these without a concurrent writer, and
        ``BEGIN IMMEDIATE`` excludes one.  No test here claims to kill them;
        the tests instead pin the *post-migration state* they protect, which
        does catch a real regression in the migration SQL (verified by
        mutating the UPDATE's WHERE and SET clauses).

    L678  ``initialize`` terminal-link reconciliation: the loop re-reads rows
          it selected in the same transaction, so ``rowcount`` is always 1.
          Pinned by asserting exactly one unlink + one audit row per stale
          link, and that a non-terminal link is untouched.

    L1359 ``link_session`` -- requires the landing event to change between the
          SELECT and the UPDATE on the same connection inside one
          ``BEGIN IMMEDIATE``.  Structurally impossible.
    L1413 ``link_session`` -- requires ``has_active_link()`` to be true after
          rollback while the pre-consume check saw no active link, i.e. a
          concurrent commit, which the write lock excludes.
    L1391 ``link_session`` -- dead branch, documented in
          ``tests/test_authority_rollback_propagation.py``.

No ``@unittest.skip`` is added: ``.github/workflows/ci.yml`` derives its
expected skip count from the two ``os.name`` decorators only.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from src.session_ops.authority import (
    NotAuthorized,
    Principal,
    SecureWorkItemStore,
)
from src.session_ops.work_items import SessionAlreadyLinked

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
CONSUMED_AT = "2026-01-01T00:00:00.000000Z"

# Unique to the consume UPDATE in link_session (reject_after_rollback uses
# "SET rejected_at = ?, rejection_reason = ?").
MARKER_CONSUME = "SET consumed_at = ?"

LEGACY_DDL = (
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

LEGACY_ROWS = (
    ("le_pending_1", SESSION_A, "confirmed", 1, "2026-01-01T00:00:00.000001Z", None),
    ("le_pending_2", SESSION_B, "confirmed", 1, "2026-01-01T00:00:00.000002Z", None),
    ("le_consumed", SESSION_A, "confirmed", 1, "2026-01-01T00:00:00.000003Z", CONSUMED_AT),
    ("le_ambiguous", SESSION_B, "ambiguous", 1, "2026-01-01T00:00:00.000004Z", None),
)


class _FailingConnection:
    """Delegating proxy that fails the first matching statement.

    ``sqlite3.Connection`` does not accept attribute assignment, so the
    failure is injected by wrapping the object returned from ``_connect``.
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


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="so-migration-")
        self.db_path = Path(self.tempdir.name) / "work-items.sqlite3"
        self.store = SecureWorkItemStore(self.db_path)
        self.store.initialize()
        self.principal = Principal(tenant_id="tenant-a", principal_id="owner-a")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _raw_all(self, sql: str, params=()):
        conn = self._raw()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _raw_scalar(self, sql: str, params=()):
        conn = self._raw()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def _snapshot(self) -> dict:
        """Replicates ``_landing_migration_snapshot`` with a raw connection."""
        total = self._raw_scalar("SELECT COUNT(*) FROM authority_landing_events")
        distributions = {
            (row["match"], row["consumed"]): row["count"]
            for row in self._raw_all(
                "SELECT match, "
                "CASE WHEN consumed_at IS NULL THEN 0 ELSE 1 END AS consumed, "
                "COUNT(*) AS count FROM authority_landing_events "
                "GROUP BY match, consumed"
            )
        }
        events_with_link = self._raw_scalar(
            "SELECT COUNT(*) FROM authority_landing_events AS e "
            "WHERE e.consumed_at IS NOT NULL AND EXISTS ("
            "SELECT 1 FROM session_links AS s WHERE s.session_id = e.session_id "
            "AND s.linked_at = e.consumed_at)"
        )
        links_with_event = self._raw_scalar(
            "SELECT COUNT(*) FROM session_links AS s WHERE EXISTS ("
            "SELECT 1 FROM authority_landing_events AS e "
            "WHERE e.session_id = s.session_id AND e.consumed_at = s.linked_at)"
        )
        return {
            "total": total,
            "distributions": distributions,
            "events_with_link": events_with_link,
            "links_with_event": links_with_event,
        }

    def _install_legacy_landing_table(self) -> None:
        # A real WorkItem, so the historical link below has a valid FK target.
        self.legacy_item = self.store.create_work_item(self.principal)
        conn = self._raw()
        try:
            conn.execute("DROP TABLE authority_landing_events")
            conn.execute(LEGACY_DDL)
            conn.executemany(
                "INSERT INTO authority_landing_events VALUES (?, ?, ?, ?, ?, ?)",
                LEGACY_ROWS,
            )
            # One historical link whose linked_at matches the consumed event,
            # so events_with_link / links_with_event are non-trivial.
            conn.execute(
                "INSERT INTO session_links "
                "(link_id, session_id, work_item_id, linked_at, unlinked_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                ("sl_legacy", SESSION_A, self.legacy_item.work_item_id, CONSUMED_AT),
            )
            conn.commit()
        finally:
            conn.close()

    def _landing_row(self, event_id: str):
        rows = self._raw_all(
            "SELECT * FROM authority_landing_events WHERE event_id = ?", (event_id,)
        )
        self.assertEqual(len(rows), 1)
        return rows[0]


class LandingMigrationInvariantTests(_Base):
    """initialize: L564, L578, L597, L622, L633, L651 (all unreachable)."""

    def setUp(self) -> None:
        super().setUp()
        self._install_legacy_landing_table()
        self.before = self._snapshot()
        self.assertEqual(self.before["total"], 4)
        self.assertEqual(self.before["events_with_link"], 1)
        self.assertEqual(self.before["links_with_event"], 1)

    def test_downgrade_changes_exactly_the_legacy_pending_rows(self) -> None:
        """L564 / L578 / L597 / L622: the downgrade is exact and total-preserving.

        Kills a regression in the UPDATE's WHERE clause (over- or under-reach)
        and in its SET clause, not the ``raise`` -- see the module docstring.
        """
        self.store.initialize()
        after = self._snapshot()

        # L597: the migration never INSERTs or DELETEs.
        self.assertEqual(after["total"], self.before["total"])

        # L564 + L622: exactly the two pending confirmed rows moved to
        # pending ambiguous; every other bucket is byte-identical.
        self.assertEqual(
            after["distributions"],
            {("ambiguous", 0): 3, ("confirmed", 1): 1},
        )
        self.assertEqual(self.before["distributions"], {("confirmed", 0): 2,
                                                        ("confirmed", 1): 1,
                                                        ("ambiguous", 0): 1})

        # L578: nothing is left in the legacy pending-confirmed shape.
        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM authority_landing_events "
                "WHERE work_item_id IS NULL AND attempt_id IS NULL "
                "AND consumed_at IS NULL AND rejected_at IS NULL "
                "AND match = 'confirmed'"
            ),
            0,
        )

        for event_id in ("le_pending_1", "le_pending_2"):
            row = self._landing_row(event_id)
            self.assertEqual(row["match"], "ambiguous")
            self.assertEqual(row["evidence_complete"], 0)
            self.assertIsNone(row["work_item_id"])
            self.assertIsNone(row["attempt_id"])
            self.assertIsNone(row["consumed_at"])
            self.assertIsNone(row["rejected_at"])
            self.assertIsNone(row["rejection_reason"])

        # Untouched rows: a consumed event and a pre-existing ambiguous one.
        consumed = self._landing_row("le_consumed")
        self.assertEqual(consumed["match"], "confirmed")
        self.assertEqual(consumed["evidence_complete"], 1)
        self.assertEqual(consumed["consumed_at"], CONSUMED_AT)
        pre_existing = self._landing_row("le_ambiguous")
        self.assertEqual(pre_existing["match"], "ambiguous")
        self.assertEqual(pre_existing["evidence_complete"], 1)

    def test_legacy_row_verification_counts_exactly_the_downgraded_rows(self) -> None:
        """L651: ``invalid_legacy_rows`` is exactly ``legacy_pending_count``.

        The guard fires when the downgraded rows fail to match the
        verification query.  Mutating the UPDATE's SET clause (dropping
        ``evidence_complete = 0``) makes this count 0 and is caught here.
        """
        self.store.initialize()
        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM authority_landing_events "
                "WHERE match = 'ambiguous' AND evidence_complete = 0 "
                "AND work_item_id IS NULL AND attempt_id IS NULL "
                "AND consumed_at IS NULL AND rejected_at IS NULL "
                "AND rejection_reason IS NULL"
            ),
            2,
        )

    def test_historical_event_link_correspondence_is_preserved(self) -> None:
        """L633: the migration must not rewrite consumed_at or linked_at."""
        self.store.initialize()
        after = self._snapshot()
        self.assertEqual(after["events_with_link"], self.before["events_with_link"])
        self.assertEqual(after["links_with_event"], self.before["links_with_event"])
        self.assertEqual(after["events_with_link"], 1)
        self.assertEqual(after["links_with_event"], 1)

    def test_migration_is_idempotent(self) -> None:
        """A second initialize() must not move anything a second time."""
        self.store.initialize()
        first = self._snapshot()
        self.store.initialize()
        self.assertEqual(self._snapshot(), first)


class TerminalReconciliationInvariantTests(_Base):
    """initialize: L678 (unreachable)."""

    def _link(self, session_id: str):
        item = self.store.create_work_item(self.principal)
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
        return item, link

    def test_reconciliation_unlinks_each_stale_link_exactly_once(self) -> None:
        """L678: ``changed == 1`` for every stale link, and only for those.

        The work item is moved to a terminal status with raw SQL so the link
        is left active for ``initialize`` to reconcile -- ``set_status``
        would have unlinked it already.
        """
        terminal_item, terminal_link = self._link(SESSION_A)
        active_item, active_link = self._link(SESSION_B)

        conn = self._raw()
        try:
            conn.execute(
                "UPDATE work_items SET status = 'completed' WHERE work_item_id = ?",
                (terminal_item.work_item_id,),
            )
            conn.commit()
        finally:
            conn.close()

        self.store.initialize()

        stale = self._raw_all(
            "SELECT unlinked_at FROM session_links WHERE link_id = ?",
            (terminal_link.link_id,),
        )
        self.assertEqual(len(stale), 1)
        self.assertIsNotNone(stale[0]["unlinked_at"])

        # The non-terminal link is untouched: the loop's predicate is pinned.
        live = self._raw_all(
            "SELECT unlinked_at FROM session_links WHERE link_id = ?",
            (active_link.link_id,),
        )
        self.assertEqual(len(live), 1)
        self.assertIsNone(live[0]["unlinked_at"])
        self.assertEqual(active_item.work_item_id, active_link.work_item_id)

        def audit_count() -> int:
            return self._raw_scalar(
                "SELECT COUNT(*) FROM authority_audit_events "
                "WHERE action = 'session.auto_unlink_terminal' AND link_id = ?",
                (terminal_link.link_id,),
            )

        self.assertEqual(audit_count(), 1)

        # Re-running must not unlink or audit a second time.
        self.store.initialize()
        self.assertEqual(audit_count(), 1)


class LinkSessionGuardTests(_Base):
    """link_session: L1333 (reachable) and L1290 (unreachable, stubbed)."""

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

    def test_active_link_conflict_is_rejected_before_consuming(self) -> None:
        """L1333: the deterministic conflict is detected BEFORE consumption.

        Deleting the guard still raises ``SessionAlreadyLinked`` (the INSERT
        then trips the unique index and the IntegrityError handler reclassifies
        it), so a type/message assertion cannot tell the two apart.  This test
        fails the consume UPDATE instead: the guard must fire before that
        statement is reached.
        """
        first = self.store.create_work_item(self.principal)
        second = self.store.create_work_item(self.principal)
        first_attempt = self._record_landing(first, SESSION_A)
        self.store.link_session(
            self.principal, SESSION_A, first.work_item_id, first_attempt
        )
        second_attempt = self._record_landing(second, SESSION_A)

        real_connect = self.store._connect

        def connect():
            return _FailingConnection(real_connect(), MARKER_CONSUME)

        with mock.patch.object(self.store, "_connect", connect):
            with self.assertRaises(SessionAlreadyLinked) as ctx:
                self.store.link_session(
                    self.principal, SESSION_A, second.work_item_id, second_attempt
                )

        self.assertEqual(
            str(ctx.exception), "session already has an active WorkItem link"
        )
        # Raised by the pre-consume guard, not reclassified from an
        # IntegrityError (which would chain the original exception).
        self.assertIsNone(ctx.exception.__cause__)

        # The rejected evidence is recorded as REJECTED, never consumed, and
        # no link row was created for the second work item.
        row = self._raw_all(
            "SELECT consumed_at, rejected_at, rejection_reason "
            "FROM authority_landing_events WHERE work_item_id = ?",
            (second.work_item_id,),
        )
        self.assertEqual(len(row), 1)
        self.assertIsNone(row[0]["consumed_at"])
        self.assertIsNotNone(row[0]["rejected_at"])
        self.assertEqual(row[0]["rejection_reason"], "session_already_linked")
        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM session_links WHERE work_item_id = ?",
                (second.work_item_id,),
            ),
            0,
        )

    def test_consumption_is_bound_to_the_exact_attempt(self) -> None:
        """L1359: the consume UPDATE must match exactly one row.

        Two confirmed, complete, unconsumed events exist for the same session
        and WorkItem under different attempts.  Linking with attempt A must
        consume A and leave B untouched: a widened predicate (for example a
        dropped ``attempt_id`` term) makes ``rowcount`` 2 and is caught here.
        """
        item = self.store.create_work_item(self.principal)
        attempt_a = "att_" + str(uuid.uuid4())
        attempt_b = "att_" + str(uuid.uuid4())
        for attempt_id in (attempt_a, attempt_b):
            self.store._record_trusted_landing(
                SESSION_A,
                match="confirmed",
                evidence_complete=True,
                work_item_id=item.work_item_id,
                attempt_id=attempt_id,
            )

        self.store.link_session(
            self.principal, SESSION_A, item.work_item_id, attempt_a
        )

        rows = {
            row["attempt_id"]: row["consumed_at"]
            for row in self._raw_all(
                "SELECT attempt_id, consumed_at FROM authority_landing_events "
                "WHERE work_item_id = ?",
                (item.work_item_id,),
            )
        }
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[attempt_a])
        self.assertIsNone(rows[attempt_b])

    def test_unique_index_still_forbids_two_active_links_per_session(self) -> None:
        """L1413: the constraint whose violation the handler classifies.

        The guard only exists because a duplicate active link surfaces as
        ``sqlite3.IntegrityError``.  If the partial unique index were dropped,
        the reclassification branch would be dead and the one-active-link
        invariant would be silently gone.
        """
        item = self.store.create_work_item(self.principal)
        attempt_id = self._record_landing(item, SESSION_A)
        link = self.store.link_session(
            self.principal, SESSION_A, item.work_item_id, attempt_id
        )

        self.assertEqual(
            [
                row["name"]
                for row in self._raw_all(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'ux_session_links_one_active_work_item_per_session'"
                )
            ],
            ["ux_session_links_one_active_work_item_per_session"],
        )

        conn = self._raw()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO session_links "
                    "(link_id, session_id, work_item_id, linked_at, unlinked_at) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (
                        "sl_" + str(uuid.uuid4()),
                        SESSION_A,
                        item.work_item_id,
                        link.linked_at,
                    ),
                )
        finally:
            conn.close()

    def test_missing_work_item_row_fails_closed_after_authorization(self) -> None:
        """L1290: the second lookup must not fall through to ``item["status"]``.

        ``_require`` already rejects an unknown ``work_item_id``, so the row
        cannot really vanish between the two statements.  ``_require`` is
        stubbed to isolate the guard; the database state (an owner row with no
        ``work_items`` row, inserted with foreign keys off) is real.
        """
        orphan_id = "wi_" + str(uuid.uuid4())
        conn = self._raw()
        try:
            conn.execute(
                "INSERT INTO work_item_owners "
                "(work_item_id, tenant_id, owner_principal_id, created_at) "
                "VALUES (?, 'tenant-a', 'owner-a', '2026-01-01T00:00:00.000000Z')",
                (orphan_id,),
            )
            conn.commit()
        finally:
            conn.close()

        attempt_id = "att_" + str(uuid.uuid4())
        with mock.patch.object(SecureWorkItemStore, "_require", lambda *a, **k: None):
            with self.assertRaises(NotAuthorized) as ctx:
                self.store.link_session(
                    self.principal, SESSION_A, orphan_id, attempt_id
                )
            self.assertEqual(str(ctx.exception), "resource is not available")

            # Acceptance pairing: the stub does not break the happy path.
            item = self.store.create_work_item(self.principal)
            good_attempt = self._record_landing(item, SESSION_B)
            link = self.store.link_session(
                self.principal, SESSION_B, item.work_item_id, good_attempt
            )
        self.assertEqual(link.work_item_id, item.work_item_id)
        self.assertEqual(
            self._raw_scalar(
                "SELECT COUNT(*) FROM session_links WHERE work_item_id = ?",
                (orphan_id,),
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
