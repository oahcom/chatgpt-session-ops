"""Startup-guard tests for ``scripts/run_mcp_server.py``.

Covers issue #114: this is the production entry point (the file systemd execs,
the file ``docs/DEPLOY-so-mcp.md`` tells operators to run) and no test imported
it. Every ``raise`` in the module is enumerated with ``ast`` -- not from a
hand-written list -- and pinned here so a fail-closed startup decision cannot
be silently weakened.

The enumeration at the time of writing (re-derive, do not trust these):

    required_env                  1  (L48)
    _resolve_memory_executable   10  (L55,60,70,76,82,95,101,116,122,127)
    load_memory_adapter_command   4  (L152,157,164,169)
    _validate_https_url           5  (L214,217,220,223,226)
    _realm_namespace_from_issuer  2  (L239,245)
    load_config                   5  (L266,276,281,284,295)
    main                          1  (L537, bare re-raise)
                                  --
                                  28

Platform notes (Windows, the CI host):

* ``os.access(path, os.X_OK)`` returns True for *every* regular file on
  Windows, so the "exists but is not executable" guards (L82, L101, L122) are
  dead code on the real filesystem here. They are exercised by patching
  ``os.access`` to return False -- the guard is literally
  ``if not os.access(...): raise``, so this drives the real branch instead of
  faking a pass.
* ``shutil.which`` only returns paths that pass ``_access_check`` (exists +
  access + not a directory), so L95 (non-regular file from PATH) and L101
  (non-executable from PATH) are unreachable through ``which`` on *any*
  platform; L95/L101 are driven by patching ``shutil.which``.
* L245 (``if not realm``) is unreachable: ``parts`` is built with
  ``if part`` so every element is non-empty, and the ``realms``-at-the-end
  case raises ``IndexError`` which is caught by the L239 handler. The gap is
  pinned rather than skipped: ``test_trailing_slash_still_cannot_derive``
  asserts the L239 message for ``.../realms/`` and fails if the ``if part``
  filter is removed, since the input then reaches L245 and raises
  "derived Keycloak realm namespace is empty" instead.

``import uvicorn`` at module scope is stubbed below; uvicorn is not installed
on this host and cannot be installed. The stub never lives in the source or in
any requirements file.

Run with:
    python -m unittest tests.test_run_mcp_server_startup
"""

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

try:  # pragma: no cover - host dependent
    import uvicorn  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["uvicorn"] = types.ModuleType("uvicorn")

from scripts.run_mcp_server import (  # noqa: E402
    DeploymentASGI,
    MEMORY_STDIO_ENV,
    _realm_namespace_from_issuer,
    _resolve_memory_executable,
    _validate_https_url,
    create_application,
    load_config,
    load_memory_adapter_command,
    main,
    required_env,
)


class TestRequiredEnv(unittest.TestCase):
    """L48: a missing or whitespace-only variable is a startup error."""

    def test_missing_variable_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                required_env("SO_MCP_MISSING")
        self.assertEqual(
            str(ctx.exception),
            "required environment variable is missing: SO_MCP_MISSING",
        )

    def test_whitespace_only_raises(self):
        with mock.patch.dict(os.environ, {"SO_MCP_BLANK": "   \t\n"}):
            with self.assertRaises(RuntimeError) as ctx:
                required_env("SO_MCP_BLANK")
        self.assertEqual(
            str(ctx.exception),
            "required environment variable is missing: SO_MCP_BLANK",
        )

    def test_present_value_is_stripped(self):
        with mock.patch.dict(os.environ, {"SO_MCP_OK": "  value  "}):
            self.assertEqual(required_env("SO_MCP_OK"), "value")


class TestResolveMemoryExecutable(unittest.TestCase):
    """The 10 guards of ``_resolve_memory_executable`` (L55-127)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_file(self, name, *, executable=True):
        path = self.tmp / name
        path.write_bytes(b"#!/bin/sh\n")
        if executable:
            os.chmod(path, 0o755)
        else:
            os.chmod(path, 0o644)
        return path

    # L55 ------------------------------------------------------------------
    def test_empty_name_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_memory_executable("")
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV + " command has an empty executable path",
        )

    def test_blank_name_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_memory_executable("   ")
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV + " command has an empty executable path",
        )

    # L60 ------------------------------------------------------------------
    def test_nul_in_name_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_memory_executable("bad\x00name")
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV + " executable path contains a NUL character",
        )

    # L70 ------------------------------------------------------------------
    def test_missing_path_with_separator_raises(self):
        missing = self.tmp / "does-not-exist"
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_memory_executable(str(missing))
        self.assertEqual(
            str(ctx.exception),
            "memory adapter executable does not exist: %s" % missing,
        )

    # L76 ------------------------------------------------------------------
    def test_directory_with_separator_raises(self):
        directory = self.tmp / "adir"
        directory.mkdir()
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_memory_executable(str(directory))
        self.assertEqual(
            str(ctx.exception),
            "memory adapter executable is not a regular file: %s" % directory,
        )

    # L82 ------------------------------------------------------------------
    # os.access(X_OK) is always True for a regular file on Windows; the guard
    # is `if not os.access(...)`, so patch it to drive the real branch.
    def test_file_with_separator_not_executable_raises(self):
        path = self._make_file("tool", executable=False)
        with mock.patch("os.access", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_memory_executable(str(path))
        self.assertEqual(
            str(ctx.exception),
            "memory adapter executable is not executable: %s" % path,
        )

    # L95 ------------------------------------------------------------------
    # shutil.which never returns a directory, so feed it one directly.
    def test_which_returns_non_regular_file_raises(self):
        directory = self.tmp / "whichdir"
        directory.mkdir()
        with mock.patch(
            "scripts.run_mcp_server.shutil.which",
            return_value=str(directory),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_memory_executable("somename")
        self.assertEqual(
            str(ctx.exception),
            "memory adapter executable is not a regular file: %s" % directory,
        )

    # L101 -----------------------------------------------------------------
    def test_which_returns_non_executable_raises(self):
        path = self._make_file("resolved", executable=False)
        with mock.patch(
            "scripts.run_mcp_server.shutil.which",
            return_value=str(path),
        ), mock.patch("os.access", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_memory_executable("resolved")
        self.assertEqual(
            str(ctx.exception),
            "memory adapter executable is not executable: %s" % path,
        )

    # L116 -----------------------------------------------------------------
    def test_path_entry_directory_raises(self):
        (self.tmp / "blocker").mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_memory_executable("blocker")
        self.assertEqual(
            str(ctx.exception),
            "memory adapter executable is not a regular file: %s"
            % (self.tmp / "blocker"),
        )

    # L122 -----------------------------------------------------------------
    def test_path_entry_non_executable_raises(self):
        path = self._make_file("noext", executable=False)
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}), mock.patch(
            "os.access", return_value=False
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_memory_executable("noext")
        self.assertEqual(
            str(ctx.exception),
            "memory adapter executable is not executable: %s" % path,
        )

    # L127 -----------------------------------------------------------------
    def test_not_found_on_path_raises(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_memory_executable("definitely-not-here-xyz")
        self.assertEqual(
            str(ctx.exception),
            "memory adapter executable was not found on PATH: "
            "definitely-not-here-xyz",
        )

    # acceptance -----------------------------------------------------------
    def test_absolute_path_resolves(self):
        path = self._make_file("abs-tool")
        self.assertEqual(
            _resolve_memory_executable(str(path)),
            str(path.resolve()),
        )

    def test_bare_name_resolved_through_path(self):
        # shutil.which consults PATHEXT on Windows, so only there does a file
        # named "bare-tool.exe" satisfy a lookup for "bare-tool". On POSIX the
        # filename must match exactly, so the suffix must not be appended.
        name = "bare-tool" + (".exe" if os.name == "nt" else "")
        path = self._make_file(name)
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            resolved = _resolve_memory_executable("bare-tool")
        self.assertEqual(resolved, str(path.resolve()))


class TestLoadMemoryAdapterCommand(unittest.TestCase):
    """The 4 guards of ``load_memory_adapter_command`` (L152-169)."""

    def test_unset_returns_none(self):
        env = {k: v for k, v in os.environ.items() if k != MEMORY_STDIO_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(load_memory_adapter_command())

    # L152 -----------------------------------------------------------------
    def test_invalid_json_raises(self):
        with mock.patch.dict(os.environ, {MEMORY_STDIO_ENV: "not json"}):
            with self.assertRaises(RuntimeError) as ctx:
                load_memory_adapter_command()
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV + " must contain a valid JSON array of strings",
        )

    # L157 -----------------------------------------------------------------
    def test_object_not_array_raises(self):
        with mock.patch.dict(os.environ, {MEMORY_STDIO_ENV: json.dumps({})}):
            with self.assertRaises(RuntimeError) as ctx:
                load_memory_adapter_command()
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV
            + " must be a non-empty JSON array of command arguments",
        )

    def test_empty_array_raises(self):
        with mock.patch.dict(os.environ, {MEMORY_STDIO_ENV: "[]"}):
            with self.assertRaises(RuntimeError) as ctx:
                load_memory_adapter_command()
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV
            + " must be a non-empty JSON array of command arguments",
        )

    # L164 -----------------------------------------------------------------
    def test_non_string_element_raises(self):
        with mock.patch.dict(os.environ, {MEMORY_STDIO_ENV: '["ok", 5]'}):
            with self.assertRaises(RuntimeError) as ctx:
                load_memory_adapter_command()
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV + " argument 1 must be a non-empty string",
        )

    def test_empty_string_element_raises(self):
        with mock.patch.dict(os.environ, {MEMORY_STDIO_ENV: '["ok", ""]'}):
            with self.assertRaises(RuntimeError) as ctx:
                load_memory_adapter_command()
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV + " argument 1 must be a non-empty string",
        )

    # L169 -----------------------------------------------------------------
    def test_nul_in_element_raises(self):
        with mock.patch.dict(
            os.environ, {MEMORY_STDIO_ENV: '["ok", "a\\u0000b"]'}
        ):
            with self.assertRaises(RuntimeError) as ctx:
                load_memory_adapter_command()
        self.assertEqual(
            str(ctx.exception),
            MEMORY_STDIO_ENV + " argument 1 contains a NUL character",
        )


class TestValidateHttpsUrl(unittest.TestCase):
    """The 5 guards of ``_validate_https_url`` (L214-226)."""

    # L214 -----------------------------------------------------------------
    def test_out_of_range_port_is_invalid(self):
        with self.assertRaises(RuntimeError) as ctx:
            _validate_https_url("SO_MCP_ISSUER", "https://x:99999/y", require_path=True)
        self.assertEqual(str(ctx.exception), "SO_MCP_ISSUER is not a valid URL")

    # L217 -----------------------------------------------------------------
    def test_http_scheme_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            _validate_https_url("SO_MCP_ISSUER", "http://x/y", require_path=True)
        self.assertEqual(
            str(ctx.exception), "SO_MCP_ISSUER must be an absolute HTTPS URL"
        )

    def test_https_without_host_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            _validate_https_url("SO_MCP_ISSUER", "https://", require_path=False)
        self.assertEqual(
            str(ctx.exception), "SO_MCP_ISSUER must be an absolute HTTPS URL"
        )

    # L220 -----------------------------------------------------------------
    def test_credentials_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            _validate_https_url(
                "SO_MCP_ISSUER", "https://user:pass@x/y", require_path=True
            )
        self.assertEqual(
            str(ctx.exception), "SO_MCP_ISSUER must not contain URL credentials"
        )

    # L223 -----------------------------------------------------------------
    def test_query_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            _validate_https_url("SO_MCP_ISSUER", "https://x/y?a=1", require_path=True)
        self.assertEqual(
            str(ctx.exception),
            "SO_MCP_ISSUER must not contain a query or fragment",
        )

    def test_fragment_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            _validate_https_url("SO_MCP_ISSUER", "https://x/y#frag", require_path=True)
        self.assertEqual(
            str(ctx.exception),
            "SO_MCP_ISSUER must not contain a query or fragment",
        )

    # L226 -----------------------------------------------------------------
    def test_require_path_rejects_root(self):
        with self.assertRaises(RuntimeError) as ctx:
            _validate_https_url("SO_MCP_ISSUER", "https://x/", require_path=True)
        self.assertEqual(
            str(ctx.exception),
            "SO_MCP_ISSUER must contain a non-root resource path",
        )

    def test_require_path_rejects_bare_host(self):
        with self.assertRaises(RuntimeError) as ctx:
            _validate_https_url("SO_MCP_ISSUER", "https://x", require_path=True)
        self.assertEqual(
            str(ctx.exception),
            "SO_MCP_ISSUER must contain a non-root resource path",
        )

    def test_accepting_case(self):
        value = "https://x/realms/y"
        self.assertEqual(
            _validate_https_url("SO_MCP_ISSUER", value, require_path=True), value
        )

    def test_root_allowed_when_path_not_required(self):
        self.assertEqual(
            _validate_https_url("SO_MCP_AUDIENCE", "https://x/", require_path=False),
            "https://x/",
        )


class TestRealmNamespaceFromIssuer(unittest.TestCase):
    """The 2 guards of ``_realm_namespace_from_issuer`` (L239, L245)."""

    def test_normal_issuer(self):
        self.assertEqual(
            _realm_namespace_from_issuer("https://kc.example.com/realms/my-realm"),
            "my-realm",
        )

    # L239 -----------------------------------------------------------------
    def test_no_realms_segment_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            _realm_namespace_from_issuer("https://kc.example.com/auth")
        self.assertEqual(
            str(ctx.exception),
            "cannot derive realm namespace from SO_MCP_ISSUER; "
            "set SO_MCP_REALM_NAMESPACE explicitly",
        )

    def test_realms_at_end_raises(self):
        # parts.index succeeds, parts[index+1] raises IndexError -> same handler.
        with self.assertRaises(RuntimeError) as ctx:
            _realm_namespace_from_issuer("https://kc.example.com/realms")
        self.assertEqual(
            str(ctx.exception),
            "cannot derive realm namespace from SO_MCP_ISSUER; "
            "set SO_MCP_REALM_NAMESPACE explicitly",
        )

    # L245 is unreachable because the `if part` filter at L233 drops the empty
    # segment a trailing slash would produce, so parts == ["realms"] and
    # parts[1] raises IndexError into the L239 handler. This test pins that:
    # remove the filter and "/realms/" yields parts == ["realms", ""], reaches
    # L245, and raises "derived Keycloak realm namespace is empty" instead --
    # so this assertion is a mutation detector for the filter, not a fake pass.
    def test_trailing_slash_still_cannot_derive(self):
        with self.assertRaises(RuntimeError) as ctx:
            _realm_namespace_from_issuer("https://kc.example.com/realms/")
        self.assertEqual(
            str(ctx.exception),
            "cannot derive realm namespace from SO_MCP_ISSUER; "
            "set SO_MCP_REALM_NAMESPACE explicitly",
        )


class TestLoadConfig(unittest.TestCase):
    """The 5 guards of ``load_config`` (L266,276,281,284,295) + happy path."""

    def _base_env(self):
        return {
            "SO_MCP_ISSUER": "https://issuer.example.com/realms/test",
            "SO_MCP_AUDIENCE": "https://api.example.com/mcp",
            "SO_MCP_DB": "/var/lib/so-mcp/store.db",
            "SO_MCP_TENANT_CLAIM": "tenant",
            "SO_MCP_HOST": "127.0.0.1",
            "SO_MCP_PORT": "8791",
            "UVICORN_LOG_LEVEL": "info",
        }

    def _env_with(self, **overrides):
        env = self._base_env()
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        # drop everything else so a stray real SO_MCP_* cannot leak in
        return {k: v for k, v in env.items()}

    def _run(self, **overrides):
        env = self._env_with(**overrides)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SO_MCP_")}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            return load_config()

    # happy path -----------------------------------------------------------
    def test_happy_path(self):
        config = self._run()
        self.assertEqual(
            config,
            {
                "issuer": "https://issuer.example.com/realms/test",
                "audience": "https://api.example.com/mcp",
                "database_path": "/var/lib/so-mcp/store.db",
                "tenant_claim": "tenant",
                "realm_namespace": "test",
                "host": "127.0.0.1",
                "port": 8791,
                "log_level": "info",
                "discovery_url": None,
                "memory_adapter_command": None,
            },
        )

    # L266 -----------------------------------------------------------------
    def test_in_memory_db_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(SO_MCP_DB=":memory:")
        self.assertEqual(
            str(ctx.exception),
            "SO_MCP_DB must be a persistent filesystem path",
        )

    # host: loopback-only contract (docs/DEPLOY-so-mcp.md section 2a) -------
    # Issue #127: every non-loopback value used to reach uvicorn.run(host=...).
    # The old exact assertion "SO_MCP_HOST must not be empty" conflicted with
    # the merged contract message; it is now a keyword assertion below, and
    # the full accept/reject matrix is pinned by the two table-driven tests.
    def test_empty_host_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(SO_MCP_HOST="   ")
        message = str(ctx.exception)
        self.assertIn("must not be empty", message)
        self.assertIn("loopback", message)

    def test_host_accepts_loopback_values(self):
        for value in ("127.0.0.1", "::1"):
            with self.subTest(host=value):
                config = self._run(SO_MCP_HOST=value)
                self.assertEqual(config["host"], value)

    def test_host_rejects_non_loopback_values(self):
        rejected = (
            "",                 # empty
            "   ",              # whitespace-only (old L276 empty guard)
            "0.0.0.0",          # wildcard IPv4
            "::",               # wildcard IPv6
            "10.0.0.5",         # private IPv4
            "172.16.0.1",       # private IPv4
            "8.8.8.8",          # public IPv4
            "2001:db8::1",      # public IPv6 (documentation range)
            "example.com",      # DNS name
            "localhost",        # DNS name resolving to loopback
            "127.0.0.2",        # loopback range, wrong spelling
            "[::1]",            # bracketed IPv6, wrong spelling
        )
        for value in rejected:
            with self.subTest(host=value):
                with self.assertRaises(RuntimeError) as ctx:
                    self._run(SO_MCP_HOST=value)
                message = str(ctx.exception)
                self.assertIn("loopback", message)
                self.assertIn("127.0.0.1", message)
                self.assertIn("::1", message)

    # L281 -----------------------------------------------------------------
    def test_non_integer_port_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(SO_MCP_PORT="abc")
        self.assertEqual(str(ctx.exception), "SO_MCP_PORT must be an integer")

    # L284 -----------------------------------------------------------------
    def test_port_zero_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(SO_MCP_PORT="0")
        self.assertEqual(
            str(ctx.exception), "SO_MCP_PORT must be between 1 and 65535"
        )

    def test_port_too_high_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(SO_MCP_PORT="65536")
        self.assertEqual(
            str(ctx.exception), "SO_MCP_PORT must be between 1 and 65535"
        )

    def test_port_negative_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(SO_MCP_PORT="-1")
        self.assertEqual(
            str(ctx.exception), "SO_MCP_PORT must be between 1 and 65535"
        )

    # L295 -----------------------------------------------------------------
    def test_invalid_log_level_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(UVICORN_LOG_LEVEL="verbose")
        self.assertEqual(str(ctx.exception), "UVICORN_LOG_LEVEL is invalid")


class TestDeploymentASGILifespan(unittest.TestCase):
    def test_background_dispatcher_starts_and_stops_with_asgi_lifespan(self):
        calls = []

        class Task:
            def start(self):
                calls.append("start")

            def stop(self):
                calls.append("stop")

        class Inner:
            async def __call__(self, scope, receive, send):
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return

        messages = iter((
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ))
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        app = DeploymentASGI(
            Inner(),
            issuer="https://issuer.example.test/realm",
            audience="https://resource.example.test/mcp",
            background_tasks=[Task()],
        )
        asyncio.run(app({"type": "lifespan"}, receive, send))

        self.assertEqual(calls, ["start", "stop"])
        self.assertEqual(
            [message["type"] for message in sent],
            ["lifespan.startup.complete", "lifespan.shutdown.complete"],
        )

    def test_failed_stop_is_not_retried_from_finally(self):
        calls = []

        class Task:
            def start(self):
                calls.append("start")

            def stop(self):
                calls.append("stop")
                raise TimeoutError("dispatcher did not drain")

        class Inner:
            async def __call__(self, scope, receive, send):
                await receive()
                await send({"type": "lifespan.startup.complete"})
                await receive()

        messages = iter((
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ))

        async def receive():
            return next(messages)

        async def send(_message):
            return None

        app = DeploymentASGI(
            Inner(),
            issuer="https://issuer.example.test/realm",
            audience="https://resource.example.test/mcp",
            background_tasks=[Task()],
        )
        with self.assertRaisesRegex(TimeoutError, "did not drain"):
            asyncio.run(app({"type": "lifespan"}, receive, send))

        self.assertEqual(calls, ["start", "stop"])


class TestMCPEventsRuntimeWiring(unittest.TestCase):
    def test_create_application_wires_local_event_store_and_dispatcher(self):
        config = {
            "database_path": "/tmp/session-ops-runtime-test.sqlite3",
            "issuer": "https://issuer.example.test/realm",
            "audience": "https://resource.example.test/mcp",
            "realm_namespace": "realm",
            "tenant_claim": "tenant_id",
            "discovery_url": None,
            "memory_adapter_command": None,
        }
        events_store = mock.Mock()
        dispatcher = mock.Mock()
        mcp_app = object()

        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "events.key"
            key_path.write_bytes(b"e" * 32)
            key_path.chmod(0o400)
            events_db = config["database_path"]

            with (
                mock.patch.dict(os.environ, {
                    "SO_MCP_EVENTS_KEY_FILE": str(key_path),
                    "SO_MCP_EVENTS_DB": events_db,
                }),
                mock.patch("scripts.run_mcp_server.SecureWorkItemStore") as store_type,
                mock.patch("scripts.run_mcp_server.JWTVerifier"),
                mock.patch("scripts.run_mcp_server.MCPEventsStore", return_value=events_store) as events_type,
                mock.patch("scripts.run_mcp_server.MCPEventDispatcher", return_value=dispatcher) as dispatcher_type,
                mock.patch("scripts.run_mcp_server.build_mcp_server", return_value=mcp_app) as build_server,
            ):
                app = create_application(config)

        store_type.return_value.initialize.assert_called_once_with()
        events_type.assert_called_once_with(str(Path(events_db).resolve()), encryption_key=b"e" * 32)
        store_type.assert_called_once_with(
            Path(events_db).resolve(),
            completion_event_publisher=events_store.publish_job_completed_in_transaction,
        )
        dispatcher_type.assert_called_once()
        self.assertIs(dispatcher_type.call_args.args[0], events_store)
        self.assertTrue(callable(dispatcher_type.call_args.kwargs["authorize"]))
        build_server.assert_called_once()
        self.assertIs(build_server.call_args.kwargs["events_store"], events_store)
        self.assertIs(app.inner, mcp_app)
        self.assertEqual(app.background_tasks, (dispatcher,))


class TestMain(unittest.TestCase):
    """L537: startup failure is logged then re-raised unchanged."""

    def test_uvicorn_graceful_shutdown_budget_is_explicit(self):
        config = {
            "issuer": "https://issuer.example.test/realm",
            "audience": "https://resource.example.test/mcp",
            "host": "127.0.0.1",
            "port": 8791,
            "log_level": "info",
            "discovery_url": None,
            "memory_adapter_command": None,
        }
        app = object()
        with (
            mock.patch("scripts.run_mcp_server.load_config", return_value=config),
            mock.patch("scripts.run_mcp_server.create_application", return_value=app),
            mock.patch("scripts.run_mcp_server.uvicorn.run", create=True) as run,
        ):
            main()
        run.assert_called_once_with(
            app,
            host="127.0.0.1",
            port=8791,
            log_level="info",
            access_log=True,
            timeout_graceful_shutdown=5,
        )

    def test_load_config_failure_propagates(self):
        boom = RuntimeError("SO_MCP_PORT must be an integer")
        with mock.patch(
            "scripts.run_mcp_server.load_config", side_effect=boom
        ), mock.patch("scripts.run_mcp_server.create_application") as create:
            with self.assertRaises(RuntimeError) as ctx:
                main()
        self.assertIs(ctx.exception, boom)
        self.assertEqual(str(ctx.exception), "SO_MCP_PORT must be an integer")
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
