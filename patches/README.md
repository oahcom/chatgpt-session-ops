# Historical deployed-source integration patch artifacts (not production-ready)

These patches were generated from deployed Python source snapshots on 2026-11-27. **The Sender Gateway patch set is currently stale against `/opt/so-sender`:** its `job_store.py` / `app.py` hunks do not match the current deployed files (verified 2026-11-28), so do not apply it as-is. Rebase/regenerate it against the current deployed-source hashes before any deployment. The patches are intentionally split by service root because MCP and Sender Gateway are deployed separately.

## Apply roots

- Sender Gateway patch artifacts (`sender-gateway-event-outbox.patch`, `sender-gateway-thread-connection-cleanup.patch`, `sender-gateway-failed-terminal-event.patch`, and `sender-gateway-outbox-retention.patch`) are historical. **None is approved for application to the current `/opt/so-sender` tree.** The current source already has a bounded SQLite connection pool, but does not contain the event outbox or event claim/ack routes. Reconstruct the desired changes against the live hashes and validate all patch hunks in a clean fixture before considering deployment.
- MCP patches apply from `/opt/so-mcp`:
  - `mcp-sender-event-client.patch`: add `SenderGatewayClient.claim_events()` / `ack_event()` using the existing mTLS + service-token + HMAC identity assertion path.
  - `mcp-server-events.patch`: register MCP Events request handlers and advertise `capabilities.events`.
  - `mcp-runtime-events.patch`: add key-file loading, opt-in store creation, and ASGI-lifespan-managed collector/delivery workers.
  - `mcp-event-modules.patch`: add `mcp_events.py`, `mcp_event_collector.py`, and `mcp_event_workers.py` with subscribe-time job ownership checks, per-delivery authorization and subscription-expiry rechecks immediately before callback I/O, bounded retry, and retention cleanup.

`deployed-source.sha256` records the original deployed base before any Sender Gateway changes. `deployed-source-post-combined.sha256` records the production state after the combined cleanup + success-outbox patch and is the base for `sender-gateway-failed-terminal-event.patch`. `deployed-source-post-failure-event.sha256` records the current files after that incremental patch was applied to disk. `sender-original-base.sha256` records the original Sender Gateway files preserved under `/var/backups`. The sender-gateway and MCP client patches are not guaranteed to apply to the older `/srv/so-mcp-trusted` client file; the MCP client patch is based on the current deployed `/opt/so-mcp` version.

## Required deployment configuration (not created)

Provision a raw 32-byte AES-GCM key at `/etc/so-mcp/events.key`, owned by the `ubuntu` service user with mode `0400`. Add `SO_MCP_EVENTS_KEY_FILE=/etc/so-mcp/events.key` to the MCP service environment. Optional `SO_MCP_EVENTS_DB` must be an absolute path; otherwise the default is `/var/lib/so-mcp/events/events.sqlite3`. The events directory is created mode `0700`, and the SQLite database mode is `0600`.

Without `SO_MCP_EVENTS_KEY_FILE`, the feature remains disabled and the MCP server does not advertise Events. Setting the key file enables both the Events handlers and the supervised background collector/delivery loops.

## Safety

The live `/opt/so-sender/sender_gateway/job_store.py` and `app.py` inspected on 2026-11-28 do **not** contain `job_event_outbox` or the `/internal/v1/events/claim` and `/internal/v1/events/{event_id}/ack` routes. Their SHA256 values differ from the patch's expected base, and the event-outbox patch fails its first hunk in a read-only fixture. Treat all previous deployment-state notes in this file as historical, not current production evidence. No production files, database rows, credentials, or service state were changed during this verification. The local snapshots under `/dev/shm` must not be treated as production source or used to consume historical outbox records.

## Reproducing isolated deployed-source tests

The deployed-runtime and Sender Gateway tests are preserved under `patches/tests/`. Build clean source snapshots from the exact base files and apply the patch sequence above, then run the corresponding test files with those snapshot roots on `PYTHONPATH`. The test receiver is mocked; no production callback, credential, or database is used.

- `mcp-request-id-idempotency.patch` applies from `/opt/so-mcp` to live
  `src/session_ops/mcp_server.py` base SHA-256
  `4aebe295b53bdc14d99765f61eb1d526329bbacdb604f7dd4eed739439d4a82c`.
  It preserves caller-supplied keys and derives a deterministic UUIDv4 from
  authenticated tenant/principal, MCP session id, tool, and JSON-RPC request id
  when legacy connectors omit the key. Same-request replay is deduplicated;
  a new request id is a new intent. Test with
  `PYTHONPATH=/opt/so-mcp pytest patches/tests/test_mcp_request_id_idempotency.py`.
