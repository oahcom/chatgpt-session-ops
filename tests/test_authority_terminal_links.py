"""Regression tests for terminal WorkItem Session-link invalidation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.session_ops.authority import Principal, SecureWorkItemStore


class TerminalSessionLinkAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="so-terminal-link-")
        self.db_path = Path(self.tempdir.name) / "work-items.sqlite3"
        self.store = SecureWorkItemStore(self.db_path)
        self.store.initialize()
        self.principal = Principal(tenant_id="tenant-a", principal_id="owner-a")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_terminal_transition_unlinks_and_audits_each_link(self) -> None:
        for terminal_status in ("completed", "cancelled"):
            with self.subTest(status=terminal_status):
                item = self.store.create_work_item(self.principal)
                session_id = (
                    "12345678-1234-4234-8234-"
                    + ("123456789abc" if terminal_status == "completed" else "123456789abd")
                )
                attempt_id = (
                    "att_12345678-1234-4234-8234-123456789abc"
                    if terminal_status == "completed"
                    else "att_12345678-1234-4234-8234-123456789abd"
                )
                self.store._record_trusted_landing(
                    session_id,
                    match="confirmed",
                    evidence_complete=True,
                    work_item_id=item.work_item_id,
                    attempt_id=attempt_id,
                )
                linked = self.store.link_session(
                    self.principal,
                    session_id,
                    item.work_item_id,
                    attempt_id,
                )

                updated = self.store.set_status(
                    self.principal,
                    item.work_item_id,
                    terminal_status,
                    request_id="terminal-" + terminal_status,
                )
                self.assertEqual(updated.status, terminal_status)

                # 1. A terminal WorkItem's link is absent from the default view.
                self.assertEqual(
                    self.store.list_sessions(self.principal, item.work_item_id), []
                )

                # 2. History remains queryable and records the unlink timestamp.
                history = self.store.list_sessions(
                    self.principal, item.work_item_id, include_unlinked=True
                )
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0].link_id, linked.link_id)
                self.assertIsNotNone(history[0].unlinked_at)

                # 3. Exactly one terminal-auto-unlink audit row identifies this link.
                audit = [
                    event
                    for event in self.store.list_audit_events(item.work_item_id)
                    if event["action"] == "session.auto_unlink_terminal"
                    and event["link_id"] == linked.link_id
                ]
                self.assertEqual(len(audit), 1)
                self.assertEqual(audit[0]["session_id"], session_id)
                self.assertEqual(audit[0]["principal_id"], self.principal.principal_id)
                self.assertEqual(audit[0]["request_id"], "terminal-" + terminal_status)

    def test_manual_unlink_audit_is_distinct_and_link_specific(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        attempt_id = "att_" + str(__import__("uuid").uuid4())
        self.store._record_trusted_landing(
            session_id,
            match="confirmed",
            evidence_complete=True,
            work_item_id=item.work_item_id,
            attempt_id=attempt_id,
        )
        linked = self.store.link_session(
            self.principal, session_id, item.work_item_id, attempt_id
        )
        self.store.unlink_session(
            self.principal, session_id, request_id="manual-unlink"
        )
        events = [
            event
            for event in self.store.list_audit_events(item.work_item_id)
            if event["link_id"] == linked.link_id
            and event["action"]
            in ("session.unlink", "session.auto_unlink_terminal")
        ]
        self.assertEqual(
            [event["action"] for event in events],
            ["session.unlink"],
        )
        self.assertEqual(
            events[0]["principal_id"],
            self.principal.principal_id,
        )

    def test_list_sessions_handles_all_link_states(self) -> None:
        def create_link(item):
            session_id = str(__import__("uuid").uuid4())
            attempt_id = "att_" + str(__import__("uuid").uuid4())
            self.store._record_trusted_landing(
                session_id,
                match="confirmed",
                evidence_complete=True,
                work_item_id=item.work_item_id,
                attempt_id=attempt_id,
            )
            return self.store.link_session(
                self.principal,
                session_id,
                item.work_item_id,
                attempt_id,
            )

        # 1. 无 link：默认视图与历史视图均为空。
        empty_item = self.store.create_work_item(self.principal)
        self.assertEqual(
            list(self.store.list_sessions(self.principal, empty_item.work_item_id)),
            [],
        )
        self.assertEqual(
            list(
                self.store.list_sessions(
                    self.principal,
                    empty_item.work_item_id,
                    include_unlinked=True,
                )
            ),
            [],
        )

        # 2. 有活跃 link：默认视图与历史视图都返回该 link。
        active_item = self.store.create_work_item(self.principal)
        active_link = create_link(active_item)
        self.assertEqual(
            list(self.store.list_sessions(self.principal, active_item.work_item_id)),
            [active_link],
        )
        self.assertEqual(
            list(
                self.store.list_sessions(
                    self.principal,
                    active_item.work_item_id,
                    include_unlinked=True,
                )
            ),
            [active_link],
        )

        # 3. 已解链 link：仅历史视图返回。
        self.store.unlink_session(
            self.principal,
            active_link.session_id,
            expected_link_id=active_link.link_id,
        )
        self.assertEqual(
            list(self.store.list_sessions(self.principal, active_item.work_item_id)),
            [],
        )
        historical_active = list(
            self.store.list_sessions(
                self.principal,
                active_item.work_item_id,
                include_unlinked=True,
            )
        )
        self.assertEqual(len(historical_active), 1)
        self.assertEqual(historical_active[0].link_id, active_link.link_id)
        self.assertIsNotNone(historical_active[0].unlinked_at)

        # 4. 终态 WorkItem：默认视图不返回，历史视图仍返回回填/解链记录。
        terminal_item = self.store.create_work_item(self.principal)
        terminal_link = create_link(terminal_item)
        self.store.set_status(
            self.principal,
            terminal_item.work_item_id,
            "cancelled",
        )
        self.assertEqual(
            list(
                self.store.list_sessions(
                    self.principal,
                    terminal_item.work_item_id,
                )
            ),
            [],
        )
        terminal_history = list(
            self.store.list_sessions(
                self.principal,
                terminal_item.work_item_id,
                include_unlinked=True,
            )
        )
        self.assertEqual(len(terminal_history), 1)
        self.assertEqual(terminal_history[0].link_id, terminal_link.link_id)
        self.assertIsNotNone(terminal_history[0].unlinked_at)

    def test_get_active_link_respects_owner_visibility_and_unlink(self) -> None:
        item = self.store.create_work_item(self.principal)
        session_id = "12345678-1234-4234-8234-123456789abc"
        attempt_id = "att_" + str(__import__("uuid").uuid4())
        self.store._record_trusted_landing(
            session_id,
            match="confirmed",
            evidence_complete=True,
            work_item_id=item.work_item_id,
            attempt_id=attempt_id,
        )
        linked = self.store.link_session(
            self.principal, session_id, item.work_item_id, attempt_id
        )

        owner_link = self.store.get_active_link(self.principal, session_id)
        self.assertIsNotNone(owner_link)
        self.assertEqual(owner_link.link_id, linked.link_id)
        self.assertEqual(owner_link.session_id, session_id)
        self.assertEqual(owner_link.work_item_id, item.work_item_id)

        stranger = Principal(
            tenant_id="tenant-a",
            principal_id="stranger-a",
        )
        stranger_link = self.store.get_active_link(stranger, session_id)
        self.assertIsNone(stranger_link)

        self.store.unlink_session(
            self.principal,
            session_id,
            expected_link_id=linked.link_id,
        )
        self.assertIsNone(
            self.store.get_active_link(self.principal, session_id)
        )


if __name__ == "__main__":
    unittest.main()
