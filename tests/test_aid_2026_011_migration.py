import contextlib
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
    "chatgpt_send_aid_2026_011_migration",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ConversationRegistryMigrationTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = os.path.join(self.temp.name, "conv-accounts.json")
        self.patch_path = mock.patch.object(
            MODULE, "CONV_MAP_FILE", self.registry
        )
        self.patch_path.start()

    def tearDown(self):
        self.patch_path.stop()
        self.temp.cleanup()

    def write_registry(self, data):
        with open(self.registry, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def read_registry(self):
        with open(self.registry, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_migrate_legacy_registry_to_canonical(self):
        legacy = {
            "6afe889e": "acct2",
            "6afeb34e": "acct2",
            "6afeb9ae": "acct2",
            "6afeb7d8": "acct2",
            "6aff0b47": "acct1",
            "6afff365": "acct1",
            "6afff5a9": "acct2",
        }
        self.write_registry(legacy)

        result = MODULE.migrate_conv_accounts()
        actual = self.read_registry()

        self.assertEqual(result["migrated"], 7)
        self.assertTrue(result["changed"])

        for conv_id, account in legacy.items():
            self.assertEqual(
                actual[conv_id],
                {
                    "account": account,
                    "prompt": None,
                    "ts": None,
                },
            )

    def test_migrate_preserves_account_ownership(self):
        original = {
            "a1": "acct1",
            "a2": "acct2",
            "a3": "acct3",
            "a4": "acct4",
            "an": "nologin",
            "ad": "default",
        }
        self.write_registry(original)

        MODULE.migrate_conv_accounts()
        actual = self.read_registry()

        for conv_id, account in original.items():
            self.assertEqual(actual[conv_id]["account"], account)

    def test_migrate_preserves_existing_metadata(self):
        original = {
            "a1": {
                "account": "acct1",
                "prompt": "PROMPT-A",
                "ts": "2026-11-20 00:32",
                "note": "parallel workspace",
                "owner": "parallel-l0",
            },
            "a2": {
                "account": "acct2",
                "prompt": "PROMPT-B",
                "note": "historical record",
            },
        }
        self.write_registry(original)

        MODULE.migrate_conv_accounts()

        self.assertEqual(self.read_registry(), original)

    def test_migrate_does_not_invent_metadata(self):
        self.write_registry({
            "legacy": "acct2",
            "missing-ts": {
                "account": "acct3",
                "prompt": "PROMPT-C",
                "note": "historical",
            },
        })

        MODULE.migrate_conv_accounts()
        actual = self.read_registry()

        self.assertEqual(
            actual["legacy"],
            {
                "account": "acct2",
                "prompt": None,
                "ts": None,
            },
        )
        self.assertNotIn("ts", actual["missing-ts"])

    def test_migrate_is_idempotent(self):
        self.write_registry({
            "legacy": "acct2",
            "existing": {
                "account": "acct3",
                "prompt": "P",
                "ts": "2026-11-20 00:00",
            },
        })

        first = MODULE.migrate_conv_accounts()
        snapshot = self.read_registry()

        second = MODULE.migrate_conv_accounts()
        final = self.read_registry()

        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(second["migrated"], 0)
        self.assertEqual(snapshot, final)

    def test_migrate_is_atomic(self):
        original = {"legacy": "acct2"}
        self.write_registry(original)

        with mock.patch.object(
            MODULE, "_atomic_write", side_effect=OSError("simulated failure")
        ):
            with self.assertRaises(OSError):
                MODULE.migrate_conv_accounts()

        self.assertEqual(self.read_registry(), original)

    def test_migrate_uses_cross_process_lock(self):
        self.write_registry({"legacy": "acct2"})

        original_lock = MODULE._file_lock
        observed = []

        @contextlib.contextmanager
        def tracked_lock(*args, **kwargs):
            observed.append((args, kwargs))
            with original_lock(*args, **kwargs):
                yield

        with mock.patch.object(MODULE, "_file_lock", tracked_lock):
            MODULE.migrate_conv_accounts()

        self.assertTrue(observed)
        self.assertTrue(
            any(
                kwargs.get("strict") is True
                for _, kwargs in observed
            )
        )

    def test_migrate_preserves_invalid_record_diagnostic(self):
        original = {
            "bad-account": {
                "account": "not-configured",
                "prompt": "P",
            },
            "bad-shape": [1, 2, 3],
            "bad-id!": "acct2",
        }
        self.write_registry(original)

        result = MODULE.migrate_conv_accounts()
        actual = self.read_registry()

        self.assertEqual(actual, original)
        self.assertEqual(len(result["invalid"]), 3)
        self.assertFalse(result["changed"])

    def test_record_conv_rejects_account_ownership_overwrite(self):
        self.write_registry({
            "conversation": {
                "account": "acct2",
                "prompt": "OLD",
                "ts": "2026-11-19 00:00",
                "note": "must survive",
            }
        })

        with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
            MODULE.record_conv("conversation", "acct1", "NEW")

        actual = self.read_registry()["conversation"]

        self.assertEqual(actual["account"], "acct2")
        self.assertEqual(actual["prompt"], "OLD")
        self.assertEqual(actual["note"], "must survive")

    # Q1: owner is free-form metadata, not an account identity.
    def test_parallel_owner_metadata_does_not_invalidate_account(self):
        self.write_registry({
            "parallel": {
                "account": "acct1",
                "prompt": "RELAY-REQ01-CONTINUE-002",
                "ts": "2026-11-20 00:32",
                "note": "parallel instance",
                "owner": "parallel-l0",
            }
        })

        result = MODULE.lookup_conversation_owner("parallel")

        self.assertEqual(
            result,
            {"status": "known", "account": "acct1"},
        )

    def test_record_conv_preserves_free_form_owner_metadata(self):
        self.write_registry({
            "parallel": {
                "account": "acct1",
                "prompt": "OLD",
                "ts": "2026-11-19 00:00",
                "owner": "parallel-l0",
                "note": "preserve",
            }
        })

        MODULE.record_conv("parallel", "acct1", "NEW")

        record = self.read_registry()["parallel"]
        self.assertEqual(record["owner"], "parallel-l0")
        self.assertEqual(record["note"], "preserve")
        self.assertEqual(record["prompt"], "NEW")

    # Q2: nologin remains a distinct valid ownership identity.
    def test_nologin_migration_preserves_anonymous_identity(self):
        self.write_registry({"anonymous": "nologin"})

        MODULE.migrate_conv_accounts()

        record = self.read_registry()["anonymous"]
        self.assertEqual(record["account"], "nologin")

        result = MODULE.lookup_conversation_owner("anonymous")
        self.assertEqual(
            result,
            {"status": "known", "account": "nologin"},
        )

        self.assertNotEqual(
            MODULE.normalize_account_identity("nologin"),
            MODULE.normalize_account_identity("acct1"),
        )

    # Q3: default remains stored verbatim but compares as acct1.
    def test_default_migration_preserves_storage_and_comparison(self):
        self.write_registry({"historical": "default"})

        MODULE.migrate_conv_accounts()

        record = self.read_registry()["historical"]
        self.assertEqual(record["account"], "default")

        result = MODULE.lookup_conversation_owner("historical")
        self.assertEqual(result["account"], "default")

        self.assertEqual(
            MODULE.normalize_account_identity(result["account"]),
            "acct1",
        )

    def test_atomic_write_conv_map_uses_locked_save(self):
        data = {"a": {"account": "acct1"}}

        with mock.patch.object(
            MODULE, "_save_json", wraps=MODULE._save_json
        ) as save:
            MODULE.atomic_write_conv_map(data)

        save.assert_called_once_with(MODULE.CONV_MAP_FILE, data)
        self.assertEqual(self.read_registry(), data)

    def test_malformed_null_record_is_not_overwritten(self):
        original = {"conversation": None}
        self.write_registry(original)

        with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
            MODULE.record_conv("conversation", "acct1", "NEW")

        self.assertIsNone(self.read_registry()["conversation"])


if __name__ == "__main__":
    unittest.main()
