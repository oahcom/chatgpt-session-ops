# Historical deployed-source integration patch artifacts (not production-ready)

These patches were generated from deployed Python source snapshots on 2026-11-27. **The Sender Gateway patch set is currently stale against `/opt/so-sender`:** its `job_store.py` / `app.py` hunks do not match the current deployed files (verified 2026-11-28), so do not apply it as-is. Rebase/regenerate it against the current deployed-source hashes before any deployment. The patches are intentionally split by service root because MCP and Sender Gateway are deployed separately.

## Apply roots

- Sender Gateway patch artifacts (`sender-gateway-event-outbox.patch`, `sender-gateway-thread-connection-cleanup.patch`, `sender-gateway-failed-terminal-event.patch`, and `sender-gateway-outbox-retention.patch`) are historical. **None is approved for application to the current `/opt/so-sender` tree.** The current source already has a bounded SQLite connection pool, but does not contain the event outbox or event claim/ack routes. Reconstruct the desired changes against the live hashes and validate all patch hunks in a clean fixture before considering deployment.
- MCP patches apply from `/opt/so-mcp`:
  - `mcp-sender-event-client.patch`: add `SenderGatewayClient.claim_events()` / `ack_event()` using the existing mTLS + service-token + HMAC identity assertion path.
  - `mcp-server-events.patch`: register MCP Events request handlers and advertise `capabilities.events`.
  - `mcp-runtime-events.patch`: add key-file loading, opt-in store creation, and ASGI-lifespan-managed collector/delivery workers.
  - `mcp-event-modules.patch`: add `mcp_events.py`, `mcp_event_collector.py`, and `mcp_event_workers.py` with subscribe-time job ownership checks, per-delivery authorization and subscription-expiry rechecks immediately before callback I/O, bounded retry, and retention cleanup.

`deployed-source.sha256` records the original deployed base before any Sender Gateway changes. `deployed-source-post-combined.sha256` records the production state after the combined cleanup + success-outbox patch and is the base for `sender-gateway-failed-terminal-event.patch`. `deployed-source-post-failure-event.sha256` records the current files after that incremental patch was applied to disk. `sender-original-base.sha256` records the original Sender Gateway files preserved under `/var/backups`. The sender-gateway and MCP client patches are not guaranteed to apply to the older `/srv/so-mcp-trusted` client file; the MCP client patch is based on the current deployed `/opt/so-mcp` version.

## Required deployment configuration (not created)

Provision a raw 32-byte AES-GCM key at `/etc/so-mcp/events.key`, owned by the `ubuntu` service user with mode `0400`. Add `SO_MCP_EVENTS_KEY_FILE=/etc/so-mcp/events.key` to the MCP service environment. Optional `SO_MCP_EVENTS_DB` must be an absolute path; otherwise the default is `/var/lib/so-mcp/events/events.sqlite3`. The events directory is created mode `0700`, and the SQLite database mode is `0600`.

Without `SO_MCP_EVENTS_KEY_FILE`, the feature remains disabled and the MCP server does not advertise Events. Setting the key file enables both the Events handlers and the supervised background collector/delivery loops.

## Safety

The live `/opt/so-sender/sender_gateway/job_store.py` and `app.py` inspected on 2026-11-28 do **not** contain `job_event_outbox` or the `/internal/v1/events/claim` and `/internal/v1/events/{event_id}/ack` routes. Their SHA256 values differ from the patch's expected base, and the event-outbox patch fails its first hunk in a read-only fixture. Treat all previous deployment-state notes in this file as historical, not current production evidence. No production files, database rows, credentials, or service state were changed during this verification. The local snapshots under `/dev/shm` must not be treated as production source or used to consume historical outbox records.

## Reproducing isolated deployed-source tests

The deployed-runtime and Sender Gateway tests are preserved under `patches/tests/`. Build clean source snapshots from the exact base files and apply the patch sequence above, then run the corresponding test files with those snapshot roots on `PYTHONPATH`. The test receiver is mocked; no production callback, credential, or database is used.

- `mcp-request-id-idempotency.patch` applies from `/opt/so-mcp` to live
  `src/session_ops/mcp_server.py` base SHA-256
  `4aebe295b53bdc14d99765f61eb1d526329bbacdb604f7dd4eed739439d4a82c`.
  It marks `idempotency_key` required in the live tool schemas, preserves
  caller-supplied keys, and derives a deterministic UUIDv4 from authenticated
  tenant/principal, MCP session id, tool, and JSON-RPC request id before
  validation when legacy connectors omit the key. Same-request replay is
  deduplicated; a new request id is a new intent. Test with
  `PYTHONPATH=/opt/so-mcp pytest patches/tests/test_mcp_request_id_idempotency.py`.

## Sender snapshot patch registry (isolated tests; not deployment authorization)

| Artifact | Target / purpose | Base and validation |
|---|---|---|
| `sender-account-identity-pin.patch` | Snapshot-only explicit-account identity preflight and explicit 429 no-cross-account behavior in `so-sender/cli/chatgpt_send.py`. | Snapshot SHA256 `de20a07cfe7667efc4ee79fe3231e6753a1dd09fa4cdbd55279fbcf239dcb3bd`; covered by `patches/tests/test_sender_account_identity_pin.py`. |
| `sender-child-env-account-roots.patch` | Pass `SO_SENDER_ACCOUNTS_DIR` and `CHATGPT_AUTH_DIR` through the Adapter child environment allowlist. | Same 2026-11-28 Sender snapshot; covered by `patches/tests/test_sender_child_env_account_roots.py` together with the account-directory resolver patch. |
| `sender-account-directory-contract.patch` | Shared Gateway/CLI account directory resolution, including dynamic acctN credential loading. | Same isolated Sender snapshot; tested with synthetic paths and credentials. |

CI runs both Sender patch test modules together in the unit job. They are snapshot-fixture tests only and do not authorize applying any patch to production.

## Sender account-directory contract (isolated source patch)

`sender-account-directory-contract.patch` applies from a clean `so-sender` source root. It adds `sender_gateway/account_directory.py` and makes Gateway discovery, CLI identity discovery, and `ChatGPTAPI` use the same resolver. The deployed runtime must include this new module alongside `sender_gateway/account_router.py`, `sender_gateway/app.py`, and `cli/chatgpt_send.py`; omitting it makes the CLI import fail.

Contract:
- `SO_SENDER_ACCOUNTS_DIR` is the directory containing `acctN` entries; `CHATGPT_AUTH_DIR` is the credential root.
- `acct1` and `default` resolve to the auth root when it contains the credential pair. A real `accounts/acct1` marker directory may coexist as a legacy Gateway marker; it is not followed or treated as the credential source when the auth root has credentials.
- If the auth root has no acct1 credentials, a real `accounts/acct1` directory is accepted as the legacy `BASE/accounts` credential layout. If both root and a distinct real `accounts/acct1` contain credentials, resolution fails closed as ambiguous.
- Symlink account directories and symlink credential marker files are rejected, preserving Gateway's no-symlink discovery boundary.
- `acct2+` always resolve to `SO_SENDER_ACCOUNTS_DIR/acctN` (defaulting to `<auth-root>/accounts/acctN`). If `CHATGPT_AUTH_DIR` is absent, a configured accounts path named `accounts` implies its parent as auth root; another configured path is itself the auth root; with neither configured, CLI `BASE/accounts` is used.

**Release gate:** production env values, effective service-user path access, and acct1 real-directory marker/legacy credential targets were not verified because production env and process-environment reads are permission-blocked. The isolated child-env fixture uses `SO_SENDER_ACCOUNTS_DIR = CHATGPT_AUTH_DIR/accounts` with a real `accounts/acct10` directory and synthetic credentials; a separate account-directory test covers acct1's legacy auth-root mapping. These fixtures prove the tested resolver behavior, but do **not** establish that the production directory layout matches it. Before release, an authorized deployment operator must confirm the effective `SO_SENDER_ACCOUNTS_DIR` and `CHATGPT_AUTH_DIR`, inspect only path metadata and confirm no symlink account directories or credential marker files are relied upon (not credential contents), explicitly confirm the real `accounts/acct1` marker/legacy credential layout without introducing symlink exceptions, confirm that Gateway discovery and CLI credential loading resolve the same physical directories for acct1 and at least one acct2+, and include the new `account_directory.py` module in the deployed runtime. Do not create or change production symlinks/layout based on this fixture. Isolated tests do not satisfy this production gate.

## Sender explicit-account identity pin (snapshot-only patch)

`sender-account-identity-pin.patch` is a **separate Sender CLI patch artifact**; it is not part of the historical Sender Gateway event-outbox patch set above. It modifies only `so-sender/cli/chatgpt_send.py` (patch headers: `a/so-sender/cli/chatgpt_send.py` and `b/so-sender/cli/chatgpt_send.py`).

- Target file: `so-sender/cli/chatgpt_send.py`
- Required base SHA256: `de20a07cfe7667efc4ee79fe3231e6753a1dd09fa4cdbd55279fbcf239dcb3bd` (2026-11-28 `deploy/source-snapshot/2026-11-28/SHA256SUMS`, entry `so-sender/cli/chatgpt_send.py`).
- Patch application root: use `-p1` from `/opt` (the patch path resolves to `so-sender/cli/chatgpt_send.py`), or `-p2` from `/opt/so-sender` (the patch path resolves to `cli/chatgpt_send.py`). These are path-strip explanations only, **not deployment authorization**.
- Before any consideration of application, verify that the exact target live file has the required SHA256. A matching hash is necessary but not sufficient: PR review and explicit deployment authorization are also required.
- **Do not apply before PR review and authorized deployment.** The currently reported production baseline has a different SHA256 and already contains the explicit-account identity preflight; this snapshot patch is not applicable to that reported live baseline. See `sender-account-identity-pin-production.md` for the separate production-only patch assessment, currently BLOCKED.

This artifact must remain distinct from the historical event-outbox patches. The historical Sender Gateway event-outbox patch set above remains stale/not approved for application; registering this CLI patch does not change that status or make any event-outbox patch applicable.
