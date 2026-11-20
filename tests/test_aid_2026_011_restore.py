import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "chatgpt_send.py"

spec = importlib.util.spec_from_file_location(
    "chatgpt_send_restore_test",
    str(MODULE_PATH),
)
MODULE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MODULE)


CID_A = "6afe889e-abf8-83e8-8e65-20ad6f7129e3"
CID_B = "6afeb34e-abf8-83e8-8e65-20ad6f7129e3"
CID_C = "6afeb9ae-abf8-83e8-8e65-20ad6f7129e3"


class RestoreConvMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = os.path.join(
            self.tmp.name,
            "conv-accounts.json",
        )
        self.path_patch = mock.patch.object(
            MODULE,
            "CONV_MAP_FILE",
            self.registry,
        )
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def write_registry(self, data):
        with open(self.registry, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def read_registry(self):
        with open(self.registry, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def record(account, ts="2026-11-20T20:00:00"):
        return {
            "account": account,
            "prompt": "test",
            "ts": ts,
        }

    def test_restore_adds_missing_candidate_records(self):
        self.write_registry({CID_A: self.record("acct1")})
        candidate = {
            CID_A: self.record("acct1", "older"),
            CID_B: self.record("acct2"),
        }

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added"], [CID_B])
        self.assertEqual(self.read_registry()[CID_B]["account"], "acct2")

    def test_restore_preserves_existing_records(self):
        original = self.record("acct1", "live-value")
        self.write_registry({CID_A: original})
        candidate = {CID_A: self.record("acct1", "candidate-value")}

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added_count"], 0)
        self.assertEqual(self.read_registry()[CID_A], original)

    def test_restore_preserves_concurrent_existing_update(self):
        # The candidate is an older snapshot. The live record is authoritative.
        live_record = self.record("acct1", "updated-after-candidate")
        self.write_registry({CID_A: live_record})
        candidate = {CID_A: self.record("acct1", "old-candidate")}

        MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(self.read_registry()[CID_A], live_record)

    def test_restore_normalizes_legacy_string_record(self):
        self.write_registry({})
        candidate = {CID_A: "acct2"}

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added_count"], 1)
        self.assertEqual(
            self.read_registry()[CID_A],
            {
                "account": "acct2",
                "prompt": None,
                "ts": None,
            },
        )

    def test_restore_rejects_invalid_legacy_string(self):
        self.write_registry({})
        candidate = {CID_A: "not-an-account"}

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["conflict_count"], 1)
        self.assertEqual(self.read_registry(), {})

    def test_restore_rejects_invalid_account_record(self):
        self.write_registry({})
        candidate = {
            CID_A: {
                "account": "not-an-account",
                "prompt": None,
                "ts": None,
            }
        }

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added_count"], 0)
        self.assertEqual(
            result["conflicts"][0]["reason"],
            "invalid_account",
        )

    def test_restore_rejects_owner_account_conflict(self):
        self.write_registry({})
        candidate = {
            CID_A: {
                "account": "acct1",
                "owner": "acct2",
                "prompt": None,
                "ts": None,
            }
        }

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added_count"], 0)
        self.assertEqual(
            result["conflicts"][0]["reason"],
            "owner_account_conflict",
        )
        self.assertEqual(self.read_registry(), {})

    def test_restore_treats_non_account_owner_as_metadata(self):
        self.write_registry({})
        candidate = {
            CID_A: {
                "account": "acct1",
                "owner": "parallel-l0",
                "prompt": None,
                "ts": None,
            }
        }

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added_count"], 1)
        self.assertEqual(
            self.read_registry()[CID_A]["owner"],
            "parallel-l0",
        )

    def test_restore_does_not_overwrite_live_owner_conflict(self):
        live = {
            "account": "acct2",
            "prompt": None,
            "ts": "live",
        }
        self.write_registry({CID_A: live})
        candidate = {CID_A: self.record("acct1")}

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added_count"], 0)
        self.assertEqual(
            result["conflicts"][0]["reason"],
            "live_candidate_account_conflict",
        )
        self.assertEqual(self.read_registry()[CID_A], live)

    def test_restore_uses_one_atomic_write(self):
        self.write_registry({})
        candidate = {
            CID_A: self.record("acct1"),
            CID_B: self.record("acct2"),
        }

        original = MODULE._atomic_write
        calls = []

        def counted_write(path, content):
            calls.append((path, content))
            return original(path, content)

        with mock.patch.object(
            MODULE,
            "_atomic_write",
            side_effect=counted_write,
        ):
            result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["added_count"], 2)
        self.assertEqual(len(calls), 1)

    def test_restore_holds_registry_lock_during_write(self):
        self.write_registry({})
        candidate = {CID_A: self.record("acct1")}
        real_lock = MODULE._file_lock
        events = []

        class LockTracker:
            def __init__(self, *args, **kwargs):
                self.inner = real_lock(*args, **kwargs)

            def __enter__(self):
                events.append("lock_enter")
                return self.inner.__enter__()

            def __exit__(self, exc_type, exc, tb):
                events.append("lock_exit")
                return self.inner.__exit__(exc_type, exc, tb)

        real_write = MODULE._atomic_write

        def tracked_write(path, content):
            events.append("write")
            return real_write(path, content)

        with mock.patch.object(
            MODULE,
            "_file_lock",
            side_effect=lambda *a, **kw: LockTracker(*a, **kw),
        ), mock.patch.object(
            MODULE,
            "_atomic_write",
            side_effect=tracked_write,
        ):
            MODULE.restore_conv_map_minimal(candidate)

        self.assertLess(events.index("lock_enter"), events.index("write"))
        self.assertLess(events.index("write"), events.index("lock_exit"))

    def test_restore_aborts_if_registry_is_quarantined(self):
        # Invalid JSON triggers _load_json quarantine. Restore must not
        # interpret its fallback {} as an empty registry.
        with open(self.registry, "w", encoding="utf-8") as f:
            f.write("{invalid json")
        candidate = {CID_A: self.record("acct1")}

        with self.assertRaises(RuntimeError):
            MODULE.restore_conv_map_minimal(candidate)

        self.assertFalse(os.path.exists(self.registry))

    def test_restore_does_not_prune_above_500(self):
        existing = {
            "conv-%04d" % i: self.record("acct1")
            for i in range(510)
        }
        self.write_registry(existing)
        candidate = {
            CID_A: self.record("acct2"),
        }

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["final_count"], 511)
        self.assertEqual(result["pruned"], 0)

    def test_restore_readback_mismatch_fails_closed(self):
        self.write_registry({})
        candidate = {CID_A: self.record("acct1")}
        real_load = MODULE._load_json
        calls = {"count": 0}

        def mismatched_load(path, default):
            calls["count"] += 1
            data = real_load(path, default)
            if calls["count"] >= 2:
                return {}
            return data

        with mock.patch.object(
            MODULE,
            "_load_json",
            side_effect=mismatched_load,
        ):
            with self.assertRaises(RuntimeError):
                MODULE.restore_conv_map_minimal(candidate)

    def test_restore_counts_invalid_candidate_ids(self):
        self.write_registry({})
        candidate = {
            "bad/id/with/slashes": self.record("acct1"),
            CID_A: self.record("acct2"),
        }

        result = MODULE.restore_conv_map_minimal(candidate)

        self.assertEqual(result["invalid_ids"], 1)
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(len(self.read_registry()), 1)

    def test_restore_preserves_existing_legacy_live_record(self):
        self.write_registry({CID_A: "acct1"})
        candidate = {CID_A: self.record("acct1")}

        MODULE.restore_conv_map_minimal(candidate)

        # Existing entries are never rewritten, even if legacy-shaped.
        self.assertEqual(self.read_registry()[CID_A], "acct1")


if __name__ == "__main__":
    unittest.main()
