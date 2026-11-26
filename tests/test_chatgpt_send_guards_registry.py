"""Guard-level tests for the conversation registry and batch registration.

Each test targets one defensive branch in scripts/chatgpt_send.py.  The
branch is expected to fail closed with an exact exception type and a stable
message fragment; replacing that branch with ``pass`` must make at least one
test in this module fail.
"""

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
    "chatgpt_send_registry_guards_test_module",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


CID_A = "6afe889e-abf8-83e8-8e65-20ad6f7129e3"
CID_B = "6afeb34e-abf8-83e8-8e65-20ad6f7129e3"


def _record(account="acct1", **extra):
    record = {"account": account, "prompt": None, "ts": None}
    record.update(extra)
    return record


class RegistryTestCase(unittest.TestCase):
    """Shared temporary registry and module import fixture."""

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

    def write_registry(self, data):
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def read_registry(self):
        with open(self.registry_path, "r", encoding="utf-8") as f:
            return json.load(f)


class RegistryWriteGuardTests(RegistryTestCase):
    def test_atomic_write_rejects_non_dictionary_payload(self):
        # guard: atomic_write_conv_map() rejects a non-dict payload.
        with self.assertRaisesRegex(
            ValueError,
            "conversation registry must be a dictionary",
        ):
            MODULE.atomic_write_conv_map([])

    def test_migration_read_rejects_non_object_root(self):
        # guard: _read_conv_map_for_migration() refuses a non-object root.
        self.write_registry([])

        with self.assertRaisesRegex(
            ValueError,
            "conversation registry root must be an object",
        ):
            MODULE.migrate_conv_accounts()

    def test_record_conv_rejects_non_dictionary_loaded_state(self):
        # guard: record_conv() mutator rejects a non-dict loaded registry.
        with mock.patch.object(
            MODULE,
            "_load_json",
            return_value=[],
        ):
            with self.assertRaisesRegex(
                ValueError,
                "conversation registry must be a dictionary",
            ):
                MODULE.record_conv(CID_A, "acct1", None)


class BatchRegistrationGuardTests(RegistryTestCase):
    def setUp(self):
        super().setUp()
        self.write_registry({})

    def test_batch_rejects_non_sequence_registrations(self):
        # guard: registrations must be a list or tuple.
        with self.assertRaisesRegex(
            TypeError,
            "registrations must be a list or tuple",
        ):
            MODULE.register_conversations_batch({"not": "a-list"})

    def test_batch_rejects_non_dictionary_registration(self):
        # guard: every registration item must be a dictionary.
        with self.assertRaisesRegex(
            ValueError,
            "registration must be a dictionary",
        ):
            MODULE.register_conversations_batch(["not-a-dict"])

    def test_batch_rejects_invalid_conversation_id(self):
        # guard: invalid conversation id is wrapped in a batch error.
        with self.assertRaisesRegex(
            ValueError,
            "invalid conversation id in batch",
        ):
            MODULE.register_conversations_batch([
                {"id": "bad/id", "account": "acct1"},
            ])

    def test_batch_rejects_invalid_account_identity(self):
        # guard: invalid account identity is rejected before locking.
        with self.assertRaisesRegex(
            ValueError,
            "invalid account identity in batch",
        ):
            MODULE.register_conversations_batch([
                {"id": CID_A, "account": "not-an-account"},
            ])

    def test_batch_rejects_non_dictionary_registry(self):
        # guard: a non-dict registry read inside the lock is rejected.
        with mock.patch.object(
            MODULE,
            "_read_conv_map_for_migration",
            return_value=[],
        ):
            with self.assertRaisesRegex(
                ValueError,
                "conversation registry must be a dictionary",
            ):
                MODULE.register_conversations_batch([
                    {"id": CID_A, "account": "acct1"},
                ])

    def test_batch_rejects_malformed_existing_record(self):
        # guard: existing non-dict, non-string records are rejected.
        self.write_registry({CID_A: 123})

        with self.assertRaisesRegex(
            ValueError,
            "registry record format is invalid",
        ):
            MODULE.register_conversations_batch([
                {"id": CID_A, "account": "acct1"},
            ])

    def test_batch_rejects_invalid_existing_account(self):
        # guard: existing records with an invalid account are rejected.
        self.write_registry({CID_A: {"account": "not-an-account"}})

        with self.assertRaisesRegex(
            ValueError,
            "registry record account is invalid",
        ):
            MODULE.register_conversations_batch([
                {"id": CID_A, "account": "acct1"},
            ])

    def test_batch_rejects_non_dictionary_post_write_registry(self):
        # guard: the post-write registry read must be a dictionary.
        with mock.patch.object(
            MODULE,
            "_read_conv_map_for_migration",
            side_effect=[{}, []],
        ):
            with self.assertRaisesRegex(
                ValueError,
                "conversation registry is not a dictionary after write",
            ):
                MODULE.register_conversations_batch([
                    {"id": CID_A, "account": "acct1"},
                ])

    def test_batch_rejects_non_dictionary_readback_record(self):
        # guard: each read-back record must be a dictionary.
        with mock.patch.object(
            MODULE,
            "_read_conv_map_for_migration",
            side_effect=[{}, {CID_A: "acct1"}],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "batch registration verification failed",
            ):
                MODULE.register_conversations_batch([
                    {"id": CID_A, "account": "acct1"},
                ])

    def test_batch_rejects_readback_ownership_mismatch(self):
        # guard: the read-back account must match the requested owner.
        with mock.patch.object(
            MODULE,
            "_read_conv_map_for_migration",
            side_effect=[{}, {CID_A: {"account": "acct2"}}],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "batch registration ownership verification failed",
            ):
                MODULE.register_conversations_batch([
                    {"id": CID_A, "account": "acct1"},
                ])

    def test_batch_rejects_readback_owner_conflict(self):
        # guard: the read-back owner must not conflict with its account.
        with mock.patch.object(
            MODULE,
            "_read_conv_map_for_migration",
            side_effect=[
                {},
                {CID_A: {"account": "acct2", "owner": "acct1"}},
            ],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "batch registration owner verification failed",
            ):
                MODULE.register_conversations_batch([
                    {"id": CID_A, "account": "acct2"},
                ])


class RestoreGuardTests(RegistryTestCase):
    def setUp(self):
        super().setUp()
        self.write_registry({})

    def test_restore_rejects_non_dictionary_candidate(self):
        # guard: the restore candidate must itself be a dictionary.
        with self.assertRaisesRegex(
            ValueError,
            "restore candidate must be a dictionary",
        ):
            MODULE.restore_conv_map_minimal([])

    def test_restore_rejects_non_dictionary_live_registry(self):
        # guard: the live registry read must be a dictionary.
        with mock.patch.object(
            MODULE,
            "_load_json",
            return_value=[],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "live conversation registry is not a dictionary",
            ):
                MODULE.restore_conv_map_minimal({})

    def test_restore_rejects_quarantine_during_verification(self):
        # guard: a registry that disappears on read-back aborts restore.
        real_load_json = MODULE._load_json
        calls = {"count": 0}

        def quarantine_on_verify(path, default):
            calls["count"] += 1
            if calls["count"] == 1:
                return real_load_json(path, default)
            os.remove(path)
            return {}

        with mock.patch.object(
            MODULE,
            "_load_json",
            side_effect=quarantine_on_verify,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "registry was quarantined during restore verification",
            ):
                MODULE.restore_conv_map_minimal({CID_A: _record()})

    def test_restore_rejects_non_dictionary_readback(self):
        # guard: the read-back registry must be a dictionary.
        with mock.patch.object(
            MODULE,
            "_load_json",
            side_effect=[{}, []],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "registry read-back is not a dictionary",
            ):
                MODULE.restore_conv_map_minimal({CID_A: _record()})

    def test_restore_rejects_readback_mismatch(self):
        # guard: the read-back registry must equal the merged registry.
        with mock.patch.object(
            MODULE,
            "_load_json",
            side_effect=[{}, {CID_B: _record("acct2")}],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "registry read-back mismatch",
            ):
                MODULE.restore_conv_map_minimal({CID_A: _record()})

    def test_restore_rejects_missing_added_conversation(self):
        # guard: every added conversation must survive the read-back.
        class _EqualityBlindRegistry(dict):
            def __eq__(self, other):
                return True

            def __ne__(self, other):
                return False

            def __contains__(self, key):
                return False

        with mock.patch.object(
            MODULE,
            "_load_json",
            side_effect=[{}, _EqualityBlindRegistry()],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "restored conversation missing after read-back",
            ):
                MODULE.restore_conv_map_minimal({CID_A: _record()})


if __name__ == "__main__":
    unittest.main()
