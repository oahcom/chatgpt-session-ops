"""URL-guard tests for ``scripts/mcp_connector.py`` (issue #116).

``assert_safe_url`` is the SSRF gate in front of ``inspect --url <arbitrary>``.
Before this file the module had no test at all. Two defects are pinned here:

* ``100.64.0.0/10`` (RFC 6598 CGNAT, which contains the Alibaba Cloud
  instance-metadata endpoint ``100.100.100.200``) satisfied none of the six
  ``ipaddress`` predicates the guard used, so it was allowed through.
* ``urlparse(...).port`` raises ``ValueError`` for ``https://fe80::1/mcp`` and
  ``https://host:bad/x``; that escaped the guard and the caller.

Every rejection asserts the ``CheckError.code`` and, where a message is
asserted, the complete ``str(exc)``. Name-based cases patch
``socket.getaddrinfo`` so the suite never touches the network; the numeric
literal cases deliberately run the real resolution path.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mcp_connector  # noqa: E402
from mcp_connector import CheckError, assert_safe_url, inspect  # noqa: E402


def _addrinfo(*ips):
    """Shape ``getaddrinfo`` results the way ``assert_safe_url`` reads them."""
    out = []
    for ip in ips:
        if ":" in ip:
            out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0)))
        else:
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)))
    return out


def _resolver(mapping):
    """Fake ``getaddrinfo``: known names resolve, everything else fails DNS."""

    def fake(host, port, *args, **kwargs):
        try:
            return _addrinfo(*mapping[host.lower()])
        except KeyError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    return fake


class GuardTestCase(unittest.TestCase):
    def assert_rejected(self, url, code, message=None):
        with self.assertRaises(CheckError) as ctx:
            assert_safe_url(url)
        self.assertEqual(ctx.exception.code, code, url)
        if message is not None:
            self.assertEqual(str(ctx.exception), f"{code}: {message}", url)

    def assert_allowed(self, url):
        self.assertIsNone(assert_safe_url(url), url)


class TestUrlShapeRejections(GuardTestCase):
    """Scheme / credentials / fragment / missing host, all before any DNS."""

    def test_non_https_scheme(self):
        for url in ("http://x/mcp", "file://x/mcp", "ftp://x/mcp"):
            with self.subTest(url=url):
                self.assert_rejected(url, "INVALID_URL", "scheme must be https")

    def test_credentials_in_url(self):
        for url in ("https://u:p@host/mcp", "https://u@host/mcp"):
            with self.subTest(url=url):
                self.assert_rejected(
                    url, "INVALID_URL", "credentials in URL are not allowed"
                )

    def test_fragment(self):
        self.assert_rejected(
            "https://host/mcp#frag", "INVALID_URL", "fragment is not allowed"
        )

    def test_missing_host(self):
        self.assert_rejected("https:///mcp", "INVALID_URL", "missing host")


class TestAddressRejections(GuardTestCase):
    """Real resolution path: numeric literals need no DNS lookup."""

    LITERALS = (
        "127.0.0.1",
        "127.0.0.2",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "198.18.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "64:ff9b::1",
        "2002::1",
    )

    def test_literal_addresses_rejected(self):
        for ip in self.LITERALS:
            host = f"[{ip}]" if ":" in ip else ip
            with self.subTest(ip=ip):
                self.assert_rejected(
                    f"https://{host}/mcp", "UNSAFE_ENDPOINT", f"{ip} resolves to {ip}"
                )

    def test_localhost_rejected_by_real_resolver(self):
        # Whether the resolver maps localhost or refuses it, the code must hold.
        for url in ("https://localhost/mcp", "https://LOCALHOST/mcp",
                    "https://localhost./mcp"):
            with self.subTest(url=url):
                with self.assertRaises(CheckError) as ctx:
                    assert_safe_url(url)
                self.assertEqual(ctx.exception.code, "UNSAFE_ENDPOINT")

    def test_localhost_message(self):
        with mock.patch.object(
            socket, "getaddrinfo", _resolver({"localhost": ("127.0.0.1", "::1")})
        ):
            self.assert_rejected(
                "https://localhost/mcp",
                "UNSAFE_ENDPOINT",
                "localhost resolves to 127.0.0.1",
            )

    def test_ambiguous_numeric_forms_rejected(self):
        # decimal / hex / octal / short forms. On glibc these resolve to
        # 127.0.0.1; on Windows they fail DNS. Mock the glibc behaviour so the
        # address check is the thing under test rather than the host resolver.
        names = ("2130706433", "0x7f000001", "0177.0.0.1", "127.1", "127.")
        fake = _resolver({name: ("127.0.0.1",) for name in names})
        for name in names:
            with self.subTest(name=name):
                with mock.patch.object(socket, "getaddrinfo", fake):
                    self.assert_rejected(
                        f"https://{name}/mcp",
                        "UNSAFE_ENDPOINT",
                        f"{name} resolves to 127.0.0.1",
                    )


class TestCgnatRejections(GuardTestCase):
    """RFC 6598 shared address space, incl. the Alibaba Cloud metadata IP."""

    def test_network_is_module_level(self):
        self.assertEqual(
            mcp_connector.CGNAT_NETWORK, ipaddress.ip_network("100.64.0.0/10")
        )
        self.assertIsInstance(mcp_connector.CGNAT_NETWORK, ipaddress.IPv4Network)

    def test_cgnat_rejected(self):
        for ip in ("100.64.0.0", "100.64.0.1", "100.100.100.200", "100.127.255.255"):
            with self.subTest(ip=ip):
                self.assert_rejected(
                    f"https://{ip}/mcp", "UNSAFE_ENDPOINT", f"{ip} resolves to {ip}"
                )

    def test_cgnat_boundaries_and_public_addresses_allowed(self):
        for ip in ("100.63.255.255", "100.128.0.0", "8.8.8.8", "1.1.1.1"):
            with self.subTest(ip=ip):
                self.assert_allowed(f"https://{ip}/mcp")


class TestMalformedPort(GuardTestCase):
    """``urlparse().port`` raises ValueError; it must surface as INVALID_URL."""

    def test_unbracketed_ipv6(self):
        self.assert_rejected(
            "https://fe80::1/mcp",
            "INVALID_URL",
            "malformed URL: 'https://fe80::1/mcp' has an invalid port",
        )

    def test_non_numeric_port(self):
        self.assert_rejected(
            "https://host:bad/x",
            "INVALID_URL",
            "malformed URL: 'https://host:bad/x' has an invalid port",
        )

    def test_out_of_range_port(self):
        self.assert_rejected(
            "https://host:99999999999/x",
            "INVALID_URL",
            "malformed URL: 'https://host:99999999999/x' has an invalid port",
        )

    def test_bracketed_ipv6_still_checked_as_address(self):
        self.assert_rejected(
            "https://[fe80::1]/mcp", "UNSAFE_ENDPOINT", "fe80::1 resolves to fe80::1"
        )

    def test_inspect_reports_invalid_url_instead_of_raising(self):
        res = inspect("https://host:bad/x")
        self.assertEqual(res.error, "INVALID_URL")
        self.assertEqual(
            res.notes, ["malformed URL: 'https://host:bad/x' has an invalid port"]
        )


class TestTransitionFormRejections(GuardTestCase):
    """IPv6 addresses that tunnel IPv4 (the ``2002::1`` defect).

    ``2002::1`` is 6to4 carrying ``0.0.0.0`` and ``is_global`` is True, so a
    guard that judges only the envelope accepted it. Teredo (``2001::/32``)
    and IPv4-mapped (``::ffff:0:0/96``) hide an IPv4 address the same way.
    Rejection follows the embedded address; a public embedded address must
    still be allowed, or unwrapping becomes a blanket IPv6 ban.
    """

    EMBEDDED_NON_PUBLIC = (
        ("2002::1", "0.0.0.0"),                      # 6to4, unspecified
        ("2002:c0a8:101::1", "192.168.1.1"),         # 6to4, private
        ("2002:7f00:1::", "127.0.0.1"),              # 6to4, loopback
        ("2001:0:4136:e378:8000:63bf:f5ff:fffe", "10.0.0.1"),  # Teredo client
        ("::ffff:127.0.0.1", "127.0.0.1"),           # IPv4-mapped, loopback
        ("::ffff:10.0.0.1", "10.0.0.1"),             # IPv4-mapped, private
    )

    def test_embedded_address_is_decoded(self):
        for ip, embedded in self.EMBEDDED_NON_PUBLIC:
            with self.subTest(ip=ip):
                self.assertEqual(
                    str(mcp_connector._embedded_ipv4(ipaddress.ip_address(ip))),
                    embedded,
                )

    def test_plain_addresses_embed_nothing(self):
        for ip in ("1.1.1.1", "2001:4860:4860::8888"):
            with self.subTest(ip=ip):
                self.assertIsNone(
                    mcp_connector._embedded_ipv4(ipaddress.ip_address(ip))
                )

    def test_embedded_non_public_rejected(self):
        # Resolve through the fake resolver so the decision under test is the
        # guard's, not the host's: a runner without IPv6 makes the real
        # getaddrinfo fail, which would reject for the wrong reason.
        for ip, _embedded in self.EMBEDDED_NON_PUBLIC:
            with self.subTest(ip=ip):
                with mock.patch.object(socket, "getaddrinfo", _resolver({ip: (ip,)})):
                    # The message keeps the URL literal, then the parsed
                    # address, which compresses IPv4-mapped forms
                    # (::ffff:10.0.0.1 -> ::ffff:a00:1).
                    shown = str(ipaddress.ip_address(ip))
                    self.assert_rejected(
                        f"https://[{ip}]/mcp",
                        "UNSAFE_ENDPOINT",
                        f"{ip} resolves to {shown}",
                    )

    def test_public_addresses_still_allowed(self):
        # The positive control has to be public on every supported runtime.
        # 2002:808:808::1 is not: Python 3.11 lists 2002::/16 as private, so the
        # envelope alone already rejects it there, while 3.12 does not and the
        # embedded 8.8.8.8 decides. That divergence is why the decode is pinned
        # separately below instead of being asserted through the guard.
        for ip in ("2001:4860:4860::8888", "1.1.1.1"):
            url = f"https://[{ip}]/mcp" if ":" in ip else f"https://{ip}/mcp"
            with self.subTest(ip=ip):
                with mock.patch.object(socket, "getaddrinfo", _resolver({ip: (ip,)})):
                    self.assert_allowed(url)

    def test_public_embedded_ipv4_is_not_itself_unsafe(self):
        # What a 6to4 envelope carries is judged on its own merits.
        self.assertTrue(
            mcp_connector._is_public_destination(ipaddress.ip_address("8.8.8.8"))
        )
        self.assertEqual(
            str(mcp_connector._embedded_ipv4(
                ipaddress.ip_address("2002:808:808::1"))),
            "8.8.8.8",
        )


if __name__ == "__main__":
    unittest.main()
