from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.session_ops.chatgpt_cli import _discover_logged_in_accounts


class DynamicAccountDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.accounts = self.root / "accounts"
        self.auth.mkdir()
        self.accounts.mkdir()
        (self.auth / "cookies.json").write_text("{}", encoding="utf-8")
        (self.auth / "access_token.txt").write_text("token", encoding="utf-8")
        for name in ("acct2", "acct7"):
            account = self.accounts / name
            account.mkdir()
            (account / "cookies.json").write_text("{}", encoding="utf-8")
            (account / "access_token.txt").write_text("token", encoding="utf-8")
        os.environ["CHATGPT_AUTH_DIR"] = str(self.auth)
        os.environ["SO_SENDER_ACCOUNTS_DIR"] = str(self.accounts)

    def tearDown(self):
        os.environ.pop("CHATGPT_AUTH_DIR", None)
        os.environ.pop("SO_SENDER_ACCOUNTS_DIR", None)
        self.temp.cleanup()

    def test_adapter_rechecks_credentials_on_each_call(self):
        self.assertEqual(
            _discover_logged_in_accounts(),
            frozenset({"acct1", "acct2", "acct7"}),
        )
        account8 = self.accounts / "acct8"
        account8.mkdir()
        (account8 / "cookies.json").write_text("{}", encoding="utf-8")
        (account8 / "access_token.txt").write_text("token", encoding="utf-8")
        self.assertIn("acct8", _discover_logged_in_accounts())

    def test_cli_rotation_discovers_new_accounts_without_a_whitelist(self):
        env = os.environ.copy()
        env.pop("CHATGPT_ALLOWED_ACCOUNTS", None)
        script = Path(__file__).resolve().parents[1] / "scripts" / "chatgpt_send.py"
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                (
                    "import importlib.util,json,sys; "
                    "s=importlib.util.spec_from_file_location('cli',sys.argv[1]); "
                    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                    "print(json.dumps({'primary':m.ROTATION_PRIMARY,'order':m.ROTATION_ORDER}))"
                ),
                str(script),
            ],
            env=env,
            text=True,
        )
        result = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(result["primary"], ["acct1", "acct2", "acct7"])
        self.assertEqual(result["order"], ["acct1", "acct2", "acct7", "nologin"])


if __name__ == "__main__":
    unittest.main()
