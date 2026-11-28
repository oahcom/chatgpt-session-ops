from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.migrate_sender_owners import migrate
from src.session_ops.chatgpt_sessions import ChatGPTSessionRegistry


class SenderOwnerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "sender.sqlite3"
        self.target = root / "mcp.sqlite3"
        conn = sqlite3.connect(self.source)
        conn.execute(
            "CREATE TABLE session_owners ("
            "conversation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "principal_id TEXT NOT NULL, account TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO session_owners VALUES (?, ?, ?, ?, ?)",
            ("12345678-1234-4234-8234-123456789abc", "tenant-a", "principal-a",
             "acct2", "2026-11-28T00:00:00Z"),
        )
        conn.commit()
        conn.close()
        ChatGPTSessionRegistry(self.target).initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_does_not_insert(self):
        result = migrate(self.source, self.target, apply=False)
        self.assertEqual(result, {
            "source_rows": 1, "already_present": 0, "to_insert": 1, "inserted": 0,
        })
        self.assertIsNone(
            ChatGPTSessionRegistry(self.target).get_owner(
                "12345678-1234-4234-8234-123456789abc"
            )
        )

    def test_apply_imports_and_is_idempotent(self):
        first = migrate(self.source, self.target, apply=True)
        second = migrate(self.source, self.target, apply=True)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(
            ChatGPTSessionRegistry(self.target).get_owner(
                "12345678-1234-4234-8234-123456789abc"
            ),
            {"tenant_id": "tenant-a", "principal_id": "principal-a", "account": "acct2"},
        )

    def test_conflict_fails_without_overwrite(self):
        registry = ChatGPTSessionRegistry(self.target)
        registry.register_session(
            "12345678-1234-4234-8234-123456789abc",
            "tenant-b", "principal-b", "acct3",
        )
        with self.assertRaisesRegex(RuntimeError, "owner conflict"):
            migrate(self.source, self.target, apply=True)
        self.assertEqual(
            registry.get_owner("12345678-1234-4234-8234-123456789abc")["account"],
            "acct3",
        )


if __name__ == "__main__":
    unittest.main()
