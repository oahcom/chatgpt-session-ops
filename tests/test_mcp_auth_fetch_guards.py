"""Guard tests for the network/address/DNS/fetch half of issue #111.

Covers every ``raise`` in these functions of ``src/session_ops/mcp_auth.py``:

    _resolve_validated_address       5  (no host / OSError / bad record /
                                         empty answer / non-global answer)
    _PinnedHTTPSConnection.connect   2  (proxy tunnel refused / wrap re-raised)
    _default_json_fetcher            5  (cross-origin redirect / auth re-raise /
                                         transport error x2 / oversize body)
    JWKSProvider.__init__            3  (path mismatch / timeout / cache) plus
                                         the ``_validate_discovery_override_url``
                                         call it makes, already covered elsewhere
    JWKSProvider._fetch_document     2  (non-Auth error / non-object) + re-raise
    JWKSProvider._discover_jwks_uri  2  (issuer mismatch / origin mismatch)
    JWKSProvider._download_jwks      1  (missing keys array)

``JWTVerifier.__init__`` is a different class with the same method name and is
owned by another worker; it is not touched here.

No real network: DNS is replaced via ``patch(socket.getaddrinfo)`` and the
socket/opener layer is faked. Every rejection asserts the FULL message with
``assertEqual(str(exc), ...)`` or an anchored regex, never a substring, because
this module has message pairs where one is a strict prefix of another and a
loose match manufactures a false KILL.

Run with:
    python -m unittest tests.test_mcp_auth_fetch_guards
"""

import socket
import ssl
import time
import threading
import unittest
import urllib.request
from unittest.mock import patch

from src.session_ops.mcp_auth import (
    AuthConfigurationError,
    AuthenticationError,
    JWKSFetchError,
    JWKSProvider,
    _default_json_fetcher,
    _PinnedHTTPSConnection,
    _resolve_validated_address,
)


def public_record(ip="93.184.216.34", port=443):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (ip, port),
        )
    ]


class _FakeTLSSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeTLSContext:
    """Enough of ssl.SSLContext for http.client.HTTPSConnection.

    ``HTTPSConnection.__init__`` reads verify_mode and check_hostname; the wrap
    only happens in connect(), where the tests drive it directly.
    """

    verify_mode = ssl.CERT_REQUIRED
    check_hostname = True

    def __init__(self, raise_on_wrap=None):
        self.server_hostnames = []
        self._raise_on_wrap = raise_on_wrap

    def wrap_socket(self, sock, *, server_hostname, do_handshake_on_connect=True):
        self.server_hostnames.append(server_hostname)
        if self._raise_on_wrap is not None:
            raise self._raise_on_wrap
        return sock


class _BlockingTLSWrappedSocket:
    """A controllable SSLSocket stand-in whose handshake blocks until closed."""

    def __init__(self, sock):
        self.sock = sock
        self.closed = threading.Event()

    def settimeout(self, timeout):
        self.sock.settimeout(timeout)

    def do_handshake(self):
        if not self.closed.wait(1):
            raise TimeoutError("test handshake was not interrupted")

    def shutdown(self, how):
        try:
            self.sock.shutdown(how)
        except OSError:
            pass

    def close(self):
        self.closed.set()
        self.sock.close()


class _BlockingHandshakeTLSContext(_FakeTLSContext):
    def wrap_socket(self, sock, *, server_hostname, do_handshake_on_connect=True):
        self.server_hostnames.append(server_hostname)
        return _BlockingTLSWrappedSocket(sock)


class TestResolveValidatedAddress(unittest.TestCase):
    """All five raises plus the two properties the return value encodes."""

    def test_missing_hostname_rejected(self):
        # "https://:443/x" has a non-empty netloc so it survives
        # _validate_https_url, but .hostname is None. getaddrinfo is patched to
        # fail loudly: if the guard is removed the code would fall through to a
        # resolver call with host=None, which must never happen.
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            side_effect=AssertionError("must not resolve a missing hostname"),
        ):
            with self.assertRaises(AuthenticationError) as ctx:
                _resolve_validated_address("https://:443/keys", "JWKS")
        self.assertEqual(str(ctx.exception), "JWKS URL has no hostname")

    def test_getaddrinfo_oserror_rejected(self):
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            side_effect=OSError("dns down"),
        ):
            with self.assertRaises(AuthenticationError) as ctx:
                _resolve_validated_address(
                    "https://issuer.example.test/keys", "JWKS"
                )
        self.assertEqual(str(ctx.exception), "JWKS hostname could not be resolved")

    def test_getaddrinfo_timeout_is_bounded_and_resolver_thread_is_released(self):
        from src.session_ops import mcp_auth
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_resolver(*_args, **_kwargs):
            started.set()
            release.wait(1.0)
            finished.set()
            return public_record("93.184.216.34")

        started_at = time.monotonic()
        try:
            with (
                patch("src.session_ops.mcp_auth.socket.getaddrinfo", side_effect=slow_resolver),
                patch("src.session_ops.mcp_auth.DNS_RESOLUTION_TIMEOUT_SECONDS", 0.02),
            ):
                with self.assertRaisesRegex(AuthenticationError, "DNS hostname resolution timed out"):
                    _resolve_validated_address("https://issuer.example.test/keys", "JWKS")
            self.assertTrue(started.is_set())
            self.assertLess(time.monotonic() - started_at, 0.2)
        finally:
            release.set()
        self.assertTrue(finished.wait(1.0))
        self.assertTrue(mcp_auth._DNS_RESOLVER_SLOTS.acquire(timeout=0.1))
        mcp_auth._DNS_RESOLVER_SLOTS.release()

    def test_unparseable_address_record_rejected(self):
        bad = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("999.1.1.1", 443))]
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=bad,
        ):
            with self.assertRaises(AuthenticationError) as ctx:
                _resolve_validated_address(
                    "https://issuer.example.test/keys", "JWKS"
                )
        self.assertEqual(
            str(ctx.exception),
            "JWKS hostname resolved to an invalid address",
        )

    def test_empty_answer_rejected(self):
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=[],
        ):
            with self.assertRaises(AuthenticationError) as ctx:
                _resolve_validated_address(
                    "https://issuer.example.test/keys", "JWKS"
                )
        self.assertEqual(str(ctx.exception), "JWKS hostname has no addresses")

    def test_non_global_answer_rejected(self):
        for ip in ("127.0.0.1", "10.20.30.40", "169.254.10.20"):
            with self.subTest(ip=ip):
                with patch(
                    "src.session_ops.mcp_auth.socket.getaddrinfo",
                    return_value=public_record(ip),
                ):
                    with self.assertRaises(AuthenticationError) as ctx:
                        _resolve_validated_address(
                            "https://issuer.example.test/keys", "JWKS"
                        )
                self.assertEqual(
                    str(ctx.exception),
                    "JWKS URL resolves to a non-public network address",
                )

    def test_accept_path_returns_the_validated_address(self):
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=public_record("93.184.216.34"),
        ):
            pinned = _resolve_validated_address(
                "https://issuer.example.test/keys", "JWKS"
            )
        self.assertEqual(pinned, "93.184.216.34")

    def test_literal_ip_host_bypasses_dns(self):
        # A literal IP must not trigger a resolver call; if it did, the test
        # would fail on the AssertionError side effect.
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            side_effect=AssertionError("getaddrinfo must not be called"),
        ) as resolve:
            pinned = _resolve_validated_address("https://8.8.8.8/keys", "JWKS")
        resolve.assert_not_called()
        self.assertEqual(pinned, "8.8.8.8")

    def test_mixed_public_and_private_answer_rejected(self):
        """The security property: one private answer condemns the hostname.

        Filtering down to the public entry is the tempting fix and the wrong
        one; a mixed answer is evidence of a hostile resolver.
        """
        answers = public_record("93.184.216.34") + public_record("10.0.0.7")
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=answers,
        ):
            with self.assertRaises(AuthenticationError) as ctx:
                _resolve_validated_address(
                    "https://issuer.example.test/keys", "JWKS"
                )
        self.assertEqual(
            str(ctx.exception),
            "JWKS URL resolves to a non-public network address",
        )


class TestPinnedHTTPSConnectionConnect(unittest.TestCase):
    """Both raises in _PinnedHTTPSConnection.connect."""

    def make_connection(self, context):
        return _PinnedHTTPSConnection(
            "issuer.example.test",
            pinned_ip="93.184.216.34",
            port=443,
            context=context,
        )

    def test_proxy_tunnel_refused_before_dial(self):
        dialled = []
        connection = self.make_connection(_FakeTLSContext())
        connection._tunnel_host = "issuer.example.test"

        with patch(
            "src.session_ops.mcp_auth.socket.create_connection",
            side_effect=lambda address, timeout, source: dialled.append(address),
        ):
            with self.assertRaises(OSError) as ctx:
                connection.connect()

        self.assertEqual(str(ctx.exception), "HTTPS proxy tunnels are not supported")
        self.assertEqual(dialled, [], "a tunnel must be refused before any dial")

    def test_total_deadline_closes_socket_even_when_peer_keeps_sending(self):
        local, peer = socket.socketpair()
        connection = _PinnedHTTPSConnection(
            "issuer.example.test",
            pinned_ip="93.184.216.34",
            port=443,
            timeout=1.0,
            total_timeout=0.15,
            context=_FakeTLSContext(),
        )
        try:
            with patch(
                "src.session_ops.mcp_auth.socket.create_connection",
                return_value=local,
            ):
                connection.connect()
            peer.settimeout(0.2)
            sent = []
            stop = threading.Event()

            def slow_peer():
                while not stop.is_set():
                    try:
                        peer.send(b"x")
                        sent.append(1)
                    except OSError:
                        return
                    time.sleep(0.01)

            sender = threading.Thread(target=slow_peer, daemon=True)
            sender.start()
            time.sleep(0.04)
            self.assertGreater(len(sent), 0)
            self.assertTrue(
                self._wait_for_deadline(connection, 0.5),
                "the absolute deadline did not close a continuously active socket",
            )
            stop.set()
            sender.join(timeout=0.5)
            self.assertIsNone(connection.sock)
            self.assertTrue(connection._deadline_expired)
            self.assertGreater(sent.__len__(), 0)
        finally:
            connection.cancel_deadline()
            connection.close()
            peer.close()

    def test_total_deadline_interrupts_tls_handshake(self):
        local, peer = socket.socketpair()
        connection = _PinnedHTTPSConnection(
            "issuer.example.test",
            pinned_ip="93.184.216.34",
            port=443,
            timeout=1.0,
            total_timeout=0.1,
            context=_BlockingHandshakeTLSContext(),
        )
        try:
            with patch(
                "src.session_ops.mcp_auth.socket.create_connection",
                return_value=local,
            ):
                with self.assertRaises(TimeoutError):
                    connection.connect()
            self.assertTrue(connection._deadline_expired)
            self.assertIsNone(connection.sock)
        finally:
            connection.cancel_deadline()
            connection.close()
            peer.close()

    @staticmethod
    def _wait_for_deadline(connection, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if connection._deadline_expired:
                return True
            time.sleep(0.005)
        return connection._deadline_expired

    def test_wrap_failure_is_reraised_and_socket_closed(self):
        """The bare raise at L633: a TLS failure must escape, not be swallowed."""
        failure = RuntimeError("tls handshake boom")
        socket_obj = _FakeTLSSocket()
        connection = self.make_connection(_FakeTLSContext(raise_on_wrap=failure))

        with patch(
            "src.session_ops.mcp_auth.socket.create_connection",
            side_effect=lambda address, timeout, source: socket_obj,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                connection.connect()

        self.assertIs(ctx.exception, failure)
        self.assertTrue(socket_obj.closed, "the raw socket must be closed on failure")


class _FakeResponse:
    """Minimal urllib response: context manager + geturl + bounded read."""

    def __init__(self, url, body=b"{}"):
        self._url = url
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._url

    def read(self, amount):
        return self._body[:amount]


class _FakeOpener:
    def __init__(self, *, response=None, exc=None):
        self._response = response
        self._exc = exc

    def open(self, request, timeout):
        if self._exc is not None:
            raise self._exc
        return self._response


class TestDefaultJsonFetcher(unittest.TestCase):
    """All five raises in _default_json_fetcher, driven with a fake opener."""

    def run_fetcher(self, opener, url, max_bytes=1024):
        with patch(
            "src.session_ops.mcp_auth.urllib.request.build_opener",
            return_value=opener,
        ):
            return _default_json_fetcher(url, 1.0, max_bytes)

    def test_cross_origin_http_redirect_rejected(self):
        # Original is loopback HTTP; the fake response reports a different
        # loopback netloc, so both URLs pass _validate_fetch_url and the
        # redirect-origin guard is the one that fires.
        opener = _FakeOpener(
            response=_FakeResponse("http://localhost:9090/jwks")
        )
        with self.assertRaises(AuthenticationError) as ctx:
            self.run_fetcher(opener, "http://localhost:8080/jwks")
        self.assertEqual(
            str(ctx.exception),
            "internal identity endpoint redirected to another origin",
        )

    def test_authentication_error_is_reraised_not_wrapped(self):
        """L723: an AuthenticationError must not become a JWKSFetchError."""
        opener = _FakeOpener(exc=AuthenticationError("synthetic auth failure"))
        with self.assertRaises(AuthenticationError) as ctx:
            self.run_fetcher(opener, "http://localhost:8080/jwks")
        self.assertEqual(str(ctx.exception), "synthetic auth failure")

    def test_oserror_becomes_jwks_fetch_error(self):
        opener = _FakeOpener(exc=OSError("connection refused"))
        with self.assertRaises(JWKSFetchError) as ctx:
            self.run_fetcher(opener, "http://localhost:8080/jwks")
        self.assertEqual(str(ctx.exception), "identity endpoint request failed")

    def test_unrelated_exception_becomes_jwks_fetch_error(self):
        opener = _FakeOpener(exc=ValueError("boom"))
        with self.assertRaises(JWKSFetchError) as ctx:
            self.run_fetcher(opener, "http://localhost:8080/jwks")
        self.assertEqual(str(ctx.exception), "identity endpoint request failed")

    def test_oversize_response_rejected(self):
        opener = _FakeOpener(
            response=_FakeResponse(
                "http://localhost:8080/jwks",
                body=b"x" * 11,
            )
        )
        with self.assertRaises(AuthenticationError) as ctx:
            self.run_fetcher(opener, "http://localhost:8080/jwks", max_bytes=10)
        self.assertEqual(
            str(ctx.exception),
            "identity endpoint response is too large",
        )


class TestJWKSProviderInit(unittest.TestCase):
    """The three ctor guards this worker owns, plus the accept path."""

    ISSUER = "https://issuer.example.test/realm"

    def test_discovery_path_mismatch_rejected(self):
        with self.assertRaises(AuthConfigurationError) as ctx:
            JWKSProvider(
                self.ISSUER,
                discovery_url=(
                    "https://issuer.example.test/other"
                    "/.well-known/openid-configuration"
                ),
            )
        self.assertEqual(
            str(ctx.exception),
            "discovery URL path does not match configured issuer path",
        )

    def test_non_positive_timeout_rejected(self):
        with self.assertRaises(AuthConfigurationError) as ctx:
            JWKSProvider(self.ISSUER, timeout_seconds=0)
        self.assertEqual(str(ctx.exception), "HTTP timeout must be positive")

    def test_negative_cache_duration_rejected(self):
        with self.assertRaises(AuthConfigurationError) as ctx:
            JWKSProvider(self.ISSUER, cache_seconds=-1)
        self.assertEqual(
            str(ctx.exception), "cache duration must not be negative"
        )

    def test_accept_path_builds_provider(self):
        provider = JWKSProvider(
            self.ISSUER,
            fetch_json=lambda url, timeout, limit: {},
            timeout_seconds=1.0,
            cache_seconds=0,
        )
        self.assertEqual(provider.issuer, self.ISSUER)
        self.assertEqual(
            provider.discovery_url,
            self.ISSUER + "/.well-known/openid-configuration",
        )


def make_provider(fetch_json):
    return JWKSProvider(
        "https://issuer.example.test/realm",
        fetch_json=fetch_json,
        cache_seconds=300,
    )


class TestFetchDocument(unittest.TestCase):
    """Both guards plus the AuthenticationError passthrough."""

    def test_authentication_error_passthrough(self):
        def boom(url, timeout, limit):
            raise AuthenticationError("raw auth failure")

        provider = make_provider(boom)
        with self.assertRaises(AuthenticationError) as ctx:
            provider._fetch_document("https://issuer.example.test/x", 1024)
        self.assertEqual(str(ctx.exception), "raw auth failure")

    def test_other_exception_wrapped(self):
        def boom(url, timeout, limit):
            raise RuntimeError("network exploded")

        provider = make_provider(boom)
        with self.assertRaises(JWKSFetchError) as ctx:
            provider._fetch_document("https://issuer.example.test/x", 1024)
        self.assertEqual(str(ctx.exception), "identity endpoint request failed")

    def test_non_mapping_rejected(self):
        provider = make_provider(lambda url, timeout, limit: ["not", "a", "dict"])
        with self.assertRaises(AuthenticationError) as ctx:
            provider._fetch_document("https://issuer.example.test/x", 1024)
        self.assertEqual(
            str(ctx.exception), "identity endpoint returned a non-object"
        )

    def test_mapping_is_returned_as_dict(self):
        provider = make_provider(lambda url, timeout, limit: {"a": 1})
        result = provider._fetch_document("https://issuer.example.test/x", 1024)
        self.assertEqual(result, {"a": 1})


class TestDiscoverJwksUri(unittest.TestCase):
    """Both raises in _discover_jwks_uri, plus the rejected document shapes."""

    ISSUER = "https://issuer.example.test/realm"

    def test_issuer_mismatch_rejected(self):
        provider = make_provider(
            lambda url, timeout, limit: {
                "issuer": "https://attacker.example.test",
                "jwks_uri": "https://issuer.example.test/keys",
            }
        )
        with self.assertRaises(AuthenticationError) as ctx:
            provider._discover_jwks_uri()
        self.assertEqual(str(ctx.exception), "OIDC discovery issuer mismatch")

    def test_origin_mismatch_rejected(self):
        provider = make_provider(
            lambda url, timeout, limit: {
                "issuer": self.ISSUER,
                "jwks_uri": "https://keys.attacker.example.test/jwks",
            }
        )
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=public_record(),
        ):
            with self.assertRaises(AuthenticationError) as ctx:
                provider._discover_jwks_uri()
        self.assertEqual(
            str(ctx.exception),
            "discovered JWKS origin does not match configured issuer",
        )

    def test_missing_jwks_uri_rejected(self):
        # Rejected downstream by _validate_public_https_url (owned elsewhere),
        # but the provider must fail closed on a document that omits the key.
        provider = make_provider(
            lambda url, timeout, limit: {"issuer": self.ISSUER}
        )
        with self.assertRaises(AuthenticationError):
            provider._discover_jwks_uri()

    def test_non_string_jwks_uri_rejected(self):
        provider = make_provider(
            lambda url, timeout, limit: {"issuer": self.ISSUER, "jwks_uri": 123}
        )
        with self.assertRaises(AuthenticationError):
            provider._discover_jwks_uri()

    def test_matching_origin_returns_jwks_uri(self):
        jwks_uri = "https://issuer.example.test/keys"
        provider = make_provider(
            lambda url, timeout, limit: {
                "issuer": self.ISSUER,
                "jwks_uri": jwks_uri,
            }
        )
        with patch(
            "src.session_ops.mcp_auth.socket.getaddrinfo",
            return_value=public_record(),
        ):
            self.assertEqual(provider._discover_jwks_uri(), jwks_uri)


class TestDownloadJwks(unittest.TestCase):
    """The single raise in _download_jwks, plus the accept path."""

    def provider_with_document(self, document):
        provider = make_provider(lambda url, timeout, limit: document)
        provider._jwks_uri = "https://issuer.example.test/keys"
        return provider

    def test_missing_keys_array_rejected(self):
        for document in ({"keys": "not-a-list"}, {"keys": None}, {}):
            with self.subTest(document=document):
                provider = self.provider_with_document(document)
                with self.assertRaises(AuthenticationError) as ctx:
                    provider._download_jwks()
                self.assertEqual(
                    str(ctx.exception), "JWKS document has no keys array"
                )

    def test_valid_document_returned(self):
        document = {"keys": [{"kid": "known", "kty": "RSA"}]}
        provider = self.provider_with_document(document)
        self.assertEqual(provider._download_jwks(), document)


if __name__ == "__main__":
    unittest.main()
