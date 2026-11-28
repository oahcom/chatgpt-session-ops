from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "deploy/source-snapshot/2026-11-28/so-sender"
PATCH = ROOT / "patches/sender-account-identity-pin.patch"


class SenderAccountIdentityPinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="sender-account-pin-", dir=str(ROOT))
        cls.root = Path(cls.temp.name)
        cls.cli_dir = cls.root / "so-sender/cli"
        cls.gateway_dir = cls.root / "so-sender/sender_gateway"
        cls.cli_dir.mkdir(parents=True)
        cls.gateway_dir.mkdir(parents=True)
        shutil.copy2(SNAPSHOT / "cli/chatgpt_send.py", cls.cli_dir / "chatgpt_send.py")
        shutil.copy2(SNAPSHOT / "cli/pow_solver.py", cls.cli_dir / "pow_solver.py")
        (cls.gateway_dir / "__init__.py").write_text("")
        shutil.copy2(SNAPSHOT / "sender_gateway/sender_adapter.py", cls.gateway_dir / "sender_adapter.py")

        # Discovery uses filenames only; these are empty test markers, not credentials.
        cls.accounts = cls.root / "accounts"
        for name in ("acct1", "acct2", "acct10"):
            account_dir = cls.accounts / name
            account_dir.mkdir(parents=True)
            (account_dir / "cookies.json").write_text("{}")
            (account_dir / "access_token.txt").write_text("")

        cls.old_env = {
            key: os.environ.get(key)
            for key in ("SO_SENDER_ACCOUNTS_DIR", "CHATGPT_ALLOWED_ACCOUNTS", "TMPDIR")
        }
        os.environ["TMPDIR"] = str(cls.root)
        dry_run = subprocess.run(
            ["patch", "--dry-run", "-p1", "-i", str(PATCH)],
            cwd=cls.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if dry_run.returncode != 0:
            raise AssertionError(f"patch --dry-run failed: {dry_run.stdout}\n{dry_run.stderr}")
        applied = subprocess.run(
            ["patch", "-p1", "-i", str(PATCH)],
            cwd=cls.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if applied.returncode != 0:
            raise AssertionError(f"patch apply failed: {applied.stdout}\n{applied.stderr}")

        account_only_patch = ROOT / "patches/sender-account-only-adapter.patch"
        dry_run = subprocess.run(
            ["patch", "--dry-run", "-p1", "-i", str(account_only_patch)],
            cwd=cls.root, text=True, capture_output=True, check=False,
        )
        if dry_run.returncode != 0:
            raise AssertionError(
                f"account-only patch --dry-run failed: {dry_run.stdout}\n{dry_run.stderr}"
            )
        applied = subprocess.run(
            ["patch", "-p1", "-i", str(account_only_patch)],
            cwd=cls.root, text=True, capture_output=True, check=False,
        )
        if applied.returncode != 0:
            raise AssertionError(
                f"account-only patch apply failed: {applied.stdout}\n{applied.stderr}"
            )

        os.environ["SO_SENDER_ACCOUNTS_DIR"] = str(cls.accounts)
        sys.path.insert(0, str(cls.cli_dir.parent))
        spec = importlib.util.spec_from_file_location(
            "sender_cli_account_pin_test", cls.cli_dir / "chatgpt_send.py"
        )
        cls.cli = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.cli
        spec.loader.exec_module(cls.cli)

        from sender_gateway.sender_adapter import ChatGPTSenderAdapter, SenderError
        cls.adapter_type = ChatGPTSenderAdapter
        cls.sender_error_type = SenderError

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.path.remove(str(cls.cli_dir.parent))
        cls.temp.cleanup()

    def setUp(self):
        self.old_argv = sys.argv[:]
        self.old_cli_globals = {
            name: getattr(self.cli, name, None)
            for name in (
                "ChatGPTAPI", "send_prompt", "record_conv", "create_chat_rotating", "_cli_open_api",
                "load_rotation", "purge_expired", "is_unavailable", "rotation_plan",
                "_effective_rotation_primary", "_attempt_create", "_mark_success",
            )
        }

    def tearDown(self):
        sys.argv[:] = self.old_argv
        for name, value in self.old_cli_globals.items():
            setattr(self.cli, name, value)

    def run_cli_new(self, account, status=200):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct1,acct2"
        api_calls = []
        request_calls = []
        rotate = Mock(return_value=1)

        def fake_api(*, account):
            api_calls.append(account)
            return SimpleNamespace(account=account)

        def fake_send(api, _target, _prompt, **kwargs):
            request_calls.append(api.account)
            return status, None, str(uuid.uuid4()) if status == 200 else None

        self.cli.ChatGPTAPI = fake_api
        self.cli.send_prompt = fake_send
        self.cli.record_conv = Mock()
        self.cli.create_chat_rotating = rotate
        sys.argv[:] = ["chatgpt_send.py", "--account", account, "--new", "prompt.txt"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._main_unlocked()
        return result, api_calls, request_calls, rotate, output.getvalue()

    def test_disabled_acct5_is_rejected_before_api_construction_or_request(self):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct1,acct2"
        api = Mock()
        request = Mock()
        self.cli.ChatGPTAPI = api
        self.cli.send_prompt = request
        sys.argv[:] = ["chatgpt_send.py", "--account", "acct5", "--new", "prompt.txt"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._main_unlocked()
        self.assertEqual(result, 2)
        api.assert_not_called()
        request.assert_not_called()
        self.assertIn("invalid_account_identity", output.getvalue())

    def test_discovered_acct10_passes_when_present_in_effective_allowlist(self):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct10"
        api_calls = []
        requests = []

        def fake_api(*, account):
            api_calls.append(account)
            return SimpleNamespace(account=account)

        def fake_send(api, _target, _prompt, **kwargs):
            requests.append(api.account)
            return 200, None, str(uuid.uuid4())

        self.cli.ChatGPTAPI = fake_api
        self.cli.send_prompt = fake_send
        self.cli.record_conv = Mock()
        sys.argv[:] = ["chatgpt_send.py", "--account", "acct10", "--new", "prompt.txt"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._main_unlocked()
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(api_calls, ["acct10"])
        self.assertEqual(requests, ["acct10"])

    def test_account_only_acct5_rejected_before_open_api(self):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct1,acct2"
        open_api = Mock()
        self.cli._cli_open_api = open_api
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._cli_account_only_create("acct5", "prompt.txt")
        self.assertEqual(result, 2)
        open_api.assert_not_called()
        self.assertIn('"reason": "invalid_account"', output.getvalue())

    def test_account_only_acct10_allowed_and_constructs_api(self):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct10"
        api_calls = []
        api = SimpleNamespace(last_headers={"x-adapter-account": "acct10"})

        def fake_api(*, account):
            api_calls.append(account)
            return api

        self.cli.ChatGPTAPI = fake_api
        self.cli.send_prompt = Mock(return_value=(200, None, str(uuid.uuid4())))
        self.cli.record_conv = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._cli_account_only_create("acct10", "prompt.txt")
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(api_calls, ["acct10"])
        self.cli.send_prompt.assert_called_once()
        self.cli.record_conv.assert_called_once()
        payload = __import__("json").loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["event"], "create_result")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["account"], "acct10")
        self.assertRegex(payload["conversation_id"], r"^[0-9a-f-]{36}$")

    def test_account_only_echo_mismatch_preserves_created_conversation_identity(self):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct1,acct2"
        conv_id = str(uuid.uuid4())
        api = SimpleNamespace(last_headers={"x-adapter-account": "acct2"})
        self.cli.ChatGPTAPI = Mock(return_value=api)
        self.cli.send_prompt = Mock(return_value=(200, None, conv_id))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._cli_account_only_create("acct1", "prompt.txt")
        self.assertEqual(result, 1)
        payload = __import__("json").loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["reason"], "account_echo_mismatch")
        self.assertEqual(payload["conversation_id"], conv_id)
        self.assertEqual(payload["actual_account"], "acct2")

    def test_account_only_acct1_429_never_rotates_to_acct2(self):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct1,acct2"
        api = SimpleNamespace(last_headers={})
        self.cli.ChatGPTAPI = Mock(return_value=api)
        self.cli.send_prompt = Mock(return_value=(429, None, None))
        rotate = Mock(return_value=0)
        self.cli.create_chat_rotating = rotate
        self.cli._cli_retry_after_seconds = Mock(return_value=19)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._cli_account_only_create("acct1", "prompt.txt")
        self.assertEqual(result, 1)
        self.cli.ChatGPTAPI.assert_called_once_with(account="acct1")
        self.cli.send_prompt.assert_called_once()
        rotate.assert_not_called()
        payload = __import__("json").loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["http_status"], 429)
        self.assertEqual(payload["retry_after_seconds"], 19)
        self.assertNotIn("acct2", output.getvalue())

    def test_adapter_uses_account_only_argv_and_parses_create_result(self):
        adapter = object.__new__(self.adapter_type)
        prompt_root = self.root / "adapter-prompts"
        prompt_root.mkdir(exist_ok=True)
        prompt = prompt_root / "prompt.txt"
        prompt.write_text("ping")
        adapter._prompt_root = str(prompt_root)
        argv = adapter._build_argv("create", account="acct10", prompt_path=str(prompt))
        self.assertEqual(argv, ("--new", "--account-only", "acct10", str(prompt)))

        conv_id = str(uuid.uuid4())
        result = SimpleNamespace(
            stdout=__import__("json").dumps({
                "event": "create_result", "ok": True, "account": "acct10",
                "conversation_id": conv_id, "http_status": 200, "reason": None,
            }) + "\n",
            exit_code=0, stderr="",
        )
        self.assertEqual(
            adapter._parse_create(result, expected_account="acct10"),
            (conv_id, "acct10", None),
        )

    def test_adapter_preserves_account_only_429_retry_after(self):
        adapter = object.__new__(self.adapter_type)
        result = SimpleNamespace(
            stdout=__import__("json").dumps({
                "event": "create_result", "ok": False, "reason": "rate_limited",
                "account": "acct1", "http_status": 429, "retry_after_seconds": 23,
            }) + "\n",
            exit_code=1, stderr="",
        )
        with self.assertRaises(self.sender_error_type) as caught:
            adapter._parse_create(result, expected_account="acct1")
        self.assertEqual(caught.exception.code, "RESOURCE_EXHAUSTED")
        self.assertEqual(caught.exception.upstream_status, 429)
        self.assertEqual(caught.exception.retry_after_seconds, 23)

    def test_explicit_acct1_403_fails_without_acct2_attempt(self):
        result, api_calls, requests, rotate, output = self.run_cli_new("acct1", 403)
        self.assertEqual(result, 1)
        self.assertEqual(api_calls, ["acct1"])
        self.assertEqual(requests, ["acct1"])
        rotate.assert_not_called()
        self.assertNotIn("NEW_CONV", output)
        self.assertNotIn("account=acct2", output)

    def test_explicit_acct1_429_fails_without_acct2_attempt(self):
        result, api_calls, requests, rotate, output = self.run_cli_new("acct1", 429)
        self.assertEqual(result, 1)
        self.assertEqual(api_calls, ["acct1"])
        self.assertEqual(requests, ["acct1"])
        rotate.assert_not_called()
        self.assertNotIn("NEW_CONV", output)
        self.assertNotIn("account=acct2", output)

    def test_auto_create_retains_rotation_to_acct2_after_acct1_403_or_429(self):
        original_effective = self.cli._effective_rotation_primary
        self.cli.purge_expired = lambda _state: False
        self.cli.is_unavailable = lambda _state, _name: False
        self.cli.rotation_plan = lambda _state: ["acct1", "acct2"]
        self.cli._effective_rotation_primary = lambda: ("acct1", "acct2")

        for first_status in (403, 429):
            with self.subTest(first_status=first_status):
                state = {"cursor": 0, "unavailable": {}}
                attempts = []

                def attempt(_state, name, _prompt, _attempts, _thinking):
                    attempts.append(name)
                    return (first_status, None) if name == "acct1" else (200, "conv-acct2")

                self.cli.load_rotation = lambda: state
                self.cli._attempt_create = attempt
                self.cli._mark_success = Mock()
                result = self.cli.create_chat_rotating("prompt.txt")
                self.assertEqual(result, 0)
                self.assertEqual(attempts, ["acct1", "acct2"])
                self.cli._mark_success.assert_called_once()
                self.assertEqual(self.cli._mark_success.call_args.args[1], "acct2")

        self.cli._effective_rotation_primary = original_effective

    def test_adapter_rejects_mismatched_account_result(self):
        adapter = object.__new__(self.adapter_type)
        result = SimpleNamespace(
            stdout=f"NEW_CONV {uuid.uuid4()} account=acct2\n",
            exit_code=0,
            stderr="",
        )
        with self.assertRaises(self.sender_error_type):
            adapter._parse_create(result, expected_account="acct1")


if __name__ == "__main__":
    unittest.main()
