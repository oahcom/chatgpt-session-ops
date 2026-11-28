import json
import os
import tempfile
import unittest
from unittest import mock

from scripts import chatgpt_send as sender


class DynamicAccountDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def write_account(self, account, *, token="access-token", cookies=None):
        directory = self.root if account == "acct1" else os.path.join(self.root, "accounts", account)
        os.makedirs(directory, exist_ok=True)
        if cookies is None:
            cookies = [{
                "name": "__Secure-next-auth.session-token",
                "value": "session",
                "domain": ".chatgpt.com",
            }]
        with open(os.path.join(directory, "cookies.json"), "w", encoding="utf-8") as f:
            json.dump(cookies, f)
        if token is not None:
            with open(os.path.join(directory, "access_token.txt"), "w", encoding="utf-8") as f:
                f.write(token)
        return directory

    def test_discovers_dynamic_accounts_in_natural_order_and_skips_invalid(self):
        self.write_account("acct1")
        self.write_account("acct2")
        self.write_account("acct10")
        self.write_account("acct3", cookies={"not": "an array"})
        self.write_account("acct4", token="")
        self.write_account("acct0")
        self.write_account("acct02")
        self.assertEqual(
            sender.discover_configured_accounts(self.root),
            ["acct1", "acct2", "acct10"],
        )

    def test_acct1_prefers_accounts_directory_and_default_uses_same_identity(self):
        directory = os.path.join(self.root, "accounts", "acct1")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "cookies.json"), "w", encoding="utf-8") as f:
            json.dump([{"name": "session", "value": "x", "domain": ".chatgpt.com"}], f)
        with open(os.path.join(directory, "access_token.txt"), "w", encoding="utf-8") as f:
            f.write("token")
        self.assertEqual(sender._account_credentials_dir("acct1", self.root), directory)
        self.assertEqual(sender._account_credentials_dir("default", self.root), directory)

    def test_dynamic_account_constructor_uses_isolated_directory(self):
        directory = self.write_account("acct10")
        with (
            mock.patch.object(sender, "require_proxy", return_value="http://127.0.0.1:8080"),
            mock.patch.object(sender, "resolve_proxy_info", return_value={"source": "test"}),
            mock.patch.object(sender, "build_opener_for", return_value=object()),
        ):
            api = sender.ChatGPTAPI(base_dir=self.root, account="acct10")
        self.assertEqual(api.base_dir, directory)
        self.assertEqual(api.account, "acct10")

    def test_operator_disabled_is_persistent_and_migrates_legacy_marker(self):
        with mock.patch.object(sender, "_rotation_primary", return_value=["acct1", "acct10"]):
            state = sender._normalize_rotation({"operator_disabled": ["acct10"]})
            self.assertNotIn("acct10", sender.rotation_plan(state))
            legacy = sender._normalize_rotation({
                "unavailable": {
                    "acct10": {
                        "reason": "manually disabled",
                        "epoch": 1,
                        "ttl": 1,
                        "at": "old",
                    }
                }
            })
            self.assertIn("acct10", legacy["operator_disabled"])
            self.assertNotIn("acct10", legacy["unavailable"])
            self.assertNotIn("acct10", sender.rotation_plan(legacy))

    def test_dynamic_identity_validation_does_not_allow_path_fragments(self):
        self.assertEqual(sender.normalize_account_identity("acct1000"), "acct1000")
        for invalid in ("acct0", "acct01", "acct-1", "../acct2", "acct2/../acct1"):
            with self.assertRaises(ValueError):
                sender.normalize_account_identity(invalid)


    def test_cursor_and_summary_support_more_than_six_accounts(self):
        accounts = ["acct1", "acct2", "acct3", "acct4", "acct5", "acct6", "acct7", "acct10"]
        with mock.patch.object(sender, "_rotation_primary", return_value=accounts):
            state = sender._normalize_rotation({"cursor": 7})
            self.assertEqual(state["cursor"], 7)
            self.assertEqual(sender.rotation_plan(state)[0], "acct10")
            with mock.patch.object(sender, "load_rotation", return_value=state):
                summary = sender.rotation_summary()
            self.assertEqual(summary["next"], "acct10")

    def test_corrupt_unavailable_keys_are_discarded(self):
        with mock.patch.object(sender, "_rotation_primary", return_value=["acct1"]):
            state = sender._normalize_rotation({
                "unavailable": {
                    7: {"reason": "bad-key"},
                    "../acct1": {"reason": "path"},
                    "acct1": {"reason": "HTTP 429", "epoch": 1, "ttl": 60},
                }
            })
        self.assertEqual(set(state["unavailable"]), {"acct1"})


if __name__ == "__main__":
    unittest.main()
