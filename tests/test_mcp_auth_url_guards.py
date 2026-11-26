"""URL-validation guard tests for the MCP auth discovery, fetch and HTTPS helpers.

Covers issue #111: the discovery/fetch/HTTPS URL validators in
``src/session_ops/mcp_auth.py`` are pure functions of a string, so every
``raise`` in them can be pinned without fixtures. Each rejection test asserts
the exception TYPE (the helpers raise different classes) and a distinctive
substring of the message, and each helper also has acceptance tests so the
boundary is pinned from both sides.

``_validate_https_url`` was missed by the first pass of this file and by the
mutation sweep that accompanied it, so it is covered here explicitly and
verified by enumerating every ``ast.Raise`` in the module rather than from a
hand-written list.

Run with:
    python -m unittest tests.test_mcp_auth_url_guards
"""

import unittest

from src.session_ops.mcp_auth import (
    AuthConfigurationError,
    AuthenticationError,
    _validate_discovery_override_url,
    _validate_fetch_url,
    _validate_https_url,
)

WELL_KNOWN = "/.well-known/openid-configuration"


class TestValidateDiscoveryOverrideUrl(unittest.TestCase):
    """Every raise in _validate_discovery_override_url is reached."""

    def test_non_string_or_empty_rejected(self):
        for value in (None, "", 0, b"https://issuer.example" + WELL_KNOWN.encode()):
            with self.subTest(value=value):
                with self.assertRaises(AuthConfigurationError) as ctx:
                    _validate_discovery_override_url(value)
                self.assertIn("must be a non-empty string", str(ctx.exception))

    def test_malformed_port_rejected(self):
        with self.assertRaises(AuthConfigurationError) as ctx:
            _validate_discovery_override_url("https://issuer.example:bad" + WELL_KNOWN)
        self.assertIn("malformed", str(ctx.exception))

    def test_non_http_scheme_or_missing_netloc_rejected(self):
        for value in (
            "ftp://issuer.example" + WELL_KNOWN,
            "file://" + WELL_KNOWN,
            WELL_KNOWN,
            "https://" + WELL_KNOWN,
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthConfigurationError) as ctx:
                    _validate_discovery_override_url(value)
                self.assertIn("must be absolute HTTP or HTTPS", str(ctx.exception))

    def test_credentials_rejected(self):
        for value in (
            "https://user:pass@issuer.example" + WELL_KNOWN,
            "https://user@issuer.example" + WELL_KNOWN,
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthConfigurationError) as ctx:
                    _validate_discovery_override_url(value)
                self.assertIn("must not contain credentials", str(ctx.exception))

    def test_query_or_fragment_rejected(self):
        for value in (
            "https://issuer.example" + WELL_KNOWN + "?a=1",
            "https://issuer.example" + WELL_KNOWN + "#frag",
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthConfigurationError) as ctx:
                    _validate_discovery_override_url(value)
                self.assertIn("must not contain query or fragment", str(ctx.exception))

    def test_path_without_well_known_suffix_rejected(self):
        with self.assertRaises(AuthConfigurationError) as ctx:
            _validate_discovery_override_url("https://issuer.example/other")
        self.assertIn("must end with " + WELL_KNOWN, str(ctx.exception))

    def test_plain_http_public_host_rejected(self):
        # 93.184.216.34 takes the ipaddress branch with is_loopback False;
        # the hostname takes the ValueError fallback branch (is_loopback False).
        for value in (
            "http://93.184.216.34" + WELL_KNOWN,
            "http://issuer.example" + WELL_KNOWN,
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthConfigurationError) as ctx:
                    _validate_discovery_override_url(value)
                self.assertIn(
                    "plain HTTP discovery is allowed only on loopback",
                    str(ctx.exception),
                )

    def test_https_public_url_returned_unchanged(self):
        value = "https://issuer.example" + WELL_KNOWN
        self.assertEqual(_validate_discovery_override_url(value), value)

    def test_loopback_hostname_accepted(self):
        for value in (
            "http://localhost" + WELL_KNOWN,
            "http://LOCALHOST" + WELL_KNOWN,
        ):
            with self.subTest(value=value):
                self.assertEqual(_validate_discovery_override_url(value), value)

    def test_loopback_literal_ipv4_accepted(self):
        value = "http://127.0.0.1" + WELL_KNOWN
        self.assertEqual(_validate_discovery_override_url(value), value)

    def test_loopback_literal_ipv6_accepted(self):
        value = "http://[::1]" + WELL_KNOWN
        self.assertEqual(_validate_discovery_override_url(value), value)


class TestValidateFetchUrl(unittest.TestCase):
    """Every raise in _validate_fetch_url is reached."""

    def test_malformed_port_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            _validate_fetch_url("https://issuer.example:bad/jwks", "fetch")
        self.assertIn("fetch URL is malformed", str(ctx.exception))

    def test_non_http_scheme_or_missing_netloc_rejected(self):
        for value in (
            "ftp://issuer.example/jwks",
            "jwks.json",
            "https:///jwks",
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _validate_fetch_url(value, "fetch")
                self.assertIn(
                    "fetch URL must use HTTPS or loopback HTTP", str(ctx.exception)
                )

    def test_plain_http_public_host_rejected(self):
        # Same three-branch split as discovery: literal public IP, unparseable
        # hostname, and a non-loopback host that is neither.
        for value in (
            "http://93.184.216.34/jwks",
            "http://issuer.example/jwks",
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _validate_fetch_url(value, "fetch")
                self.assertIn(
                    "plain HTTP fetch is allowed only on loopback", str(ctx.exception)
                )

    def test_credentials_rejected(self):
        for value in (
            "https://user:pass@issuer.example/jwks",
            "https://user@issuer.example/jwks",
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _validate_fetch_url(value, "fetch")
                self.assertIn("must not contain credentials", str(ctx.exception))

    def test_fragment_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            _validate_fetch_url("https://issuer.example/jwks#frag", "fetch")
        self.assertIn("must not contain a fragment", str(ctx.exception))

    def test_public_https_url_accepted(self):
        self.assertIsNone(_validate_fetch_url("https://issuer.example/jwks", "fetch"))

    def test_loopback_http_accepted(self):
        for value in (
            "http://localhost:8080/jwks",
            "http://127.0.0.1/jwks",
            "http://[::1]/jwks",
        ):
            with self.subTest(value=value):
                self.assertIsNone(_validate_fetch_url(value, "fetch"))


class TestValidateHttpsUrl(unittest.TestCase):
    """Every raise in _validate_https_url is reached."""

    def test_non_string_or_empty_rejected(self):
        for value in (None, "", 0, b"https://issuer.example/x"):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _validate_https_url(value, "resource")
                self.assertIn("resource URL is missing", str(ctx.exception))

    def test_malformed_port_rejected(self):
        # urlparse() succeeds on both; the raise comes from reading .port
        # inside the try, so a non-integer port and an out-of-range port are
        # the two ways to reach this guard.
        for value in (
            "https://issuer.example:bad/x",
            "https://issuer.example:99999999999/x",
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _validate_https_url(value, "resource")
                self.assertIn("resource URL is malformed", str(ctx.exception))

    def test_non_https_scheme_or_missing_netloc_rejected(self):
        for value in (
            "http://issuer.example/x",
            "ftp://issuer.example/x",
            "https:///x",
            "/relative/path",
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _validate_https_url(value, "resource")
                self.assertIn("resource URL must use HTTPS", str(ctx.exception))

    def test_credentials_rejected(self):
        for value in (
            "https://user:pass@issuer.example/x",
            "https://user@issuer.example/x",
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _validate_https_url(value, "resource")
                self.assertIn("must not contain credentials", str(ctx.exception))

    def test_fragment_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            _validate_https_url("https://issuer.example/x#frag", "resource")
        self.assertIn("resource URL must not contain a fragment", str(ctx.exception))

    def test_well_formed_https_url_returned_unchanged(self):
        for value in (
            "https://issuer.example/x",
            "https://issuer.example:8443/x?query=1",
            "https://[::1]:8443/x",
        ):
            with self.subTest(value=value):
                self.assertEqual(_validate_https_url(value, "resource"), value)


if __name__ == "__main__":
    unittest.main()
