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
SNAPSHOT = ROOT / "deploy/source-snapshot/2026-11-28/so-sender"
PATCH = ROOT / "patches/sender-child-env-account-roots.patch"


class SenderChildAccountRootsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sender-child-roots-", dir=str(ROOT))
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
        shutil.copy2(
            SNAPSHOT / "sender_gateway/sender_adapter.py",
            self.gateway_dir / "sender_adapter.py",
        )
        (self.gateway_dir / "__init__.py").write_text("")
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

        self.auth_root = self.root / "auth-root"
        self.accounts_root = self.auth_root / "accounts"
        auth_account = self.accounts_root / "acct10"
        auth_account.mkdir(parents=True)
        # Gateway discovery markers and CLI auth files occupy the same acct10 directory.
        (auth_account / "cookies.json").write_text("[]")
        (auth_account / "access_token.txt").write_text("fixture-token-not-a-secret")

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
            from sender_gateway.sender_adapter import ChatGPTSenderAdapter, _discover_logged_in_accounts
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
            sys.modules.pop("sender_gateway.sender_adapter", None)
            sys.modules.pop("sender_gateway", None)
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
api = cli.ChatGPTAPI(account="acct10")
print(json.dumps({
    "identity_discovered": "acct10" in cli._VALID_ACCOUNT_IDENTITIES,
    "auth_root_used": api.base_dir,
    "token_loaded": api.token == "fixture-token-not-a-secret",
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
        self.assertTrue(result["identity_discovered"])
        self.assertEqual(
            result["auth_root_used"],
            str(self.auth_root / "accounts/acct10"),
        )
        self.assertTrue(result["token_loaded"])

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
            sys.modules.pop("sender_gateway.sender_adapter", None)
            sys.modules.pop("sender_gateway", None)
            sys.path.remove(str(self.root / "so-sender"))
        self.assertNotIn("SO_SENDER_ACCOUNTS_DIR", child_env)
        self.assertNotIn("CHATGPT_AUTH_DIR", child_env)


if __name__ == "__main__":
    unittest.main()
