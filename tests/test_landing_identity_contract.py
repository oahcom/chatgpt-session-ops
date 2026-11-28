from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from unittest.mock import patch

from src.session_ops.authority import (
    AuthorityError,
    Principal,
    PrincipalRecord,
    QuarantineNotCleared,
    SecureWorkItemStore,
    StaticPrincipalDirectory,
)
from src.session_ops.work_items import SessionAlreadyLinked


def new_session_id() -> str:
    return str(uuid.uuid4())


def new_attempt_id() -> str:
    return "att_" + str(uuid.uuid4())


class LandingIdentityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "authority.sqlite3"
        self.principal = Principal("tenant-a", "alice")
        self.store = SecureWorkItemStore(
            self.db_path,
            principal_directory=StaticPrincipalDirectory(
                [PrincipalRecord("alice", "tenant-a")]
            ),
        )
        self.store.initialize()

    def record(
        self,
        session_id: str,
        work_item_id: str,
        attempt_id: str,
    ) -> str:
        return self.store._record_trusted_landing(
            session_id,
            "confirmed",
            True,
            work_item_id,
            attempt_id,
        )

    def landing_row(
        self,
        session_id: str,
        work_item_id: str,
        attempt_id: str,
    ):
        conn = self.store._connect()
        try:
            return conn.execute(
                "SELECT * FROM authority_landing_events "
                "WHERE session_id = ? AND work_item_id = ? AND attempt_id = ?",
                (session_id, work_item_id, attempt_id),
            ).fetchone()
        finally:
            conn.close()

    def test_t0_attempt_receipt_cannot_be_rebound_to_another_session(self) -> None:
        item = self.store.create_work_item(self.principal)
        first_session = new_session_id()
        other_session = new_session_id()
        attempt_id = new_attempt_id()
        event_id = self.record(first_session, item.work_item_id, attempt_id)

        with self.assertRaises(QuarantineNotCleared):
            self.record(other_session, item.work_item_id, attempt_id)
        self.assertEqual(
            self.landing_row(first_session, item.work_item_id, attempt_id)["event_id"],
            event_id,
        )
        self.assertIsNone(
            self.landing_row(other_session, item.work_item_id, attempt_id)
        )

    def test_t0_concurrent_duplicate_landing_ingestion_returns_same_receipt(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_id = new_attempt_id()
        barrier = Barrier(2)

        def record_once() -> str:
            barrier.wait(timeout=2)
            return self.record(session_id, item.work_item_id, attempt_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            event_ids = list(pool.map(lambda _: record_once(), range(2)))
        self.assertEqual(event_ids[0], event_ids[1])
        conn = self.store._connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM authority_landing_events "
                "WHERE work_item_id = ? AND attempt_id = ?",
                (item.work_item_id, attempt_id),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_t0_duplicate_landing_before_link_returns_one_attempt_receipt(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_id = new_attempt_id()

        first_event = self.record(session_id, item.work_item_id, attempt_id)
        replay_event = self.record(session_id, item.work_item_id, attempt_id)

        self.assertEqual(replay_event, first_event)
        row = self.landing_row(session_id, item.work_item_id, attempt_id)
        self.assertEqual(row["event_id"], first_event)
        self.assertIsNone(row["consumed_at"])
        self.assertIsNone(row["link_id"])
        conn = self.store._connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM authority_landing_events "
                "WHERE work_item_id = ? AND attempt_id = ?",
                (item.work_item_id, attempt_id),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

        link = self.store.link_session(
            self.principal, session_id, item.work_item_id, attempt_id,
            idempotency_key="landing-before-link-1",
        )
        linked = self.landing_row(session_id, item.work_item_id, attempt_id)
        self.assertEqual(linked["link_id"], link.link_id)
        self.assertIsNotNone(linked["consumed_at"])

    def test_t0_duplicate_landing_after_link_returns_consumed_receipt(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_id = new_attempt_id()
        event_id = self.record(session_id, item.work_item_id, attempt_id)
        link = self.store.link_session(
            self.principal, session_id, item.work_item_id, attempt_id,
            idempotency_key="landing-after-link-1",
        )

        self.assertEqual(
            self.record(session_id, item.work_item_id, attempt_id), event_id
        )
        row = self.landing_row(session_id, item.work_item_id, attempt_id)
        self.assertEqual(row["link_id"], link.link_id)
        self.assertIsNotNone(row["consumed_at"])
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal, session_id, item.work_item_id, attempt_id,
                idempotency_key="different-link-request-1",
            )

    def test_t0_retry_after_unlink_cannot_resurrect_the_same_attempt(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_id = new_attempt_id()
        event_id = self.record(session_id, item.work_item_id, attempt_id)
        first_link = self.store.link_session(
            self.principal, session_id, item.work_item_id, attempt_id,
            idempotency_key="retry-after-unlink-1",
        )
        self.store.unlink_session(
            self.principal, session_id, expected_link_id=first_link.link_id
        )

        self.assertEqual(
            self.record(session_id, item.work_item_id, attempt_id), event_id
        )
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal, session_id, item.work_item_id, attempt_id,
                idempotency_key="retry-after-unlink-2",
            )
        row = self.landing_row(session_id, item.work_item_id, attempt_id)
        self.assertEqual(row["link_id"], first_link.link_id)
        self.assertIsNotNone(row["consumed_at"])
        conn = self.store._connect()
        try:
            unlinked = conn.execute(
                "SELECT unlinked_at FROM session_links WHERE link_id = ?",
                (first_link.link_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(unlinked["unlinked_at"])

    def test_t0_concurrent_duplicate_link_calls_create_one_link(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_id = new_attempt_id()
        self.record(session_id, item.work_item_id, attempt_id)
        barrier = Barrier(2)

        def link_with_key(key: str):
            barrier.wait(timeout=2)
            try:
                return ("ok", self.store.link_session(
                    self.principal, session_id, item.work_item_id, attempt_id,
                    idempotency_key=key,
                ))
            except QuarantineNotCleared:
                return ("duplicate", None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                link_with_key, ("concurrent-link-a", "concurrent-link-b")
            ))
        self.assertEqual(sum(kind == "ok" for kind, _ in results), 1)
        self.assertEqual(sum(kind == "duplicate" for kind, _ in results), 1)
        conn = self.store._connect()
        try:
            links = conn.execute(
                "SELECT link_id FROM session_links WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(links), 1)
        row = self.landing_row(session_id, item.work_item_id, attempt_id)
        self.assertEqual(row["link_id"], links[0]["link_id"])

    def test_t0_new_attempt_allows_explicit_relink_after_unlink(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        first_attempt = new_attempt_id()
        self.record(session_id, item.work_item_id, first_attempt)
        first_link = self.store.link_session(
            self.principal, session_id, item.work_item_id, first_attempt,
            idempotency_key="new-attempt-first-link",
        )
        self.store.unlink_session(
            self.principal, session_id, expected_link_id=first_link.link_id
        )

        second_attempt = new_attempt_id()
        self.record(session_id, item.work_item_id, second_attempt)
        second_link = self.store.link_session(
            self.principal, session_id, item.work_item_id, second_attempt,
            idempotency_key="new-attempt-second-link",
        )
        self.assertNotEqual(first_link.link_id, second_link.link_id)
        first_receipt = self.landing_row(
            session_id, item.work_item_id, first_attempt
        )
        second_receipt = self.landing_row(
            session_id, item.work_item_id, second_attempt
        )
        self.assertEqual(first_receipt["link_id"], first_link.link_id)
        self.assertEqual(second_receipt["link_id"], second_link.link_id)

    def test_t0_duplicate_attempt_rows_abort_migration_without_deleting_evidence(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_id = new_attempt_id()
        self.record(session_id, item.work_item_id, attempt_id)
        conn = self.store._connect()
        try:
            conn.execute("DROP INDEX ux_authority_landing_attempt_identity")
            conn.execute(
                "INSERT INTO authority_landing_events "
                "(event_id, session_id, match, evidence_complete, created_at, "
                "consumed_at, work_item_id, attempt_id, link_id, rejected_at, "
                "rejection_reason) VALUES (?, ?, 'confirmed', 1, ?, NULL, ?, ?, NULL, NULL, NULL)",
                (
                    "le_" + str(uuid.uuid4()), session_id, "2026-11-29T00:00:00Z",
                    item.work_item_id, attempt_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(AuthorityError, "duplicate landing rows"):
            self.store.initialize()
        conn = sqlite3.connect(str(self.db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM authority_landing_events "
                "WHERE work_item_id = ? AND attempt_id = ?",
                (item.work_item_id, attempt_id),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 2)

    def test_t0_link_id_is_backfilled_for_existing_consumed_receipts(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_id = new_attempt_id()
        self.record(session_id, item.work_item_id, attempt_id)
        link = self.store.link_session(
            self.principal, session_id, item.work_item_id, attempt_id,
            idempotency_key="link-id-backfill",
        )
        conn = self.store._connect()
        try:
            conn.execute("ALTER TABLE authority_landing_events DROP COLUMN link_id")
            conn.commit()
        finally:
            conn.close()

        reopened = SecureWorkItemStore(
            self.db_path,
            principal_directory=StaticPrincipalDirectory(
                [PrincipalRecord("alice", "tenant-a")]
            ),
        )
        reopened.initialize()
        row = self.landing_row(session_id, item.work_item_id, attempt_id)
        self.assertEqual(row["link_id"], link.link_id)

    def test_t1_landing_for_work_item_a_cannot_be_consumed_by_work_item_b(
        self,
    ) -> None:
        item_a = self.store.create_work_item(self.principal)
        item_b = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_id = new_attempt_id()
        self.record(session_id, item_a.work_item_id, attempt_id)

        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal,
                session_id,
                item_b.work_item_id,
                attempt_id,
            )

        row = self.landing_row(
            session_id, item_a.work_item_id, attempt_id
        )
        self.assertIsNotNone(row)
        self.assertIsNone(row["consumed_at"])
        self.assertIsNone(row["rejected_at"])

        link = self.store.link_session(
            self.principal,
            session_id,
            item_a.work_item_id,
            attempt_id,
        )
        self.assertEqual(link.work_item_id, item_a.work_item_id)

    def test_t2_landing_for_attempt_one_cannot_be_consumed_by_attempt_two(
        self,
    ) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = new_session_id()
        attempt_one = new_attempt_id()
        attempt_two = new_attempt_id()
        self.record(session_id, item.work_item_id, attempt_one)

        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal,
                session_id,
                item.work_item_id,
                attempt_two,
            )

        row = self.landing_row(
            session_id, item.work_item_id, attempt_one
        )
        self.assertIsNotNone(row)
        self.assertIsNone(row["consumed_at"])
        self.assertIsNone(row["rejected_at"])

        link = self.store.link_session(
            self.principal,
            session_id,
            item.work_item_id,
            attempt_one,
        )
        self.assertEqual(link.session_id, session_id)

    def test_t3_duplicate_active_session_link_rejects_second_landing_terminally(
        self,
    ) -> None:
        item_a = self.store.create_work_item(self.principal)
        item_b = self.store.create_work_item(self.principal)
        item_c = self.store.create_work_item(self.principal)
        session_id = new_session_id()

        first_attempt = new_attempt_id()
        self.record(session_id, item_a.work_item_id, first_attempt)
        self.store.link_session(
            self.principal,
            session_id,
            item_a.work_item_id,
            first_attempt,
        )

        second_attempt = new_attempt_id()
        self.record(session_id, item_b.work_item_id, second_attempt)
        with self.assertRaises(SessionAlreadyLinked):
            self.store.link_session(
                self.principal,
                session_id,
                item_b.work_item_id,
                second_attempt,
            )

        rejected = self.landing_row(
            session_id, item_b.work_item_id, second_attempt
        )
        self.assertIsNotNone(rejected)
        self.assertIsNotNone(rejected["rejected_at"])
        self.assertEqual(
            rejected["rejection_reason"],
            "session_already_linked",
        )
        self.assertIsNone(rejected["consumed_at"])

        # Rejected evidence cannot be reused after the original link is removed.
        active = self.store.get_active_link(self.principal, session_id)
        self.assertIsNotNone(active)
        self.store.unlink_session(
            self.principal,
            session_id,
            expected_link_id=active.link_id,
        )
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal,
                session_id,
                item_b.work_item_id,
                second_attempt,
            )

        # Nor can the rejected attempt be repurposed for another WorkItem.
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal,
                session_id,
                item_c.work_item_id,
                second_attempt,
            )

    def _create_partially_upgraded_database(
        self, db_path: Path, row_values: tuple
    ) -> None:
        """Create the post-ALTER schema shape with one malformed legacy row."""
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE authority_landing_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    match TEXT NOT NULL,
                    evidence_complete INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT,
                    work_item_id TEXT,
                    attempt_id TEXT,
                    rejected_at TEXT,
                    rejection_reason TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO authority_landing_events (
                    event_id, session_id, match, evidence_complete,
                    created_at, consumed_at, work_item_id, attempt_id,
                    rejected_at, rejection_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row_values,
            )
            conn.commit()
        finally:
            conn.close()

    def _assert_malformed_migration_rolls_back(self, row_values: tuple) -> None:
        with tempfile.TemporaryDirectory() as migration_tmp:
            migration_db_path = Path(migration_tmp) / "authority.sqlite3"

            self._create_partially_upgraded_database(
                migration_db_path, row_values
            )

            migration_store = SecureWorkItemStore(
                migration_db_path,
                principal_directory=StaticPrincipalDirectory(
                    [PrincipalRecord("alice", "tenant-a")]
                ),
            )

            with self.assertRaises(AuthorityError):
                migration_store.initialize()

            conn = sqlite3.connect(str(migration_db_path))
            try:
                row = conn.execute(
                    "SELECT * FROM authority_landing_events"
                ).fetchone()
                columns = {
                    value[1]
                    for value in conn.execute(
                        "PRAGMA table_info(authority_landing_events)"
                    ).fetchall()
                }
            finally:
                conn.close()

            self.assertEqual(row, row_values)
            self.assertTrue(
                {
                    "work_item_id",
                    "attempt_id",
                    "rejected_at",
                    "rejection_reason",
                }.issubset(columns)
            )

    def test_t4_partial_identity_row_aborts_initialize_and_rolls_back(
        self,
    ) -> None:
        # work_item_id present, attempt_id absent: exactly one side of the
        # identity pair is populated.
        row_values = (
            "le_" + str(uuid.uuid4()),
            new_session_id(),
            "confirmed",
            1,
            "2026-11-24T00:00:00.000000Z",
            None,
            "wi_" + str(uuid.uuid4()),
            None,
            None,
            None,
        )
        self._assert_malformed_migration_rolls_back(row_values)

    def test_t5_rejected_row_without_reason_aborts_initialize_and_rolls_back(
        self,
    ) -> None:
        # rejected_at without a non-empty rejection_reason is malformed.
        row_values = (
            "le_" + str(uuid.uuid4()),
            new_session_id(),
            "confirmed",
            1,
            "2026-11-24T00:00:00.000000Z",
            None,
            None,
            None,
            "2026-11-24T00:01:00.000000Z",
            None,
        )
        self._assert_malformed_migration_rolls_back(row_values)

    def test_t6_failure_while_recording_rejection_propagates_and_does_not_mark(
        self,
    ) -> None:
        item_a = self.store.create_work_item(self.principal)
        item_b = self.store.create_work_item(self.principal)
        session_id = new_session_id()

        first_attempt = new_attempt_id()
        self.record(session_id, item_a.work_item_id, first_attempt)
        first_link = self.store.link_session(
            self.principal,
            session_id,
            item_a.work_item_id,
            first_attempt,
        )

        second_attempt = new_attempt_id()
        self.record(session_id, item_b.work_item_id, second_attempt)

        original_connect = self.store._connect
        main_connection = original_connect()
        injected = sqlite3.OperationalError("injected rejection-write failure")

        # The main link transaction gets a real connection. The subsequent
        # independent rejection-record transaction fails at connection time.
        with patch.object(
            self.store,
            "_connect",
            side_effect=[main_connection, injected],
        ):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.store.link_session(
                    self.principal,
                    session_id,
                    item_b.work_item_id,
                    second_attempt,
                )

        self.assertIs(caught.exception, injected)

        # Query outside the patched connection path to prove the event was not
        # falsely represented as rejected when its rejection write failed.
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rejected = conn.execute(
                "SELECT rejected_at, rejection_reason, consumed_at "
                "FROM authority_landing_events "
                "WHERE session_id = ? AND work_item_id = ? AND attempt_id = ?",
                (session_id, item_b.work_item_id, second_attempt),
            ).fetchone()
            active = conn.execute(
                "SELECT link_id FROM session_links "
                "WHERE session_id = ? AND unlinked_at IS NULL",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(rejected)
        self.assertIsNone(rejected["rejected_at"])
        self.assertIsNone(rejected["rejection_reason"])
        self.assertIsNone(rejected["consumed_at"])
        self.assertIsNotNone(active)
        self.assertEqual(active["link_id"], first_link.link_id)

