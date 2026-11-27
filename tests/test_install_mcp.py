"""Focused safety/regression tests for the one-shot connector installer."""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import install_mcp  # noqa: E402


class InstallMcpUrlTests(unittest.TestCase):
    def test_exact_https_hosts_only(self):
        accepted = (
            "https://chatgpt.com/callback",
            "https://mcp.oachom0831.xyz/realms/rmc",
            "https://CHATGPT.COM./callback",
        )
        rejected = (
            "http://chatgpt.com/callback",
            "https://chatgpt.com.evil.example/callback",
            "https://evilchatgpt.com/callback",
            "https://chatgpt.com@evil.example/callback",
            "https://chatgpt.com:8443/callback",
            "https://mcp.oachom0831.xyz:444/realms/rmc",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(install_mcp.checked_url(url), url)
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    install_mcp.checked_url(url)

    def test_keycloak_form_action_requires_keycloak_host(self):
        self.assertEqual(
            install_mcp.checked_url("https://mcp.oachom0831.xyz/login", "mcp.oachom0831.xyz"),
            "https://mcp.oachom0831.xyz/login",
        )
        with self.assertRaises(ValueError):
            install_mcp.checked_url("https://chatgpt.com/login", "mcp.oachom0831.xyz")

    def test_oauth_error_classification(self):
        expected = {
            "invalid_scope": "invalid_scope",
            "invalid_client": "invalid_client",
            "unauthorized_client": "unauthorized_client",
            "Account is not fully set up": "Account is not fully set up",
            "callback error": "callback error",
            "error=access_denied": "access_denied",
            "normal login page": "",
        }
        for raw, label in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(install_mcp.auth_error(raw), label)


class InstallMcpSkipTests(unittest.TestCase):
    def test_healthy_links_skip_without_keycloak_password(self):
        links = [
            {"connector_id": "rmc-id", "auth_status": "ACTIVE", "actions": [{"name": "exec"}]},
            {"connector_id": "so-id", "auth_status": "ACTIVE", "actions": [{"name": "send"}]},
        ]
        details = {
            "rmc-id": {"base_url": install_mcp.TARGETS["--rmc"][1], "supported_auth": [{"type": "OAUTH"}]},
            "so-id": {"base_url": install_mcp.TARGETS["--so-mcp"][1], "supported_auth": [{"type": "OAUTH"}]},
        }

        class FakeApi:
            def __init__(self, account):
                self.account = account

            def warm_cookies(self):
                pass

            def requirements(self):
                return None, None

            def req(self, method, url, data=None, headers=None):
                if url.endswith("/links/list_accessible"):
                    return 200, json.dumps({"links": links})
                for cid, detail in details.items():
                    if url.endswith("/connectors/" + cid):
                        return 200, json.dumps(detail)
                raise AssertionError("unexpected API request: %s %s" % (method, url))

        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["install_mcp.py", "acct5"]), \
             mock.patch.dict(os.environ, {"RMC_KC_PASS": ""}), \
             mock.patch.object(install_mcp, "_side_file", return_value=""), \
             mock.patch.object(install_mcp.cs, "ChatGPTAPI", FakeApi), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                install_mcp.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().count("SKIP 已装且健康"), 2)
        self.assertNotIn("Keycloak 口令未找到", output.getvalue())


if __name__ == "__main__":
    unittest.main()
