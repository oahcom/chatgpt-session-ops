"""Redaction tests for ``_redact_memory_command`` in ``scripts/run_mcp_server.py``.

Covers issue #113: a single-dash ``-token=S`` style option skipped the inline
``=`` branch (which only matched ``--``), so the secret was emitted verbatim
into the service log *and* the following argument was replaced with
``<redacted>`` because the bare-flag path armed ``redact_next``.

This is the first test that imports the module at all, so it has to work
around ``import uvicorn`` at module scope: uvicorn is not installed on this
host and cannot be installed, therefore a stub module is registered before the
import. The stub lives here and never in the source or in requirements.

Run with:
    python -m unittest tests.test_run_mcp_server_redaction
"""

import json
import os
import sys
import types
import unittest
from unittest import mock

try:  # pragma: no cover - the stub path is host dependent
    import uvicorn  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["uvicorn"] = types.ModuleType("uvicorn")

from scripts.run_mcp_server import (  # noqa: E402
    MEMORY_STDIO_ENV,
    _redact_memory_command,
    load_memory_adapter_command,
)


class TestRedactInlineSecrets(unittest.TestCase):
    """Every argv shape from the issue table is pinned before/after the fix."""

    def test_double_dash_inline_secret(self):
        self.assertEqual(
            _redact_memory_command(["a", "--token=S", "--verbose"]),
            json.dumps(["a", "--token=<redacted>", "--verbose"]),
        )

    def test_single_dash_inline_secret_does_not_eat_next_argument(self):
        self.assertEqual(
            _redact_memory_command(["a", "-token=S", "--verbose"]),
            json.dumps(["a", "-token=<redacted>", "--verbose"]),
        )

    def test_single_dash_inline_password_only_argument(self):
        self.assertEqual(
            _redact_memory_command(["a", "-password=S"]),
            json.dumps(["a", "-password=<redacted>"]),
        )

    def test_single_dash_inline_secret_keeps_following_value(self):
        self.assertEqual(
            _redact_memory_command(["a", "-secret=S", "--host", "127.0.0.1"]),
            json.dumps(["a", "-secret=<redacted>", "--host", "127.0.0.1"]),
        )

    def test_single_dash_inline_api_key(self):
        self.assertEqual(
            _redact_memory_command(["a", "-api-key=S", "--verbose"]),
            json.dumps(["a", "-api-key=<redacted>", "--verbose"]),
        )

    def test_single_dash_java_system_property(self):
        self.assertEqual(
            _redact_memory_command(["java", "-Dapi-key=S", "-jar", "a.jar"]),
            json.dumps(["java", "-Dapi-key=<redacted>", "-jar", "a.jar"]),
        )

    def test_double_dash_space_separated_secret(self):
        self.assertEqual(
            _redact_memory_command(["a", "--token", "S"]),
            json.dumps(["a", "--token", "<redacted>"]),
        )

    def test_single_dash_space_separated_secret(self):
        self.assertEqual(
            _redact_memory_command(["a", "-token", "S"]),
            json.dumps(["a", "-token", "<redacted>"]),
        )

    def test_non_secret_inline_option_is_untouched(self):
        self.assertEqual(
            _redact_memory_command(["mem-adapter", "-timeout=30"]),
            json.dumps(["mem-adapter", "-timeout=30"]),
        )

    def test_inline_equals_in_non_option_argument_is_untouched(self):
        self.assertEqual(
            _redact_memory_command(["mem-adapter=foo", "--verbose"]),
            json.dumps(["mem-adapter=foo", "--verbose"]),
        )


class TestRedactIssueReproduction(unittest.TestCase):
    """The exact reproduction from the issue."""

    def test_reproduction_keeps_timeout_and_redacts_token(self):
        self.assertEqual(
            _redact_memory_command(
                ["mem-adapter", "-token=s3cr3t", "-timeout=30"]
            ),
            json.dumps(["mem-adapter", "-token=<redacted>", "-timeout=30"]),
        )

    def test_reproduction_never_leaks_the_secret_value(self):
        rendered = _redact_memory_command(
            ["mem-adapter", "-token=s3cr3t", "-timeout=30"]
        )
        self.assertNotIn("s3cr3t", rendered)


class TestRedactEdgeCases(unittest.TestCase):

    def test_none_renders_disabled(self):
        self.assertEqual(_redact_memory_command(None), "disabled")

    def test_empty_list_renders_empty_json_array(self):
        self.assertEqual(_redact_memory_command([]), "[]")

    def test_no_secrets_output_is_unchanged(self):
        argv = ["mem-adapter", "--host", "127.0.0.1", "--port", "8080"]
        self.assertEqual(_redact_memory_command(argv), json.dumps(argv))

    def test_secret_name_match_is_case_insensitive(self):
        self.assertEqual(
            _redact_memory_command(["a", "-ToKeN=S"]),
            json.dumps(["a", "-ToKeN=<redacted>"]),
        )

    def test_uppercase_double_dash_inline_secret(self):
        self.assertEqual(
            _redact_memory_command(["a", "--API-KEY=S"]),
            json.dumps(["a", "--API-KEY=<redacted>"]),
        )


class TestRedactEndToEnd(unittest.TestCase):
    """Env var -> load_memory_adapter_command -> _redact_memory_command."""

    def setUp(self):
        self._env_backup = os.environ.get(MEMORY_STDIO_ENV)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env_backup is None:
            os.environ.pop(MEMORY_STDIO_ENV, None)
        else:
            os.environ[MEMORY_STDIO_ENV] = self._env_backup

    def test_secret_from_environment_never_reaches_the_log_line(self):
        raw = json.dumps([sys.executable, "-token=SuperSecretValue", "--verbose"])

        with mock.patch.dict(os.environ, {MEMORY_STDIO_ENV: raw}):
            command = load_memory_adapter_command()

        self.assertEqual(command[0], os.path.realpath(sys.executable))
        rendered = _redact_memory_command(command)

        self.assertNotIn("SuperSecretValue", rendered)
        self.assertEqual(
            json.loads(rendered),
            [
                command[0],
                "-token=<redacted>",
                "--verbose",
            ],
        )


if __name__ == "__main__":
    unittest.main()
