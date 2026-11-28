# MCP database mutation idempotency

The MCP tool schemas require an `idempotency_key` (UUIDv4) for every
database mutation. New clients must generate it before the operation and reuse
the exact value across retries, new JSON-RPC request IDs, and new MCP sessions.
It is scoped by authenticated tenant/principal and tool name. The receipt
stores a digest of canonical business arguments and the original result.
Reusing the key with changed arguments returns `CONFLICT`.

For legacy clients that omit the field, the server normalizes a fallback from
the authenticated principal, tool, MCP session ID, and JSON-RPC request ID
before argument validation. That fallback is intentionally session-scoped.
If no MCP session ID exists, the server uses a per-call random key rather than
risking collisions between unrelated stateless clients. Legacy stateless
calls do not get retry deduplication; callers requiring it must send an
explicit key.

The receipt reservation, domain mutation, and terminal result commit in the
same SQLite `BEGIN IMMEDIATE` transaction. If the process exits before commit,
SQLite rolls back both. If the transaction commits but the caller times out
before receiving the response, retry with the same explicit `idempotency_key`
to replay the original result. The JSON-RPC `request_id` remains a separate
audit correlation value; the legacy receipt-table column named `request_id`
stores the idempotency key.

A running mutation that outlives the request deadline returns
non-automatically-retryable `INDETERMINATE`; the caller can manually replay
with the same idempotency key. A persisted receipt marked in-progress is
reported as indeterminate and requires reconciliation.

This applies to WorkItem create/update/finish, grant/revoke, and Session
link/unlink. External ChatGPT sends use their separate durable attempt state
machine because remote side effects cannot share the SQLite transaction.
