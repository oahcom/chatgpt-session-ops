# MCP Events integration patches

These patches describe the minimal production integration for ChatGPT Work MCP Events. They are **not deployed**. All patches are generated against the current source files read from `/opt/so-mcp` and `/opt/so-sender` on 2026-11-28 and were checked in isolated copies of those exact base files. Do not apply them to a different revision without regenerating and rechecking the diffs.

## Patch set

Apply the Sender patch from `/opt/so-sender`:

- `sender-gateway-event-outbox.patch` — adds the durable `job_event_outbox`, writes one event atomically with each terminal job transition, exposes the authenticated claim/ACK endpoints, and prunes only version-1 delivered/dead event history with existing job retention. Legacy rows default to version 0 and are excluded from claim, ack, and pruning; malformed version-1 payloads are dead-lettered; transient handoff failures are capped at 10 claims and logged.

Apply the MCP patches from `/opt/so-mcp`:

- `mcp-sender-event-client.patch` — adds claim/ACK methods to the existing Sender client, reusing mTLS, the existing service token, and the existing HMAC identity key.
- `mcp-event-modules.patch` — adds the SQLite Events store, Sender collector, and one in-process `MCPEventDispatcher` thread. It does not add a Sender worker, scheduler, queue service, Redis, or Kafka.
- `mcp-server-events.patch` — registers the Events protocol handlers, advertises the capability, and offloads blocking subscription checks. It preserves the request-scoped idempotency behavior already present in the live MCP base.
- `mcp-runtime-events.patch` — loads the event encryption key, conditionally enables Events, and starts/stops the single dispatcher through the existing ASGI lifespan.

The Sender event dispatcher identity is `session_ops:event-dispatcher` in tenant `session_ops`. It uses the existing mTLS + Bearer + signed identity path; no new OAuth client or account credential is introduced.

## Required runtime configuration

The feature stays disabled unless `SO_MCP_EVENTS_KEY_FILE` points to a raw 32-byte AES-GCM key. The intended path is `/etc/so-mcp/events.key`, mode `0400`, owned by the MCP service user. The default database is `/var/lib/so-mcp/events/events.sqlite3` (directory mode `0700`, database mode `0600`). These files and settings have **not** been provisioned in production.

## Verification

- Current reconstructed Sender + MCP integration suite: **25 passed**.
- Full repository suite: **1302 passed, 7 skipped, 1573 subtests passed**.
- Python compilation passed for the reconstructed MCP and Sender modules.
- Sender and MCP-client patches passed `git apply --check` against the current live source files; MCP server/runtime patches passed the same check against their current live source files; the additive module patch passed its clean-tree check.
- `git diff --check` clean.

No production files, databases, credentials, service configuration, or processes were changed. The production historical outbox rows were not read, claimed, replayed, acknowledged, or deleted. The migration behavior was tested only against an isolated fixture with 75 synthetic legacy rows.
