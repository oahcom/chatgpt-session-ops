"""In-process MCP adapter backed by a bounded, durable SQLite job queue.

There is no HTTP sidecar, mTLS client, Gateway service, or external worker.
The upstream CLI module remains the sole ChatGPT protocol implementation;
queued jobs are executed by a bounded in-process executor.
"""
from __future__ import annotations

import importlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionOpsError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(detail or code)


_DEFAULT_ACCOUNT_HEALTH = (
    ("acct4", "auth_unavailable", "upstream HTTP 401; awaiting authorized reauthentication and read-only probe"),
    ("acct5", "operator_disabled", "operator disabled; do not reauthenticate"),
)


class ChatGPTDirectClient:
    def __init__(
        self,
        database_path: str | Path,
        *,
        repository_root: str | Path,
        accounts_dir: str | Path,
        runtime_dir: str | Path,
        legacy_database_path: str | Path | None = None,
        max_workers: int = 1,
        # Capacity includes the active job: one executor slot plus eight
        # durable queued jobs. Keep this aligned with the MCP admission bound.
        max_queue_size: int = 9,
    ):
        self.database_path = Path(database_path)
        self.repository_root = Path(repository_root).resolve()
        self.accounts_dir = Path(accounts_dir).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        self.prompt_dir = self.runtime_dir / "prompts"
        # One in-process queue consumer only. Do not fan out CLI children
        # before durable account reservations and signed receipts exist.
        if (
            type(max_workers) is not int
            or max_workers != 1
            or type(max_queue_size) is not int
            or not 1 <= max_queue_size <= 9
        ):
            raise ValueError("the direct ChatGPT queue requires one consumer and at most nine outstanding jobs")
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        self._db_lock = threading.RLock()
        self._rotation_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chatgpt-session")
        self._prepare_runtime()
        self._initialize_database(legacy_database_path)
        self._cli = self._load_cli()
        self._recover_queued_jobs()

    @classmethod
    def from_env(cls, database_path: str | Path):
        root = Path(os.environ.get("CHATGPT_SESSION_OPS_ROOT", Path(__file__).resolve().parents[2]))
        return cls(
            database_path,
            repository_root=root,
            accounts_dir=os.environ.get("CHATGPT_SESSION_OPS_ACCOUNTS_DIR", "/var/lib/chatgpt-session-ops/accounts"),
            runtime_dir=os.environ.get("CHATGPT_SESSION_OPS_RUNTIME_DIR", "/var/lib/so-mcp/chatgpt-session-ops"),
            # Legacy metadata is migrated once by the root-owned migration
            # command; the running MCP service never reads the old database.
            legacy_database_path=None,
        )

    def start(self) -> None:
        """ASGI lifespan hook; the durable executor is initialized in __init__."""
        with self._lifecycle_lock:
            if self._closed:
                raise SessionOpsError("SERVICE_UNAVAILABLE", "ChatGPT executor is shutting down")

    def stop(self) -> None:
        """Drain the active send and cancel queued futures for restart recovery."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        # Queued jobs remain status='queued' in SQLite and are recovered on
        # the next process start. Only the single active remote operation is
        # allowed to finish before the service exits.
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _connect(self):
        conn = sqlite3.connect(str(self.database_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _prepare_runtime(self):
        if not self.accounts_dir.is_dir() or not os.access(self.accounts_dir, os.R_OK | os.X_OK):
            raise SessionOpsError("DEPENDENCY_UNAVAILABLE", "SO-MCP process cannot enumerate the ChatGPT account vault")
        self.prompt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.runtime_dir / "logs").mkdir(exist_ok=True, mode=0o700)
        accounts_runtime = self.runtime_dir / "accounts"
        accounts_runtime.mkdir(exist_ok=True, mode=0o700)
        # Credentials remain in the account vault. Writable CLI state is kept
        # under the SO-MCP runtime directory, not in the retired Sender tree.
        for source in self.accounts_dir.iterdir():
            if not re.fullmatch(r"acct[1-9][0-9]*", source.name) or source.is_symlink() or not source.is_dir():
                continue
            target = accounts_runtime / source.name
            target.mkdir(exist_ok=True, mode=0o700)
            for filename in ("cookies.json", "access_token.txt"):
                link, credential = target / filename, source / filename
                if not credential.is_file():
                    continue
                if link.is_symlink():
                    if link.resolve() != credential.resolve():
                        raise SessionOpsError("DEPENDENCY_UNAVAILABLE", "runtime credential link target mismatch")
                elif not link.exists():
                    link.symlink_to(credential)
            for dirname in ("replies", "conv-api", "timeouts"):
                (target / dirname).mkdir(exist_ok=True, mode=0o700)

    def _initialize_database(self, legacy_database_path):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db_lock, self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chatgpt_session_owners (
                    conversation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    account TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chatgpt_owners_principal
                    ON chatgpt_session_owners(tenant_id, principal_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS chatgpt_account_health (
                    account TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chatgpt_jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
                    conversation_id TEXT,
                    requested_account TEXT,
                    resolved_account TEXT,
                    prompt_path TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    result_json TEXT,
                    error_json TEXT,
                    outcome_unknown INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(tenant_id, principal_id, operation, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_chatgpt_jobs_owner
                    ON chatgpt_jobs(tenant_id, principal_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS chatgpt_migrations (
                    name TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    owners_migrated INTEGER NOT NULL,
                    health_migrated INTEGER NOT NULL
                );
            """)
            conn.executemany(
                "INSERT OR IGNORE INTO chatgpt_account_health(account,state,reason,updated_at) VALUES(?,?,?,?)",
                [(account, state, reason, _now()) for account, state, reason in _DEFAULT_ACCOUNT_HEALTH],
            )
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chatgpt_jobs)")}
            if "payload_hash" not in job_columns:
                conn.execute("ALTER TABLE chatgpt_jobs ADD COLUMN payload_hash TEXT")
            # Queued jobs are durable and safe to resume. A running operation
            # may already have reached ChatGPT, so its outcome is unknown and
            # it must never be replayed automatically.
            conn.execute("""UPDATE chatgpt_jobs SET status='failed', completed_at=?,
                outcome_unknown=1, error_json=? WHERE status='running'""",
                (_now(), json.dumps({"code": "OUTCOME_UNKNOWN", "retryable": False})))

        if legacy_database_path:
            legacy = Path(legacy_database_path)
            if legacy.is_file():
                try:
                    src = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True, timeout=3)
                    src.row_factory = sqlite3.Row
                    with self._db_lock, self._connect() as dst:
                        marker = dst.execute(
                            "SELECT 1 FROM chatgpt_migrations WHERE name='legacy_sender_gateway_v1'"
                        ).fetchone()
                        if not marker:
                            owner_rows = []
                            health_rows = []
                            if src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_owners'").fetchone():
                                owner_rows = src.execute("SELECT conversation_id,tenant_id,principal_id,account,created_at FROM session_owners").fetchall()
                                dst.executemany("INSERT OR IGNORE INTO chatgpt_session_owners VALUES(?,?,?,?,?)", [tuple(row) for row in owner_rows])
                            if src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_health'").fetchone():
                                health_rows = src.execute("SELECT account,state,reason,updated_at FROM account_health").fetchall()
                                dst.executemany("INSERT OR REPLACE INTO chatgpt_account_health VALUES(?,?,?,?)", [tuple(row) for row in health_rows])
                            dst.execute(
                                "INSERT INTO chatgpt_migrations VALUES(?,?,?,?,?)",
                                ("legacy_sender_gateway_v1", str(legacy), _now(), len(owner_rows), len(health_rows)),
                            )
                    src.close()
                except sqlite3.Error as exc:
                    raise SessionOpsError("DEPENDENCY_UNAVAILABLE", "legacy session metadata migration failed") from exc

    def _load_cli(self):
        sys.path.insert(0, str(self.repository_root))
        cli = importlib.import_module("scripts.chatgpt_send")
        cli.BASE = str(self.runtime_dir)
        cli.ROTATION_FILE = str(self.runtime_dir / "account-rotation.json")
        cli.CONV_MAP_FILE = str(self.runtime_dir / "conv-accounts.json")
        cli.ANON_SESSIONS_FILE = str(self.runtime_dir / "anon-sessions.json")
        cli.PROXY_FILE = str(self.runtime_dir / "proxy.json")
        # Do not replace cli.ChatGPTAPI: scripts.chatgpt_send is imported by
        # other callers and tests in this process. Its native account-aware
        # constructor already resolves runtime_dir/accounts/acctN correctly.
        return cli

    def _account_health(self, account: str) -> tuple[str, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, reason FROM chatgpt_account_health WHERE account=?",
                (account,),
            ).fetchone()
        return (row["state"], row["reason"]) if row else ("active", "")

    def _active_accounts(self):
        accounts = []
        if not self.accounts_dir.is_dir():
            return ()
        for entry in sorted(self.accounts_dir.iterdir()):
            if not re.fullmatch(r"acct[1-9][0-9]*", entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            cookies, token = entry / "cookies.json", entry / "access_token.txt"
            if (cookies.is_symlink() or token.is_symlink() or not cookies.is_file()
                    or not token.is_file() or not os.access(cookies, os.R_OK)
                    or not os.access(token, os.R_OK)):
                continue
            accounts.append(entry.name)
        with self._connect() as conn:
            health = {row["account"]: row["state"] for row in conn.execute("SELECT account,state FROM chatgpt_account_health")}
        return tuple(a for a in accounts if health.get(a, "active") == "active")

    def _validate_create_account(self, requested_account: str) -> tuple[str, ...]:
        """Fail before enqueue when a pinned account is unhealthy or unavailable."""
        active = self._active_accounts()
        if requested_account == "auto":
            if not active:
                raise SessionOpsError("RESOURCE_EXHAUSTED", "no active logged-in accounts")
            return active
        if not re.fullmatch(r"acct[1-9][0-9]*", requested_account):
            raise SessionOpsError("INVALID_ARGUMENT", "invalid account")
        if requested_account not in active:
            state, reason = self._account_health(requested_account)
            if requested_account not in self._available_credential_accounts():
                detail = f"account {requested_account} has no complete credential set"
            else:
                detail = f"account {requested_account} is unavailable ({state})"
                if reason:
                    detail += f": {reason}"
            raise SessionOpsError("RESOURCE_EXHAUSTED", detail)
        return active

    def _available_credential_accounts(self):
        if not self.accounts_dir.is_dir():
            return ()
        return tuple(
            entry.name for entry in sorted(self.accounts_dir.iterdir())
            if re.fullmatch(r"acct[1-9][0-9]*", entry.name)
            and not entry.is_symlink() and entry.is_dir()
            and not (entry / "cookies.json").is_symlink()
            and not (entry / "access_token.txt").is_symlink()
            and (entry / "cookies.json").is_file()
            and (entry / "access_token.txt").is_file()
        )

    def _write_prompt(self, prompt: str):
        if not isinstance(prompt, str) or not prompt or len(prompt) > 32768:
            raise SessionOpsError("INVALID_ARGUMENT", "prompt must contain 1..32768 characters")
        path = self.prompt_dir / (uuid.uuid4().hex + ".txt")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        return str(path)

    def _recover_queued_jobs(self):
        """Resubmit durable jobs accepted before the previous process stopped."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM chatgpt_jobs WHERE status='queued' ORDER BY created_at, job_id"
            ).fetchall()
        for row in rows:
            self._executor.submit(self._execute_job, row["job_id"])

    def enqueue(self, *, principal_id, tenant_id, operation, arguments, idempotency_key):
        if getattr(self, "_closed", False):
            raise SessionOpsError("SERVICE_UNAVAILABLE", "ChatGPT executor is shutting down")
        if operation not in ("session.create", "session.send"):
            raise SessionOpsError("INVALID_ARGUMENT", "unsupported ChatGPT operation")
        args = dict(arguments or {})
        requested_account = args.get("account", "auto")
        effective_args = dict(args)
        effective_args.setdefault("account", "auto")
        if operation == "session.create":
            effective_args.setdefault("prompt", "ping")
        payload_hash = hashlib.sha256(json.dumps(
            {"operation": operation, "arguments": effective_args},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        # Idempotent retries return the original job even if account health has
        # changed since it was accepted. New creates are preflighted before a
        # prompt file or queued job is created.
        with self._db_lock, self._connect() as conn:
            existing = conn.execute("""SELECT * FROM chatgpt_jobs WHERE tenant_id=? AND principal_id=? AND idempotency_key=?""", (tenant_id, principal_id, idempotency_key)).fetchone()
            if existing:
                if existing["operation"] != operation or existing["payload_hash"] != payload_hash:
                    raise SessionOpsError("CONFLICT", "idempotency key was already used for a different or unverifiable request")
                return self._job_payload(existing)
        if operation == "session.create":
            self._validate_create_account(requested_account)
        else:
            conversation_id = args.get("conversation_id")
            if not conversation_id:
                raise SessionOpsError("INVALID_ARGUMENT", "conversation_id is required")
            owner = self._owner(conversation_id, tenant_id, principal_id)
            if requested_account != "auto" and requested_account != owner["account"]:
                raise SessionOpsError("CONFLICT", "requested account does not match the recorded session owner")
            if owner["account"] not in self._active_accounts():
                raise SessionOpsError("RESOURCE_EXHAUSTED", "recorded session account is currently unavailable")
        prompt = args.get("prompt", "ping" if operation == "session.create" else None)
        prompt_path = self._write_prompt(prompt) if prompt is not None else None
        created = _now()
        job_id = "cgj_" + str(uuid.uuid4())
        with self._db_lock, self._connect() as conn:
            # Serialize capacity check + insert across concurrent MCP requests.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("""SELECT * FROM chatgpt_jobs WHERE tenant_id=? AND principal_id=? AND idempotency_key=?""", (tenant_id, principal_id, idempotency_key)).fetchone()
            if existing:
                if prompt_path:
                    Path(prompt_path).unlink(missing_ok=True)
                if existing["operation"] != operation or existing["payload_hash"] != payload_hash:
                    raise SessionOpsError("CONFLICT", "idempotency key was already used for a different or unverifiable request")
                return self._job_payload(existing)
            outstanding = conn.execute(
                "SELECT count(*) FROM chatgpt_jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if outstanding >= self.max_queue_size:
                if prompt_path:
                    Path(prompt_path).unlink(missing_ok=True)
                raise SessionOpsError(
                    "RESOURCE_EXHAUSTED",
                    f"ChatGPT job queue is full ({outstanding}/{self.max_queue_size}); retry after capacity is available",
                )
            conn.execute("""INSERT INTO chatgpt_jobs(job_id,tenant_id,principal_id,operation,idempotency_key,status,conversation_id,requested_account,prompt_path,created_at,payload_hash)
                VALUES(?,?,?,?,?,'queued',?,?,?,?,?)""", (job_id, tenant_id, principal_id, operation, idempotency_key, args.get("conversation_id"), requested_account, prompt_path, created, payload_hash))
        try:
            self._executor.submit(self._execute_job, job_id)
        except RuntimeError:
            # Shutdown raced with admission after the durable insert. Leave the
            # job queued for restart recovery rather than dropping or replaying it.
            pass
        return {"job_id": job_id, "operation": operation, "status": "queued", "created_at": created, "conversation_id": args.get("conversation_id")}

    def _execute_job(self, job_id):
        with self._db_lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM chatgpt_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["status"] != "queued":
                return
            conn.execute("UPDATE chatgpt_jobs SET status='running',started_at=? WHERE job_id=?", (_now(), job_id))
        try:
            result = self._run_operation(row)
            with self._db_lock, self._connect() as conn:
                conn.execute("UPDATE chatgpt_jobs SET status='succeeded',conversation_id=COALESCE(?,conversation_id),resolved_account=?,result_json=?,completed_at=? WHERE job_id=?", (result.get("conversation_id"), result.get("account"), json.dumps(result), _now(), job_id))
        except SessionOpsError as exc:
            with self._db_lock, self._connect() as conn:
                conn.execute("UPDATE chatgpt_jobs SET status='failed',error_json=?,outcome_unknown=?,completed_at=? WHERE job_id=?", (json.dumps({"code": exc.code, "retryable": False}), int(exc.code == "OUTCOME_UNKNOWN"), _now(), job_id))
        except Exception:
            with self._db_lock, self._connect() as conn:
                conn.execute("UPDATE chatgpt_jobs SET status='failed',error_json=?,outcome_unknown=1,completed_at=? WHERE job_id=?", (json.dumps({"code": "OUTCOME_UNKNOWN", "retryable": False}), _now(), job_id))
        finally:
            with self._connect() as conn:
                row = conn.execute("SELECT prompt_path FROM chatgpt_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row and row["prompt_path"]:
                Path(row["prompt_path"]).unlink(missing_ok=True)
            with self._connect() as conn:
                conn.execute("UPDATE chatgpt_jobs SET prompt_path=NULL WHERE job_id=?", (job_id,))

    def _run_operation(self, row):
        tenant_id, principal_id = row["tenant_id"], row["principal_id"]
        requested = row["requested_account"] or "auto"
        prompt_path = row["prompt_path"]
        if row["operation"] == "session.create":
            # Re-check at execution: health can change after enqueue preflight.
            active = self._validate_create_account(requested)
            with self._rotation_lock:
                self._cli.ROTATION_PRIMARY = active
                previous_anon_fallback = os.environ.get("CHATGPT_ANON_FALLBACK")
                previous_allowed_accounts = os.environ.get("CHATGPT_ALLOWED_ACCOUNTS")
                try:
                    os.environ["CHATGPT_ANON_FALLBACK"] = "0"
                    os.environ.pop("CHATGPT_ALLOWED_ACCOUNTS", None)
                    if requested != "auto":
                        api = self._cli.ChatGPTAPI(account=requested)
                        status, path, conv = self._cli.send_prompt(api, None, prompt_path, new_chat=True)
                        conversation_id = str(getattr(conv, "id", None) or conv or "")
                        if status != 200 or not re.fullmatch(r"[0-9a-f-]{36}", conversation_id) or getattr(conv, "send_truncated", False):
                            raise SessionOpsError("OUTCOME_UNKNOWN", "session create may have reached ChatGPT but was not fully verified; do not retry")
                        self._cli.record_conv(conversation_id, api.account, prompt_path)
                        account = api.account
                    else:
                        previous = (self._cli.load_rotation().get("last") or {}).get("conv_id")
                        exit_code = self._cli.create_chat_rotating(prompt_path)
                        state = self._cli.load_rotation()
                        last = state.get("last") or {}
                        conversation_id = last.get("conv_id")
                        account = last.get("account")
                        if exit_code != 0 or not conversation_id or conversation_id == previous or account not in active:
                            raise SessionOpsError("OUTCOME_UNKNOWN", "session create may have reached ChatGPT but did not produce a uniquely verified result; do not retry")
                finally:
                    if previous_anon_fallback is None:
                        os.environ.pop("CHATGPT_ANON_FALLBACK", None)
                    else:
                        os.environ["CHATGPT_ANON_FALLBACK"] = previous_anon_fallback
                    if previous_allowed_accounts is None:
                        os.environ.pop("CHATGPT_ALLOWED_ACCOUNTS", None)
                    else:
                        os.environ["CHATGPT_ALLOWED_ACCOUNTS"] = previous_allowed_accounts
            self._store_owner(conversation_id, tenant_id, principal_id, account)
            return {"conversation_id": conversation_id, "account": account, "reply_state": "unverified", "receipt_durable": False}

        conversation_id = row["conversation_id"]
        owner = self._owner(conversation_id, tenant_id, principal_id)
        account = owner["account"]
        if account not in self._active_accounts():
            raise SessionOpsError("RESOURCE_EXHAUSTED", "recorded session account is currently unavailable")
        if requested != "auto" and requested != account:
            raise SessionOpsError("CONFLICT", "requested account does not match the recorded session owner")
        api = self._cli.ChatGPTAPI(account=account)
        status, path, conv = self._cli.send_prompt(api, conversation_id, prompt_path)
        if status != 200 or getattr(conv, "send_truncated", False):
            raise SessionOpsError("OUTCOME_UNKNOWN", "send was not confirmed complete; do not retry automatically")
        reply = getattr(api, "_last_reply_text", None)
        import hashlib
        return {
            "conversation_id": conversation_id,
            "account": account,
            "response_chars": len(reply) if isinstance(reply, str) else None,
            "reply_sha256": hashlib.sha256(reply.encode("utf-8")).hexdigest() if isinstance(reply, str) else None,
            "reply_state": "captured" if isinstance(reply, str) else "unavailable",
            "reply_source": "upstream_cli" if isinstance(reply, str) else None,
            "receipt_durable": bool(path),
        }

    def _store_owner(self, conversation_id, tenant_id, principal_id, account):
        with self._db_lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO chatgpt_session_owners VALUES(?,?,?,?,?)", (conversation_id, tenant_id, principal_id, account, _now()))

    def _owner(self, conversation_id, tenant_id, principal_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chatgpt_session_owners WHERE conversation_id=?", (conversation_id,)).fetchone()
        if not row or row["tenant_id"] != tenant_id or row["principal_id"] != principal_id:
            raise SessionOpsError("NOT_FOUND", "conversation not found")
        return dict(row)

    def get_job(self, *, principal_id, tenant_id, job_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chatgpt_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row or row["tenant_id"] != tenant_id or row["principal_id"] != principal_id:
            raise SessionOpsError("NOT_FOUND", "job not found")
        return self._job_payload(row)

    @staticmethod
    def _job_payload(row):
        result = dict(row)
        return {
            "job_id": result["job_id"], "operation": result["operation"], "status": result["status"],
            "conversation_id": result["conversation_id"], "created_at": result["created_at"],
            "started_at": result["started_at"], "completed_at": result["completed_at"],
            "result": json.loads(result["result_json"]) if result["result_json"] else None,
            "error": json.loads(result["error_json"]) if result["error_json"] else None,
            "outcome_unknown": bool(result["outcome_unknown"]),
        }

    def list_sessions(self, *, principal_id, tenant_id, page_size=25, cursor=None):
        offset = int(cursor or 0)
        if offset < 0 or offset > 1000000:
            raise SessionOpsError("INVALID_ARGUMENT", "invalid cursor")
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM chatgpt_session_owners WHERE tenant_id=? AND principal_id=? ORDER BY created_at DESC,conversation_id LIMIT ? OFFSET ?", (tenant_id, principal_id, page_size + 1, offset)).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        return {"items": [{"conversation_id": r["conversation_id"], "account": r["account"], "created_at": r["created_at"], "last_message_at": None, "message_count": None} for r in rows], "next_cursor": str(offset + page_size) if has_more else None}

    def read_session(self, *, principal_id, tenant_id, conversation_id, after_message_id=None, limit=20):
        owner = self._owner(conversation_id, tenant_id, principal_id)
        if owner["account"] not in self._active_accounts():
            raise SessionOpsError("RESOURCE_EXHAUSTED", "recorded session account is currently unavailable")
        api = self._cli.ChatGPTAPI(account=owner["account"])
        tail, raw = api.live_tail(conversation_id, tries=2)
        if not raw:
            raise SessionOpsError("DEPENDENCY_UNAVAILABLE", "conversation read returned no mapping")
        rows, complete = self._cli._scan_mapping(raw)
        messages = []
        found_anchor = after_message_id is None
        for message_id, role, content in rows:
            if after_message_id is not None and not found_anchor:
                if message_id == after_message_id:
                    found_anchor = True
                continue
            if after_message_id is not None and message_id == after_message_id:
                continue
            if role not in ("user", "assistant", "system", "tool") or not message_id:
                continue
            messages.append({"message_id": str(message_id), "role": role, "content": content, "created_at": None})
        if after_message_id is not None and not found_anchor:
            raise SessionOpsError("INVALID_ARGUMENT", "after_message_id was not found in the conversation")
        page = messages[-limit:] if after_message_id is None else messages[:limit]
        next_after = page[-1]["message_id"] if after_message_id is not None and len(messages) > limit and page else None
        return {"conversation_id": conversation_id, "messages": page, "complete": bool(complete), "next_after_message_id": next_after}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
