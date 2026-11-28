# MCP Events: `chatgpt.job.completed`

## Status — 2026-11-28 21:30 +08

The implementation and production-source patch set are ready for deployment review. **Nothing is deployed or enabled in production.**

- Branch `poc/mcp-events-20261127-r1`; PR #163 remains Draft.
- Live `/opt/so-mcp` and `/opt/so-sender` still have no Events code, key, or database. No real ChatGPT Work callback has been exercised.
- `deploy/source-snapshot/2026-11-28/` contains the current source reconstruction. `BASE-SHA256SUMS` records live pre-patch files; `SHA256SUMS` records the patched snapshot. It also carries the separate PR #215 Sender Adapter ownership fix, not yet deployed. Runtime databases and credentials are excluded.
- Sender writes one `event_version=1` outbox row atomically with each terminal job transition. Existing mTLS, Bearer token, and signed identity are reused for claim/ACK.
- MCP uses one in-process `MCPEventDispatcher` thread for collection, persistence, ACK, and callback delivery. No extra Sender worker, second delivery thread, scheduler, queue service, Redis, or Kafka.
- Existing OAuth session read/write scopes are reused. No Events-specific OAuth scope or new OAuth client is introduced.
- The first upgraded Sender startup adds `event_version` with default 0, so existing rows are excluded from claim, ACK, and pruning. The 75 historical rows have not been read, claimed, replayed, acknowledged, or deleted.

## Event contract

Event: `chatgpt.job.completed`

Optional subscription filter: `{"job_id":"cgj_..."}`.

Payload fields: `job_id`, `conversation_id`, `status`, `summary`, `completed_at`, and on failure `error_code`/`outcome_unknown`.

This is a completion notification, not a transcript dump. `conversation_id` lets the subscribed Work conversation read the actual messages through the existing session-read tool. The callback subscription associates the event with the original Work conversation; the server does not infer a UI conversation ID from an MCP session ID or OAuth subject.

## Implementation

- Authenticated `events/list`, `events/subscribe`, and `events/unsubscribe`; advertises `capabilities.events`.
- Subscription ownership is the authenticated tenant/principal. Fan-out is owner-scoped; optional `job_id` filters are validated at subscribe time.
- Callback challenge and Standard Webhooks signatures are verified. Destinations require public HTTPS with pinned-address transport; redirects and proxies are disabled.
- SQLite WAL stores subscriptions, event payloads, and per-subscription deliveries. Callback secrets are AES-GCM encrypted using a separate raw 32-byte key.
- Supports bounded TTL, idempotent refresh/unsubscribe, and dual-signature secret rotation.
- Sender `JobStore.complete()`/`fail()` writes the event in the same transaction as terminal status. MCP persists the stable event ID before ACK; replay after a crash is deduplicated. New malformed version-1 rows are moved to `dead` with a bounded `last_error`, so one poison row cannot block later events.
- Sender job pruning preserves jobs while their outbox event is pending/leased, then retains the existing owner/idempotency tombstone. This keeps callback authorization valid after the 14-day job-body retention window; the 30-day MCP event history does not require the full job row.
- One dispatcher thread runs collection and callback delivery sequentially with batch size one. Shutdown finishes only the current bounded operation and starts no new callback after the stop signal.
- Sender-to-MCP claims are capped at 10 attempts; malformed or exhausted events become `dead` with `last_error` and an error log. Callback delivery retries are separately bounded; event IDs stay stable, each attempt gets a fresh signature timestamp, and payloads are capped at 256 KiB. No replay cursor is implemented (`cursor: null`).
- `events/subscribe` performs optional Sender ownership lookup and callback verification outside the MCP event loop via `asyncio.to_thread`.

## Production patch set

From `/opt/so-sender`: `sender-gateway-event-outbox.patch` (atomic outbox, claim/ACK routes, retention fencing).

From `/opt/so-mcp`: `mcp-sender-event-client.patch`, `mcp-event-modules.patch`, `mcp-server-events.patch`, and `mcp-runtime-events.patch` (existing client methods, SQLite store/collector/dispatcher, protocol handlers, and ASGI lifespan wiring).

All patches passed `git apply --check` against copies of the current live source files; the additive module patch passed a clean-tree check. Regenerate and recheck before applying to any other source revision.

## Remaining deployment gates

1. Provision `/etc/so-mcp/events.key` as a raw 32-byte key, owned by the MCP service user, mode `0400`; provision `/var/lib/so-mcp/events/` mode `0700` and the database mode `0600`. None exists in production.
2. Keep the 75 historical Sender rows quarantined. Their provenance and replay policy remain unapproved.
3. Define an operator alert/cleanup policy for retained `status=dead` rows. The code now quarantines malformed version-1 events instead of retrying them indefinitely.
4. Run the real ChatGPT Work acceptance test: subscribe in a Work conversation, submit a Sender job from it, leave it idle, then verify that same conversation receives the callback and reads the resulting messages without polling. Mock callback tests do not prove product-level resumption.

Original deployment markers (`8aa2597` for MCP, `dbdcf3c` for Sender) still do not resolve to canonical Git commits. The snapshot and patch hashes establish the files used for this reconstruction, not original upstream commit provenance.

## Verification

- Full repository suite: **1299 passed, 7 skipped, 1573 subtests passed**.
- Current reconstructed Sender + MCP integration tests: **23 passed**.
- Python compilation and exact production-base patch checks passed; `git diff --check` is clean.

These checks establish local implementation and patch compatibility, not production activation or real ChatGPT Work conversation resumption. No production source, configuration, database, key, or service was modified.
