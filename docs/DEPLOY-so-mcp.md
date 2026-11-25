# Deploy Session Ops MCP

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
  scripts src tools/verify_deploy.py requirements-mcp.txt \
  | tar -xf - -C "$TMP"
```

The pipeline must run with `set -o pipefail` if placed in a deployment
script. Check the extracted tree before switching production:

```bash
test -f "$TMP/scripts/run_mcp_server.py"
test -f "$TMP/src/session_ops/authority.py"
test -f "$TMP/src/session_ops/mcp_server.py"
test ! -e "$TMP/.git"
test ! -e "$TMP/tests"
test ! -e "$TMP/deploy"
test ! -e "$TMP/docs"
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

Before switching, ensure the candidate has the correct ownership and
permissions and has passed the verifier. Do not copy `.venv` from an
older deployment into the new tree. Create or populate the venv from
the deployment's pinned dependency process.

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
RETIRE=/var/backups/so-mcp/retired
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

sudo install -d -o root -g root -m 0700 "$RETIRE"

for ROOT in /opt/so-mcp.stage /opt/so-mcp.ab; do
  [ -d "$ROOT" ] || continue

  NAME="$(basename "$ROOT")"
  DEST="$RETIRE/$NAME-$STAMP"
  sudo install -d -o root -g root -m 0700 "$DEST"

  while IFS= read -r -d '' DB; do
    REL="${DB#"$ROOT"/}"
    OUT="$DEST/$REL"

    sudo install -d -o root -g root -m 0700 "$(dirname "$OUT")"
    sudo sqlite3 "$DB" ".backup '$OUT'"

    CHECK="$(sudo sqlite3 "$OUT" 'PRAGMA integrity_check;')"
    test "$CHECK" = ok || {
      echo "SQLite backup integrity failure: $DB" >&2
      exit 1
    }
  done < <(sudo find "$ROOT" -type f -name '*.sqlite3' -print0)
done
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
