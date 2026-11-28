# MCP Events: `chatgpt.job.completed`

## Status

Implementation and production patches are ready for review, **not deployed**. PR #163 remains Draft. Production has no Events key/database or Events code; no real Work callback has been exercised.

The source snapshot is based on current live files. `BASE-SHA256SUMS` records live pre-patch hashes; `SHA256SUMS` records the snapshot. The separate PR #215 Sender Adapter fix is included in the snapshot, but not in the Events patch. Runtime state and credentials are excluded.

The feature adds one in-process MCP `MCPEventDispatcher` thread. Existing Sender consumer/concurrency settings are preserved: no new Sender consumer, second delivery thread, scheduler, Redis, or Kafka. Existing mTLS, Bearer identity, and OAuth session read/write scopes are reused; no new OAuth client or Events-specific scope.

Event payload contains `job_id`, status, summary, completion time, and optional `conversation_id`; it does not copy the transcript. The original Work conversation can read messages through the existing session-read tool. Historical Sender rows are version 0 and excluded from claim, ACK, and pruning. The 75 historical rows have not been read, claimed, replayed, acknowledged, or deleted.

## Reliability and security

- Sender `complete()`/`fail()` writes the version-1 outbox row in the same transaction as terminal job status. MCP persists before ACK; stable event IDs deduplicate replay.
- Sender-to-MCP claims are capped at 10 attempts. Malformed/exhausted events become `dead`, record `last_error`, and emit an error log. Pending/leased events preserve their job rows; delivered/dead rows are retained until cleanup.
- One dispatcher thread processes collection and callback delivery sequentially, batch size one. Callback retries are bounded; each attempt gets a fresh signature timestamp; payload limit is 256 KiB.
- Subscription ownership is tenant/principal scoped. Optional job filters are checked at subscribe time. Callback challenge and Standard Webhooks signatures are verified; destinations require public HTTPS with pinned-address transport, no redirects/proxies.
- Subscription secrets are AES-GCM encrypted in SQLite WAL storage with a separate 32-byte key. TTL, idempotent refresh/unsubscribe, and secret rotation are supported.

## Patches

- Sender: `sender-gateway-event-outbox.patch` (outbox, authenticated claim/ACK, retry cap, retention fencing; preserves live worker settings).
- MCP: `mcp-sender-event-client.patch`, `mcp-event-modules.patch`, `mcp-server-events.patch`, `mcp-runtime-events.patch` (client, store/collector/dispatcher, protocol, ASGI lifecycle).

All patches passed `git apply --check` against copies of current live source; the additive module patch passed a clean-tree check. Regenerate before applying to any other revision.

## Remaining gates

1. Provision `SO_MCP_EVENTS_KEY_FILE` as a raw 32-byte key (default `/etc/so-mcp/events.key`, owner MCP service user, mode `0400`) and `SO_MCP_EVENTS_DB` (default `/var/lib/so-mcp/events/events.sqlite3`; parent `0700`, DB `0600`). Neither exists in production.
2. Keep the 75 historical Sender rows quarantined until provenance/replay policy is approved.
3. Define operator alert/cleanup for retained `status=dead` rows.
4. Run a real Work acceptance test from an eligible account/workspace. OpenAI documents MCP Events for Work chats, but event-triggered tasks are unavailable on Free/Go. Mock callbacks do not prove product-level conversation resumption.

References: [MCP Events](https://developers.openai.com/plugins/build/mcp-events) · [Event-triggered task eligibility](https://help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt).

Original deployment markers (`8aa2597` MCP, `dbdcf3c` Sender) do not resolve to canonical Git commits. The snapshot hashes identify this reconstruction, not original upstream provenance.

## Verification

- Full repository suite: **1304 passed, 7 skipped, 1573 subtests passed**.
- Sender outbox/authenticated route tests: **14 passed**.
- MCP ASGI and Sender-to-MCP pipeline tests: **12 passed** (26 reconstructed integration tests total).
- Python compilation, production-base patch checks, and `git diff --check` passed.

These results establish local behavior and patch compatibility, not production activation or real Work resumption. No production source, configuration, database, key, or service was changed.
