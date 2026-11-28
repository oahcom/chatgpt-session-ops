# MCP Events: `chatgpt.job.completed`

## Design

The event path is owned by `chatgpt-session-ops`; it has no Sender Gateway, Sender client, polling bridge, or second job system.

1. A trusted WorkItem send creates/links the ChatGPT conversation to its WorkItem.
2. When the WorkItem transitions to `completed` or `cancelled`, `SecureWorkItemStore.set_status()` writes the terminal state, audit rows, session unlink records, and exactly one completion event into one SQLite transaction.
3. `MCPEventsStore` stores the outbox, subscription, and delivery tables in that same database. The completion event identifies the WorkItem, terminal result, and completion time; it never copies conversation content. For backward compatibility it emits `conversation_id` when exactly one conversation is linked and `conversation_ids` when multiple conversations are linked.
4. Event ownership follows the WorkItem owner, not a delegated principal who happens to perform the terminal status update. One event fans out once to each active subscription owned by that principal.
5. One in-process dispatcher delivers pending callbacks with bounded retries and Standard Webhooks signatures. Failed delivery remains durable for retry; duplicate delivery is idempotently identified by the stable event ID.

The event store key is separate from the database file. `SO_MCP_EVENTS_DB`, if configured, must resolve to the same file as the WorkItem database; a second database is rejected because it would break atomicity. `SO_MCP_EVENTS_KEY_FILE` must contain a raw 32-byte key with service-user-only read access.

## Direct ChatGPT execution

- `chatgpt.session.send` calls the repository's existing `scripts/chatgpt_send.py` runtime directly. Existing account ownership resolution, account rotation, sending, SSE collection, and trusted landing logic remain the single implementation; no Sender HTTP client or sidecar is involved.
- A send is authorized against the WorkItem before any remote request. The confirmed conversation is linked using the same WorkItem attempt ID. New conversations use the existing authenticated-account rotation path; anonymous fallback is disabled for trusted WorkItem sends.
- `chatgpt.session.read` reads the linked conversation through the same `ChatGPTAPI` implementation. It accepts historical links after a WorkItem completes and auto-unlinks, while still requiring WorkItem read access. `session.list` remains the source for listing linked sessions.
- WorkItem status is the task lifecycle. There is no separate Sender JobStore, `job.get` polling tool, Sender Outbox, or event collector.

## Reliability and security

- Event creation and terminal WorkItem state commit or roll back together. A process crash cannot leave a completed WorkItem without its outbox event.
- Tenant and principal identity come from verified MCP authentication, never from event payload arguments.
- Subscription secrets are AES-GCM encrypted. Callback challenge verification and per-delivery Standard Webhooks signatures are enforced.
- Callback destinations require public HTTPS; DNS is validated and the selected public address is pinned for the request. Redirects and ambient proxies are not used.
- Delivery uses leases, bounded retry/backoff, idempotent event IDs, and retention cleanup. A single dispatcher thread is supervised by the MCP ASGI lifespan.
- Dispatcher shutdown waits at most 30 seconds for an in-flight operation and 3 seconds for thread exit. Uvicorn has a 5-second graceful request-shutdown budget. The 600-second systemd stop budget covers in-flight ChatGPT sends (up to 420 seconds), request drain, and bounded event drain.

## Verification

Focused integration tests cover the full local path: authenticated WorkItem -> terminal transition -> same-transaction outbox insert -> webhook delivery. They also verify one event is fanned out once when a WorkItem has multiple linked sessions, delegated completion is routed to the owner, and an outbox insertion failure rolls back the terminal transition and session unlink.

A real ChatGPT Work callback/resumption acceptance test is still required. Local tests and mock callbacks do not prove product-level conversation resumption. No production service, database, key, or configuration has been changed by this work.
