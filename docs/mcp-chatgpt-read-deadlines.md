# MCP ChatGPT read admission and deadlines

The MCP `chatgpt.session.read` path performs one direct upstream GET. It does
not use the retired Sender/Gateway path and does not retry the GET. The
upstream HTTP connect/read and total response-body deadline is 15 seconds; the
MCP caller deadline is 20 seconds, leaving time for local ownership checks,
parsing, and response serialization.

`ChatGPTAPI.live_tail_once` passes a total deadline into the streaming HTTP
reader. The reader updates the socket timeout to the remaining wall-clock
budget before each chunk, so a slow trickle cannot reset the whole timeout
indefinitely. The existing retrying `live_tail` remains unchanged for
interactive CLI operations; the MCP read path does not call it.

MCP `chatgpt.session.read` calls use a dedicated two-worker executor and a
two-slot non-queuing admission gate. A single-flight key is scoped by tool, verified issuer/realm/
subject, tenant/principal, and conversation ID. A second read for the same
principal/conversation is rejected while the first worker is still active;
different conversations may use the two available slots concurrently.

If the MCP waiter reaches its 20-second deadline or disconnects, the worker is
not falsely marked cancelled. Its single-flight key and capacity slot remain
held until the real concurrent Future completes. This prevents duplicate
upstream reads and unbounded executor queue growth. Timeout, cancellation, and admission logs use hashed request/principal/resource
identifiers rather than raw request IDs or conversation IDs.

Upstream 401, 404, 429, timeout, and other dependency failures retain distinct
MCP error categories (`UNAUTHENTICATED`, `NOT_FOUND`, `RESOURCE_EXHAUSTED`,
`TIMEOUT`, and `DEPENDENCY_UNAVAILABLE`). The local `chatgpt.session.list`
query remains on the ordinary DB read path and does not occupy an upstream-read
slot. No Gateway, worker service, queue,
or production deployment is introduced by this path.
