# Deployed-source integration patches (Sender outbox partially applied; MCP Events not enabled)

These patches were generated from the live deployed Python source trees on 2026-11-27 and validated against pristine copies of those exact files. They are intentionally split by service root because MCP and Sender Gateway are deployed separately.

## Apply roots

- Sender Gateway patches apply from `/opt/so-sender`:
  - `sender-gateway-event-outbox.patch`: add `job_event_outbox` in the same SQLite transaction as `JobStore.complete()`, leased claim/ack methods, dispatcher-only HTTP routes, and delivered-event retention.
  - `sender-gateway-thread-connection-cleanup.patch`: close and unregister each request thread's JobStore SQLite connection in the HTTP handler `finish()` hook.
  - `sender-gateway-failed-terminal-event.patch`: incremental addition of failure-terminal events, applied on disk but not yet loaded by the running service.
  - `sender-gateway-outbox-retention.patch`: incremental retention index and 30-day pruning of terminal (`delivered`/`failed`) outbox rows only. Never deletes `pending` or `sending` rows. Based on the exact current deployed source SHA256 in `deployed-source-post-failure-event.sha256`. Validated against a pristine copy of that file; not deployed. This hotfix was applied to `/opt/so-sender` and the service restarted after a backup was saved at `/var/backups/so-sender-thread-cleanup-20261127`. The incident had accumulated 509 per-request connections and 1023/1024 FDs, causing SQLite `CANTOPEN` errors; after the fix, 20 parallel SO-MCP session-list probes succeeded with 14 FDs still open.
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

The exact combined patch `sender-gateway-applied-combined.patch` (connection cleanup plus success outbox/claim/ack) has been applied to `/opt/so-sender`; the original two source files are backed up at `/var/backups/so-sender-thread-cleanup-20261127`. `so-sender.service` was restarted and is active. The incremental failure-event patch is on disk but is not yet loaded by the running process. The additional retention patch is isolated only and has not been applied to production. At the last read-only inspection, 18 jobs were queued, 1 was running, and 32 success events were pending in the Sender Gateway outbox; no rows were older than 30 days. No MCP Events patch has been applied to `/opt/so-mcp`; no MCP collector or webhook delivery worker is active. The current MCP implementation checks job ownership at subscribe time and before each delivery, and periodically prunes expired/terminal records. The local snapshots under `/dev/shm` contain no production credentials, tokens, private keys, account data, virtual environments, or production sender adapter. Recover/version the canonical source and review the remaining diffs before enabling event delivery.

## Reproducing isolated deployed-source tests

The deployed-runtime and Sender Gateway tests are preserved under `patches/tests/`. Build clean source snapshots from the exact base files and apply the patch sequence above, then run the corresponding test files with those snapshot roots on `PYTHONPATH`. The test receiver is mocked; no production callback, credential, or database is used.
