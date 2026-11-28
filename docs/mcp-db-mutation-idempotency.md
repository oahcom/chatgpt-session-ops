# MCP database mutation receipts

The MCP database mutations now use the existing `authority_operation_receipts`
table. A receipt stores a digest of canonical arguments and the original result
inside the same SQLite `BEGIN IMMEDIATE` transaction as the domain mutation.
A repeated key with changed arguments is rejected as `CONFLICT`.

The current key is authenticated tenant + principal + tool name + JSON-RPC
request ID. This is a **transport-request replay receipt**, not yet a
caller-stable idempotency key:

- A retry only replays the original result if the caller reuses the exact same
  JSON-RPC request ID.
- JSON-RPC IDs are response-correlation IDs and clients may generate a new ID
  after reconnecting or retrying; a new ID does not deduplicate the original
  operation.
- The receipt key does not include the MCP session ID, so two sessions for the
  same principal that reuse the same request ID and tool can collide. This
  scope must be resolved before claiming cross-session idempotency.

If the process exits before commit, SQLite rolls back both receipt and
mutation. If the transaction commits but the MCP caller times out before
receiving the response, an exact same-request-ID retry can replay the stored
result. A running mutation that outlives the request deadline returns
`INDETERMINATE`; do not retry it with a new request ID until the outcome is
reconciled.

Receipts currently cover WorkItem create/update/finish, grant/revoke, and
Session link/unlink. External ChatGPT sends use their separate durable
attempt-state machine because remote side effects cannot share the SQLite
transaction.

**Remaining #205 work:** introduce an explicit caller-stable mutation
`idempotency_key` (or an equivalent documented contract), define MCP-session
scoping and reconnect behavior, and test same-key/same-arguments replay,
same-key/changed-arguments conflict, cross-session request-ID collisions,
late commit after caller timeout, and restart reconciliation. Do not describe
the current JSON-RPC request-ID receipts as full durable idempotency.
