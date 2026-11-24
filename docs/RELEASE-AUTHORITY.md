# chatgpt-session-ops — Repository Authority and Runtime Release Policy

**Status:** Active  
**Effective date:** 2026-11-24  
**Authoritative repository:** B  
**Runtime copy:** A  
**Authorized release direction:** B → A only

## 1. Authority and copy roles

This project maintains two distinct copies:

- **B — authoritative Git repository**
  - Path: this repository checkout (relative path: `.`). Locate it via
    `git rev-parse --show-toplevel` from any directory inside the checkout.
    The historical absolute path is intentionally not recorded here so the
    policy remains portable across machines and drives.
  - The only authoritative source for tracked project code, documentation,
    tests, and release decisions.
  - Changes must be reviewed and committed here.

- **A — WorkBuddy Skill runtime copy**
  - Path: the installed WorkBuddy skill directory for `chatgpt-session-ops`
    (resolve at runtime, e.g. `<workbuddy-skills-dir>/chatgpt-session-ops`).
    The machine-specific absolute path is intentionally not recorded here.
  - The installed runtime copy used by WorkBuddy.
  - It may contain machine-local accounts, credentials, browser state,
    temporary files, diagnostic scripts, and other runtime artifacts.
  - It is not an independent source of authority for formal project releases.

The only authorized formal publication direction is **B → A**.

A → B is not an authorized synchronization direction. A local change may be
considered for the project only after it has been reviewed, deliberately
incorporated into B, tested, and committed.

## 2. Explicit B → A publication allowlist

The following eight files are the complete allowlist for the current release
baseline:

| B-relative path | A-relative path | Purpose |
|---|---|---|
| `SKILL.md` | `SKILL.md` | Skill instructions |
| `references/baselines.md` | `references/baselines.md` | Baseline reference |
| `references/evidence-matrix.md` | `references/evidence-matrix.md` | Evidence reference |
| `scripts/chatgpt_send.py` | `scripts/chatgpt_send.py` | Reviewed conversation-send utility |
| `scripts/mcp_connector.py` | `scripts/mcp_connector.py` | Reviewed MCP connector eligibility utility |
| `tests/test_hardening.py` | `tests/test_hardening.py` | Reviewed hardening tests |
| `references/mcp-oauth-connector-contract.md` | `references/mcp-oauth-connector-contract.md` | Connector/OAuth contract that `SKILL.md` cites |
| `references/mcp-tools.md` | `references/mcp-tools.md` | MCP tool operations contract for the ten exposed tools |

This is an explicit file allowlist, not permission to synchronize the containing
directories.

A file not listed above MUST NOT be copied as part of this release procedure.
A new file or path may be added only through a separately reviewed and
documented change to this policy.

The allowlist does not authorize copying local configuration, credentials,
test outputs, temporary files, or other runtime state.

## 3. A runtime paths and data that MUST NOT be overwritten

The following A-side runtime data is outside the publication allowlist and
MUST NOT be overwritten, deleted, mirrored, or replaced by a B-side release:

- `accounts/` and all descendants
- `cookies.json`
- `access_token.txt`
- `accounts/**/access_token.txt`
- `conv-accounts.json`
- `proxy.json`
- `anon-sessions.json`
- `scripts/conv-accounts.json.highwater.json`
- `scripts/_convq*.txt`
- `scripts/_q*.txt`
- `scripts/*_send*.log`
- Local browser profiles, browser sessions, and browser authentication state
- Any other local credentials, tokens, account data, or machine-specific
  configuration
- A-only diagnostic and operational scripts not included in the allowlist

This list is a minimum explicit exclusion list, not an exhaustive inventory
of every possible runtime artifact.

The release operator MUST preserve all A-side paths outside the eight-file
allowlist. The operator MUST NOT use directory mirroring or whole-directory
replacement to achieve a release.

In particular, the following are prohibited:

- `robocopy /MIR`
- Recursive replacement of `A/scripts/` or `A/references/`
- `git add -A` as a substitute for deliberate repository staging
- Any operation that deletes or overwrites A-side files merely because they
  do not exist in B

## 4. Pre-release checks

Before copying any file:

1. Confirm that B is the intended repository and that the intended release
   commit is present.
2. Inspect `git status --short` and the release diff in B.
3. Confirm that the eight allowlisted source files exist in B.
4. Confirm that the corresponding target directories and files in A are
   present where required.
5. Inspect the A-side runtime state and confirm that the release will not
   touch excluded paths.
6. Confirm that the release consists only of the six explicit file paths.
7. Record the source commit and the release time in the execution record.

If any source file is missing, any required target is unexpectedly missing,
or the A runtime state is abnormal, STOP. Do not create a replacement by
copying an entire directory, do not delete the unexpected state, and do not
continue with a partial release until the discrepancy has been investigated.

## 5. Copy procedure

Copy each allowlisted file individually from B to its corresponding path in A.

The operator MUST NOT:

- Copy a parent directory recursively.
- Copy files outside the allowlist.
- Delete files in A to make A resemble B.
- Replace A-side runtime configuration with repository examples.
- Treat an untracked A-side file as disposable merely because B does not
  contain it.

A release is a controlled eight-file publication, not a synchronization of
the two directory trees.

## 6. SHA-256 verification

For each of the six allowlisted files, calculate SHA-256 for both the B source
and the A destination after copying.

The release passes file-integrity verification only when:

- Both files exist.
- Both SHA-256 values can be calculated.
- The source and destination SHA-256 values are identical.

Example PowerShell verification:

