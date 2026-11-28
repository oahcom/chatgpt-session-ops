# MCP Events: `chatgpt.job.completed`

## Design

The event path is owned by `chatgpt-session-ops`; it has no Sender Gateway, Sender client, polling bridge, or second job system.

1. A trusted WorkItem send creates/links the ChatGPT conversation to its WorkItem.
2. When the WorkItem transitions to `completed` or `cancelled`, `SecureWorkItemStore.set_status()` writes the terminal state, audit rows, session unlink records, and exactly one completion event into one SQLite transaction.
3. `MCPEventsStore` stores the outbox, subscription, and delivery tables in that same database. The durable event identifies the WorkItem, terminal result, completion time, and linked conversation IDs; it never copies conversation content.
4. Only the immutable WorkItem owner may perform a terminal status transition. Delegated writers may update non-terminal status, but cannot complete/cancel the WorkItem or trigger its completion event. Event ownership and subscriptions are scoped to that owner.
5. Every subscription must specify one exact destination `conversation_id` (with optional `job_id`). The destination is the ChatGPT Work conversation that receives notifications; it does not need to be linked to each source WorkItem. Omitting `job_id` aggregates every completion event owned by the authenticated tenant/principal into that one conversation. Supplying `job_id` narrows the subscription to one owned WorkItem. Each callback receives a sanitized payload whose `conversation_id` is the destination. WorkItem source-session links remain independent and can be absent or different.
6. Legacy unscoped subscriptions are disabled during store initialization and their pending deliveries are failed closed; historical outbox rows are retained.
7. One in-process dispatcher delivers pending callbacks with bounded retries and Standard Webhooks signatures. Failed delivery remains durable for retry; duplicate delivery is idempotently identified by the stable event ID.

The event store key is separate from the database file. `SO_MCP_EVENTS_DB`, if configured, must resolve to the same file as the WorkItem database; a second database is rejected because it would break atomicity. `SO_MCP_EVENTS_KEY_FILE` must contain a raw 32-byte key with service-user-only read access.

## Direct ChatGPT execution

- `chatgpt.session.send` calls the repository's existing `scripts/chatgpt_send.py` runtime directly. Existing account ownership resolution, account rotation, sending, SSE collection, and trusted landing logic remain the single implementation; no Sender HTTP client or sidecar is involved.
- A send is authorized against the WorkItem before any remote request. The confirmed conversation is linked using the same WorkItem attempt ID. New conversations use the existing authenticated-account rotation path; anonymous fallback is disabled for trusted WorkItem sends.
- `chatgpt.session.read` reads the linked conversation through the same `ChatGPTAPI` implementation. It accepts historical links after a WorkItem completes and auto-unlinks, while still requiring WorkItem read access. `session.list` remains the source for listing linked sessions.
- `chatgpt.session.send` requires a caller-supplied UUIDv4 `attempt_id`. The server durably reserves it before the remote side effect and binds it to a SHA-256 hash of the exact request parameters. Reusing the same ID and parameters returns the stored result or `in_progress`; reusing it with different parameters is rejected.
- The MCP request deadline is shorter than the maximum ChatGPT send timeout. If the caller receives `TIMEOUT`, the executor may still be completing the original send. The caller must repeat the exact request with the same `attempt_id` to inspect status; a different attempt ID is blocked while an earlier attempt is running or has an unresolved outcome. HTTP 5xx, request timeouts, and HTTP 200 without a conversation ID are `outcome_unknown`, never automatic retries. A known-delivered but unlinked session remains blocked until the exact trusted landing link resolves it.
- WorkItem status is the task lifecycle. There is no separate Sender JobStore, `job.get` polling tool, Sender Outbox, or event collector.

## Reliability and security

- Event creation and terminal WorkItem state commit or roll back together. A process crash cannot leave a completed WorkItem without its outbox event.
- Tenant and principal identity come from verified MCP authentication, never from event payload arguments. Subscription identity includes the destination conversation and optional WorkItem filter. Owner-level aggregation is strictly scoped by the authenticated tenant/principal in the outbox query; another owner’s events cannot enter the subscription. Before each callback, current WorkItem read/write access for the event owner is rechecked; an authorization failure is terminal for that delivery and sends no callback.
- Subscription secrets are AES-GCM encrypted. Callback challenge verification and per-delivery Standard Webhooks signatures are enforced. Subscribe challenge I/O and unsubscribe database mutations run off the ASGI event loop.
- Callback destinations require public HTTPS; DNS is validated and the selected public address is pinned for the request. Redirects and ambient proxies are not used.
- Delivery uses leases, bounded retry/backoff, idempotent event IDs, and retention cleanup. A single dispatcher thread is supervised by the MCP ASGI lifespan.
- Dispatcher shutdown waits at most 30 seconds for an in-flight operation and 3 seconds for thread exit. Uvicorn has a 5-second graceful request-shutdown budget. The 600-second systemd stop budget covers in-flight ChatGPT sends (up to 420 seconds), request drain, and bounded event drain.

## Verification

Latest local verification: `python3 -m pytest -q --ignore=tools/proton-batch/tests` — **1,406 passed, 13 skipped, 1,577 subtests passed**; Python compilation and `git diff --check` passed. The local VM does not have the optional MCP SDK installed, so SDK-specific tests are skipped locally. GitHub Actions run #774 on ancestor head `7991f6b283c075a123d98c8982b27cb6fa1f6ccf` passed all four jobs, including the pinned real MCP SDK integration and real memory-stdio E2E. The later DB-mutation hardening commit and dispatcher lifecycle fix still require their own latest-head CI run.

Focused integration tests cover the full local path: authenticated WorkItem -> owner-authorized terminal transition -> same-transaction outbox insert -> webhook delivery. They verify delegated writers cannot complete/cancel a WorkItem, cannot create a filtered subscription for another owner's WorkItem, one destination conversation aggregates multiple WorkItems, source sessions can differ or be absent, cross-principal isolation, destination payload sanitization, owner/job-filtered replay/backfill, rejection and migration shutdown of unscoped subscriptions, and outbox rollback on terminal-transition failure. Additional regression tests cover bounded DNS resolution, dispatcher start/stop race fencing and shutdown failure without duplicate stop waits, send-attempt idempotency, and ambiguous remote-send outcomes without account rotation/retry.

A real ChatGPT Work callback/resumption acceptance test is still required. Local tests and mock callbacks do not prove product-level conversation resumption. No production service, database, key, or configuration has been changed by this work.
