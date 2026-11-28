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
        for source in (SNAPSHOT / "sender_gateway").glob("*.py"):
            shutil.copy2(source, cls.gateway_dir / source.name)

        # Discovery uses filenames only; these are empty test markers, not credentials.
        cls.accounts = cls.root / "accounts"
        for name in ("acct1", "acct2", "acct10"):
            account_dir = cls.accounts / name
            account_dir.mkdir(parents=True)
            (account_dir / "cookies.json").write_text("[]")
            (account_dir / "access_token.txt").write_text("synthetic-token")

        cls.old_env = {
            key: os.environ.get(key)
            for key in ("SO_SENDER_ACCOUNTS_DIR", "CHATGPT_AUTH_DIR", "CHATGPT_ALLOWED_ACCOUNTS", "TMPDIR")
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

        resolver_patch = ROOT / "patches/sender-account-directory-contract.patch"
        dry_run = subprocess.run(
            ["patch", "--dry-run", "-p1", "-i", str(resolver_patch)],
            cwd=cls.root, text=True, capture_output=True, check=False,
        )
        if dry_run.returncode != 0:
            raise AssertionError(
                f"directory contract patch --dry-run failed: {dry_run.stdout}\n{dry_run.stderr}"
            )
        applied = subprocess.run(
            ["patch", "-p1", "-i", str(resolver_patch)],
            cwd=cls.root, text=True, capture_output=True, check=False,
        )
        if applied.returncode != 0:
            raise AssertionError(
                f"directory contract patch apply failed: {applied.stdout}\n{applied.stderr}"
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
        from sender_gateway.account_router import discover_accounts as gateway_discover_accounts
        cls.adapter_type = ChatGPTSenderAdapter
        cls.sender_error_type = SenderError
        cls.gateway_discover_accounts = gateway_discover_accounts

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.modules.pop("sender_gateway.sender_adapter", None)
        sys.modules.pop("sender_gateway.account_router", None)
        sys.modules.pop("sender_gateway", None)
        sys.path.remove(str(cls.cli_dir.parent))
        cls.temp.cleanup()

    def setUp(self):
        self.old_argv = sys.argv[:]
        self.old_path_env = {
            key: os.environ.get(key)
            for key in ("SO_SENDER_ACCOUNTS_DIR", "CHATGPT_AUTH_DIR", "CHATGPT_ALLOWED_ACCOUNTS")
        }
        self.old_cli_globals = {
            name: getattr(self.cli, name, None)
            for name in (
                "ChatGPTAPI", "_cli_open_api", "send_prompt", "record_conv", "create_chat_rotating",
                "load_rotation", "purge_expired", "is_unavailable", "rotation_plan",
                "_effective_rotation_primary", "_attempt_create", "_mark_success",
            )
        }

    def tearDown(self):
        sys.argv[:] = self.old_argv
        for name, value in self.old_cli_globals.items():
            setattr(self.cli, name, value)
        for key, value in self.old_path_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

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

    @staticmethod
    def _write_synthetic_credentials(path, token):
        path.mkdir(parents=True, exist_ok=True)
        (path / "cookies.json").write_text("[]")
        (path / "access_token.txt").write_text(token)

    def _construct_api_for_path_contract(self, account, auth_root):
        old_helpers = (
            self.cli.resolve_proxy_info, self.cli.require_proxy, self.cli.build_opener_for
        )
        self.cli.resolve_proxy_info = lambda: {"source": "isolated-test"}
        self.cli.require_proxy = lambda: "http://127.0.0.1:1"
        self.cli.build_opener_for = lambda _proxy, _ctx: object()
        try:
            return self.cli.ChatGPTAPI(
                base_dir=str(auth_root) if auth_root is not None else None, account=account
            )
        finally:
            (
                self.cli.resolve_proxy_info, self.cli.require_proxy, self.cli.build_opener_for
            ) = old_helpers

    def test_shared_path_contract_acct1_symlink_and_acct2_plus(self):
        auth_root = self.root / "contract-symlink-root"
        accounts_dir = auth_root / "accounts"
        self._write_synthetic_credentials(auth_root, "acct1-root-token")
        self._write_synthetic_credentials(accounts_dir / "acct2", "acct2-token")
        self._write_synthetic_credentials(accounts_dir / "acct10", "acct10-token")
        accounts_dir.mkdir(parents=True, exist_ok=True)
        (accounts_dir / "acct1").symlink_to(auth_root, target_is_directory=True)

        os.environ["CHATGPT_AUTH_DIR"] = str(auth_root)
        os.environ["SO_SENDER_ACCOUNTS_DIR"] = str(accounts_dir)
        expected = ("acct1", "acct2", "acct10")
        self.assertEqual(
            type(self).gateway_discover_accounts(accounts_dir, auth_root=auth_root), expected
        )
        self.assertTrue(expected[0] in self.cli._discover_account_identities())
        resolver = self.cli.AccountDirectoryResolver.from_environment(base_dir=self.cli.BASE)
        self.assertEqual(resolver.discover_accounts(), expected)

        api1 = self._construct_api_for_path_contract("acct1", auth_root)
        api_default = self._construct_api_for_path_contract("default", auth_root)
        api2 = self._construct_api_for_path_contract("acct2", auth_root)
        api10 = self._construct_api_for_path_contract("acct10", auth_root)
        self.assertEqual(Path(api1.base_dir), auth_root)
        self.assertEqual(Path(api_default.base_dir), Path(api1.base_dir))
        self.assertEqual(api_default.token, api1.token)
        self.assertEqual(api1.token, "acct1-root-token")
        self.assertEqual(Path(api2.base_dir), accounts_dir / "acct2")
        self.assertEqual(api2.token, "acct2-token")
        self.assertEqual(Path(api10.base_dir), accounts_dir / "acct10")
        self.assertEqual(api10.token, "acct10-token")

    def test_shared_path_contract_legacy_base_accounts_layout(self):
        auth_root = self.root / "contract-legacy-base"
        accounts_dir = auth_root / "accounts"
        self._write_synthetic_credentials(accounts_dir / "acct1", "acct1-legacy-token")
        self._write_synthetic_credentials(accounts_dir / "acct2", "acct2-legacy-token")
        os.environ["CHATGPT_AUTH_DIR"] = str(auth_root)
        os.environ["SO_SENDER_ACCOUNTS_DIR"] = str(accounts_dir)

        expected = ("acct1", "acct2")
        self.assertEqual(
            type(self).gateway_discover_accounts(accounts_dir, auth_root=auth_root), expected
        )
        self.assertEqual(
            self.cli.AccountDirectoryResolver.from_environment(base_dir=self.cli.BASE).discover_accounts(),
            expected,
        )
        api1 = self._construct_api_for_path_contract("acct1", auth_root)
        api_default = self._construct_api_for_path_contract("default", auth_root)
        api2 = self._construct_api_for_path_contract("acct2", auth_root)
        self.assertEqual(Path(api1.base_dir), accounts_dir / "acct1")
        self.assertEqual(Path(api_default.base_dir), Path(api1.base_dir))
        self.assertEqual(api_default.token, api1.token)
        self.assertEqual(api1.token, "acct1-legacy-token")
        self.assertEqual(Path(api2.base_dir), accounts_dir / "acct2")
        self.assertEqual(api2.token, "acct2-legacy-token")

    def test_shared_path_contract_custom_accounts_root_without_auth_env(self):
        custom_root = self.root / "custom-accounts-root"
        self._write_synthetic_credentials(custom_root, "acct1-custom-token")
        self._write_synthetic_credentials(custom_root / "acct2", "acct2-custom-token")
        os.environ.pop("CHATGPT_AUTH_DIR", None)
        os.environ["SO_SENDER_ACCOUNTS_DIR"] = str(custom_root)

        expected = ("acct1", "acct2")
        self.assertEqual(
            type(self).gateway_discover_accounts(custom_root, auth_root=custom_root), expected
        )
        resolver = self.cli.AccountDirectoryResolver.from_environment(base_dir=self.cli.BASE)
        self.assertEqual(resolver.discover_accounts(), expected)
        api1 = self._construct_api_for_path_contract("acct1", None)
        api2 = self._construct_api_for_path_contract("acct2", None)
        self.assertEqual(Path(api1.base_dir), custom_root)
        self.assertEqual(api1.token, "acct1-custom-token")
        self.assertEqual(Path(api2.base_dir), custom_root / "acct2")
        self.assertEqual(api2.token, "acct2-custom-token")

    def test_shared_path_contract_cli_base_accounts_fallback(self):
        base_dir = Path(self.cli.BASE)
        accounts_dir = base_dir / "accounts"
        self._write_synthetic_credentials(accounts_dir / "acct1", "acct1-base-token")
        self._write_synthetic_credentials(accounts_dir / "acct2", "acct2-base-token")
        os.environ.pop("CHATGPT_AUTH_DIR", None)
        os.environ.pop("SO_SENDER_ACCOUNTS_DIR", None)

        expected = ("acct1", "acct2")
        self.assertEqual(
            type(self).gateway_discover_accounts(accounts_dir, auth_root=base_dir), expected
        )
        self.assertEqual(
            self.cli.AccountDirectoryResolver.from_environment(base_dir=self.cli.BASE).discover_accounts(),
            expected,
        )
        api1 = self._construct_api_for_path_contract("acct1", base_dir)
        api_default = self._construct_api_for_path_contract("default", base_dir)
        api2 = self._construct_api_for_path_contract("acct2", base_dir)
        self.assertEqual(Path(api1.base_dir), accounts_dir / "acct1")
        self.assertEqual(Path(api_default.base_dir), Path(api1.base_dir))
        self.assertEqual(api1.token, "acct1-base-token")
        self.assertEqual(Path(api2.base_dir), accounts_dir / "acct2")
        self.assertEqual(api2.token, "acct2-base-token")

    def test_account_only_helper_rejects_acct5_before_open_api(self):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct1,acct2"
        open_api = Mock()
        self.cli._cli_open_api = open_api
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._cli_account_only_create("acct5", "prompt.txt")
        self.assertEqual(result, 2)
        open_api.assert_not_called()
        payload = __import__("json").loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["event"], "create_result")
        self.assertEqual(payload["reason"], "invalid_account_identity")

    def test_account_only_helper_allows_discovered_acct10_after_allowlist(self):
        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = "acct10"
        api = SimpleNamespace(last_headers={"x-adapter-account": "acct10"})
        self.cli._cli_open_api = Mock(return_value=(api, None))
        self.cli.send_prompt = Mock(return_value=(200, None, str(uuid.uuid4())))
        self.cli.record_conv = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.cli._cli_account_only_create("acct10", "prompt.txt")
        self.assertEqual(result, 0, output.getvalue())
        self.cli._cli_open_api.assert_called_once_with("acct10")
        self.cli.send_prompt.assert_called_once()
        payload = __import__("json").loads(output.getvalue().strip().splitlines()[-1])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["account"], "acct10")

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

    def test_adapter_classifies_account_only_429_and_preserves_retry_after(self):
        adapter = object.__new__(self.adapter_type)
        payload = {
            "event": "create_result",
            "ok": False,
            "reason": "rate_limited",
            "account": "acct1",
            "http_status": 429,
            "retry_after_seconds": 37,
        }
        result = SimpleNamespace(
            stdout=__import__("json").dumps(payload) + "\n",
            exit_code=1,
            stderr="",
            operation="create",
        )
        with self.assertRaises(self.sender_error_type) as raised:
            adapter._parse_create(result, expected_account="acct1")
        error = raised.exception
        self.assertEqual(error.code, "RESOURCE_EXHAUSTED")
        self.assertEqual(error.upstream_status, 429)
        self.assertEqual(error.retry_after_seconds, 37)
        self.assertEqual(error.operation, "create")

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
