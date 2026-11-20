import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "chatgpt_send.py")

SPEC = importlib.util.spec_from_file_location(
    "chatgpt_send_batch_registration_test_module",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ConversationBatchRegistrationTestCase(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.registry_path = os.path.join(
            self.temp_dir.name,
            "conv-accounts.json",
        )

        self.path_patcher = mock.patch.object(
            MODULE,
            "CONV_MAP_FILE",
            self.registry_path,
        )
        self.path_patcher.start()
        self.addCleanup(self.path_patcher.stop)

        self.write_registry({})

    def write_registry(self, data):
        with open(
            self.registry_path,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def read_registry(self):
        with open(
            self.registry_path,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)

    def test_batch_adds_all_records_with_one_atomic_write(self):
        registrations = [
            {"id": "batch-a", "account": "acct1"},
            {"id": "batch-b", "account": "acct2"},
            {"id": "batch-c", "account": "acct3"},
        ]

        with mock.patch.object(
            MODULE,
            "_atomic_write",
            wraps=MODULE._atomic_write,
        ) as atomic_write, mock.patch.object(
            MODULE,
            "_now",
            return_value="2026-11-20 21:00:00",
        ):
            registered = MODULE.register_conversations_batch(
                registrations
            )

        self.assertEqual(atomic_write.call_count, 1)
        self.assertEqual(
            registered,
            [
                {"id": "batch-a", "account": "acct1"},
                {"id": "batch-b", "account": "acct2"},
                {"id": "batch-c", "account": "acct3"},
            ],
        )

        registry = self.read_registry()
        self.assertEqual(len(registry), 3)

        for item in registrations:
            record = registry[item["id"]]
            self.assertEqual(record["account"], item["account"])
            self.assertIsNone(record["prompt"])
            self.assertEqual(record["ts"], "2026-11-20 21:00:00")

    def test_existing_matching_record_is_preserved_without_write(self):
        existing = {
            "account": "acct1",
            "prompt": "existing-prompt.md",
            "ts": "2026-01-01 00:00:00",
            "custom": {"keep": True},
        }
        self.write_registry({"existing-id": existing})

        with mock.patch.object(
            MODULE,
            "_atomic_write",
            wraps=MODULE._atomic_write,
        ) as atomic_write:
            registered = MODULE.register_conversations_batch([
                {"id": "existing-id", "account": "acct1"},
            ])

        atomic_write.assert_not_called()
        self.assertEqual(
            registered,
            [{"id": "existing-id", "account": "acct1"}],
        )
        self.assertEqual(
            self.read_registry()["existing-id"],
            existing,
        )

    def test_conflict_rejects_entire_batch_before_write(self):
        self.write_registry({
            "conflict-id": {"account": "acct2"},
        })

        before = self.read_registry()

        with mock.patch.object(
            MODULE,
            "_atomic_write",
            wraps=MODULE._atomic_write,
        ) as atomic_write:
            with self.assertRaisesRegex(
                ValueError,
                "registry account conflict",
            ):
                MODULE.register_conversations_batch([
                    {"id": "new-id", "account": "acct1"},
                    {"id": "conflict-id", "account": "acct1"},
                ])

        atomic_write.assert_not_called()
        self.assertEqual(self.read_registry(), before)

    def test_owner_account_conflict_rejects_entire_batch(self):
        self.write_registry({
            "owner-conflict": {
                "account": "acct1",
                "owner": "acct2",
            },
        })

        before = self.read_registry()

        with mock.patch.object(
            MODULE,
            "_atomic_write",
            wraps=MODULE._atomic_write,
        ) as atomic_write:
            with self.assertRaisesRegex(
                ValueError,
                "registry owner/account conflict",
            ):
                MODULE.register_conversations_batch([
                    {"id": "new-id", "account": "acct1"},
                    {"id": "owner-conflict", "account": "acct1"},
                ])

        atomic_write.assert_not_called()
        self.assertEqual(self.read_registry(), before)

    def test_legacy_string_record_is_rejected_before_write(self):
        self.write_registry({
            "legacy-id": "acct1",
        })

        before = self.read_registry()

        with mock.patch.object(
            MODULE,
            "_atomic_write",
            wraps=MODULE._atomic_write,
        ) as atomic_write:
            with self.assertRaisesRegex(
                ValueError,
                "legacy string registry record requires migration",
            ):
                MODULE.register_conversations_batch([
                    {"id": "new-id", "account": "acct1"},
                    {"id": "legacy-id", "account": "acct1"},
                ])

        atomic_write.assert_not_called()
        self.assertEqual(self.read_registry(), before)

    def test_identical_duplicate_input_is_deduplicated(self):
        with mock.patch.object(
            MODULE,
            "_atomic_write",
            wraps=MODULE._atomic_write,
        ) as atomic_write:
            registered = MODULE.register_conversations_batch([
                {"id": "same-id", "account": "acct1"},
                {"id": "same-id", "account": "acct1"},
            ])

        self.assertEqual(atomic_write.call_count, 1)
        self.assertEqual(
            registered,
            [{"id": "same-id", "account": "acct1"}],
        )
        self.assertEqual(len(self.read_registry()), 1)

    def test_duplicate_input_with_different_accounts_is_rejected(self):
        before = self.read_registry()

        with mock.patch.object(
            MODULE,
            "_atomic_write",
            wraps=MODULE._atomic_write,
        ) as atomic_write:
            with self.assertRaisesRegex(
                ValueError,
                "duplicate batch id has conflicting accounts",
            ):
                MODULE.register_conversations_batch([
                    {"id": "same-id", "account": "acct1"},
                    {"id": "same-id", "account": "acct2"},
                ])

        atomic_write.assert_not_called()
        self.assertEqual(self.read_registry(), before)

    def test_invalid_candidate_rejects_batch_before_write(self):
        before = self.read_registry()

        with mock.patch.object(
            MODULE,
            "_atomic_write",
            wraps=MODULE._atomic_write,
        ) as atomic_write:
            with self.assertRaises(ValueError):
                MODULE.register_conversations_batch([
                    {"id": "valid-id", "account": "acct1"},
                    {"id": "invalid-id", "account": "not-an-account"},
                ])

        atomic_write.assert_not_called()
        self.assertEqual(self.read_registry(), before)

    def test_held_lock_raises_locktimeout_without_writing(self):
        """锁被占用时必须抛 LockTimeout，绝不无锁提交。"""
        self.write_registry({"keep-id": {"account": "acct1"}})
        before = self.read_registry()

        thread_lock = MODULE._thread_lock_for(self.registry_path)
        thread_lock.acquire()
        try:
            with mock.patch.object(
                MODULE,
                "LOCK_TIMEOUT_SEC",
                0.2,
            ), mock.patch.object(
                MODULE,
                "_atomic_write",
                wraps=MODULE._atomic_write,
            ) as atomic_write:
                with self.assertRaises(MODULE.LockTimeout):
                    MODULE.register_conversations_batch([
                        {"id": "new-id", "account": "acct2"},
                    ])
        finally:
            thread_lock.release()

        atomic_write.assert_not_called()
        self.assertEqual(self.read_registry(), before)


if __name__ == "__main__":
    unittest.main()
