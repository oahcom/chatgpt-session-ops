# MCP Events PoC: `chatgpt.job.completed`

## Status

This is a tested implementation and draft PR, but MCP Events webhook delivery is not yet enabled in production.

- Branch: `poc/mcp-events-20261127-r1`
- Production `/opt/so-sender` now contains the request-thread SQLite cleanup, success-event outbox, and dispatcher claim/ack routes. The failure-terminal event addition is patched on disk but not yet loaded by the running process.
- `so-sender.service` is active; its last restart cleared the descriptor exhaustion. The pending failure-event patch will be loaded after the current worker job drains.
- `/opt/so-mcp` is unchanged. No MCP Events subscription store, collector, webhook delivery worker, or event capability is active in production.
- No real ChatGPT Work callback has been exercised end to end.

## Event contract

Event name: `chatgpt.job.completed`

Optional subscription filter:

```json
{"job_id": "job_..."}
```

Payload:

```json
{
  "job_id": "job_...",
  "status": "succeeded | failed",
  "summary": "...",
  "completed_at": "2026-11-27T12:00:00Z",
  "error_code": "OUTCOME_UNKNOWN",
  "outcome_unknown": true
}
```

The event is intentionally a short completion summary. It does not embed model instructions or a full conversation transcript. The subscribed ChatGPT conversation is associated by the Events callback subscription; the server does not guess a ChatGPT UI conversation ID from an MCP session ID or OAuth subject.

## Implemented in the PoC

- Registers `events/list`, `events/subscribe`, and `events/unsubscribe` on the authenticated MCP endpoint.
- Adds `capabilities.events` to both `initialize` and `server/discover` responses.
- Uses the authenticated tenant/principal as subscription ownership.
- Validates the `whsec_` key (24–64 decoded bytes), verifies a fresh signed callback challenge, and emits JSON-RPC `-32015` with a reason on callback verification failure.
- Validates callback destinations using the existing public-HTTPS resolver and pinned-address transport; redirects and proxy use are disabled.
- Persists subscriptions and event deliveries in SQLite WAL mode.
- Encrypts subscription secrets with AES-GCM; a 32-byte encryption key must be supplied by the caller. The key is not stored in SQLite.
- Supports default expiry, bounded `ttlMs`, `ttlMs: null`, deterministic subscription IDs, idempotent refresh/unsubscribe, and 24-hour dual-signature secret rotation.
- Creates an event outbox and per-subscription deliveries, filters by `job_id`, and claims deliveries with a lease so two workers do not normally send the same pending row concurrently.
- Emits the same terminal event for both `running -> succeeded` and `running -> failed`; failure events include `error_code` and `outcome_unknown`.
- Closes the completion-vs-subscribe race by backfilling matching owner-scoped events from the last 24 hours when a subscription is persisted; repeated subscribe/refresh remains idempotent.
- Accepts the Sender Gateway's stable `event_id`, rejects cross-owner/payload ID reuse, and deduplicates replay before acknowledging the sender.
- Includes `MCPEventCollector`: it claims Sender Gateway events, persists them in the MCP event store, then acknowledges the sender lease. The deployed-runtime patch supervises collector and webhook delivery workers through ASGI lifespan.
- Signs Standard Webhooks payloads, keeps event IDs stable across retries, uses a fresh signing timestamp per attempt, enforces 256 KiB, and does not retry `410` or `413`.

The PoC intentionally has no event replay cursor (`cursor: null`). A crashed or disconnected callback may miss an event after its bounded delivery attempts are exhausted.

## Required production integration

The deployed system has two separate services and databases:

1. MCP service: `/opt/so-mcp`, systemd unit `so-mcp.service`, user `ubuntu`.
2. Sender Gateway: `/opt/so-sender`, systemd unit `so-sender.service`, user `chatgpt-sender`.

The Sender Gateway owns the job state transition. Therefore the durable source event must be written atomically with `JobStore.complete()` in the Sender Gateway database, not only into the MCP event database.

Recommended flow:

1. `events/subscribe` stores the callback subscription in the MCP database.
2. Sender Gateway `JobStore.complete()` or `JobStore.fail()` changes a job to its terminal state and inserts a job-completion outbox row in the *same SQLite transaction*.
3. An MCP-side collector polls the Sender Gateway over the *existing* `SenderGatewayClient` mTLS connection. Add `POST /internal/v1/events/claim` and `POST /internal/v1/events/{event_id}/ack` to the Sender Gateway. The client already sends the mTLS certificate, service Bearer token, and short-lived HMAC identity assertion.
4. Restrict those two routes to a dedicated service identity, for example principal `session_ops:event-dispatcher` in tenant `session_ops`. The existing identity assertion generator accepts non-empty service identities; the route must explicitly require this exact pair. Do not use an end-user identity to enumerate cross-tenant events.
5. The collector persists the event in the MCP event store using the *same stable event_id*, then acknowledges the Sender Gateway lease. If the MCP process crashes before the ack, the Sender Gateway lease expires and the event is delivered again; MCP deduplication prevents a second fan-out.
6. An MCP delivery worker sends the signed ChatGPT callback and updates per-subscription delivery state.

This two-stage outbox prevents a crash between job completion and event publication from silently losing the event. The Sender Gateway derives tenant/principal from the job row, never from the caller-supplied event payload. The MCP event store accepts a sender-provided event ID and rejects reuse of that ID with a different owner or payload.

## Integration blockers

- The source currently deployed under `/opt/so-mcp` contains `SenderGatewayClient` and `chatgpt.job.*` operations that are absent from the checked-out Git source tree. `/opt/so-mcp` has no Git metadata.
- `/opt/so-sender` and `/opt/so-mcp` have no Git metadata. I found `/srv/so-mcp-trusted`, but it is root-owned, detached at `825cd9a`, has no remote, and has an uncommitted `sender_gateway/app.py` change. Its `sender_gateway/app.py` and `job_store.py` match `/opt/so-sender`, but its MCP client/server/runtime files differ from the deployed `/opt/so-mcp` files. It is therefore only a partial source root, not a clean authoritative source for both services.
- The isolated sender snapshot copied only `app.py`, `job_store.py`, `models.py`, `account_router.py`, `worker.py`, and `sender_gateway_client.py`; no account files, credentials, tokens, private keys, virtual environments, or production sender adapter were copied. A test-only adapter stub was used.
- The sender feature patch adds `job_event_outbox` in the same SQLite transaction as both `JobStore.complete()` and `JobStore.fail()`, leased `claim`/`ack` endpoints restricted to the dedicated `session_ops:event-dispatcher` service identity, and matching mTLS client methods. Production currently has the success transition and claim/ack routes active; the failed-terminal event patch is on disk pending restart.
- The sender snapshot tests include a full local mTLS pipeline: real `SenderGatewayClient` → real `IdentityVerifier` and sender HTTP routes → sender outbox lease → MCP collector → MCP event store → signed callback to a mocked receiver.
- Reviewable patches are saved under `patches/`. They are split by service and include both the pre-hotfix deployed checksums and post-hotfix Sender Gateway checksums. They pass apply/compile checks against pristine copies of the corresponding deployed bases.
- Consequently the MCP Events implementation is a tested patch against the deployed source snapshot, but it cannot yet be safely merged into the canonical Git source or enabled in production.
- The 32-byte AES-GCM key needs production provisioning. The runtime patch requires an absolute key-file path with mode 0400 or 0600 and readable by the MCP service account; it does not create or provision the key.
- Sender Gateway outbox schema/transaction integration for success and failure, leased claim/ack routes, and `SenderGatewayClient.claim_events/ack_event` were implemented and tested in an isolated copy of the deployed source at `/dev/shm/so-events-integration-poc`. Success outbox + claim/ack are already present in production as part of the combined patch; failure event support is patched to disk and awaits restart. None of this is yet in the canonical Git source tree.
- The MCP-side collector and worker group are wired into the isolated deployed-runtime patch, but that patch has not been applied to `/opt`; no production collector thread is running.
- A supervised MCP webhook delivery worker is included in the patch. Production key provisioning, full retention/cleanup, authorization-revocation handling, metrics, and operational alerting remain unimplemented.
- During SO-MCP agent creation, Sender Gateway began returning HTTP 500 from `IdentityVerifier._connect()`. Investigation confirmed 509 leaked `JobStore` per-request thread-local SQLite connections, consuming 1023 of the process's 1024 file descriptors. The HTTP handler never closed the connection retained in the store's global registry after `ThreadingHTTPServer` request threads exited. A combined patch (connection cleanup plus success outbox/claim/ack) was applied to `/opt/so-sender` after backing up original files at `/var/backups/so-sender-thread-cleanup-20261127`, then `so-sender.service` was restarted. The service returned active with 11 FDs immediately after restart; after 20 concurrent SO-MCP session-list calls, all 20 succeeded and the process remained at 14 FDs. No further SQLite CANTOPEN errors were observed. The exact applied diff is preserved as `sender-gateway-applied-combined.patch`.
- A separate `sender-gateway-failed-terminal-event.patch` adds failure notifications atomically with `JobStore.fail()`. It is applied to `/opt/so-sender` on disk and has passed compile checks, but the process has not yet been restarted to load it; restart is deferred until the active sender job drains.
- The production Sender Gateway currently has 5 pending successful-job outbox events. No MCP collector is running, so these remain safely queued and no ChatGPT callback has been sent.
- The ChatGPT Work subscription and original-chat continuation still require the product-level end-to-end test.

## Verification

Executed with the installed MCP SDK environment (`/opt/so-mcp/.venv`):

- Targeted MCP PoC + SDK + collector + worker tests: 34 passed, including Streamable HTTP lifecycle, sender-event deduplication, success/failure event delivery, resource-access revocation, transient authorization retry, retention cleanup, callback 410 deactivation, expiry, private database-directory checks, and subscribe-time race backfill/owner scoping.
- Deployed MCP runtime snapshot tests: 4 passed, covering current deployed `mcp_server.py` protocol lifecycle, opt-in `create_application` wiring, key-file permissions, and ASGI worker startup/shutdown.
- Isolated Sender Gateway outbox tests: 7 passed, including success/failure transaction rollback atomicity, lease expiry/stale-ack protection, dispatcher-only routes, per-request SQLite connection cleanup across 40 HTTP requests, and an end-to-end local mTLS + HMAC identity pipeline from job completion through the deployed MCP collector to a mocked ChatGPT callback.
- Full repository suite after authorization and retention hardening: 1285 tests passed, 7 skipped, exit code 0.
- The six clean-deployment patches (thread cleanup, full Sender outbox, MCP client, MCP server, MCP runtime, MCP event modules) apply in order to pristine copies of their exact deployed bases and compile. The incremental failure-event patch also applies to the current production sender base and compiles.
- Python compilation checks passed.
- `git diff --check` passed.

The full suite was run with temporary files redirected to `/dev/shm` because the host's `/tmp` user quota was exhausted. The earlier run against `/tmp` produced unrelated quota-induced failures; the successful rerun used `/dev/shm` and exited 0.
