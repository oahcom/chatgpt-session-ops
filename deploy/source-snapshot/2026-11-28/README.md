# Production source snapshot — 2026-11-28

This directory is a source-only reconstruction of the files read from the live
`/opt/so-mcp` and `/opt/so-sender` trees on 2026-11-28 18:19–18:26 +08.
The snapshot preserves the exact pre-change bytes of the deployed runtime files
except for the three MCP Events modules and the Sender outbox/client/route
changes described below. `BASE-SHA256SUMS` records the pre-feature production source hashes. `SHA256SUMS`
records the current reconstructed tree after the Events changes.

## Provenance

- MCP deployment marker: `8aa2597`, timestamp `2026-11-28T00:49:31Z`.
- Sender deployment marker: `dbdcf3c`, timestamp `2026-11-28T05:15:00Z`.
- Neither marker resolves to a commit in the canonical GitHub repository.
- `/opt` trees have no `.git` metadata. This snapshot is therefore a reproducible
  source baseline, not proof of the original upstream commit.
- Source files were copied from `/opt/so-mcp/{src,scripts,requirements-mcp.txt}`
  and `/opt/so-sender/{sender_gateway,cli}`. No runtime DB, env file, credentials,
  certificates, private keys, account registries, proxy configuration, or venvs
  are included. Operational JSON files were explicitly excluded.

## Event changes in this snapshot

- Sender `JobStore.complete()` and `JobStore.fail()` now insert one stable
  `chatgpt.job.completed` event into `job_event_outbox` in the same SQLite
  transaction as the terminal job transition. Event summaries are generic and
  do not copy job result content into the notification.
- Sender exposes dispatcher-only `/internal/v1/events/claim` and
  `/internal/v1/events/{event_id}/ack` routes. The dispatcher identity is
  `session_ops:event-dispatcher` in tenant `session_ops`.
- The MCP Sender client uses its existing mTLS + signed identity path for claim
  and ack. MCP Events store, collector, delivery worker, MCP protocol handlers,
  and ASGI lifecycle wiring are included from the tested project implementation.

## Validation

The exact reconstructed snapshot was compiled and tested without touching the
live `/opt` trees, production databases, service configuration, or processes.
See the project test file `patches/tests/test_sender_outbox_current_snapshot.py`
and the MCP Events tests for current-snapshot coverage.


## Runtime convergence refresh (2026-11-28)

This snapshot was refreshed from the deployed source after the Sender runtime convergence work. The refreshed files cover persistent account-health gating (acct4 unavailable, acct5 operator-disabled by default), structured upstream 401/429 handling, bounded Retry-After cooldown with jitter, Worker deferral through `next_eligible_at`, stable account-identity rotation cursors, read/list 429 fail-fast behavior, and permission-safe JSON state loading. The rotation counter file is group-readable (`0640`, group `chatgpt-sender`); credential-bearing state remains `0600`.

Verification against the refreshed source: **1287 passed, 11 skipped, 1545 subtests passed**; Python compilation and `git diff --check` clean. Production MCP and Sender services were active at refresh time with no queued/running Sender jobs. No live upstream 429 was induced; cooldown/deferral behavior is covered by deterministic regression tests. No credentials, tokens, certificates, private keys, databases, environment files, or virtual environments are included in this source snapshot.
