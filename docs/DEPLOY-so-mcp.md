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

The verifier therefore uses Git blob object IDs as the content identity of
managed files. A blob ID is

    SHA1("blob " + decimal_byte_length + NUL + file_bytes)

which is the same object identity `git hash-object` computes for those bytes.
It lets the verifier compare a deployed file against the blob ID recorded in a
manifest generated from a specific commit, even when the deployment host has
no `.git` directory.

A blob ID identifies content, not a commit: identical content can appear in
many commits, at one path or several. The evidence is the combination of

- the full commit SHA selected as the deployment source,
- the repository-relative path,
- the blob ID that commit records for that path, and
- the verifier's comparison of the deployed file against that blob.

SHA-256 of the raw and LF-normalized bytes is recorded as well. It makes line
ending drift diagnosable and is a second, independent content check. It does
not replace the commit identity carried by the blob ID and the manifest
commit.

A deployment is attributable to a commit only when all of the following hold:

1. The manifest names a full commit SHA, not a branch or tag.
2. The manifest's managed path list is exactly the intended deployment
   selection.
3. Every deployed managed file passes verification against that manifest.
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

The verifier result, the manifest commit, and the deployment directory
timestamp belong together in the deployment record. A commit SHA written in a
manifest is an expected source identity; on its own it is not proof that the
files on disk were installed from that commit.


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
touch -m "$TMP"
mv /opt/so-mcp "$PREV"
mv "$TMP" /opt/so-mcp
```

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
COMMIT=<FULL_40_HEX_SHA>

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

The generator requires the database path to be absolute and to resolve to an
existing, non-empty file. That catches a mistyped path, an unmounted volume, or
a database that does not exist yet at manifest-generation time; the verifier
checks the same path again at deployment-verification time. Asking for required
columns without a database path is an error, not a contract that gets silently
dropped.

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

## 8. Proving the production commit without network access

An offline server cannot independently prove that a commit is the
current GitHub `main` tip. It can prove only that its deployed files
match the commit SHA recorded in its manifest.

The stronger statement "production = <SHA>" is permitted in the
change record only when all three conditions are satisfied:

1. `verify_deploy.py` passes against the production root and manifest.
2. The manifest's full `commit` equals the full SHA obtained from
   `git rev-parse main` in a trusted checkout whose `main` was
   refreshed from GitHub for this deployment.
3. The deployment root directory modification time is later than
   that commit's committer timestamp.

On the trusted connected operator checkout:

```bash
git fetch origin main
MAIN_SHA="$(git rev-parse main)"
MAIN_TIME="$(git show -s --format=%ct "$MAIN_SHA")"
```

On the production host, run the verifier and inspect the manifest
commit and directory timestamp:

```bash
sudo /opt/so-mcp/.venv/bin/python /opt/so-mcp/tools/verify_deploy.py \
  --root /opt/so-mcp \
  --manifest /etc/so-mcp/manifest.json

MANIFEST_SHA="$(python3 -c \
  'import json; print(json.load(open("/etc/so-mcp/manifest.json"))["commit"])')"
DEPLOY_MTIME="$(stat -c %Y /opt/so-mcp)"
```

Compare `MANIFEST_SHA` with the trusted `MAIN_SHA`, and
`DEPLOY_MTIME` with `MAIN_TIME`. All three conditions must be true.

If the server is offline and the operator has not brought the trusted
`MAIN_SHA` and `MAIN_TIME` into the deployment record through an
authenticated channel, the correct status is:

"deployed tree matches manifest commit <SHA>; current GitHub main
equivalence not independently established."

Do not write "production = <SHA>" based on the manifest alone.
