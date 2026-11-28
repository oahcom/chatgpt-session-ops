# MCP database mutation idempotency

The MCP database mutations use the existing `authority_operation_receipts`
table, keyed by authenticated tenant/principal, tool name, and JSON-RPC request
ID. The receipt stores a digest of canonical arguments and the original result.
A repeated key with changed arguments is rejected as `CONFLICT`.

The receipt is reserved and completed inside the same SQLite
`BEGIN IMMEDIATE` transaction as the domain mutation. If the process exits
before commit, SQLite rolls back both. If the transaction commits but the MCP
caller times out before receiving the response, retry the same operation with
the same JSON-RPC request ID to replay the stored result. A running mutation
that outlives the request deadline returns non-automatically-retryable
`INDETERMINATE`; the same request ID remains the only safe replay key.

This applies to WorkItem create/update/finish, grant/revoke, and Session
link/unlink. External ChatGPT sends use their separate durable attempt state
machine because remote side effects cannot share the SQLite transaction.
