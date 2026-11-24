"""Regression tests for MCP authentication transport hardening.

All network, DNS, and optional MCP SDK dependencies are replaced with
deterministic fakes. Run with:
    python -m unittest tests.test_mcp_transport
"""

import asyncio
import socket
import sys
import types
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from src.session_ops.mcp_auth import (
    AuthenticationError,
    JWKSFetchError,
    JWKSProvider,
    _default_json_fetcher,
    _validate_public_https_url,
)
from src.session_ops.mcp_server import MCPToolService, build_mcp_server


KNOWN_JWKS = {
    "keys": [
        {
            "kid": "known",
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "n": "test-modulus",
            "e": "AQAB",
        }
    ]
}


def make_provider():
    return JWKSProvider(
        "https://issuer.example.test/realm",
        fetch_json=lambda url, timeout, limit: {},
        cache_seconds=300,
    )


class JWKSRefreshRegressionTests(unittest.TestCase):

    def test_fresh_cache_unknown_kid_never_downloads(self):
        provider = make_provider()
        provider._jwks = KNOWN_JWKS
        provider._expires_at = float("inf")

        with patch.object(provider, "_download_jwks") as download:
            with self.assertRaises(AuthenticationError):
                provider.get_jwk("attacker-random-kid")

        # This is the core regression assertion: the old implementation
        # downloaded on every fresh-cache kid miss.
        download.assert_not_called()

    def test_expired_cache_concurrent_refresh_is_single_flight(self):
        provider = make_provider()
        entered = __import__("threading").Event()
        release = __import__("threading").Event()

        def blocking_download():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release refresh")
            return KNOWN_JWKS

        with patch.object(
            provider,
            "_download_jwks",
            side_effect=blocking_download,
        ) as download:
            with ThreadPoolExecutor(max_workers=6) as pool:
                first = pool.submit(provider.get_jwk, "known")
                self.assertTrue(entered.wait(2))

                # Requests arriving while the refresh is in progress must
                # fail fast or wait for the one shared refresh; they must
                # not start additional downloads.
                for _ in range(5):
                    with self.assertRaises(JWKSFetchError):
                        provider.get_jwk("known")

                release.set()
                self.assertEqual(first.result(timeout=3)["kid"], "known")

        self.assertEqual(download.call_count, 1)

    def test_failed_refresh_is_not_retried_inside_backoff_window(self):
        provider = make_provider()
        with patch.object(
            provider,
            "_download_jwks",
            side_effect=JWKSFetchError("simulated outage"),
        ) as download:
            with self.assertRaises(JWKSFetchError):
                provider.get_jwk("known")

            with self.assertRaises(JWKSFetchError):
                provider.get_jwk("known")

            self.assertEqual(download.call_count, 1)

    def test_refresh_can_retry_after_backoff_expires(self):
        provider = make_provider()
        with patch.object(
            provider,
            "_download_jwks",
            side_effect=[
                JWKSFetchError("simulated outage"),
                KNOWN_JWKS,
            ],
        ) as download:
            with self.assertRaises(JWKSFetchError):
                provider.get_jwk("known")

            # Model the passage of the configured backoff interval.
            provider._refresh_retry_at = __import__("time").monotonic() - 1

            self.assertEqual(provider.get_jwk("known")["kid"], "known")
            self.assertEqual(download.call_count, 2)


class DiscoverySSRFRegressionTests(unittest.TestCase):

    @staticmethod
    def public_dns_record(ip="8.8.8.8"):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, 443),
            )
        ]

    def test_non_override_discovery_rejects_different_jwks_origin(self):
        provider = make_provider()
        provider.fetch_json = lambda url, timeout, limit: {
            "issuer": provider.issuer,
            "jwks_uri": "https://attacker.example.test/keys",
        }

        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=self.public_dns_record(),
        ):
            with self.assertRaises(AuthenticationError):
                provider._discover_jwks_uri()

    def test_default_fetcher_does_not_follow_redirects(self):
        requested = []
        initial_url = "https://issuer.example.test/keys"

        class NoRedirectOpener:
            def open(self, request, timeout):
                requested.append(request.full_url)
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {},
                    None,
                )

        # The legacy urlopen path is instrumented too. If the implementation
        # reverts to urlopen, this fake records the redirected destination.
        def redirect_following_urlopen(request, timeout):
            requested.append(request.full_url)
            requested.append("https://redirect-target.example.test/keys")
            raise AssertionError("redirect-following urlopen was used")

        with patch(
            "src.session_ops.mcp_auth.urllib.request.build_opener",
            return_value=NoRedirectOpener(),
        ), patch(
            "src.session_ops.mcp_auth.urllib.request.urlopen",
            side_effect=redirect_following_urlopen,
        ) as old_urlopen, patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", 443),
                )
            ],
        ):
            with self.assertRaises(JWKSFetchError):
                _default_json_fetcher(initial_url, 1.0, 1024)

        self.assertEqual(requested, [initial_url])
        old_urlopen.assert_not_called()

    def test_https_jwks_rejects_loopback_link_local_and_private_ips(self):
        for address in (
            "127.0.0.1",
            "169.254.10.20",
            "10.20.30.40",
        ):
            with self.subTest(address=address):
                with self.assertRaises(AuthenticationError):
                    _validate_public_https_url(
                        "https://%s/keys" % address,
                        "JWKS",
                    )

    def test_jwks_rejects_all_dns_answers_if_any_is_non_global(self):
        answers = self.public_dns_record("8.8.8.8") + [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.7", 443),
            )
        ]

        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=answers,
        ):
            with self.assertRaises(AuthenticationError):
                _validate_public_https_url(
                    "https://keys.example.test/keys",
                    "JWKS",
                )


class AuthMiddlewarePathRegressionTests(unittest.TestCase):

    def build_app_without_installed_mcp_sdk(self):
        """Build the real middleware while stubbing only the optional SDK."""
        mcp_module = types.ModuleType("mcp")
        mcp_module.__path__ = []
        mcp_types = types.ModuleType("mcp.types")
        server_module = types.ModuleType("mcp.server")
        security_module = types.ModuleType("mcp.server.transport_security")

        class Placeholder:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        mcp_types.Tool = Placeholder
        mcp_types.ListToolsResult = Placeholder
        mcp_types.CallToolResult = Placeholder
        mcp_types.TextContent = Placeholder

        inner_calls = []

        async def inner(scope, receive, send):
            inner_calls.append(scope["path"])
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            })
            await send({
                "type": "http.response.body",
                "body": b"",
            })

        class FakeServer:
            def __init__(self, *args, **kwargs):
                pass

            def streamable_http_app(self, **kwargs):
                return inner

        server_module.Server = FakeServer
        security_module.TransportSecuritySettings = Placeholder
        mcp_module.types = mcp_types

        class TokenProvider:
            audience = "https://resource.example.test/mcp"

            def verify(self, token):
                raise AssertionError("no token should be verified")

        modules = {
            "mcp": mcp_module,
            "mcp.types": mcp_types,
            "mcp.server": server_module,
            "mcp.server.transport_security": security_module,
        }

        with patch.dict(sys.modules, modules):
            app = build_mcp_server(MCPToolService(object()), TokenProvider())

        return app, inner_calls

    @staticmethod
    def asgi_request(app, path):
        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("resource.example.test", 443),
            "scheme": "https",
        }
        asyncio.run(app(scope, receive, send))
        return messages

    def test_mcp_subpath_is_authenticated(self):
        app, inner_calls = self.build_app_without_installed_mcp_sdk()
        messages = self.asgi_request(app, "/mcp/anything")
        status = next(
            message["status"]
            for message in messages
            if message["type"] == "http.response.start"
        )
        self.assertEqual(status, 401)
        self.assertEqual(inner_calls, [])

    def test_mcp_endpoint_itself_is_authenticated(self):
        app, inner_calls = self.build_app_without_installed_mcp_sdk()
        messages = self.asgi_request(app, "/mcp")
        status = next(
            message["status"]
            for message in messages
            if message["type"] == "http.response.start"
        )
        self.assertEqual(status, 401)
        self.assertEqual(inner_calls, [])

    def test_health_and_protected_resource_metadata_remain_public(self):
        app, inner_calls = self.build_app_without_installed_mcp_sdk()

        for path in (
            "/healthz",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            with self.subTest(path=path):
                messages = self.asgi_request(app, path)
                status = next(
                    message["status"]
                    for message in messages
                    if message["type"] == "http.response.start"
                )
                self.assertEqual(status, 204)

        self.assertEqual(
            inner_calls,
            [
                "/healthz",
                "/.well-known/oauth-protected-resource/mcp",
            ],
        )


if __name__ == "__main__":
    unittest.main()
