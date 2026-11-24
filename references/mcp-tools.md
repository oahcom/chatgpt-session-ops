# Session Ops MCP Tools — v0.1

Source of truth:
- `src/session_ops/mcp_server.py`
- `src/session_ops/mcp_auth.py`
- `src/session_ops/authority.py`

Endpoint examples use `https://so-mcp.example.com/mcp`.
Replace the host and `$SO_MCP_TOKEN` with deployment values.

## 1. Common protocol

All ten tools use MCP JSON-RPC `tools/call`.

Request format:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "work_item.create",
    "arguments": {}
  }
}
```

Successful service envelope:

```json
{
  "ok": true,
  "data": {},
  "meta": {
    "request_id": "request-id",
    "contract_version": "0.1"
  }
}
```

Failure envelope:

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_ARGUMENT",
    "message": "invalid arguments",
    "retryable": false,
    "details": {}
  },
  "meta": {
    "request_id": "request-id",
    "contract_version": "0.1"
  }
}
```

The service envelope is carried inside the MCP tool result.

### Closed input schemas

Every tool uses:

`additionalProperties: false`

Undeclared fields are rejected.

Pagination:
- `work_item.list`: `page_size`, not `limit`.
- `session.list`: `page_size`, not `limit`.
- `session.context`: `limit`.

Limits:
- `page_size`: integer 1–50, default 25.
- `cursor`: maximum 2,048 characters.
- `session.context.limit`: integer 1–6, default 6.
- `session.context.query`: maximum 2,048 characters and 8,192 UTF-8 bytes.

### Common error codes

The following errors can occur across the tool dispatch boundary:

| Code | Meaning | Actual condition |
|---|---|---|
| `UNAUTHENTICATED` | Authentication failure | Missing verified AuthContext or invalid authentication |
| `PERMISSION_DENIED` | Insufficient OAuth permissions | One or more required scopes absent |
| `RESOURCE_EXHAUSTED` | Resource limit exceeded | SQLite locked/busy or structured response exceeds configured size |
| `TIMEOUT` | Operation timed out | Database, request, or adapter deadline exceeded |
| `INTERNAL` | Internal failure | Unclassified exception, non-busy SQLite operational error, or adapter scope violation |

Tool-specific errors:

| Code | Meaning | Actual condition |
|---|---|---|
| `INVALID_ARGUMENT` | Invalid input | Schema validation, malformed identifier, invalid enum, invalid cursor, or invalid AuthorityError |
| `NOT_FOUND` | Resource unavailable | Missing, cross-tenant, or unauthorized WorkItem |
| `CONFLICT` | State conflict | Invalid lifecycle transition, duplicate Session link, terminal WorkItem link attempt, or optimistic concurrency failure |
| `QUARANTINED_SESSION` | Session trust requirement unmet | Missing confirmed/complete/unused landing evidence, rejected caller-supplied evidence, or missing authorized active link |
| `DEPENDENCY_UNAVAILABLE` | Dependency unavailable | Principal directory or Memory Adapter unavailable/not configured |

Important:
- `session.unlink` deliberately returns a successful no-op for missing or unauthorized links. This is not `NOT_FOUND`.
- `work_item.grant` performs principal-directory validation. An unknown, disabled, or cross-tenant target is `INVALID_ARGUMENT`.
- `session.context` never accepts a caller-selected `work_item_id`; it derives the WorkItem from the authorized active link.

---

## 2. session.context

**Purpose:** Retrieve bounded historical memory through the authorized active Session link.

**Required scopes:**
- `session_ops.context.read`
- `session_ops.session.read`
- `session_ops.work_item.read`

**Required parameters:**
- `session_id`: canonical lowercase UUID string.
- `query`: nonblank string.

**Optional parameters:**
- `limit`: integer 1–6, default 6.

**Success data:**

```json
{
  "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
  "session_id": "12345678-1234-4234-8234-123456789abc",
  "entries": [
    {
      "memory_id": "memory-1",
      "session_id": "12345678-1234-4234-8234-123456789abc",
      "content": "historical memory",
      "provenance": {},
      "created_at": "2026-01-01T00:00:00Z",
      "score": 0.9,
      "trust": "untrusted_historical_context"
    }
  ]
}
```

**Failures:**
- `INVALID_ARGUMENT`: missing/invalid session_id, missing/blank/oversized query, invalid limit.
- `QUARANTINED_SESSION`: no authorized active Session link.
- `DEPENDENCY_UNAVAILABLE`: Memory Adapter not configured, unavailable, or crashed.
- `TIMEOUT`: request or adapter deadline exceeded.
- `RESOURCE_EXHAUSTED`: response size exceeded.
- `INTERNAL`: adapter scope violation or unclassified runtime failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"session.context","arguments":{"session_id":"12345678-1234-4234-8234-123456789abc","query":"deployment decisions","limit":4}}}'
```

---

## 3. session.link

**Purpose:** Link a confirmed Session to a visible, nonterminal WorkItem using a trusted landing event.

**Required scopes:**
- `session_ops.session.write`
- `session_ops.work_item.write`

**Required parameters:**
- `session_id`: canonical lowercase UUID string.
- `work_item_id`: canonical `wi_<UUIDv4>` string.

**Optional parameters:** None.

**Success data:** SessionLink object:

```json
{
  "link_id": "sl_12345678-1234-4234-8234-123456789abc",
  "session_id": "12345678-1234-4234-8234-123456789abc",
  "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
  "linked_at": "2026-01-01T00:00:00Z",
  "unlinked_at": null
}
```

**Failures:**
- `INVALID_ARGUMENT`: missing/malformed identifiers or undeclared arguments.
- `NOT_FOUND`: WorkItem unavailable to caller.
- `CONFLICT`: terminal WorkItem or existing active Session link.
- `QUARANTINED_SESSION`: no confirmed, complete, unused trusted landing event; caller-supplied evidence rejected.
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"session.link","arguments":{"session_id":"12345678-1234-4234-8234-123456789abc","work_item_id":"wi_12345678-1234-4234-8234-123456789abc"}}}'
```

---

## 4. session.list

**Purpose:** List Session links for a visible WorkItem, optionally including historical unlinked records.

**Terminal WorkItem behaviour:** a WorkItem in `completed` or `cancelled` has
all of its active links released automatically, in the same transaction that
changes the status (see §11). The default view therefore returns no links for a
terminal WorkItem; `include_unlinked=true` still returns them, with
`unlinked_at` set. This is a visibility rule, not data loss.

**Required scopes:**
- `session_ops.session.read`
- `session_ops.work_item.read`

**Required parameters:**
- `work_item_id`: canonical `wi_<UUIDv4>` string.

**Optional parameters:**
- `include_unlinked`: boolean, default false.
- `page_size`: integer 1–50, default 25.
- `cursor`: opaque cursor, maximum 2,048 characters.

**Success data:**

```json
{
  "items": [
    {
      "link_id": "sl_12345678-1234-4234-8234-123456789abc",
      "session_id": "12345678-1234-4234-8234-123456789abc",
      "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
      "linked_at": "2026-01-01T00:00:00Z",
      "unlinked_at": null
    }
  ],
  "next_cursor": null
}
```

**Failures:**
- `INVALID_ARGUMENT`: missing/malformed work_item_id, invalid page_size/cursor, undeclared arguments.
- `NOT_FOUND`: WorkItem unavailable to caller.
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"session.list","arguments":{"work_item_id":"wi_12345678-1234-4234-8234-123456789abc","include_unlinked":true,"page_size":20}}}'
```

---

## 5. session.unlink

**Purpose:** Unlink the active Session link if visible and, optionally, matching the expected link ID.

**Required scopes:**
- `session_ops.session.write`
- `session_ops.work_item.write`

**Required parameters:**
- `session_id`: canonical lowercase UUID string.

**Optional parameters:**
- `expected_link_id`: canonical `sl_<UUIDv4>` string.

**Success data:**

```json
{
  "unlinked": true,
  "link": {
    "link_id": "sl_12345678-1234-4234-8234-123456789abc",
    "session_id": "12345678-1234-4234-8234-123456789abc",
    "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
    "linked_at": "2026-01-01T00:00:00Z",
    "unlinked_at": "2026-01-02T00:00:00Z"
  }
}
```

When no visible active link exists or expected_link_id mismatches:

```json
{
  "unlinked": false,
  "link": null
}
```

**Failures:**
- `INVALID_ARGUMENT`: missing/malformed session_id, malformed expected_link_id, undeclared arguments.
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"session.unlink","arguments":{"session_id":"12345678-1234-4234-8234-123456789abc","expected_link_id":"sl_12345678-1234-4234-8234-123456789abc"}}}'
```

---

## 6. work_item.create

**Purpose:** Create an active WorkItem owned by the authenticated principal.

**Required scopes:**
- `session_ops.work_item.write`

**Required parameters:** None.

**Optional parameters:** None.

**Success data:**

```json
{
  "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
  "status": "active",
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z"
}
```

**Failures:**
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"work_item.create","arguments":{}}}'
```

---

## 7. work_item.get

**Purpose:** Retrieve a WorkItem visible to the authenticated principal.

**Required scopes:**
- `session_ops.work_item.read`

**Required parameters:**
- `work_item_id`: canonical `wi_<UUIDv4>` string.

**Optional parameters:** None.

**Success data:** WorkItem object:

```json
{
  "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
  "status": "active",
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z"
}
```

**Failures:**
- `INVALID_ARGUMENT`: missing/malformed work_item_id or undeclared argument.
- `NOT_FOUND`: WorkItem unavailable to caller.
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"work_item.get","arguments":{"work_item_id":"wi_12345678-1234-4234-8234-123456789abc"}}}'
```

---

## 8. work_item.grant

**Purpose:** Grant a same-tenant principal read or write access to a WorkItem.

**Required scopes:**
- `session_ops.work_item.write`
- `session_ops.access.manage`

**Required parameters:**
- `work_item_id`: canonical `wi_<UUIDv4>` string.
- `target_principal_id`: nonempty principal identifier.
- `scope`: `read` or `write`.

**Optional parameters:** None.

**Success data:**

```json
{
  "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
  "target_principal_id": "realm-a:reader",
  "scope": "read",
  "granted": true
}
```

**Failures:**
- `INVALID_ARGUMENT`: missing/malformed fields, invalid scope, invalid principal identifier, unknown/disabled/cross-tenant target, undeclared arguments.
- `NOT_FOUND`: caller is not the WorkItem owner or WorkItem unavailable.
- `DEPENDENCY_UNAVAILABLE`: principal directory lookup failure.
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"work_item.grant","arguments":{"work_item_id":"wi_12345678-1234-4234-8234-123456789abc","target_principal_id":"realm-a:reader","scope":"read"}}}'
```

---

## 9. work_item.list

**Purpose:** List WorkItems visible to the authenticated principal using keyset pagination.

**Required scopes:**
- `session_ops.work_item.read`

**Required parameters:** None.

**Optional parameters:**
- `status`: `active`, `paused`, `completed`, or `cancelled`.
- `page_size`: integer 1–50, default 25.
- `cursor`: opaque cursor, maximum 2,048 characters.

**Success data:**

```json
{
  "items": [
    {
      "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
      "status": "active",
      "created_at": "2026-01-01T00:00:00Z",
      "updated_at": "2026-01-01T00:00:00Z"
    }
  ],
  "next_cursor": null
}
```

**Failures:**
- `INVALID_ARGUMENT`: invalid status, page_size, cursor, or undeclared argument.
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"work_item.list","arguments":{"status":"active","page_size":20}}}'
```

---

## 10. work_item.revoke

**Purpose:** Revoke a same-tenant principal's read or write grant on a WorkItem.

**Required scopes:**
- `session_ops.work_item.write`
- `session_ops.access.manage`

**Required parameters:**
- `work_item_id`: canonical `wi_<UUIDv4>` string.
- `target_principal_id`: nonempty principal identifier.
- `scope`: `read` or `write`.

**Optional parameters:** None.

**Success data:**

```json
{
  "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
  "target_principal_id": "realm-a:reader",
  "scope": "read",
  "revoked": true
}
```

**Failures:**
- `INVALID_ARGUMENT`: missing/malformed fields, invalid scope, malformed principal identifier, undeclared arguments.
- `NOT_FOUND`: caller is not the WorkItem owner or WorkItem unavailable.
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"work_item.revoke","arguments":{"work_item_id":"wi_12345678-1234-4234-8234-123456789abc","target_principal_id":"realm-a:reader","scope":"read"}}}'
```

---

## 11. work_item.update

**Purpose:** Change a WorkItem lifecycle status, optionally enforcing optimistic concurrency.

**Required scopes:**
- `session_ops.work_item.write`

**Required parameters:**
- `work_item_id`: canonical `wi_<UUIDv4>` string.
- `status`: `active`, `paused`, `completed`, or `cancelled`.

**Optional parameters:**
- `expected_updated_at`: nonempty timestamp string; when supplied, must match the current `updated_at`.

**Success data:** Updated WorkItem object:

```json
{
  "work_item_id": "wi_12345678-1234-4234-8234-123456789abc",
  "status": "completed",
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-02T00:00:00Z"
}
```

**Failures:**
- `INVALID_ARGUMENT`: missing/malformed work_item_id, invalid status enum, invalid argument type, or undeclared argument.
- `NOT_FOUND`: WorkItem unavailable to caller.
- `CONFLICT`: forbidden lifecycle transition, transition out of terminal state, or expected_updated_at mismatch.
- `RESOURCE_EXHAUSTED`: database or response limit.
- `INTERNAL`: unclassified storage failure.
- Common errors: see §1.

When status changes to `completed` or `cancelled`, the implementation closes
all active Session links in the same database transaction and appends one
`session.auto_unlink_terminal` audit event per affected link.

**JSON-RPC example:**

```bash
curl -sS https://so-mcp.example.com/mcp \
  -H "Authorization: Bearer $SO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"work_item.update","arguments":{"work_item_id":"wi_12345678-1234-4234-8234-123456789abc","status":"completed","expected_updated_at":"2026-01-01T00:00:00Z"}}}'
```

---

## 12. Terminal WorkItem link invalidation

`completed` and `cancelled` are terminal. Entering either state releases every
active Session link of that WorkItem **inside the same `BEGIN IMMEDIATE`
transaction** that changes the status, so a status change never leaves a
readable-but-terminal link behind. `paused` does not release links.

No MCP tool exposes this behaviour directly; it is a side effect of
`work_item.update`. It matters when reading `session.list` afterwards.

### Two audit actions, not one

`authority_audit_events` carries a nullable `link_id` column (indexed by
`ix_authority_audit_link`). Both unlink paths record it, which is what keeps
them distinguishable after `unlinked_at` has been written:

| Action | Recorded by | `principal_id` |
|---|---|---|
| `session.unlink` | explicit `session.unlink` tool call | the calling principal |
| `session.auto_unlink_terminal` | terminal `work_item.update` | the principal performing the update |
| `session.auto_unlink_terminal` | startup backfill | `system:terminal-link-reconciler` |

Relying on `unlinked_at` alone cannot tell a manual unlink from an automatic
one, nor which of several links on the same Session was released. The audit
row is the only place that distinction survives.

### Startup backfill

Links that were already active on a terminal WorkItem before this rule existed
never see a second status transition, so they would otherwise stay valid
forever. `initialize()` reconciles them idempotently on every start and writes
one audit row each with `request_id='terminal-link-backfill'`.

### Tracing one link

```sql
SELECT
    l.link_id,
    l.session_id,
    l.work_item_id,
    l.linked_at,
    l.unlinked_at,
    a.action,
    a.principal_id AS actor,
    a.request_id,
    a.created_at AS audited_at
FROM session_links AS l
LEFT JOIN authority_audit_events AS a
    ON a.link_id = l.link_id
   AND a.action IN ('session.unlink', 'session.auto_unlink_terminal')
WHERE l.link_id = ?
ORDER BY a.created_at, a.audit_id;
```

### Post-deploy verification

```sql
-- must return zero rows: no terminal WorkItem keeps an active link
SELECT s.link_id, s.work_item_id, w.status
FROM session_links AS s
JOIN work_items AS w ON w.work_item_id = s.work_item_id
WHERE s.unlinked_at IS NULL
  AND w.status IN ('completed', 'cancelled');

-- one row per released link, never aggregated
SELECT action, link_id, principal_id, request_id
FROM authority_audit_events
WHERE action = 'session.auto_unlink_terminal';
```
