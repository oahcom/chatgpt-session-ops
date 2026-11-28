# Deployed-source integration tests

These tests exercise deployed-source patch artifacts without requiring production credentials or touching live databases.

- `test_sender_account_identity_pin.py`: applies `patches/sender-account-identity-pin.patch` to a temporary copy of the 2026-11-28 Sender snapshot and tests explicit-account allowlist preflight, explicit 403 single-account failure, explicit 429 no-rotation behavior, retained auto-rotation after 403/429, and Adapter mismatch rejection. Account-discovery marker files are empty test fixtures; no credentials are read. Run with `python3 -m unittest -v patches.tests.test_sender_account_identity_pin`.
- `test_sender_child_env_account_roots.py`: applies `patches/sender-child-env-account-roots.patch` to an isolated Sender Adapter copy and proves the two configured path roots are passed to the CLI child, acct10 is discovered from the Gateway accounts root, and `ChatGPTAPI` loads acct10 under `CHATGPT_AUTH_DIR`; also proves unset roots are omitted. Uses synthetic fixture files/tokens only. Run with `python3 -m unittest -v patches.tests.test_sender_child_env_account_roots`.

- `test_sender_event_outbox.py`: run with the isolated Sender Gateway source snapshot on `PYTHONPATH`.
- `test_sender_mcp_event_pipeline.py`: runs a local mTLS Sender Gateway, MCP collector, SQLite outbox, and mocked HTTPS callback.
- `test_deployed_mcp_events_asgi.py`: run with the isolated deployed MCP snapshot after applying `mcp-server-events.patch` and `mcp-runtime-events.patch`; the callback receiver and Sender Gateway are test doubles.

They are kept under `patches/tests/` because `sender_gateway/` and the deployed launcher are installed from a separate runtime tree and are not part of the canonical Git checkout. No production account files, tokens, private keys, or databases are included.
