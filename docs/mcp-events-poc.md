# MCP Events PoC: `chatgpt.job.completed`

## Status — rechecked 2026-11-28 18:33 +08

MCP Events is implemented in the PoC and reconstructed against the exact current production source snapshot, but it is **not deployed or enabled in production**.

- Branch: `poc/mcp-events-20261127-r1`; PR #163 remains Draft and unmerged.
- Live `/opt/so-mcp` and `/opt/so-sender` contain no Events implementation. The production Sender currently completes jobs without creating outbox events.
- Production has no MCP Events key, Events database, collector, or webhook delivery worker. No real ChatGPT Work callback has been exercised.
- A source-only snapshot was reconstructed at `deploy/source-snapshot/2026-11-28/`, with pre-feature and current SHA-256 manifests. It excludes runtime databases, environment files, credentials, certificates, private keys, account registries, proxy configuration, and virtual environments.
- The snapshot adds atomic Sender terminal-event outbox writes, dispatcher-only claim/ack routes, and MCP client methods using the existing mTLS + signed-identity path. It also includes the MCP Events store, collector, protocol handlers, and ASGI worker lifecycle. These are repository artifacts only; live `/opt` files were not changed.
- Sender outbox claim/ack now fences expired leases. MCP callback delivery is explicitly limited to batches of two; each callback lease is renewed and verified before external I/O.
- The 75 historical rows in the production Sender database remain untouched. Do not claim, replay, acknowledge, or delete them until their reconciliation policy is approved.

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
- Closes only the completion-vs-subscribe race: events completed after the subscribe request starts but before callback verification finishes are backfilled; older events are not replayed. Repeated subscribe/refresh remains idempotent.
- Accepts the Sender Gateway's stable `event_id`, rejects cross-owner/payload ID reuse, and deduplicates replay before acknowledging the sender.
- Includes `MCPEventCollector`: it claims Sender Gateway events, persists them in the MCP event store, then acknowledges the sender lease. The deployed-runtime patch supervises collector and webhook delivery workers through ASGI lifespan.
- Signs Standard Webhooks payloads, keeps event IDs stable across retries, uses a fresh signing timestamp per attempt, enforces 256 KiB, and does not retry `410` or `413`.

The PoC intentionally has no event replay cursor (`cursor: null`). A crashed or disconnected callback may miss an event after its bounded delivery attempts are exhausted.

## Reconstructed integration flow

The deployed system has two separate services and databases:

1. MCP service: `/opt/so-mcp`, systemd unit `so-mcp.service`, user `ubuntu`.
2. Sender Gateway: `/opt/so-sender`, systemd unit `so-sender.service`, user `chatgpt-sender`.

The Sender Gateway owns the job state transition. Therefore the durable source event must be written atomically with `JobStore.complete()` in the Sender Gateway database, not only into the MCP event database.

Recommended flow:

1. `events/subscribe` stores the callback subscription in the MCP database.
2. Sender Gateway `JobStore.complete()` or `JobStore.fail()` changes a job to its terminal state and inserts a job-completion outbox row in the *same SQLite transaction*.
3. The MCP-side collector polls the Sender Gateway over the *existing* `SenderGatewayClient` mTLS connection using the reconstructed dispatcher-only `POST /internal/v1/events/claim` and `POST /internal/v1/events/{event_id}/ack` routes. The client reuses the mTLS certificate, service Bearer token, and short-lived HMAC identity assertion.
4. Restrict those two routes to a dedicated service identity, for example principal `session_ops:event-dispatcher` in tenant `session_ops`. The existing identity assertion generator accepts non-empty service identities; the route must explicitly require this exact pair. Do not use an end-user identity to enumerate cross-tenant events.
5. The collector persists the event in the MCP event store using the *same stable event_id*, then acknowledges the Sender Gateway lease. If the MCP process crashes before the ack, the Sender Gateway lease expires and the event is delivered again; MCP deduplication prevents a second fan-out.
6. An MCP delivery worker sends the signed ChatGPT callback and updates per-subscription delivery state.

This two-stage outbox prevents a crash between job completion and event publication from silently losing the event. The Sender Gateway derives tenant/principal from the job row, never from the caller-supplied event payload. The MCP event store accepts a sender-provided event ID and rejects reuse of that ID with a different owner or payload.

## Integration blockers

- Production source provenance is now captured as a versioned snapshot, but the original deployment markers (`8aa2597` for MCP and `dbdcf3c` for Sender) still do not resolve to canonical Git commits. The snapshot is a reproducible baseline, not proof of the original upstream commit.
- Production key provisioning and OAuth authorization-server/client grants are not complete. Code-side scope advertisement does not prove a real token can acquire and use the Events scopes.
- The 75 legacy Sender outbox rows require explicit reconciliation. Their job/owner/payload structure was validated in prior read-only work, but replay/content policy and end-to-end deduplication remain unapproved.
- Sender job retention is 14 days by default while MCP event history is 30 days. The retention mismatch (#197) must be resolved before a long outage can cause otherwise-valid events to fail resource authorization.
- Poison-event quarantine and bounded diagnostics need final policy (#173). The collector isolates each item and persists before ack, but permanently malformed trusted-source rows must not retry forever.
- The real ChatGPT Work acceptance test (#174) remains mandatory: subscribe in a Work conversation, submit a job from that conversation, leave it idle, and prove the same conversation receives the callback and continues without polling. Mock receiver tests do not prove product-level resumption.
- No production source, configuration, database, key, or service was modified in this repair pass.

## Verification

- Full repository test suite: **1289 passed, 13 skipped, 1573 subtests passed**.
- Exact reconstructed production snapshot tests: **10 passed**, covering MCP Events ASGI lifecycle, Sender terminal outbox atomicity/idempotency, rollback on outbox-write failure, dispatcher identity enforcement, per-event ack routing, expired/stale lease rejection, and the local mTLS + HMAC event pipeline to a mocked callback receiver.
- MCP Events core/collector/worker focused tests: **39 passed**.
- Python compilation checks passed. `git diff --check` passed.

These tests establish the local implementation and reconstructed-source integration. They do not establish production activation or real ChatGPT Work conversation resumption.
