from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts import chatgpt_send as sender
from src.session_ops.authority import (
    DenyAllPrincipalDirectory,
    Principal,
    PrincipalRecord,
    QuarantineNotCleared,
    SecureWorkItemStore,
    StaticPrincipalDirectory,
)
from tests.test_send_prompt_for_work_item import FakeAPI, cid


class CountingFakeAPI(FakeAPI):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.send_calls = 0

    def send(self, *args, **kwargs):
        self.send_calls += 1
        return super().send(*args, **kwargs)


class WorkItemCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "authority.sqlite3"
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("CLI test prompt", encoding="utf-8")

        self.principal = Principal("tenant-1", "alice")
        seed = SecureWorkItemStore(
            self.db,
            principal_directory=StaticPrincipalDirectory(
                [PrincipalRecord("alice", "tenant-1")]
            ),
        )
        seed.initialize()
        self.item = seed.create_work_item(self.principal)

    def run_main(self, argv, api=None, env=None):
        stdout = io.StringIO()
        with patch.object(sender.sys, "argv", ["chatgpt_send.py"] + argv):
            with patch.dict(os.environ, env or {}, clear=True):
                with patch.object(
                    sender, "ChatGPTAPI",
                    return_value=api,
                ) as api_factory:
                    with contextlib.redirect_stdout(stdout):
                        code = sender.main()
        return code, stdout.getvalue(), api_factory

    def cli_flags(self):
        return [
            "--work-item", self.item.work_item_id,
            "--principal", "alice",
            "--tenant", "tenant-1",
            "--db", str(self.db),
        ]

    def event_count(self):
        conn = SecureWorkItemStore(
            self.db,
            principal_directory=DenyAllPrincipalDirectory(),
        )._connect()
        try:
            return conn.execute(
                "SELECT count(*) FROM authority_landing_events"
            ).fetchone()[0]
        finally:
            conn.close()

    def link_count(self):
        conn = SecureWorkItemStore(
            self.db,
            principal_directory=DenyAllPrincipalDirectory(),
        )._connect()
        try:
            return conn.execute(
                "SELECT count(*) FROM session_links WHERE work_item_id = ?",
                (self.item.work_item_id,),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_without_work_item_preserves_legacy_cli_path(self):
        api = CountingFakeAPI([cid()], root=self.root)
        with patch.object(
            sender, "send_prompt",
            return_value=(200, "reply.md", sender.ConvRef(cid())),
        ) as send:
            with patch.object(sender, "record_conv"):
                code, output, _ = self.run_main(
                    ["--account", "acct1", cid(), str(self.prompt)],
                    api=api,
                )
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        send.assert_called_once()
        self.assertEqual(self.event_count(), 0)
        self.assertEqual(self.link_count(), 0)

    def test_work_item_cli_new_send_records_consumed_event_and_active_link(self):
        session_id = cid()
        api = CountingFakeAPI([session_id], root=self.root)
        with patch.object(sender, "record_conv") as record:
            code, output, _ = self.run_main(
                [
                    "--account", "acct1",
                    *self.cli_flags(),
                    "--new", str(self.prompt),
                ],
                api=api,
            )

        self.assertEqual(code, 0, output)
        self.assertEqual(api.send_calls, 1)
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(self.link_count(), 1)
        record.assert_called_once_with(session_id, "acct1", str(self.prompt))

        conn = SecureWorkItemStore(
            self.db, principal_directory=DenyAllPrincipalDirectory()
        )._connect()
        try:
            row = conn.execute(
                "SELECT consumed_at FROM authority_landing_events "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(row["consumed_at"])
        finally:
            conn.close()

    def test_mismatch_ambiguous_and_incomplete_never_create_event_or_link(self):
        cases = [
            ("mismatch", [cid()], True, True),
            ("ambiguous", [cid(), cid()], True, False),
            ("incomplete", [cid()], False, False),
        ]
        for name, ids, complete, mismatch in cases:
            with self.subTest(name=name):
                # 每个子用例使用独立数据库和 WorkItem，避免事件互相污染。
                case_db = self.root / (name + ".sqlite3")
                store = SecureWorkItemStore(
                    case_db,
                    principal_directory=StaticPrincipalDirectory(
                        [PrincipalRecord("alice", "tenant-1")]
                    ),
                )
                store.initialize()
                item = store.create_work_item(self.principal)
                target = cid()
                api = CountingFakeAPI(
                    ids,
                    complete=complete,
                    root=self.root,
                )
                argv = [
                    "--account", "acct1",
                    "--work-item", item.work_item_id,
                    "--principal", "alice",
                    "--tenant", "tenant-1",
                    "--db", str(case_db),
                    target, str(self.prompt),
                ]
                quarantine_patch = patch.object(
                    sender,
                    "_quarantine_reply",
                    return_value=(str(self.root / "quarantine"), True),
                )
                with quarantine_patch:
                    with patch.object(sender, "record_conv"):
                        code, output, _ = self.run_main(argv, api=api)

                self.assertNotEqual(code, 0, output)
                self.assertEqual(api.send_calls, 1)
                conn = store._connect()
                try:
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM authority_landing_events"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM session_links"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    conn.close()

    def test_missing_principal_tenant_or_db_fails_before_send(self):
        required = {
            "--principal": "alice",
            "--tenant": "tenant-1",
            "--db": str(self.db),
        }
        for missing in required:
            with self.subTest(missing=missing):
                api = CountingFakeAPI([cid()], root=self.root)
                flags = ["--work-item", self.item.work_item_id]
                for flag, value in required.items():
                    if flag != missing:
                        flags.extend([flag, value])
                code, output, factory = self.run_main(
                    ["--account", "acct1", *flags, "--new", str(self.prompt)],
                    api=api,
                )
                self.assertNotEqual(code, 0, output)
                self.assertEqual(api.send_calls, 0)
                factory.assert_not_called()

    def test_evidence_and_match_flags_are_rejected(self):
        for forbidden in ("--evidence", "--match", "--confirmed"):
            with self.subTest(forbidden=forbidden):
                api = CountingFakeAPI([cid()], root=self.root)
                code, output, factory = self.run_main(
                    [
                        "--account", "acct1",
                        *self.cli_flags(),
                        forbidden, "confirmed",
                        "--new", str(self.prompt),
                    ],
                    api=api,
                )
                self.assertEqual(code, 2, output)
                self.assertIn("usage:", output)
                self.assertEqual(api.send_calls, 0)
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
