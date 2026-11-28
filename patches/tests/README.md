# Deployed-source integration tests

These tests exercise the deployed-source patch set without requiring production credentials or touching the live databases.

- `test_sender_event_outbox.py`: run with the isolated Sender Gateway source snapshot on `PYTHONPATH`.
- `test_sender_mcp_event_pipeline.py`: runs a local mTLS Sender Gateway, event collector, SQLite outbox, and mocked HTTPS callback.
- `test_sender_outbox_current_snapshot.py`: validates the current Sender schema, dispatcher-only HTTP routes, lease fencing, and safe migration/quarantine of 75 legacy outbox rows.
- `test_deployed_mcp_events_asgi.py`: run with the isolated deployed MCP snapshot after applying the MCP patch series; covers ASGI lifecycle, dispatcher shutdown, and callback delivery. The callback receiver is a test double.

They are kept under `patches/tests/` because `sender_gateway/` and the deployed launcher are installed from a separate runtime tree and are not part of the canonical Git checkout. No production account files, tokens, private keys, or databases are included.
