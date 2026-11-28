# MCP database mutation idempotency

The WorkItem and SessionLink mutation schemas require an
`idempotency_key` (UUIDv4). New clients must generate it before the operation and reuse
the exact value across retries, new JSON-RPC request IDs, and new MCP sessions.
It is scoped by authenticated tenant/principal and tool name. The receipt
stores a digest of canonical business arguments and the original result.
Reusing the key with changed arguments returns `CONFLICT`.

A missing key is rejected before the domain operation runs. The server does
not derive this key from JSON-RPC `request_id`: JSON-RPC ids are
response-correlation values, not durable user-intent identifiers, and clients
may reuse them after a response. This prevents an unrelated later operation
from replaying an older mutation merely because it reused a transport id.
`session.link` additionally requires the verified `attempt_id` from the
trusted landing receipt. The database enforces one link per landing attempt;
replaying that attempt returns the existing link instead of creating another.

The receipt reservation, domain mutation, and terminal result commit in the
same SQLite `BEGIN IMMEDIATE` transaction. If the process exits before commit,
SQLite rolls back both. Subprocess regression tests cover both crash windows: killing the process
while the SQLite transaction is still open rolls back the mutation and receipt;
killing it after commit but before the response, then opening a new store,
proves that retrying with the same key returns the original result without
creating a second mutation. If the transaction commits but the caller times out before
receiving the response, retry with the same explicit `idempotency_key` to
replay the original result. The JSON-RPC `request_id` remains a separate
audit correlation value; the legacy receipt-table column named `request_id`
stores the idempotency key.

A running mutation that outlives the request deadline returns
non-automatically-retryable `INDETERMINATE`; the caller can manually replay
with the same idempotency key.
This applies to WorkItem create/update/finish, grant/revoke, and Session
link/unlink. External ChatGPT sends use their separate durable attempt state
machine because remote side effects cannot share the SQLite transaction.
