import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.migrate_sender_runtime_state import (
    STATE_FILES,
    merge_legacy_account_health,
    migrate_state,
)


class MigrateSenderRuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "old-cli"
        self.target = self.root / "runtime"
        self.source.mkdir()
        self.target.mkdir()
        for index, name in enumerate(STATE_FILES):
            (self.source / name).write_text("state-%d" % index, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_copies_only_declared_state_files_and_is_idempotent(self):
        first = migrate_state(self.source, self.target)
        self.assertEqual([row[1] for row in first], ["COPIED"] * len(STATE_FILES))
        for name in STATE_FILES:
            self.assertEqual(
                (self.target / name).read_text(encoding="utf-8"),
                (self.source / name).read_text(encoding="utf-8"),
            )
            self.assertEqual((self.target / name).stat().st_mode & 0o777, 0o660)
        second = migrate_state(self.source, self.target)
        self.assertEqual([row[1] for row in second], ["UNCHANGED"] * len(STATE_FILES))

    def test_refuses_to_overwrite_different_destination(self):
        (self.target / STATE_FILES[0]).write_text("newer", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "refusing overwrite"):
            migrate_state(self.source, self.target)

    def test_refuses_symlink_source(self):
        source = self.source / STATE_FILES[0]
        replacement = self.source / "real-state"
        source.rename(replacement)
        source.symlink_to(replacement)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            migrate_state(self.source, self.target)

    def test_refuses_nested_source_and_target(self):
        nested = self.source / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(RuntimeError, "separate"):
            migrate_state(self.source, nested)

    def test_imports_legacy_disabled_accounts_without_overwriting_cli_state(self):
        state_file = self.target / "account-rotation.json"
        state_file.write_text(json.dumps({
            "cursor": 0,
            "unavailable": {
                "acct4": {
                    "reason": "manually disabled by CLI",
                    "at": "2026-11-28 00:00:00",
                    "epoch": 1,
                    "ttl": 999999999,
                }
            },
        }), encoding="utf-8")
        legacy_db = self.root / "sender.sqlite3"
        conn = sqlite3.connect(legacy_db)
        conn.execute(
            "CREATE TABLE account_health (account TEXT PRIMARY KEY, "
            "state TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO account_health VALUES (?, ?, ?, ?)",
            [
                ("acct4", "auth_unavailable", "stale token", "2026-11-28T00:00:00Z"),
                ("acct5", "operator_disabled", "operator disabled", "2026-11-28T00:00:00Z"),
            ],
        )
        conn.commit()
        conn.close()

        imported = merge_legacy_account_health(self.target, legacy_db)
        self.assertEqual(imported, [("acct5", "operator_disabled")])
        rotation = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(
            rotation["unavailable"]["acct4"]["reason"],
            "manually disabled by CLI",
        )
        self.assertIn(
            "legacy Gateway operator_disabled",
            rotation["unavailable"]["acct5"]["reason"],
        )
        self.assertGreater(
            rotation["unavailable"]["acct5"]["ttl"],
            365 * 24 * 60 * 60,
        )

        check = sqlite3.connect("file:" + str(legacy_db) + "?mode=ro", uri=True)
        self.assertEqual(check.execute("SELECT count(*) FROM account_health").fetchone()[0], 2)
        check.close()


if __name__ == "__main__":
    unittest.main()
