# Deployed-source integration tests

These tests exercise the deployed-source patch set without requiring production credentials or touching the live databases.

- `test_sender_event_outbox.py`: run with the isolated Sender Gateway source snapshot on `PYTHONPATH`.
- `test_sender_mcp_event_pipeline.py`: runs a local mTLS Sender Gateway, MCP collector, SQLite outbox, and mocked HTTPS callback.
- `test_deployed_mcp_events_asgi.py`: run with the isolated deployed MCP snapshot after applying `mcp-server-events.patch` and `mcp-runtime-events.patch`; the callback receiver and Sender Gateway are test doubles.

They are kept under `patches/tests/` because `sender_gateway/` and the deployed launcher are installed from a separate runtime tree and are not part of the canonical Git checkout. No production account files, tokens, private keys, or databases are included.
