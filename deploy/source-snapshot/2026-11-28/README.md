# Production source snapshot — 2026-11-28

This directory is a source-only reconstruction of the live `/opt/so-mcp` and
`/opt/so-sender` source trees. Sender runtime files were refreshed again after
the account-health convergence at 2026-11-28 20:53 +08.
`BASE-SHA256SUMS` records the current deployed source hashes before the Events
patches; `SHA256SUMS` records the reconstructed tree including the Events changes.
The Sender Adapter snapshot also carries the separate prompt-directory ownership
fix from PR #215; that one file is not yet present in the live `/opt` tree.

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
  transaction as the terminal job transition. The event carries the job and
  conversation IDs so the original conversation can read the actual messages;
  it does not copy the full job result into the notification. Job retention
  keeps terminal jobs while their Sender event remains pending or leased.
- Sender exposes dispatcher-only `/internal/v1/events/claim` and
  `/internal/v1/events/{event_id}/ack` routes. The dispatcher identity is
  `session_ops:event-dispatcher` in tenant `session_ops`.
- The MCP Sender client uses its existing mTLS + signed identity path for claim
  and ack. MCP Events store, collector, single in-process event dispatcher, MCP
  protocol handlers, and ASGI lifecycle wiring are included from the tested project implementation.

## Validation

- Full repository tests: **1299 passed, 7 skipped, 1573 subtests passed**.
- Current reconstructed production integration tests: **19 passed**.
- Sender and MCP patch series both apply cleanly to the current live source files
  in isolated temporary copies and pass Python compilation.
- The legacy-outbox migration test seeds 75 pending version-0 rows, verifies that
  none are claimed or mutated, then proves a new version-1 event is claimable.
- Dispatcher shutdown tests verify no new webhook starts after the stop signal and
  shutdown waits for in-flight collection to finish.
- No live `/opt` source, production database, environment file, or service was
  modified by this validation.


## Runtime convergence refresh (2026-11-28)

This snapshot was refreshed from the deployed source after the Sender runtime convergence work. The refreshed files cover persistent account-health gating (acct4 unavailable, acct5 operator-disabled by default), structured upstream 401/429 handling, bounded Retry-After cooldown with jitter, Worker deferral through `next_eligible_at`, stable account-identity rotation cursors, read/list 429 fail-fast behavior, and permission-safe JSON state loading. The rotation counter file is group-readable (`0640`, group `chatgpt-sender`); credential-bearing state remains `0600`.

Verification against the refreshed source and current event changes: **1299 passed, 7 skipped, 1573 subtests passed**; Python compilation and `git diff --check` clean. Production MCP and Sender services were active at refresh time with no queued/running Sender jobs. No live upstream 429 was induced; cooldown/deferral behavior is covered by deterministic regression tests. No credentials, tokens, certificates, private keys, databases, environment files, or virtual environments are included in this source snapshot.
