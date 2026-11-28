# Sender account identity pin — production compatibility status

**Live production patch: BLOCKED — do not deploy.**

## Artifact scope

`patches/sender-account-identity-pin.patch` is a snapshot-only patch. It is authored against the checked-in Sender source snapshot:

- Snapshot file: `deploy/source-snapshot/2026-11-28/so-sender/cli/chatgpt_send.py`
- Snapshot SHA256: `de20a07cfe7667efc4ee79fe3231e6753a1dd09fa4cdbd55279fbcf239dcb3bd`

It is not a production deployment artifact and must not be applied to `/opt/so-sender`.

## Production baseline evidence

The following is an operator-supplied read-only measurement from `vm-43-ubuntu`; it was not independently re-read by this worker:

- Production file: `/opt/so-sender/cli/chatgpt_send.py`
- SHA256: `0d758febc28ea608eee050aad4e60eb6afe77807b7f9e8d8640b3836293443ca`
- Size: `413113` bytes
- Around line 9499: explicit-account preflight already contains `if explicit_account and not validate_account_identity(account):`.
- Around line 9454: explicit-account 429 handling still marks the account unavailable and then calls `return create_chat_rotating(...)`, permitting cross-account rotation.

The production and snapshot file hashes differ. The snapshot patch adds identity-validation hunks that are already present in the reported production baseline and therefore are redundant or context-incompatible there. Its 429 hunk is also tied to snapshot-specific surrounding context. A successful snapshot test does not establish production applicability.

## Production-only patch assessment

A separate production-only artifact is required if the production change is pursued. Its intended, narrowly scoped semantic delta is:

1. Match the exact production baseline SHA256 above.
2. In the explicit-account 429 branch, preserve the existing `mark_unavailable(..., "HTTP 429 限速冷却", COOLDOWN_429)` mutation.
3. Remove the recursive `create_chat_rotating(...)` call and return failure directly from the explicit-account request.
4. Do not alter the already-present identity-validation logic or unrelated rotation behavior.

**Status: BLOCKED — artifact not generated or validated.** The `vm-43-ubuntu` device was not available through the connected Remote Desktop Commander device list in this execution environment. Consequently, the production file could not be read-only copied to an isolated temporary directory, and no production-baseline dry-run, apply, or tests were performed. No access to `/opt/so-sender` was attempted from another host or by an alternate path.

Unblock only after the exact production file can be read through the authorized remote device: copy it read-only into a temporary workspace, verify the copied SHA256 and byte size against the baseline above, construct and test the production-only patch against that copy, and retain the live artifact as blocked unless all checks pass. Never overwrite or modify the production file as part of patch validation.
