# Direct ChatGPT MCP

The ChatGPT session tools run inside the Session Ops MCP process. They invoke
the repository's local `scripts/chatgpt_send.py` CLI directly; there is no
Sender Gateway HTTP hop, job queue, sender worker, or separate sender service
in the target architecture.

## Tools

- `chatgpt.session.create`: create a session synchronously and persist its
  tenant/principal/account owner.
- `chatgpt.session.send`: send synchronously to an existing owned session.
- `chatgpt.session.read`: read messages from an existing owned session.
- `chatgpt.session.list`: list only sessions owned by the authenticated
  tenant/principal.

The existing OAuth boundary applies. Read tools require
`session_ops.chatgpt.session.read`; create/send require
`session_ops.chatgpt.session.write`.

The owner and idempotency records live in the existing MCP SQLite database.
They are not a job queue. A call executes in the MCP request's thread and
returns its completed result. An interrupted write with an uncertain outcome
is marked unknown; the same idempotency key cannot silently send it again.

## Runtime configuration

The systemd service uses these values:

- `SO_SENDER_ACCOUNTS_DIR=/var/lib/chatgpt-session-ops/accounts`
- `CHATGPT_STATE_DIR=/var/lib/so-mcp/chatgpt-cli`
- `CHATGPT_PROXY_FILE=/var/lib/so-mcp/chatgpt-cli/proxy.json`

The MCP process runs as `ubuntu`. Account credentials remain owned by
`chatgpt-sender`; grant the MCP process read-only ACLs on account directories
and credential files. Grant write access only to per-account `replies/`
directories, because the CLI archives completed replies there. Do not make the
credential files writable by the MCP process.

The MCP service's `ReadWritePaths` includes the account root so the CLI can
write replies, but Unix ACLs remain the effective per-file access boundary.

## One-time migration from the old Sender Gateway

Do this before disabling the old sender service.

1. Confirm the old sender has no running send operation. Do not interrupt an
   in-flight send. Keep the old SQLite database as a rollback/archive copy.
2. Create the MCP CLI state directory and copy the existing CLI state and proxy
   configuration. Preserve the source files; do not move them:

   ```sh
   sudo install -d -o ubuntu -g ubuntu -m 0700 /var/lib/so-mcp/chatgpt-cli
   for f in account-rotation.json conv-accounts.json conv-accounts.json.highwater.json anon-sessions.json; do
     if sudo test -f "/opt/so-sender/cli/$f"; then
       sudo install -o ubuntu -g ubuntu -m 0600 "/opt/so-sender/cli/$f" "/var/lib/so-mcp/chatgpt-cli/$f"
     fi
   done
   sudo install -o ubuntu -g ubuntu -m 0600 /opt/so-sender/cli/proxy.json /var/lib/so-mcp/chatgpt-cli/proxy.json
   ```

3. Grant read-only account access and write access to reply archives:

   ```sh
   sudo setfacl -m u:ubuntu:--x /var/lib/chatgpt-session-ops
   sudo setfacl -m u:ubuntu:r-x /var/lib/chatgpt-session-ops/accounts
   sudo find /var/lib/chatgpt-session-ops/accounts -type d -exec setfacl -m u:ubuntu:r-x {} +
   sudo find /var/lib/chatgpt-session-ops/accounts -type f -exec setfacl -m u:ubuntu:r-- {} +
   sudo find /var/lib/chatgpt-session-ops/accounts -type d -name replies -exec setfacl -m u:ubuntu:rwx,d:u:ubuntu:rwx {} +
   ```

   Apply the same ACL policy when provisioning a new account. Never grant
   write access to `cookies.json` or `access_token.txt`.

4. Stop only the MCP service while importing ownership. First run the dry run:

   ```sh
   sudo systemctl stop so-mcp
   cd /opt/so-mcp
   .venv/bin/python -m scripts.migrate_sender_owners \
     --source /var/lib/chatgpt-session-ops/state/sender-gateway.sqlite3 \
     --target /var/lib/so-mcp/work-items.sqlite3
   ```

   Review the counts and any conflict. Then apply:

   ```sh
   .venv/bin/python -m scripts.migrate_sender_owners \
     --source /var/lib/chatgpt-session-ops/state/sender-gateway.sqlite3 \
     --target /var/lib/so-mcp/work-items.sqlite3 --apply
   ```

   The migration is idempotent, copies only `session_owners`, verifies every
   imported row, and never changes the source database. It deliberately does
   not recreate Gateway jobs, worker leases, or the unused event outbox.

5. Deploy the repository files listed in `deploy/paths.txt`, install the
   updated `deploy/so-mcp.service`, reload systemd, and start MCP. Verify the
   MCP health endpoint and all four tools before changing the sender service.
6. After a successful live send/read/list/create verification and after the
   old sender has no running operation, stop and disable `so-sender`. Keep
   `/var/lib/chatgpt-session-ops/state/sender-gateway.sqlite3` and
   `/opt/so-sender` archived until the migration has passed its rollback
   window. Do not delete the old database or credentials during cutover.

## Rollback

Stop the updated MCP service, restore the previous systemd unit and
`/etc/so-mcp/so-mcp.env`, then restart the previous MCP and sender services.
The source Gateway database and CLI state were not modified by the ownership
migration, so the old sender can be restarted without reversing the import.
