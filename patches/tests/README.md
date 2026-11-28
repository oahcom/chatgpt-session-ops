# Deployed-source integration tests

These tests exercise deployed-source patch artifacts without requiring production credentials or touching live databases.

- `test_sender_account_identity_pin.py`: applies the explicit-account identity patch and shared account-directory contract patch to a temporary copy of the 2026-11-28 Sender snapshot. It verifies Gateway discovery, CLI identity discovery, and `ChatGPTAPI` credential loading resolve acct1/default and acct2+ to the same physical directories; covers an `accounts/acct1` symlink to the auth root, a legacy `BASE/accounts/acct1` directory, and CLI `BASE/accounts` fallback; tests both `_main_unlocked` and direct `_cli_account_only_create` allowlist preflight (acct5 rejected before `_cli_open_api`, allowlisted acct10 accepted), explicit 403/429 pinning, retained auto-rotation, Adapter mismatch rejection, and account-only 429 classification with retry-after preservation. Synthetic cookies/tokens only. Run with `python3 -m unittest -v patches.tests.test_sender_account_identity_pin`.
- `test_sender_child_env_account_roots.py`: applies the child-environment and shared account-directory patches to an isolated Sender snapshot and proves both configured path roots reach the CLI child, acct10 is discovered from the Gateway root and loaded from the identical CLI credential directory, and unset roots are omitted. Uses synthetic fixture files/tokens only. Run with `python3 -m unittest -v patches.tests.test_sender_child_env_account_roots`.

- `test_sender_event_outbox.py`: run with the isolated Sender Gateway source snapshot on `PYTHONPATH`.
- `test_sender_mcp_event_pipeline.py`: runs a local mTLS Sender Gateway, MCP collector, SQLite outbox, and mocked HTTPS callback.
- `test_deployed_mcp_events_asgi.py`: run with the isolated deployed MCP snapshot after applying `mcp-server-events.patch` and `mcp-runtime-events.patch`; the callback receiver and Sender Gateway are test doubles.

They are kept under `patches/tests/` because `sender_gateway/` and the deployed launcher are installed from a separate runtime tree and are not part of the canonical Git checkout. No production account files, tokens, private keys, or databases are included.
