# Deploy Session Ops MCP

## Why deployment must be commit-verifiable

A deployment is not proven correct merely because the expected files exist.

There are two distinct sets:

- The deployment set: the paths and bytes actually present in the deployed
  directory.
- The commit tree set: the paths and Git blob objects recorded by one specific
  commit.

For a deployment to be attributable to a commit, every managed deployment file
must match the blob recorded at the same path in that commit. In other words,
the deployed `(path, content)` pairs must be contained in the selected commit
tree. A deliberately scoped deployment may contain only a subset of the
commit's paths, but every deployed managed file must still match that commit.

A directory assembled by copying individual files can violate this condition
without looking incomplete. It may contain every expected filename while
combining files from different commits. In that case there is no single commit
whose tree explains the deployed set.

This has happened in this service.

Production `src/session_ops/mcp_auth.py` was found with SHA-256 `312a1836…`
and 34843 bytes, which is commit `f28561b` (PR #36), Git blob `c17bfefd`.
`main` carried SHA-256 `117d0725…` and 38278 bytes, commit `aef7a1f`
(PR #42), Git blob `1a285abb`. The production file existed, but its content
was one PR older and lacked two SSRF protections introduced by #42:

- DNS validation and the subsequent HTTPS connection performed two independent
  resolutions. The address that passed validation was not necessarily the
  address that was connected to. The fix resolves once and pins that address
  for the connection while keeping TLS SNI and hostname verification.
- The default `urllib` opener honoured proxy environment variables, and a
  configured proxy performs its own DNS resolution. With a proxy in play the
  local destination check was skipped entirely. The fix disables proxy
  handling for these requests.

A file-existence check cannot detect this class of failure. It sees a file at
the expected path and learns nothing about which revision supplied its bytes.

The manifest records Git blob object IDs for managed files. A blob ID is

    SHA1("blob " + decimal_byte_length + NUL + file_bytes)

which is the same object identity `git hash-object` computes for those bytes.
It names a file's content without requiring a `.git` directory on the
deployment host, which is why it is the field an operator cross-checks against
the selected commit.

A blob ID identifies content, not a commit: identical content can appear in
many commits, at one path or several. Binding a path to a blob ID and a commit
is the manifest's claim, and the verifier does not resolve that claim against
git.

- the full commit SHA selected as the deployment source,
- the repository-relative path, and
- the blob ID that commit records for that path.

SHA-256 of the raw and LF-normalized bytes is recorded as well. The gate is
the LF-normalized digest: a file whose LF-normalized content matches passes,
and a difference in the raw bytes alone is reported as `EOL_DRIFT`, which
`--strict` turns into a failure.

The blob comparison runs only when the LF-normalized digest has already
differed, so it labels a failure the run has already recorded rather than
adding an independent gate. The blob ID in the list above is a field to
cross-check by hand; see "What a passing run does not prove" in section 7.

A deployment is attributable to a commit only when all of the following hold:

1. The manifest names a full commit SHA, not a branch or tag, and that SHA was
   resolved to a commit in a trusted checkout (`git rev-parse --verify
   '<SHA>^{commit}'`). The verifier checks the shape of the field, not that the
   value names a commit, so this condition is met by resolving it, not by the
   verifier passing.
2. The manifest's managed path list is exactly the intended deployment
   selection, and is not empty.
3. Every deployed managed file passes verification against that manifest under
   `--strict`, so no file differs from the commit even in line endings, and the
   deployment root contains no symbolic links.
4. Required external objects pass their recorded hash checks.
5. The database contract is present in the manifest and its required columns
   pass verification.
6. The deployment record states which manifest and which commit were
   installed.

A successful health check is necessary operational evidence but proves nothing
about commit identity: a service can be healthy while running a mixture of old
and new files. Likewise, a successful file-existence check says nothing about
where the bytes came from.

When the verifier reports a missing file, a content or blob mismatch, external
object drift, or a database schema failure, do not describe the deployment as
matching the selected commit. Correct the deployment first.

The verifier result and the manifest commit belong together in the deployment
record. The deployment directory timestamp may be recorded beside them as an
operational marker — it locates the window in the journal — but it is not
evidence of what was installed. A commit SHA written in a manifest is an
expected source identity; on its own it is not proof that the files on disk
were installed from that commit. Section 8 states what is.


## 1. Deployment authority

The only deployment authority is a full Git commit SHA from
`oahcom/chatgpt-session-ops`'s GitHub `main` history.

A branch name, `main`, a tag, or a moving ref is not a deployment
identifier. Resolve the intended commit before deployment and record
the full 40-character SHA.

The production tree is built from Git objects, not copied file by file:

```bash
git fetch origin main
git rev-parse --verify '<FULL_40_HEX_SHA>^{commit}'
git show -s --format='commit=%H%ncommitter_time=%ct' '<FULL_40_HEX_SHA>'
```

`committer_time` is recorded for provenance only. It is not an acceptance
criterion; section 8 establishes production equality from content, not from
timestamps.

Create a clean candidate from that exact commit:

```bash
COMMIT='<FULL_40_HEX_SHA>'
SHORT="$(git rev-parse --short=12 "$COMMIT")"
TMP="/opt/so-mcp.new-$SHORT"

test "$(git cat-file -t "$COMMIT")" = commit
test ! -e "$TMP"
install -d -o ubuntu -g ubuntu -m 0750 "$TMP"
git archive --format=tar "$COMMIT" \
  scripts/run_mcp_server.py \
  src \
  requirements-mcp.txt \
  tools/verify_deploy.py \
  | tar -xf - -C "$TMP"
```

The pipeline must run with `set -o pipefail` if placed in a deployment
script. Check the extracted tree before switching production:

```bash
test -f "$TMP/scripts/run_mcp_server.py"
test -f "$TMP/src/session_ops/authority.py"
test -f "$TMP/src/session_ops/mcp_auth.py"
test -f "$TMP/src/session_ops/mcp_server.py"
test -f "$TMP/src/session_ops/memory_adapter_client.py"
test -f "$TMP/src/session_ops/work_items.py"
test -f "$TMP/src/session_ops/__init__.py"
test -f "$TMP/requirements-mcp.txt"
test -f "$TMP/tools/verify_deploy.py"

test ! -e "$TMP/tests"
test ! -e "$TMP/deploy"
test ! -e "$TMP/docs"
test ! -e "$TMP/references"
test ! -e "$TMP/.github"
test ! -e "$TMP/scripts/chatgpt_send.py"
test ! -e "$TMP/scripts/continue_full.py"
test ! -e "$TMP/scripts/extract_auth.py"
test ! -e "$TMP/scripts/mcp_connector.py"
test ! -e "$TMP/scripts/pow_solver.py"
test ! -e "$TMP/scripts/accounts"
```

The managed path list in `deploy/paths.txt` must describe exactly this
archive set. A manifest built from the full repository cannot verify a
trimmed production tree.

Do not use manual `scp` of selected Python files. A prior incident left
`mcp_auth.py` and `mcp_server.py` at the first MCP commit (`05ffbbdf`),
missing changes from `85666489` and `aef7a1f4`. A separate deployment
copied only `authority.py`, producing a partial deployment. A commit
archive makes the application tree one immutable Git snapshot.

## 2. Production directory layout

The active tree is `/opt/so-mcp`.

The immediately previous tree is retained as:

`/opt/so-mcp.prev-<12-character-short-sha>`

A candidate is unpacked into:

`/opt/so-mcp.new-<12-character-short-sha>`

Do not leave timestamped `.bak-*` files in the application tree.
Files such as `mcp_auth.py.bak-1795566998` are deployment debris, not
rollback artifacts. Rollback is the previous complete tree.

Preserve them under `/var/backups/so-mcp/` instead of deleting them.
Do not delete historical backup files merely to make verification pass:
some of them are forensic evidence of a version that was deployed.
`mcp_auth.py.bak-1795573995` is 34843 bytes, the same size as the
`f28561b` revision of that file, which is how the drift described above
was established. Record the original path, destination path, SHA-256,
size, and the change record that authorised the move. The verifier still
treats `*.bak-*` inside the deployment root as a hard failure; the
exemption for `.venv/**` and `bin/**` does not extend to them.

Before switching, ensure the candidate has the correct ownership and
permissions and has passed the verifier.

An existing `.venv` may be preserved and moved into the candidate tree only
when all of the following hold:

- The `requirements-mcp.txt` Git blob is identical between the currently
  running deployment and the target commit.
- The Python interpreter version, ABI, operating-system environment, and
  architecture remain compatible with the existing environment.
- The existing environment passes its dependency and runtime checks before the
  directory switch.
- The environment ends up at its original absolute runtime path after the
  switch. Do not leave a venv at a different path merely because its directory
  was moved.

If any condition is not established, rebuild the environment from the pinned
dependency process. An unchanged requirements file alone is not proof that the
existing environment is valid.

At minimum, check the environment before the switch:

```bash
/opt/so-mcp/.venv/bin/python --version
/opt/so-mcp/.venv/bin/python -m pip check
```

`pip check` finds dependency conflicts between installed packages. It does not
prove that the installed set matches the requirements file exactly, so it is
not an environment-equivalence proof.

Switch the directories only after the database backup and candidate
checks have succeeded:

```bash
PREV="/opt/so-mcp.prev-$SHORT"

test -d /opt/so-mcp
test ! -e "$PREV"
mv /opt/so-mcp "$PREV"
mv "$TMP" /opt/so-mcp
```

Do not `touch` the new directory. Its mtime is an observation, not evidence;
section 8 explains why and what replaced that condition.

The service uses `WorkingDirectory=/opt/so-mcp`; this directory swap
keeps the configured path stable. There is a short interval between
the two `mv` operations in which the path is absent. Perform the swap
during the deployment window and immediately request the restart.

Keep only the current tree and the immediately previous complete tree
under `/opt`. Remove older previous trees only after the new version
has passed its observation period and rollback retention policy.

## 3. Database backup and migration accounting

The production authority data is in:

`/var/lib/so-mcp/work-items.sqlite3`

Do not use `cp` to take a live SQLite backup. A previous `cp` snapshot
was missing `authority_landing_events`. Use SQLite's online backup
API through the CLI `.backup` command:

```bash
DB=/var/lib/so-mcp/work-items.sqlite3
BACKUP_DIR=/var/backups/so-mcp
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

install -d -o root -g root -m 0700 "$BACKUP_DIR"
sqlite3 "$DB" ".backup '$BACKUP_DIR/work-items-$STAMP.sqlite3'"
test -s "$BACKUP_DIR/work-items-$STAMP.sqlite3"
sqlite3 "$BACKUP_DIR/work-items-$STAMP.sqlite3" "PRAGMA integrity_check;"
```

The integrity check must return `ok`. Preserve the backup until the
deployment has passed its rollback window.

Record the following pre-migration accounting output:

```bash
sqlite3 -header -column "$DB" \
  "SELECT COUNT(*) AS total FROM authority_landing_events;"

sqlite3 -header -column "$DB" \
  "SELECT match, COUNT(*) AS rows
     FROM authority_landing_events
    GROUP BY match
    ORDER BY match;"

sqlite3 -header -column "$DB" \
  "SELECT COUNT(*) AS consumed
     FROM authority_landing_events
    WHERE consumed_at IS NOT NULL;"

sqlite3 -header -column "$DB" \
  "SELECT COUNT(*) AS active_links
     FROM session_links
    WHERE unlinked_at IS NULL;"
```

After migration, run exactly the same four queries against the live
database and compare the results with the recorded pre-migration
values. The migration must not silently lose landing rows, change
their `match` distribution, change the consumed count, or change the
active-link count.

A changed count is not automatically a migration failure if a
separately authorized application operation changed data during the
deployment window. In that case, stop and reconcile the exact rows
and audit history; do not waive the discrepancy based only on a
plausible explanation.

## 4. Restart and health verification

Do not use synchronous `systemctl restart so-mcp` from the SSH
deployment session. It has twice terminated the active SSH session
during restart. Request the restart asynchronously and poll systemd.

Capture the old process identity first:

```bash
OLD_PID="$(systemctl show so-mcp -p MainPID --value)"
OLD_ENTER="$(systemctl show so-mcp -p ActiveEnterTimestamp --value)"

sudo systemctl restart --no-block so-mcp
```

Poll until both the process ID and active-enter timestamp have changed
and the service is active:

```bash
for attempt in $(seq 1 60); do
  STATE="$(systemctl show so-mcp -p ActiveState --value)"
  NEW_PID="$(systemctl show so-mcp -p MainPID --value)"
  NEW_ENTER="$(systemctl show so-mcp -p ActiveEnterTimestamp --value)"

  if [ "$STATE" = active ] &&
     [ "$NEW_PID" != 0 ] &&
     [ "$NEW_PID" != "$OLD_PID" ] &&
     [ -n "$NEW_ENTER" ] &&
     [ "$NEW_ENTER" != "$OLD_ENTER" ]; then
    break
  fi
  sleep 1
done

test "$(systemctl show so-mcp -p ActiveState --value)" = active
NEW_PID="$(systemctl show so-mcp -p MainPID --value)"
test "$NEW_PID" != 0
test "$NEW_PID" != "$OLD_PID"
test "$(systemctl show so-mcp -p ActiveEnterTimestamp --value)" != "$OLD_ENTER"
```

Check the local health endpoint and the externally routed MCP endpoints.
Set `SO_MCP_ORIGIN` to the real HTTPS origin before running:

```bash
SO_MCP_ORIGIN='https://<production-host>'

test "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8791/healthz)" = 200

test "$(curl -sS -o /dev/null -w '%{http_code}' \
  "$SO_MCP_ORIGIN/.well-known/oauth-protected-resource/so-mcp")" = 200

test "$(curl -sS -o /dev/null -w '%{http_code}' \
  "$SO_MCP_ORIGIN/so-mcp")" = 401
```

The unauthenticated MCP request must return 401; a 200 response is
not an acceptable substitute for the authentication boundary.

The health probe is the root path. `/so-mcp/healthz` is protected by the same
authentication policy as the rest of `/so-mcp`: it answers 401 without
credentials, and that 401 means the request reached the authentication layer,
not that the nginx route is missing.

| Target | Expected |
|---|---|
| `/healthz` | 200, the health probe |
| `/so-mcp` | 401 without credentials |
| `/so-mcp/healthz` | 401 without credentials; not a routing failure |
| `/.well-known/oauth-protected-resource/so-mcp` | 200 |

Exempting `/so-mcp/healthz` from authentication would be a new security
boundary decision, not a deployment fix, and has not been made. If external
monitoring ever needs an unauthenticated probe there, decide it separately:
what the endpoint reveals, how far the exemption reaches in nginx location
matching, and which release authorises it.

Also inspect the service journal for startup errors:

```bash
sudo journalctl -u so-mcp --since '-5 minutes' --no-pager
```

## 5. Rollback

The code rollback target is the immediately previous complete tree:

`/opt/so-mcp.prev-<previous-short-sha>`

Do not reconstruct a rollback by copying selected `.py` files.

```bash
FAILED="/opt/so-mcp"
ROLLBACK="/opt/so-mcp.prev-<previous-short-sha>"
FAILED_HOLD="/opt/so-mcp.failed-$(date -u +%Y%m%dT%H%M%SZ)"

test -d "$ROLLBACK"
mv "$FAILED" "$FAILED_HOLD"
mv "$ROLLBACK" /opt/so-mcp
sudo systemctl restart --no-block so-mcp
```

Poll `ActiveEnterTimestamp` and `MainPID` as in §4, then repeat
the health and authentication checks.

The authority migration in this release is additive: it adds columns
to `authority_landing_events` and does not remove the legacy columns.
The previous application code reads its known columns and can run
with additional columns present. Therefore, a code-only rollback does
not require restoring the database snapshot.

Restore the pre-migration SQLite backup only if data integrity or
migration accounting proves that the database itself must be rolled
back. Restoring a database snapshot discards writes made after that
snapshot; stop the service and explicitly account for those writes
before doing so.

## 6. Retiring stage and A/B deployment remnants

`/opt/so-mcp.stage` and `/opt/so-mcp.ab` are not production
deployment targets. They have no systemd unit and no listener. They
must not remain as runnable alternate application trees.

Preserve their SQLite data as offline retirement snapshots, then
remove both executable trees, including their `.venv` directories.
Do not archive their Python code or virtual environments as runnable
deployments.

Back up every SQLite database actually found under either retired
tree. Do not assume the set from the two files observed earlier; discover
it, back it up with `.backup`, and verify each snapshot. Never use `cp`;
a plain copy of a live SQLite file can capture a torn page image.

```bash
#!/usr/bin/env bash
set -euo pipefail

RETIRE=/var/backups/so-mcp/retired
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

sudo install -d -o root -g root -m 0700 "$RETIRE"

# Keep the find output outside the directories being retired.
LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT

for ROOT in /opt/so-mcp.stage /opt/so-mcp.ab; do
  [ -d "$ROOT" ] || continue

  NAME="$(basename "$ROOT")"
  DEST="$RETIRE/$NAME-$STAMP"
  COUNT=0

  # Do not use process substitution here: its find exit status would not
  # reliably become the status of this script.
  if ! sudo find "$ROOT" -type f -name '*.sqlite3' -print0 > "$LIST"; then
    echo "Cannot enumerate SQLite databases under $ROOT" >&2
    exit 1
  fi

  # These directories are known to contain databases. An empty list must
  # not silently authorize deletion.
  if [ ! -s "$LIST" ]; then
    echo "No SQLite databases found under $ROOT; refusing retirement" >&2
    exit 1
  fi

  sudo install -d -o root -g root -m 0700 "$DEST"

  while IFS= read -r -d '' DB; do
    REL="${DB#"$ROOT"/}"
    OUT="$DEST/$REL"

    sudo install -d -o root -g root -m 0700 "$(dirname "$OUT")"

    # SQLite's .backup API produces a consistent database snapshot.
    sudo sqlite3 "$DB" ".backup '$OUT'"

    # Require a real, non-empty backup before running integrity_check.
    sudo test -s "$OUT"

    CHECK="$(sudo sqlite3 "$OUT" 'PRAGMA integrity_check;')"
    if [ "$CHECK" != "ok" ]; then
      echo "SQLite backup integrity failure: $DB" >&2
      echo "Backup path: $OUT" >&2
      echo "integrity_check: $CHECK" >&2
      exit 1
    fi

    COUNT=$((COUNT + 1))
    printf 'Backed up and checked: %s -> %s
' "$DB" "$OUT"
  done < "$LIST"

  if [ "$COUNT" -eq 0 ]; then
    echo "No SQLite backup completed under $ROOT; refusing retirement" >&2
    exit 1
  fi

  printf 'Verified %s SQLite backup(s) for %s
' "$COUNT" "$ROOT"
done

echo "All retirement database backups passed integrity_check."
echo "Only now is it safe to remove the retired directories."
```

The retirement snapshots are data, not deployments: they must not be
referenced by any unit file.

After verifying all found database files have corresponding valid
snapshots:

```bash
sudo rm -rf -- /opt/so-mcp.stage /opt/so-mcp.ab
```

Do not delete the retirement snapshots as part of this cleanup.

## 7. Verify the deployed tree

### Generate the manifest from the trusted commit

The manifest is generated from a trusted checkout and an explicit full commit
SHA. The commit must come from a trusted checkout; the environment that runs
the generator must be able to read the external objects and the database that
the manifest names. In practice that means the production host, or a host with
the exact production paths mounted and readable.

Do not generate it from the production deployment directory: that would turn
the files being checked into their own expected values.

Use `deploy/paths.txt` as the authoritative list of repository-managed
deployment files. It must match the explicit `git archive` selection used in
section 1.

Run the following from the trusted checkout. Replace the placeholder with the
full 40-character commit SHA selected for deployment.

```bash
COMMIT='<FULL_40_HEX_SHA>'

sudo python3 tools/gen_manifest.py \
  --repo "$PWD" \
  --commit "$COMMIT" \
  --paths-from deploy/paths.txt \
  --external systemd-unit=/etc/systemd/system/so-mcp.service \
  --external nginx-snippet=/etc/nginx/snippets/so-mcp.conf \
  --external environment-file=/etc/so-mcp/so-mcp.env \
  --db-path /var/lib/so-mcp/work-items.sqlite3 \
  --db-required-column session_links.session_id \
  --db-required-column authority_landing_events.attempt_id \
  --allow-unmanaged '.venv/**' \
  --allow-unmanaged 'bin/**' \
  --allow-unmanaged '*.pyc' \
  --allow-unmanaged '**/*.pyc' \
  --out /etc/so-mcp/manifest.json
```

The command needs permission to read the environment file and the database and
to write `/etc/so-mcp/manifest.json`. The manifest records hashes and metadata
for the external objects; it does not copy the environment file's contents.

The generator rejects a database path that is not absolute or does not name an
existing file. That catches a mistyped path or an unmounted volume at
manifest-generation time; the verifier opens the same path again at
verification time and checks the required columns. The generator does not
inspect the file's contents or its size: an empty or corrupt database passes
generation and fails verification. Asking for required columns without a
database path is an error, not a contract that gets silently dropped.

The database contract requires the listed columns to exist. In particular,
`authority_landing_events.attempt_id` must be present after the P1/P2
migration. A database missing that column fails with `DB_SCHEMA_BEHIND`; a
missing database file fails with `DB_MISSING`.

The manifest's `database.schema_version` is a contract label. The verifier
reports it as `expected_schema_version` but does not compare it against SQLite
`PRAGMA user_version`. The enforced gate is the `required_columns` mapping. Do
not treat `schema_version` as proof that the database carries a particular
`user_version`.

`allowed_unmanaged` records the deliberate deployment-root exclusions. The
`.venv/**` and `bin/**` patterns cover the Python virtual environment and the
separately maintained Memory Adapter executable, neither of which is a
commit-managed application file. Files matching these patterns are not
individually content-verified. This does **not** verify `.pth` files,
`sitecustomize.py`, installed packages, or the interpreter inside `.venv`. A
`.pth` file or a `sitecustomize.py` planted in that environment would execute
on every service start and would not be reported. Do not describe the runtime
environment as verified; the accurate statement is that the managed
application files and the listed external objects were verified.

The following external-object values were observed on the production host when
this procedure was written. They are reference values for review, not
constants: a legitimate operations change to a unit, snippet, or environment
file changes its hash and requires regenerating and reviewing the manifest.

| Kind | Path | SHA-256 | Bytes | Observed mode |
|---|---|---|---:|---|
| `systemd-unit` | `/etc/systemd/system/so-mcp.service` | `f8ac60885fa5ee99206648b81e7f809fed169ece8c43dc50efdbd5ab77aad678` | 740 | `0644 root:root` |
| `environment-file` | `/etc/so-mcp/so-mcp.env` | `aa32562faaf758e4474c7a650ea6f4adc8aa40bf447671502c0f36323a80c2fa` | 412 | `0600 root:root` |
| `nginx-snippet` | `/etc/nginx/snippets/so-mcp.conf` | `ea2a2525476d100bd3c0b6fa0fd1e4803859a70b8364d68a7476046a145902e8` | 1794 | `0644 root:root` |

The snippet is included by `/etc/nginx/conf.d/rmc-native-ip.conf` at line 36.
That including file is shared with unrelated server configuration and is
deliberately not hashed as a service-specific external object: hashing it
would turn an unrelated site's nginx change into a failure of this service's
deployment check.

The generator records external-object content hashes and byte counts. The
observed modes above are operational reference information; they are not
enforced by the verifier.

After generating, review the full commit SHA, the managed file list, the
external-object entries, the database path and required columns, and the
unmanaged allow-list. Do not install a manifest whose commit or managed path
list does not match the approved deployment selection.

### Run the verifier


Keep the manifest outside the application root, for example:

`/etc/so-mcp/manifest.json`

The application root must not contain `deploy/`, `.git/`, `tests/`,
or `*.bak-*`.

Run:

```bash
sudo /opt/so-mcp/.venv/bin/python /opt/so-mcp/tools/verify_deploy.py \
  --root /opt/so-mcp \
  --manifest /etc/so-mcp/manifest.json
```

Use `--strict` when EOL drift must also block acceptance:

```bash
sudo /opt/so-mcp/.venv/bin/python /opt/so-mcp/tools/verify_deploy.py \
  --root /opt/so-mcp \
  --manifest /etc/so-mcp/manifest.json \
  --strict
```

A successful non-strict run may contain `EOL_DRIFT` warnings. It must
not contain any other issue. A strict run treats EOL drift as failure.

### What a passing run does not prove

`DEPLOY VERIFY OK` means the files the verifier could see matched the hashes in
the manifest it was given. It does not establish that the manifest describes
the named commit, and there are trees it cannot see. The following are measured
properties of `tools/verify_deploy.py`, not cautions about misuse.

**The commit is a label, not a check.** The verifier never invokes `git`. It
requires `commit` to be 40 lowercase hex characters and prints it back
unchanged, so a manifest naming `0000000000000000000000000000000000000000`
passes. The SHA becomes evidence only through the rule above that the manifest
is generated on the trusted checkout, and through section 8's source and
managed-tree checks. Do not cite the verifier's exit status as proof of the
commit it names.

**A symlinked directory is skipped silently.** The tree walk does not descend
into it, `Path.is_dir()` follows it so the "managed files must be regular
files" check never sees it, and the missing-file check `(root / rel).is_file()`
follows it as well, so nothing is reported missing. The managed files
underneath are never read. Measured: a manifest carrying the authentic hashes
for `pkg/auth.py`, produced exactly as described above, against a root where
`pkg/` is a symlink to a directory holding a different `auth.py`, prints
`DEPLOY VERIFY OK: 1 files, 0 external objects` and exits 0. The same bytes in
a real `pkg/` directory produce `CONTENT_MISMATCH`. Only a symlinked *file* is
rejected, as `UNEXPECTED_SYMLINK`.

Confirm the deployed tree holds no links before trusting a pass:

```bash
sudo find /opt/so-mcp -type l -print
```

Any output invalidates the run. Check the recorded commit the same way before
concluding a link came from it:

```bash
git -C <trusted/checkout> ls-tree -r "$COMMIT" | awk '$1=="120000"'
```

An empty result means the commit carries no symbolic links, so a link in the
deployment root was not installed from it: rebuild the root from the commit and
verify again. `git archive` preserves links rather than dropping them, so check
the rebuilt root instead of assuming the extraction removed them. If the
command does list a link, the commit itself carries one, and the verifier's
inability to look through a symlinked directory applies to that path by design:
resolve it in the deployment record rather than treating the run as clean.

**`checked_files` counts manifest entries, not verified files.** The banner's
`N files` is the length of `manifest["files"]`. A run that read no file bodies
still announces files, so read that number as the size of the manifest, not the
size of the evidence.

**The blob ID is checked independently.** `git_blob_sha1` is compared on every
managed file, outside the LF-normalized comparison. A manifest whose `sha256_raw`
and `sha256_lf` match the deployed bytes but whose `git_blob_sha1` does not is a
`GIT_BLOB_MISMATCH` and fails the run. Verified on 15dca1a by editing only the
blob field of an otherwise-correct manifest: exit 1, `GIT_BLOB_MISMATCH`.

The SHA-256 pair and the blob answer different questions. The SHA-256 pair says
the LF-normalized content matches; the blob says the exact bytes match. Neither
substitutes for the other, which is why the comparison sits outside the branch
that records `CONTENT_MISMATCH`.

**An empty managed list fails.** `files: []` is rejected: with nothing managed,
every file in the root is `UNMANAGED_FILE`, so the run exits 1. Verified on
15dca1a with `files: []` and one file in the root: exit 1, `UNMANAGED_FILE`.
`tools/gen_manifest.py` additionally refuses to produce such a manifest
(`the explicit managed path list is empty`). An empty allow-list is what makes
the rejection work — a manifest that also sets `allowed_unmanaged: ["*"]` still
suppresses `UNMANAGED_FILE` and passes with `checked_files: 0`, which is the
entry below.

**`allowed_unmanaged` is taken from the manifest.** A `*` entry matches every
path, including across `/`, and suppresses `UNMANAGED_FILE` for the whole tree.
The patterns in the command above are the reviewed set; any other entry is an
unreviewed exclusion.

None of this argues against running the verifier. It argues for running it on a
tree extracted from the recorded commit, with a manifest generated on the
trusted checkout, with the link check above returning nothing, and for reading
the result as "these bytes match this manifest" rather than "these bytes came
from this commit".

## 8. Proving the deployed tree matches main

Production equality is established by content identity, not by the order of
timestamps.

The claim "production = `<SHA>`" is permitted only when all three conditions
hold:

1. **Trusted source.** The manifest's `commit` is an ancestor of the current
   trusted `origin/main`.
2. **Managed tree equality.** The manifest commit and the current
   `origin/main` agree, blob for blob, on every path listed in
   `deploy/paths.txt` — content, file type, and presence.
3. **Production equality.** `verify_deploy.py` passes against the production
   root and the manifest, in both the default and `--strict` modes, and
   `find /opt/so-mcp -type l -print` returns nothing (section 7).

The verifier compares each deployed file against the manifest, which records
both a blob ID for the path and the digest of the deployed content. A file
passes when its LF-normalized content matches; a raw-byte difference is
reported as `EOL_DRIFT` and fails under `--strict`. That is why condition 3
requires both modes. Conditions 2 and 3 then chain to:

```
deployed managed tree == manifest commit managed tree
                      == current origin/main managed tree
```

Run the source and tree checks from a checkout that can reach the trusted
remote. The production host does not need a `.git` directory, and it does not
need network access; the comparison happens where the trusted reference lives.

```bash
set -euo pipefail

REPO=/path/to/trusted/checkout
MANIFEST_SHA='<commit recorded in /etc/so-mcp/manifest.json>'

git -C "$REPO" fetch origin main
MAIN_SHA="$(git -C "$REPO" rev-parse 'origin/main^{commit}')"

# The comparison set is deploy/paths.txt, the same list the archive
# selection and the manifest use.
# Read the list from the reference commit, not from the working tree:
# the comparison is between two commits, and a stale checkout must not
# define what gets compared.
mapfile -t MANAGED \
  < <(git -C "$REPO" show "$MAIN_SHA:deploy/paths.txt" \
      | grep -vE '^[[:space:]]*(#|$)')

if ! git -C "$REPO" merge-base --is-ancestor "$MANIFEST_SHA" "$MAIN_SHA"; then
  echo "SOURCE: FAIL - manifest commit is not in origin/main history" >&2
  exit 1
fi

if ! git -C "$REPO" diff --quiet \
    "$MANIFEST_SHA" "$MAIN_SHA" -- "${MANAGED[@]}"; then
  echo "MANAGED TREE: FAIL - managed paths differ from origin/main" >&2
  git -C "$REPO" diff --stat \
    "$MANIFEST_SHA" "$MAIN_SHA" -- "${MANAGED[@]}" >&2
  exit 1
fi

echo "SOURCE AND MANAGED TREE: PASS"
echo "manifest_commit=$MANIFEST_SHA"
echo "main_commit=$MAIN_SHA"
```

If `git fetch` fails, the script exits before evaluating anything. That is the
correct outcome: conditions 1 and 2 compare against a reference that has to be
current, and an unreachable remote means there is no current reference. A local
`main` that was not refreshed for this check is not a substitute. Record the
check as not performed rather than as passed.

Then, on the production host:

```bash
sudo /opt/so-mcp/.venv/bin/python /opt/so-mcp/tools/verify_deploy.py \
  --root /opt/so-mcp \
  --manifest /etc/so-mcp/manifest.json

sudo /opt/so-mcp/.venv/bin/python /opt/so-mcp/tools/verify_deploy.py \
  --root /opt/so-mcp \
  --manifest /etc/so-mcp/manifest.json \
  --strict
```

Both runs must print `DEPLOY VERIFY OK` with no issues.

### Interpreting the result

| Outcome | Meaning |
|---|---|
| All three conditions pass | Production managed tree equals current `main`. |
| Condition 1 fails | The manifest names a commit outside `main` history; no trusted source relationship exists. |
| Condition 2 fails | Managed content on `main` has moved; production or the manifest has not caught up. |
| Condition 3 fails | Production disagrees with the manifest on a file, an external object, or the database contract. |

A pass on conditions 1–3 says the deployed managed tree equals `main`'s managed
tree, provided the deployment root is a plain directory tree. Two properties of
the verifier bound that claim; section 7 states them and how to check for the
first:

- A symlinked directory is not entered, so managed files beneath it are never
  read and are not reported missing. The `find … -type l` check in condition 3
  is what closes this.
- `commit` is checked for shape only. Condition 1, not the verifier, is what
  ties the manifest to a trusted commit.

If the operator has not brought a trusted `origin/main` reference into the
deployment record through an authenticated channel, the correct status is
narrower: "deployed tree matches manifest commit `<SHA>`; equivalence with
current `main` not established." Do not write "production = `<SHA>`" from the
manifest alone.

### The manifest does not have to track the tip

A manifest commit that is an ancestor of `main`, with an identical managed
tree, is sufficient. Commits that touch only `docs/`, `tests/`, CI
configuration, or any other unmanaged path do not require regenerating the
manifest or redeploying: the managed content did not change, and condition 2
still holds against the newer tip.

Regenerate and redeploy when a managed path changes, or when `deploy/paths.txt`
itself changes — a change to the managed set has to be reviewed as a change to
the contract, not waved through as documentation.

The manifest continues to record the commit it was actually generated from.
This section removes the requirement that it always name the tip; it does not
authorise rewriting a manifest's commit field to a revision whose content was
never deployed.

### Why the timestamp condition was removed

An earlier revision of this section required the deployment root's directory
mtime to be later than the tip commit's committer timestamp. Two failures made
that unsound:

- A directory mtime changes when the directory is swapped, and not otherwise.
  Any commit that leaves the managed paths untouched — documentation, tests,
  CI — advances the tip and turns a correct deployment into a reported
  failure.
- The cheapest way to satisfy it was `touch /opt/so-mcp`. That fabricates the
  evidence the condition was meant to establish, and it does so without
  changing a single deployed byte.

Git committer timestamps are commit metadata, not a trustworthy record of when
something was installed. The directory mtime stays useful as an operational
observation — it is how the deployment window is found in the journal — but it
is not a pass/fail condition, and it must not be manufactured.

Deployment time is established by the deployment log and the change record,
not by comparing a directory mtime with a commit timestamp.
