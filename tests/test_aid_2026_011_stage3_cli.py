import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "chatgpt_send.py")

SPEC = importlib.util.spec_from_file_location(
    "chatgpt_send_stage3_cli_test_module",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeAPI:
    def __init__(self, account):
        self.account = account
        self.is_anon = account == "nologin"
        self.quota = {}

        self.tail_calls = []
        self.list_calls = 0

    def live_tail(self, conv_id, tries=2):
        self.tail_calls.append((conv_id, tries))
        return ("tail text", "raw mapping")

    def harvest_mapping(self, raw):
        return "harvested mapping"

    def list_conversations(self):
        self.list_calls += 1
        return ([], 200, None)


class Stage3CLITestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.registry_path = os.path.join(
            self.tempdir.name,
            "conv-accounts.json",
        )
        self.registry_patch = mock.patch.object(
            MODULE,
            "CONV_MAP_FILE",
            self.registry_path,
        )
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.addCleanup(self.tempdir.cleanup)

        self.api_instances = []
        self.factory_calls = []

    def write_registry(self, data):
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def api_factory(self, account=None):
        self.factory_calls.append(account)
        api = FakeAPI(account)
        self.api_instances.append(api)
        return api

    def run_main(self, argv, send_prompt_result=(200, None, "conv-1")):
        output = io.StringIO()

        with (
            mock.patch.object(MODULE, "ChatGPTAPI", side_effect=self.api_factory),
            mock.patch.object(
                MODULE,
                "send_prompt",
                return_value=send_prompt_result,
            ) as send_prompt,
            mock.patch.object(
                MODULE.sys,
                "argv",
                ["chatgpt_send.py"] + list(argv),
            ),
            contextlib.redirect_stdout(output),
        ):
            rc = MODULE.main()

        return rc, output.getvalue(), send_prompt

    def test_tail_auto_routes_to_registered_owner(self):
        self.write_registry({
            "conv-1": {
                "account": "acct2",
                "prompt": "prompt.md",
                "ts": "2026-11-20 00:00",
            }
        })

        rc, output, _ = self.run_main(["--tail", "conv-1"])

        self.assertEqual(rc, 0)
        self.assertEqual(self.factory_calls, ["acct2"])
        self.assertEqual(len(self.api_instances), 1)
        self.assertEqual(
            self.api_instances[0].tail_calls,
            [("conv-1", 2)],
        )
        self.assertIn("tail:", output)

    def test_delivery_auto_routes_to_registered_owner(self):
        self.write_registry({
            "conv-1": {
                "account": "acct2",
                "prompt": "prompt.md",
                "ts": "2026-11-20 00:00",
            }
        })

        rc, _, send_prompt = self.run_main(["conv-1", "next.md"])

        self.assertEqual(rc, 0)
        self.assertEqual(self.factory_calls, ["acct2"])
        send_prompt.assert_called_once()
        args, _kwargs = send_prompt.call_args
        self.assertIs(args[0], self.api_instances[0])
        self.assertEqual(args[1], "conv-1")
        self.assertEqual(args[2], "next.md")

    def test_unknown_owner_does_not_fallback_to_acct1(self):
        self.write_registry({})

        rc, output, _ = self.run_main(["conv-unknown", "next.md"])

        self.assertEqual(rc, 2)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.api_instances, [])
        self.assertIn("ownership is unknown", output)

    def test_unknown_owner_explicit_account_is_allowed(self):
        self.write_registry({})

        rc, _, send_prompt = self.run_main([
            "--account",
            "acct2",
            "conv-unknown",
            "next.md",
        ])

        self.assertEqual(rc, 0)
        self.assertEqual(self.factory_calls, ["acct2"])
        send_prompt.assert_called_once()

    def test_explicit_matching_owner_is_allowed(self):
        self.write_registry({
            "conv-1": {
                "account": "acct2",
                "prompt": "prompt.md",
                "ts": "2026-11-20 00:00",
            }
        })

        rc, _, send_prompt = self.run_main([
            "--account",
            "acct2",
            "conv-1",
            "next.md",
        ])

        self.assertEqual(rc, 0)
        self.assertEqual(self.factory_calls, ["acct2"])
        send_prompt.assert_called_once()

    def test_explicit_mismatching_owner_hard_fails(self):
        self.write_registry({
            "conv-1": {
                "account": "acct2",
                "prompt": "prompt.md",
                "ts": "2026-11-20 00:00",
            }
        })

        rc, output, _ = self.run_main([
            "--account",
            "acct1",
            "conv-1",
            "next.md",
        ])

        self.assertEqual(rc, 2)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.api_instances, [])
        self.assertIn("conflicts", output)

    def test_invalid_owner_hard_fails(self):
        self.write_registry({
            "conv-1": {
                "account": "not-an-account",
                "prompt": "prompt.md",
                "ts": "2026-11-20 00:00",
            }
        })

        rc, output, _ = self.run_main(["conv-1", "next.md"])

        self.assertEqual(rc, 2)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.api_instances, [])
        self.assertIn("invalid", output)

    def test_conflicting_owner_hard_fails(self):
        self.write_registry({
            "conv-1": {
                "account": "acct1",
                "owner": "acct2",
                "prompt": "prompt.md",
                "ts": "2026-11-20 00:00",
            }
        })

        rc, output, _ = self.run_main(["conv-1", "next.md"])

        self.assertEqual(rc, 2)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.api_instances, [])
        self.assertIn("conflicts", output)

    def test_routing_fails_before_api_build(self):
        self.write_registry({})

        rc, output, _ = self.run_main(["conv-unknown", "next.md"])

        self.assertEqual(rc, 2)
        self.assertIn("ownership is unknown", output)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.api_instances, [])

    def test_owner_reports_legacy_string_as_invalid(self):
        self.write_registry({"legacy-conv": "acct2"})

        payload, exit_code = MODULE.conversation_owner_cli_result(
            "legacy-conv"
        )

        self.assertEqual(
            payload,
            {
                "conv_id": "legacy-conv",
                "account": None,
                "known": False,
                "status": "invalid",
            },
        )
        self.assertEqual(exit_code, 2)

    def test_new_conversation_still_uses_default_account(self):
        rotated = []

        with (
            mock.patch.object(
                MODULE,
                "create_chat_rotating",
                side_effect=lambda *args, **kwargs:
                    rotated.append((args, kwargs)) or 0,
            ),
            mock.patch.object(
                MODULE,
                "ChatGPTAPI",
                side_effect=self.api_factory,
            ),
            mock.patch.object(
                MODULE.sys,
                "argv",
                ["chatgpt_send.py", "--new", "prompt.md"],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rc = MODULE.main()

        self.assertEqual(rc, 0)
        self.assertEqual(
            rotated,
            [ (("prompt.md",), {"thinking": None}) ],
        )
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.api_instances, [])

    def test_list_is_account_scoped(self):
        self.write_registry({})

        rc, output, _ = self.run_main([
            "--account",
            "acct2",
            "--list",
        ])

        self.assertEqual(rc, 0)
        self.assertEqual(self.factory_calls, ["acct2"])
        self.assertEqual(len(self.api_instances), 1)
        self.assertEqual(self.api_instances[0].list_calls, 1)

        payload = json.loads(output)
        self.assertEqual(payload["account"], "acct2")
        self.assertEqual(payload["status"], 200)
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["conversations"], [])


if __name__ == "__main__":
    unittest.main()
