# MCP Events: `chatgpt.job.completed`

## Status — 2026-11-28

Implementation and production-source patches are ready for deployment review. **Not deployed or enabled.** PR #163 remains Draft.

- Production `/opt/so-mcp` and `/opt/so-sender` still have no Events code, key, or database. No real ChatGPT Work callback has been exercised.
- The source snapshot is based on current live files and includes the separate PR #215 Sender Adapter ownership fix. `BASE-SHA256SUMS` records live pre-patch hashes; `SHA256SUMS` records the snapshot. Runtime state and credentials are excluded.
- Sender writes one version-1 event row in the same transaction as job completion/failure. Existing mTLS, Bearer token, and signed identity are reused.
- MCP uses one in-process `MCPEventDispatcher` thread for collection, persistence, ACK, and callback delivery. The existing Sender consumer count/concurrency configuration is preserved; the Events patch adds no Sender consumer, second delivery thread, scheduler, Redis, or Kafka.
- Existing OAuth session read/write scopes are reused; no new OAuth client or Events-specific scope.
- Event payload includes `job_id` and `conversation_id`, not a transcript. The original Work conversation can read the actual messages through the existing session-read tool.
- Historical Sender rows are version 0 and excluded from claim, ACK, and pruning. The 75 historical rows have not been read, claimed, replayed, acknowledged, or deleted.

## Reliability and security

- Sender `complete()`/`fail()` writes the outbox row atomically with terminal job status. MCP persists the event before ACK; stable event IDs deduplicate replay after a crash.
- Sender-to-MCP claims are capped at 10 attempts. Malformed or exhausted events become `dead`, record `last_error`, and emit an error log. Delivered/dead rows are retained until normal retention cleanup; pending/leased events keep their job rows until ACK.
- The existing owner/idempotency tombstone preserves callback authorization after the 14-day job-body retention window; MCP event history is retained independently.
- One dispatcher thread processes collection and callback delivery sequentially, batch size one. Callback retries are bounded, signatures are refreshed per attempt, and payloads are capped at 256 KiB.
- Subscription ownership is the authenticated tenant/principal. Optional job filters are checked at subscribe time. Callback challenge and Standard Webhooks signatures are verified; destinations require public HTTPS with pinned-address transport, with redirects/proxies disabled.
- Subscription secrets are AES-GCM encrypted in SQLite WAL storage using a separate raw 32-byte key. TTL, idempotent refresh/unsubscribe, and secret rotation are supported.
- Existing MCP behavior normalizes legacy submissions missing `idempotency_key` using tenant, principal, MCP session ID, tool name, and JSON-RPC request ID; explicit caller keys remain preferred.

## Production patches

- `/opt/so-sender`: `sender-gateway-event-outbox.patch` — outbox schema/writes, authenticated claim/ACK, bounded retries, and retention fencing.
- `/opt/so-mcp`: `mcp-sender-event-client.patch`, `mcp-event-modules.patch`, `mcp-server-events.patch`, `mcp-runtime-events.patch` — existing Sender client integration, SQLite store/collector/dispatcher, protocol handlers, and ASGI lifespan wiring.

All patches passed `git apply --check` against copies of the current live source. The additive module patch passed a clean-tree check. Regenerate and recheck before applying to another revision.

## Remaining deployment gates

1. Provision `/etc/so-mcp/events.key` as a raw 32-byte key (owner MCP service user, mode `0400`) and `/var/lib/so-mcp/events/` (mode `0700`, DB mode `0600`). Neither exists in production.
2. Keep the 75 historical Sender rows quarantined until their provenance and replay policy are approved.
3. Define an operator alert/cleanup policy for retained `status=dead` rows.
4. Run the real ChatGPT Work acceptance test: subscribe in a Work conversation, submit a Sender job from it, leave it idle, then verify that same conversation receives the callback and reads the resulting messages without polling. Mock callback tests do not prove product-level resumption.

Original deployment markers (`8aa2597` for MCP, `dbdcf3c` for Sender) still do not resolve to canonical Git commits. The snapshot hashes establish the files used for this reconstruction, not original upstream commit provenance.

## Verification

- Full repository suite: **1299 passed, 7 skipped, 1573 subtests passed**.
- Sender outbox and authenticated route tests: **14 passed**.
- MCP Events ASGI and Sender-to-MCP pipeline tests: **11 passed**.
- Python compilation, exact production-base patch checks, and `git diff --check` passed.

These checks establish local behavior and patch compatibility, not production activation or real ChatGPT Work conversation resumption. No production source, configuration, database, key, or service was modified.
