from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMP_ROOT = Path.home() / ".worktrees"


def _purge_sender_gateway_modules():
    for module_name in tuple(sys.modules):
        if module_name == "sender_gateway" or module_name.startswith("sender_gateway."):
            sys.modules.pop(module_name, None)

SNAPSHOT = ROOT / "deploy/source-snapshot/2026-11-28/so-sender"
PATCH = ROOT / "patches/sender-child-env-account-roots.patch"


class SenderChildAccountRootsTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="sender-child-roots-", dir=str(TEMP_ROOT))
        self.root = Path(self.temp.name)
        self.old_env = {
            key: os.environ.get(key)
            for key in ("SO_SENDER_ACCOUNTS_DIR", "CHATGPT_AUTH_DIR", "CHATGPT_PROXY", "TMPDIR")
        }
        # patch(1) and tempfile must use this worktree, not the quota-limited /tmp tmpfs.
        os.environ["TMPDIR"] = str(self.root)
        self.cli_dir = self.root / "so-sender/cli"
        self.gateway_dir = self.root / "so-sender/sender_gateway"
        self.cli_dir.mkdir(parents=True)
        self.gateway_dir.mkdir(parents=True)
        shutil.copy2(SNAPSHOT / "cli/chatgpt_send.py", self.cli_dir / "chatgpt_send.py")
        shutil.copy2(SNAPSHOT / "cli/pow_solver.py", self.cli_dir / "pow_solver.py")
        for source in (SNAPSHOT / "sender_gateway").glob("*.py"):
            shutil.copy2(source, self.gateway_dir / source.name)
        applied = subprocess.run(
            ["patch", "--dry-run", "-p1", "-i", str(PATCH)],
            cwd=self.root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        applied = subprocess.run(
            ["patch", "-p1", "-i", str(PATCH)],
            cwd=self.root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        resolver_patch = ROOT / "patches/sender-account-directory-contract.patch"
        applied = subprocess.run(
            ["patch", "--dry-run", "-p1", "-i", str(resolver_patch)],
            cwd=self.root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        applied = subprocess.run(
            ["patch", "-p1", "-i", str(resolver_patch)],
            cwd=self.root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)

        self.auth_root = self.root / "auth-root"
        self.accounts_root = self.auth_root / "accounts"
        self.auth_root.mkdir(parents=True, exist_ok=True)
        # This child-env contract test is intentionally acct10-only. acct1's
        # legacy auth-root mapping is covered separately; no acct1 symlink or
        # marker is introduced into this dynamic acctN closure fixture.
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        auth_account = self.accounts_root / "acct10"
        auth_account.mkdir(parents=True)
        (auth_account / "cookies.json").write_text("[]")
        (auth_account / "access_token.txt").write_text("fixture-acct10-token-not-a-secret")

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_child_receives_both_configured_roots_and_cli_uses_them(self):
        os.environ["SO_SENDER_ACCOUNTS_DIR"] = str(self.accounts_root)
        os.environ["CHATGPT_AUTH_DIR"] = str(self.auth_root)
        os.environ["CHATGPT_PROXY"] = "http://must-not-be-inherited.invalid"

        sys.path.insert(0, str(self.root / "so-sender"))
        try:
            _purge_sender_gateway_modules()
            from sender_gateway.account_router import discover_accounts
            from sender_gateway.sender_adapter import ChatGPTSenderAdapter, _discover_logged_in_accounts
            gateway_accounts = discover_accounts(self.accounts_root, auth_root=self.auth_root)
            self.assertEqual(gateway_accounts, ("acct10",))
            # The legacy Adapter discovery set includes built-in acct1-acct6;
            # assert the dynamic acct10 identity is added, not that defaults vanish.
            self.assertIn("acct10", _discover_logged_in_accounts())

            adapter = ChatGPTSenderAdapter(
                str(self.cli_dir / "chatgpt_send.py"),
                cwd=str(self.root),
                prompt_dir=str(self.root / "prompts"),
            )
            try:
                child_env = adapter._child_env()
            finally:
                adapter.close()
        finally:
            _purge_sender_gateway_modules()
            sys.path.remove(str(self.root / "so-sender"))

        self.assertEqual(child_env["SO_SENDER_ACCOUNTS_DIR"], str(self.accounts_root))
        self.assertEqual(child_env["CHATGPT_AUTH_DIR"], str(self.auth_root))
        self.assertNotIn("CHATGPT_PROXY", child_env)

        script = r'''
import importlib.util
import json
from pathlib import Path
p = Path(__import__("os").environ["TEST_CLI_PATH"])
spec = importlib.util.spec_from_file_location("child_cli_roots_test", p)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
cli.resolve_proxy_info = lambda: {"source": "isolated-test"}
cli.require_proxy = lambda: "http://127.0.0.1:8080"
cli.build_opener_for = lambda *args, **kwargs: object()
acct10 = cli.ChatGPTAPI(account="acct10")
print(json.dumps({
    "acct1_identity_discovered": "acct1" in cli._VALID_ACCOUNT_IDENTITIES,
    "acct10_identity_discovered": "acct10" in cli._VALID_ACCOUNT_IDENTITIES,
    "acct10_auth_root_used": acct10.base_dir,
    "acct10_token_loaded": acct10.token == "fixture-acct10-token-not-a-secret",
}))
'''
        child_env["TEST_CLI_PATH"] = str(self.cli_dir / "chatgpt_send.py")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            env=child_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertFalse(result["acct1_identity_discovered"])
        self.assertTrue(result["acct10_identity_discovered"])
        self.assertEqual(
            result["acct10_auth_root_used"],
            str(self.auth_root / "accounts/acct10"),
        )
        self.assertTrue(result["acct10_token_loaded"])

    def test_gateway_discovery_rejects_symlink_account_dirs_and_credential_files(self):
        sys.path.insert(0, str(self.root / "so-sender"))
        try:
            _purge_sender_gateway_modules()
            from sender_gateway.account_directory import discover_accounts
        finally:
            _purge_sender_gateway_modules()
            sys.path.remove(str(self.root / "so-sender"))

        symlink_dir = self.accounts_root / "acct11"
        symlink_dir.symlink_to(self.accounts_root / "acct10", target_is_directory=True)

        symlink_file_dir = self.accounts_root / "acct12"
        symlink_file_dir.mkdir()
        (symlink_file_dir / "cookies.json").symlink_to(
            self.accounts_root / "acct10/cookies.json"
        )
        (symlink_file_dir / "access_token.txt").write_text(
            "fixture-acct12-token-not-a-secret"
        )

        found = discover_accounts(self.accounts_root, auth_root=self.auth_root)
        self.assertEqual(found, ("acct10",))

    def test_unset_roots_are_not_added_to_child_environment(self):
        os.environ.pop("SO_SENDER_ACCOUNTS_DIR", None)
        os.environ.pop("CHATGPT_AUTH_DIR", None)
        sys.path.insert(0, str(self.root / "so-sender"))
        try:
            from sender_gateway.sender_adapter import ChatGPTSenderAdapter
            adapter = ChatGPTSenderAdapter(
                str(self.cli_dir / "chatgpt_send.py"),
                prompt_dir=str(self.root / "prompts"),
            )
            try:
                child_env = adapter._child_env()
            finally:
                adapter.close()
        finally:
            _purge_sender_gateway_modules()
            sys.path.remove(str(self.root / "so-sender"))
        self.assertNotIn("SO_SENDER_ACCOUNTS_DIR", child_env)
        self.assertNotIn("CHATGPT_AUTH_DIR", child_env)


if __name__ == "__main__":
    unittest.main()
