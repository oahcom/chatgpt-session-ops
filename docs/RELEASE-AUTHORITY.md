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

The following eleven files are the complete allowlist for the current release
baseline:

| B-relative path | A-relative path | Purpose |
|---|---|---|
| `SKILL.md` | `SKILL.md` | Skill instructions |
| `references/baselines.md` | `references/baselines.md` | Baseline reference |
| `references/evidence-matrix.md` | `references/evidence-matrix.md` | Evidence reference |
| `scripts/chatgpt_send.py` | `scripts/chatgpt_send.py` | Reviewed conversation-send utility |
| `scripts/pow_solver.py` | `scripts/pow_solver.py` | Proof-of-work solver that `scripts/chatgpt_send.py` imports at module level |
| `scripts/mcp_connector.py` | `scripts/mcp_connector.py` | Reviewed MCP connector eligibility utility |
| `scripts/continue_full.py` | `scripts/continue_full.py` | CDP continuation channel that `tests/test_hardening.py` loads by path |
| `scripts/extract_auth.py` | `scripts/extract_auth.py` | Cookie extractor whose domain predicate `tests/test_hardening.py` cross-checks against the sender |
| `tests/test_hardening.py` | `tests/test_hardening.py` | Reviewed hardening tests |
| `references/mcp-oauth-connector-contract.md` | `references/mcp-oauth-connector-contract.md` | Connector/OAuth contract that `SKILL.md` cites |
| `references/mcp-tools.md` | `references/mcp-tools.md` | MCP tool operations contract for the ten exposed tools |

This is an explicit file allowlist, not permission to synchronize the containing
directories.

Three entries are here only because another entry cannot run without them.

`scripts/chatgpt_send.py` executes `from pow_solver import solve` at module
level. While the solver is outside this list, two things are both true: a copy
left behind in A from an earlier release never receives updates, so a change to
the proof-of-work algorithm in B silently never reaches the copy that runs; and
a tree assembled only from the list cannot import the sender at all
(`ModuleNotFoundError: No module named 'pow_solver'`), nor collect a single test
in `tests/test_hardening.py`, which loads the sender at import time. Pure
algorithm, no credential or account state.

`tests/test_hardening.py` loads `scripts/continue_full.py` and
`scripts/extract_auth.py` by path. While they are outside this list, a tree
assembled from it still imports the product, but the shipped test file cannot
run: 24 of its cases fail on `FileNotFoundError` for `continue_full.py`, one on
the `extract_auth` import, and one more on the A27 citation
`code:enable_thinking`, a symbol that only resolves because `continue_full.py`
is on disk. Those failures are indistinguishable at a glance
from a real regression, so an operator who follows the `SKILL.md` instruction to
run the suite before touching locks, persistence, ids, device identity or proxy
sees a red suite on a healthy install. The closure was computed, not guessed:
the eleven files are exactly the shipped files plus every local module they
load, and a tree built from them runs the suite green at the same counts as B.

`scripts/extract_auth.py` is the module that reads browser cookies, so listing
it does change what a release may overwrite: the copy in A is now maintained by
B instead of being frozen at whatever an earlier release left behind. That is
the point. The file is the one place where the cookie domain predicate exists in
a second implementation, and the shipped test exists precisely to catch the two
implementations drifting apart. Freezing the A-side copy means the drift check
runs against a file no release can correct.

One file is deliberately NOT listed, and the difference matters.
`scripts/start_chrome.py` is launched, not imported: no shipped file loads it at
module level, no shipped test loads it at all, and `scripts/chatgpt_send.py`
resolves it at runtime from the CDP directory (`CHATGPT_CDP_DIR`, defaulting to
the `browser-automation-cdp` skill). A tree built from this list therefore still
imports the product and still runs the suite green without it. Publishing it
would make this repository the owner of a script it does not test, and would
silently overwrite whatever browser launcher the operator's CDP install
provides.

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

The release operator MUST preserve all A-side paths outside the allowlist. The operator MUST NOT use directory mirroring or whole-directory
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
3. Confirm that every allowlisted source file in §2 exists in B.
4. Confirm that the corresponding target directories and files in A are
   present where required.
5. Inspect the A-side runtime state and confirm that the release will not
   touch excluded paths.
6. Confirm that the release consists only of the explicit file paths
   in the §2 allowlist.
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

A release is a controlled allowlist publication, not a synchronization of
the two directory trees.

## 6. SHA-256 verification

For each allowlisted file, calculate SHA-256 for both the B source
and the A destination after copying.

The release passes file-integrity verification only when:

- Both files exist.
- Both SHA-256 values can be calculated.
- The source and destination SHA-256 values are identical.

Example PowerShell verification:

