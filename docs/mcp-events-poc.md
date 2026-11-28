# MCP Events: `chatgpt.job.completed`

## Design

The event path is owned by `chatgpt-session-ops`; it has no Sender Gateway, Sender client, polling bridge, or second job system.

1. The authenticated principal creates a WorkItem and may link one or more confirmed ChatGPT conversations to it with `session.link`. The event destination is a separately selected ChatGPT Work conversation and need not be one of those source sessions.
2. The immutable WorkItem owner calls `work_item.finish` with `completed` or `cancelled`. `SecureWorkItemStore.finish_work_item()` writes the terminal state, audit rows, session unlink records, and exactly one completion event into one SQLite transaction.
3. `MCPEventsStore` stores the outbox, subscription, and delivery tables in that same database. The event `job_id` is the WorkItem ID (not the internal `cgj_...` ChatGPT execution-queue job ID). The durable event identifies the WorkItem, terminal result, completion time, and linked conversation IDs; it never copies conversation content.
4. Only the immutable WorkItem owner may call `work_item.finish`. Delegated writers may update non-terminal status, but cannot complete/cancel the WorkItem or trigger its completion event. Event ownership and subscriptions are scoped to that owner.
5. Every subscription must specify one exact destination `conversation_id` (with optional `job_id`). The destination is the ChatGPT Work conversation that receives notifications; it does not need to be linked to each source WorkItem. Omitting `job_id` aggregates every completion event owned by the authenticated tenant/principal into that one conversation. Supplying `job_id` narrows the subscription to one owned WorkItem. Multiple owners may independently subscribe the same destination conversation; each subscription receives only its authenticated owner's events, so the shared chat can receive all owners' completions without cross-principal event access. Each callback receives a sanitized payload whose `conversation_id` is the destination. WorkItem source-session links remain independent and can be absent or different.
6. Legacy unscoped subscriptions are disabled during store initialization and their pending deliveries are failed closed; historical outbox rows are retained.
7. One in-process dispatcher delivers pending callbacks with bounded retries and Standard Webhooks signatures. Failed delivery remains durable for retry; duplicate delivery is idempotently identified by the stable event ID.

The event store key is separate from the database file. `SO_MCP_EVENTS_DB`, if configured, must resolve to the same file as the WorkItem database; a second database is rejected because it would break atomicity. `SO_MCP_EVENTS_KEY_FILE` must contain a raw 32-byte key with service-user-only read access.

## Direct ChatGPT execution

- `chatgpt.session.create` and `chatgpt.session.send` use `ChatGPTDirectClient`, an in-process adapter over the repository's existing `scripts/chatgpt_send.py` implementation. There is no Sender Gateway, HTTP sidecar, mTLS client, or separate worker service.
- The adapter persists requests in `chatgpt_jobs` in the canonical WorkItem SQLite database and runs them through one bounded executor thread. Capacity is nine outstanding jobs total (one active plus eight queued); admission fails fast when full. This queue is deliberately retained to prevent bursts of MCP calls from overwhelming account/session operations.
- Every request supplies a stable UUIDv4 `idempotency_key`. The key is bound to the operation and canonical request hash; an exact retry returns the original job, while reuse with changed or unverifiable arguments is rejected. `chatgpt.job.get` reads the durable job state.
- On restart, queued jobs are resubmitted. A job that was `running` at process death is marked `OUTCOME_UNKNOWN` and is never replayed automatically because the remote side effect may already have happened.
- New-session account availability is checked before enqueue and again before execution. Existing-session sends require the conversation to belong to the authenticated tenant/principal and remain pinned to its recorded account. Anonymous fallback is disabled for the direct MCP runtime.
- A send that is not positively confirmed is reported as `OUTCOME_UNKNOWN`; the queue does not rotate to another account or automatically repeat the remote POST. `chatgpt.session.read` reads only sessions owned by the authenticated tenant/principal.
- ChatGPT execution jobs and WorkItem lifecycle are distinct. The `chatgpt.job.completed` MCP Event currently reports immutable-owner WorkItem terminal transitions; its `job_id` is a WorkItem ID. It is not emitted when a `chatgpt_jobs` queue entry completes.

## Reliability and security

- Event creation and terminal WorkItem state commit or roll back together. A process crash cannot leave a completed WorkItem without its outbox event.
- Tenant and principal identity come from verified MCP authentication, never from event payload arguments. Subscription identity includes the destination conversation and optional WorkItem filter. Owner-level aggregation is strictly scoped by the authenticated tenant/principal in the outbox query; another owner’s events cannot enter the subscription. Before each callback, current WorkItem read/write access for the event owner is rechecked; an authorization failure is terminal for that delivery and sends no callback.
- Subscription secrets are AES-GCM encrypted. Callback challenge verification and per-delivery Standard Webhooks signatures are enforced. Subscribe challenge I/O and unsubscribe database mutations run off the ASGI event loop.
- Callback destinations require public HTTPS; DNS is validated and the selected public address is pinned for the request. Redirects and ambient proxies are not used.
- Delivery uses leases, bounded retry/backoff, idempotent event IDs, and retention cleanup. A single dispatcher thread is supervised by the MCP ASGI lifespan.
- Dispatcher shutdown waits at most 30 seconds for an in-flight operation and 3 seconds for thread exit. Uvicorn has a 5-second graceful request-shutdown budget. The 600-second systemd stop budget covers in-flight ChatGPT sends (up to 420 seconds), request drain, and bounded event drain.

## Verification

Latest local verification on merged head `3063b08` plus the shared-destination regression: `python3 -m pytest -q tests` — **1,359 passed, 13 skipped, 1,587 subtests passed**; pinned MCP SDK 2.3.0 integration suite — **6 passed**. Python compilation, `git diff --check`, and `systemd-analyze verify deploy/so-mcp.service` passed. Systemd emitted unrelated permission warnings for other host units. GitHub Actions must also pass on the exact pushed head; ancestor CI is not sufficient.

Focused integration tests cover the full local path: authenticated WorkItem -> owner-authorized terminal transition -> same-transaction outbox insert -> webhook delivery. They verify delegated writers cannot complete/cancel a WorkItem, cannot create a filtered subscription for another owner's WorkItem, one destination conversation aggregates multiple WorkItems from one owner and can be shared by multiple owners' independent subscriptions, source sessions can differ or be absent, cross-principal isolation, destination payload sanitization, owner/job-filtered replay/backfill, rejection and migration shutdown of unscoped subscriptions, and outbox rollback on terminal-transition failure. Additional regression tests cover bounded DNS resolution, dispatcher start/stop race fencing and shutdown failure without duplicate stop waits, direct-client queue capacity and durable idempotency, restart recovery that never replays an ambiguous remote send, account preflight, and owner-scoped session reads.

A real ChatGPT Work callback/resumption acceptance test is still required. Local tests and mock callbacks do not prove product-level conversation resumption. No production service, database, key, or configuration has been changed by this work.
