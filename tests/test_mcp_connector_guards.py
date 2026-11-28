"""Guard tests for ``scripts/mcp_connector.py`` that the URL-shape suite does
not reach (issue #154).

A mutation audit of the module counted nine effective ``raise`` guards (ten
``raise`` statements minus the ``SystemExit`` self-check). Five of them are the
URL-shape / malformed-port checks in ``assert_safe_url``; each already has a
named sub-case in ``tests/test_mcp_connector_url_guard.py``, and
``UNSAFE_ENDPOINT: ... resolves to {ip}`` is killed by the module's own
``__main__`` self-check at import time. Three guards were survivors:

* the ``socket.gaierror`` branch in ``assert_safe_url`` -- every existing test
  either mocked a *successful* ``getaddrinfo`` or passed a numeric literal, so
  nothing ever raised a DNS failure;
* both ``AUTH_MISSING`` guards in ``load_token`` -- ``load_token`` had zero
  references anywhere in ``tests/`` or ``tools/tests/``.

Every rejection here asserts the *exact* exception type. That is load-bearing:
deleting a guard does not make the call return quietly, it falls through to a
later statement (``for info in infos`` -> ``UnboundLocalError``,
``open(path)`` -> ``FileNotFoundError``), so "an exception was raised" would
let those mutants live. ``assert_guard`` therefore catches ``BaseException``
(including those fall-through errors) and then fails on the type assertion.
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mcp_connector  # noqa: E402
from mcp_connector import CheckError, assert_safe_url  # noqa: E402


class GuardAssertions(unittest.TestCase):
    def assert_guard(self, exc_type, code, call, *args):
        """Run ``call`` and require the *exact* ``exc_type`` with ``code``.

        Falls through to an AssertionError rather than an error when the guard
        is missing and a downstream statement raises something else.
        """
        with self.assertRaises(BaseException) as ctx:
            call(*args)
        exc = ctx.exception
        self.assertIs(
            type(exc),
            exc_type,
            "expected %s, got %s: %s" % (exc_type.__name__, type(exc).__name__, exc),
        )
        if code is not None:
            self.assertEqual(exc.code, code)
        return exc


class TestDnsResolutionFailure(GuardAssertions):
    """The ``except socket.gaierror`` branch in ``assert_safe_url``."""

    def test_gaierror_is_reported_as_unsafe_endpoint(self):
        failure = socket.gaierror(-2, "Name or service not known")
        with mock.patch.object(socket, "getaddrinfo", side_effect=failure):
            exc = self.assert_guard(
                CheckError,
                "UNSAFE_ENDPOINT",
                assert_safe_url,
                "https://nonexistent.invalid/mcp",
            )
        message = str(exc)
        self.assertTrue(
            message.startswith("UNSAFE_ENDPOINT: dns resolution failed"),
            message,
        )
        self.assertIn("dns resolution failed", message)
        self.assertIs(exc.__cause__, failure, "the gaierror must be chained")

    def test_every_gaierror_lands_on_the_same_guard(self):
        for errno in (-2, -3, socket.EAI_NONAME, socket.EAI_AGAIN):
            with self.subTest(errno=errno):
                failure = socket.gaierror(errno, "no name")
                with mock.patch.object(socket, "getaddrinfo", side_effect=failure):
                    exc = self.assert_guard(
                        CheckError,
                        "UNSAFE_ENDPOINT",
                        assert_safe_url,
                        "https://nope.invalid/mcp",
                    )
                self.assertIn("dns resolution failed", str(exc))


class TmpAccountDirs(unittest.TestCase):
    """Point the module's account roots at a throwaway directory tree.

    ``token_path`` reads the module-level ``SKILL_DIR`` / ``ACCOUNTS_DIR`` at
    call time, so patching those two names exercises the real ``os.path``
    logic instead of mocking ``os.path.exists`` (which would only test the
    mock).
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mcp154-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.skill_dir = os.path.join(self.root, "skill")
        self.accounts_dir = os.path.join(self.root, "accounts")
        os.makedirs(self.skill_dir)
        os.makedirs(self.accounts_dir)
        for name, value in (("SKILL_DIR", self.skill_dir),
                            ("ACCOUNTS_DIR", self.accounts_dir)):
            patcher = mock.patch.object(mcp_connector, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def account_file(self, account):
        return os.path.join(self.accounts_dir, account, "access_token.txt")

    def write_token(self, account, content):
        path = mcp_connector.token_path(account)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path


class TestLoadToken(GuardAssertions, TmpAccountDirs):
    """``load_token`` had no test at all before issue #154."""

    def test_token_path_branches(self):
        self.assertEqual(
            mcp_connector.token_path("acct1"),
            os.path.join(self.skill_dir, "access_token.txt"),
        )
        self.assertEqual(
            mcp_connector.token_path("acct9"),
            self.account_file("acct9"),
        )

    def test_acct1_prefers_canonical_accounts_directory(self):
        canonical = os.path.join(self.accounts_dir, "acct1")
        os.makedirs(canonical)
        self.assertEqual(
            mcp_connector.token_path("acct1"),
            os.path.join(canonical, "access_token.txt"),
        )

    def test_acct1_symlink_directory_does_not_override_legacy_root(self):
        external = os.path.join(self.root, "external")
        os.makedirs(external)
        os.symlink(external, os.path.join(self.accounts_dir, "acct1"))
        legacy = os.path.join(self.skill_dir, "access_token.txt")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("legacy-token")
        self.assertEqual(mcp_connector.token_path("acct1"), legacy)
        self.assertEqual(mcp_connector.discover_accounts(), ["acct1"])

    def test_token_path_rejects_noncanonical_account_names(self):
        for account in ("acct0", "acct01", "acctx", "../acct1"):
            with self.subTest(account=account):
                with self.assertRaisesRegex(CheckError, "acctN"):
                    mcp_connector.token_path(account)

    def test_missing_token_file_is_auth_missing(self):
        account = "acct9"
        os.makedirs(os.path.join(self.accounts_dir, account))
        path = self.account_file(account)
        self.assertFalse(os.path.exists(path), "precondition: file must be absent")

        exc = self.assert_guard(
            CheckError, "AUTH_MISSING", mcp_connector.load_token, account
        )
        self.assertEqual(exc.detail, f"{account}: no access_token.txt at {path}")
        self.assertTrue(exc.detail.startswith(f"{account}: "), exc.detail)
        self.assertIn("no access_token.txt at", exc.detail)
        self.assertEqual(str(exc), f"AUTH_MISSING: {exc.detail}")

    def test_missing_token_file_for_acct1_reads_skill_dir(self):
        path = os.path.join(self.skill_dir, "access_token.txt")
        exc = self.assert_guard(
            CheckError, "AUTH_MISSING", mcp_connector.load_token, "acct1"
        )
        self.assertIn("no access_token.txt at", exc.detail)
        self.assertIn(path, exc.detail)
        self.assertTrue(exc.detail.startswith("acct1: "), exc.detail)

    def test_empty_token_file_is_auth_missing(self):
        account = "acct9"
        path = self.write_token(account, "  \n\t\n")
        self.assertTrue(os.path.exists(path))

        exc = self.assert_guard(
            CheckError, "AUTH_MISSING", mcp_connector.load_token, account
        )
        self.assertEqual(exc.detail, f"{account}: empty token file")
        self.assertTrue(exc.detail.startswith(f"{account}: "), exc.detail)
        self.assertIn("empty token file", exc.detail)
        self.assertEqual(str(exc), f"AUTH_MISSING: {exc.detail}")

    def test_whitespace_only_token_is_empty(self):
        for content in ("", "\n", " \r\n\t "):
            with self.subTest(content=content):
                account = "acct9"
                self.write_token(account, content)
                exc = self.assert_guard(
                    CheckError, "AUTH_MISSING", mcp_connector.load_token, account
                )
                self.assertIn("empty token file", exc.detail)

    def test_token_is_returned_stripped(self):
        cases = (("acct1", "  skill-token-abc \n\n"),
                 ("acct9", "\tacct-token-xyz\r\n"))
        for account, content in cases:
            with self.subTest(account=account):
                self.write_token(account, content)
                self.assertEqual(
                    mcp_connector.load_token(account),
                    content.strip(),
                )


class TestDynamicAccountDiscovery(TmpAccountDirs):
    def test_discover_accounts_uses_natural_numeric_order(self):
        for name in ("acct10", "acct2", "acct1", "acct0", "acct01", "not-an-account"):
            os.makedirs(os.path.join(self.accounts_dir, name))
        self.assertEqual(mcp_connector.discover_accounts(), ["acct1", "acct2", "acct10"])

    def test_accounts_all_reports_directory_permission_errors(self):
        with mock.patch.object(os, "scandir", side_effect=PermissionError("denied")):
            with mock.patch.object(sys, "stdout", io.StringIO()) as out:
                rc = mcp_connector.main(["eligibility", "--accounts", "all"])
        self.assertEqual(rc, 1)
        self.assertIn("ACCOUNT_DISCOVERY_FAILED", out.getvalue())

    def test_accounts_all_uses_dynamic_discovery(self):
        for name in ("acct10", "acct2", "acct1"):
            os.makedirs(os.path.join(self.accounts_dir, name))
        seen = []

        def fake_eligibility(account, capability):
            seen.append(account)
            return mcp_connector.Eligibility(account=account, capability=capability, status="OK")

        with mock.patch.object(mcp_connector, "eligibility", side_effect=fake_eligibility):
            with mock.patch.object(sys, "stdout", io.StringIO()):
                rc = mcp_connector.main(["eligibility", "--accounts", "all"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["acct1", "acct2", "acct10"])


class TestMainAuthMissingExitCode(GuardAssertions, TmpAccountDirs):
    """Pin the exit code for a missing credential file.

    The module docstring used to claim ``2``. ``main()`` has no ``return 2``;
    ``AUTH_MISSING`` flows into ``return 1 if any(status.startswith('AUTH'))``.
    The docstring was corrected (issue #154), the behaviour was not: callers
    key on the exit code, so renumbering it to 2 would be a silent regression.
    """

    def run_main(self, argv):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            try:
                rc = mcp_connector.main(argv)
            except BaseException as exc:  # noqa: BLE001 - surfaced as a failure
                self.fail(f"main() raised {type(exc).__name__}: {exc}")
        return rc, out.getvalue()

    def test_auth_missing_exits_1(self):
        account = "acct9"
        os.makedirs(os.path.join(self.accounts_dir, account))
        rc, text = self.run_main(["eligibility", "--account", account])
        self.assertEqual(rc, 1, text)
        self.assertIn("AUTH_MISSING", text)

    def test_auth_missing_exits_1_across_multiple_accounts(self):
        os.makedirs(os.path.join(self.accounts_dir, "acct9"))
        os.makedirs(os.path.join(self.accounts_dir, "acct10"))
        rc, text = self.run_main(["eligibility", "--accounts", "acct9,acct10"])
        self.assertEqual(rc, 1, text)
        self.assertEqual(text.count("AUTH_MISSING"), 2, text)

    def test_eligibility_status_is_auth_missing(self):
        # Minimal driver for the same path, in case main() grows more setup:
        # main() returns 1 for any row whose status is AUTH_MISSING (see the
        # class docstring), which this status produces.
        account = "acct9"
        os.makedirs(os.path.join(self.accounts_dir, account))
        try:
            row = mcp_connector.eligibility(account)
        except BaseException as exc:  # noqa: BLE001 - surfaced as a failure
            self.fail(f"eligibility() raised {type(exc).__name__}: {exc}")
        self.assertEqual(row.status, "AUTH_MISSING")
        self.assertEqual(row.reason, f"{account}: no access_token.txt at "
                                     f"{self.account_file(account)}")


if __name__ == "__main__":
    unittest.main()
