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
- Around line 9454: explicit-account 429 handling marks the account unavailable and then calls `return create_chat_rotating(...)`, permitting cross-account rotation. The production baseline already has `validate_account_identity`; the only requested live delta is to remove this explicit-429 cross-account recursive fallback.

The production and snapshot file hashes differ. The snapshot patch adds identity-validation hunks that are already present in the reported production baseline and therefore are redundant or context-incompatible there. Its 429 hunk is also tied to snapshot-specific surrounding context. A successful snapshot test does not establish production applicability.

## Production-only patch assessment

A separate production-only artifact is required if the production change is pursued. Its intended, narrowly scoped semantic delta is:

1. Match the exact production baseline SHA256 `0d758febc28ea608eee050aad4e60eb6afe77807b7f9e8d8640b3836293443ca` (413113 bytes).
2. In the existing explicit-account 429 branch, leave the current account-health/cooldown mutation byte-for-byte unchanged.
3. Remove only the recursive `create_chat_rotating(...)` cross-account fallback; let the existing explicit-account result path return failure.
4. Do not add or duplicate `validate_account_identity`, alter failure accounting, change ordinary auto-rotation, or touch unrelated production behavior.

**Status: BLOCKED — no live-only patch artifact generated or validated.** The authorized `vm-43-ubuntu` device / exact production temporary copy is not available in the current worktree session. The supplied hash and branch description identify the intended delta, but are not a substitute for the exact 413113-byte source needed to generate a context-correct patch. Therefore no production-baseline dry-run, apply, or temporary-copy tests were performed. No production file was read, overwritten, or modified by this work.

Unblock only when the exact production file can be read through the authorized remote device: copy it read-only into an isolated temporary workspace, verify SHA256 and byte size, generate a patch that removes only the explicit-429 recursive `create_chat_rotating(...)` call, dry-run and apply it only to that temporary copy, and test that identity validation and existing cooldown mutation remain unchanged while explicit 429 returns failure without cross-account rotation. Keep the live artifact marked BLOCKED until all checks pass. Never apply the patch to `/opt/so-sender` as part of validation.
