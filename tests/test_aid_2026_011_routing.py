import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "chatgpt_send.py"

_spec = importlib.util.spec_from_file_location(
    "chatgpt_send_aid_2026_011_routing_test",
    MODULE_PATH,
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Unable to load scripts/chatgpt_send.py")

_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


class RoutingTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.registry_path = Path(self.tempdir.name) / "conv-accounts.json"
        self.registry_path.write_text("{}", encoding="utf-8")
        self.registry_patch = mock.patch.object(
            _module,
            "CONV_MAP_FILE",
            str(self.registry_path),
        )
        self.registry_patch.start()

    def tearDown(self):
        self.registry_patch.stop()
        self.tempdir.cleanup()

    def write_registry(self, value):
        self.registry_path.write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_account_identity_validation(self):
        self.assertTrue(_module.validate_account_identity("default"))
        self.assertTrue(_module.validate_account_identity("acct1"))
        self.assertTrue(_module.validate_account_identity("acct2"))
        self.assertTrue(_module.validate_account_identity("acct3"))
        self.assertTrue(_module.validate_account_identity("acct4"))
        self.assertTrue(_module.validate_account_identity("nologin"))

        self.assertFalse(_module.validate_account_identity("unknown"))
        self.assertFalse(_module.validate_account_identity(""))
        self.assertFalse(_module.validate_account_identity(None))
        self.assertFalse(_module.validate_account_identity(1))

    def test_default_and_acct1_are_aliases(self):
        self.assertEqual(
            _module.normalize_account_identity("default"),
            _module.normalize_account_identity("acct1"),
        )

    def test_known_structured_record(self):
        self.write_registry({
            "conv-known": {
                "account": "acct2",
                "prompt": "prompt.md",
                "ts": "2026-11-20T00:00:00Z",
            }
        })

        self.assertEqual(
            _module.lookup_conversation_owner("conv-known"),
            {"status": "known", "account": "acct2"},
        )

    def test_missing_ts_does_not_invalidate_record(self):
        self.write_registry({
            "conv-no-ts": {
                "account": "acct3",
                "note": "legacy record without timestamp",
                "prompt": "prompt.md",
            }
        })

        self.assertEqual(
            _module.lookup_conversation_owner("conv-no-ts"),
            {"status": "known", "account": "acct3"},
        )

    def test_legacy_bare_string_is_invalid(self):
        self.write_registry({
            "conv-legacy": "acct2",
        })

        self.assertEqual(
            _module.lookup_conversation_owner("conv-legacy"),
            {"status": "invalid", "account": None},
        )

    def test_owner_and_account_same_identity_is_known(self):
        self.write_registry({
            "conv-owner-same": {
                "account": "acct1",
                "owner": "default",
                "prompt": "prompt.md",
                "ts": "2026-11-20T00:00:00Z",
            }
        })

        self.assertEqual(
            _module.lookup_conversation_owner("conv-owner-same"),
            {"status": "known", "account": "acct1"},
        )

    def test_owner_and_account_disagreement_is_conflict(self):
        self.write_registry({
            "conv-owner-conflict": {
                "account": "acct1",
                "owner": "acct2",
                "prompt": "prompt.md",
                "ts": "2026-11-20T00:00:00Z",
            }
        })

        self.assertEqual(
            _module.lookup_conversation_owner("conv-owner-conflict"),
            {"status": "conflict", "account": None},
        )

    def test_invalid_account_in_record_is_invalid(self):
        self.write_registry({
            "conv-bad-account": {
                "account": "not-an-account",
                "prompt": "prompt.md",
            }
        })

        self.assertEqual(
            _module.lookup_conversation_owner("conv-bad-account"),
            {"status": "invalid", "account": None},
        )

    def test_non_dict_registry_is_invalid(self):
        self.write_registry([])

        self.assertEqual(
            _module.lookup_conversation_owner("conv-any"),
            {"status": "invalid", "account": None},
        )

    def test_missing_registry_entry_is_unknown(self):
        self.write_registry({})

        self.assertEqual(
            _module.lookup_conversation_owner("conv-unknown"),
            {"status": "unknown", "account": None},
        )

    def test_missing_registry_file_is_unknown(self):
        self.registry_path.unlink()

        self.assertEqual(
            _module.lookup_conversation_owner("conv-unknown"),
            {"status": "unknown", "account": None},
        )

    def test_malformed_registry_json_is_invalid(self):
        self.registry_path.write_text("{broken", encoding="utf-8")

        self.assertEqual(
            _module.lookup_conversation_owner("conv-any"),
            {"status": "invalid", "account": None},
        )

    def test_invalid_conversation_id_is_invalid(self):
        self.assertEqual(
            _module.lookup_conversation_owner("../../bad"),
            {"status": "invalid", "account": None},
        )

    def test_known_owner_routes_automatically(self):
        self.write_registry({
            "conv-known": {"account": "acct2"}
        })

        self.assertEqual(
            _module.resolve_conversation_account("conv-known"),
            {"status": "known", "account": "acct2"},
        )

    def test_unknown_owner_without_explicit_account_fails_closed(self):
        self.write_registry({})

        self.assertEqual(
            _module.resolve_conversation_account("conv-unknown"),
            {"status": "unknown", "account": None},
        )

    def test_unknown_owner_with_explicit_valid_account_is_permitted(self):
        self.write_registry({})

        self.assertEqual(
            _module.resolve_conversation_account(
                "conv-unknown",
                "acct3",
            ),
            {"status": "unknown", "account": "acct3"},
        )

    def test_known_owner_matching_explicit_account_is_permitted(self):
        self.write_registry({
            "conv-known": {"account": "acct2"}
        })

        self.assertEqual(
            _module.resolve_conversation_account(
                "conv-known",
                "acct2",
            ),
            {"status": "known", "account": "acct2"},
        )

    def test_known_owner_and_explicit_alias_are_compatible(self):
        self.write_registry({
            "conv-known": {"account": "default"}
        })

        self.assertEqual(
            _module.resolve_conversation_account(
                "conv-known",
                "acct1",
            ),
            {"status": "known", "account": "acct1"},
        )

    def test_known_owner_and_explicit_different_account_conflict(self):
        self.write_registry({
            "conv-known": {"account": "acct2"}
        })

        self.assertEqual(
            _module.resolve_conversation_account(
                "conv-known",
                "acct1",
            ),
            {"status": "conflict", "account": None},
        )

    def test_invalid_explicit_account_is_invalid(self):
        self.write_registry({})

        self.assertEqual(
            _module.resolve_conversation_account(
                "conv-unknown",
                "not-an-account",
            ),
            {"status": "invalid", "account": None},
        )

    def test_owner_cli_known_json_and_exit_code(self):
        self.write_registry({
            "conv-known": {"account": "acct2"}
        })

        payload, exit_code = _module.conversation_owner_cli_result(
            "conv-known"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload,
            {
                "conv_id": "conv-known",
                "account": "acct2",
                "known": True,
                "status": "known",
            },
        )

    def test_owner_cli_unknown_json_and_exit_code(self):
        self.write_registry({})

        payload, exit_code = _module.conversation_owner_cli_result(
            "conv-unknown"
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload,
            {
                "conv_id": "conv-unknown",
                "account": None,
                "known": False,
                "status": "unknown",
            },
        )

    def test_owner_cli_invalid_id_does_not_echo_input(self):
        payload, exit_code = _module.conversation_owner_cli_result(
            "../../secret"
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload,
            {
                "conv_id": None,
                "account": None,
                "known": False,
                "status": "invalid",
            },
        )

    def test_owner_cli_invalid_record_and_exit_code(self):
        self.write_registry({
            "conv-invalid": "acct2"
        })

        payload, exit_code = _module.conversation_owner_cli_result(
            "conv-invalid"
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload,
            {
                "conv_id": "conv-invalid",
                "account": None,
                "known": False,
                "status": "invalid",
            },
        )

    def test_owner_cli_conflict_does_not_select_either_account(self):
        self.write_registry({
            "conv-conflict": {
                "account": "acct1",
                "owner": "acct2",
            }
        })

        payload, exit_code = _module.conversation_owner_cli_result(
            "conv-conflict"
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload,
            {
                "conv_id": "conv-conflict",
                "account": None,
                "known": False,
                "status": "conflict",
            },
        )


if __name__ == "__main__":
    unittest.main()
