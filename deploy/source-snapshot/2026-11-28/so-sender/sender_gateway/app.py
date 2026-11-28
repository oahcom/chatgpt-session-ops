"""HTTPS entry point for the ChatGPT sender sidecar.

Identity contract:
  X-SO-MCP-Identity: base64url(canonical-json).base64url(HMAC-SHA256)
The signed JSON claims are iss, aud, sub, tenant, iat, exp and jti.
The HMAC key is /etc/so-sender/mcp-identity.key (32 raw bytes).
mTLS is independently mandatory; the client certificate must chain to
SO_SENDER_TLS_CLIENT_CA. Authorization also requires the bearer token
stored at SO_SENDER_SERVICE_TOKEN_FILE.

HTTP contract:
  POST /internal/v1/jobs
  POST /internal/v1/events/claim
  POST /internal/v1/events/{event_id}/ack
  GET  /internal/v1/jobs/{job_id}
  GET  /internal/v1/sessions?page_size=N&cursor=C
  GET  /internal/v1/sessions/{conversation_id}/messages?limit=N&after_message_id=ID
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .account_router import AccountRouter, discover_accounts
from .job_store import JobStore
from .models import ErrorCode, GatewayError, JobStatus, utc_now_iso
from .sender_adapter import ChatGPTSenderAdapter, SenderError
from .worker import Worker


IDENTITY_HEADER = "X-SO-MCP-Identity"
IDENTITY_KEY_PATH = "/etc/so-sender/mcp-identity.key"
MAX_BODY_BYTES = 1024 * 1024
CONVERSATION_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
JOB_RE = re.compile(r"cgj_[0-9a-f-]{36}")
EVENT_DISPATCHER_PRINCIPAL = "session_ops:event-dispatcher"
EVENT_DISPATCHER_TENANT = "session_ops"

_LOG = logging.getLogger("sender_gateway.app")


SYSTEMD_STOP_TIMEOUT_SECONDS = 600.0
WORKER_DRAIN_MARGIN_SECONDS = 15.0
HTTP_HANDLER_CLEANUP_SECONDS = 5.0
HTTP_HANDLER_DRAIN_SECONDS = 15.0  # default read timeout (10s) + cleanup


def _worker_drain_timeout(
    send_timeout: float, lease_seconds: float, read_timeout: float = 10.0
) -> float:
    drain = float(send_timeout) + WORKER_DRAIN_MARGIN_SECONDS
    handler_drain = float(read_timeout) + HTTP_HANDLER_CLEANUP_SECONDS
    required = drain + handler_drain
    if required >= SYSTEMD_STOP_TIMEOUT_SECONDS:
        raise RuntimeError("Sender execution/read budgets exceed the systemd stop timeout")
    if float(lease_seconds) >= SYSTEMD_STOP_TIMEOUT_SECONDS:
        raise RuntimeError("Sender worker lease must be below systemd stop timeout")
    return drain


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _integer(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _number(name: str, default: float, minimum: float = 0.1) -> float:
    raw = os.environ.get(name)
    value = default if raw is None or not raw.strip() else float(raw)
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _read_secret(path: str, *, expected_bytes: int | None = None) -> bytes:
    target = Path(path)
    data = target.read_bytes()
    if expected_bytes is not None and len(data) != expected_bytes:
        raise RuntimeError(f"{path} must contain exactly {expected_bytes} raw bytes")
    if os.name == "posix" and target.stat().st_mode & 0o077:
        raise RuntimeError(f"{path} must not be accessible by group or others")
    return data


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class IdentityVerifier:
    """Verifies short-lived MCP identity assertions and consumes each jti once."""

    def __init__(self, database: str, key: bytes, service_token: bytes) -> None:
        self._database = database
        self._key = key
        self._service_token = service_token.strip()
        self._local = threading.local()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consumed_identity_jti (
                    jti TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._database, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def verify(self, handler: BaseHTTPRequestHandler) -> tuple[str, str]:
        auth = handler.headers.get("Authorization", "")
        scheme, _, supplied_token = auth.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not supplied_token
            or not hmac.compare_digest(
                supplied_token.encode("utf-8"), self._service_token
            )
        ):
            raise PermissionError("invalid service bearer token")

        assertion = handler.headers.get(IDENTITY_HEADER, "")
        try:
            encoded, signature = assertion.split(".", 1)
            raw = _b64decode(encoded)
            supplied_mac = _b64decode(signature)
            expected_mac = hmac.new(self._key, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied_mac, expected_mac):
                raise ValueError("bad signature")
            claims = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise PermissionError("invalid identity assertion") from exc

        if not isinstance(claims, dict):
            raise PermissionError("invalid identity claims")
        now = int(time.time())
        required = ("iss", "aud", "sub", "tenant", "iat", "exp", "jti")
        if any(name not in claims for name in required):
            raise PermissionError("identity claims are incomplete")
        if claims["iss"] != "so-mcp" or claims["aud"] != "so-sender":
            raise PermissionError("identity issuer or audience is invalid")
        if (
            not isinstance(claims["sub"], str)
            or not claims["sub"]
            or not isinstance(claims["tenant"], str)
            or not claims["tenant"]
            or not isinstance(claims["jti"], str)
            or not claims["jti"]
        ):
            raise PermissionError("identity subject, tenant or jti is invalid")
        if (
            isinstance(claims["iat"], bool)
            or not isinstance(claims["iat"], int)
            or isinstance(claims["exp"], bool)
            or not isinstance(claims["exp"], int)
            or claims["exp"] <= now
            or claims["iat"] > now + 5
            or claims["exp"] - claims["iat"] > 30
            or claims["iat"] < now - 35
        ):
            raise PermissionError("identity assertion is expired or out of window")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM consumed_identity_jti WHERE expires_at < ?", (now,)
            )
            try:
                conn.execute(
                    "INSERT INTO consumed_identity_jti(jti, expires_at) VALUES(?, ?)",
                    (claims["jti"], claims["exp"]),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise PermissionError("identity assertion replayed") from exc
            conn.commit()
        finally:
            conn.close()
        return claims["sub"], claims["tenant"]


def _safe_id(value: object) -> str:
    """只放行受约束格式的 job/conversation id，其余记占位符。"""

    if isinstance(value, str) and (
        JOB_RE.fullmatch(value) or CONVERSATION_RE.fullmatch(value)
    ):
        return value
    return "-"


def _adapter_call(adapter: ChatGPTSenderAdapter, method: str, *, router=None, **kwargs):
    try:
        return getattr(adapter, method)(**kwargs)
    except SenderError as exc:
        mapped = True
        try:
            code = ErrorCode(exc.code)
        except ValueError:
            code = ErrorCode.INTERNAL
            mapped = False
        # 边界层唯一的 SenderError 日志（已映射与未映射都留痕）：
        # 只有错误类别、上游状态码、操作类型与非敏感关联 id；
        # 不含 Authorization/Bearer/HMAC/identity assertion/请求头/凭据。
        _LOG.warning(
            "sender_adapter_error code=%s mapped=%s upstream_status=%s "
            "operation=%s job_id=%s conversation_id=%s meaning=%s detail=%s",
            exc.code,
            str(mapped).lower(),
            exc.upstream_status if exc.upstream_status is not None else "-",
            method,
            _safe_id(kwargs.get("job_id")),
            _safe_id(kwargs.get("conversation_id")),
            (
                "upstream_account_credential_rejected;caller_mtls_identity=unaffected"
                if code is ErrorCode.UNAUTHENTICATED
                else "-"
            ),
            exc.detail,
        )
        retry_after_seconds = exc.retry_after_seconds
        if router is not None and kwargs.get("account"):
            try:
                if exc.upstream_status == 401:
                    router.mark_auth_unavailable(
                        kwargs["account"], reason=f"upstream HTTP 401 during {method}"
                    )
                elif exc.upstream_status == 429:
                    retry_after_seconds = router.mark_rate_limited(
                        kwargs["account"], cooldown_seconds=retry_after_seconds,
                        reason=f"upstream HTTP 429 during {method}",
                    )
            except Exception:
                _LOG.exception("failed to persist upstream account health/cooldown")
        raise GatewayError(
            exc.detail, code=code, upstream_status=exc.upstream_status,
            retry_after_seconds=retry_after_seconds,
        ) from exc


class SenderApplication:
    def __init__(self) -> None:
        bind_host = _required("SO_SENDER_BIND_HOST")
        if bind_host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError("SO_SENDER_BIND_HOST must be loopback-only")
        self.bind_host = bind_host
        self.bind_port = _integer("SO_SENDER_BIND_PORT", 8792)

        state_dir = Path(_required("SO_SENDER_STATE_DIR"))
        accounts_dir = Path(_required("SO_SENDER_ACCOUNTS_DIR"))
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not accounts_dir.is_dir():
            raise RuntimeError("SO_SENDER_ACCOUNTS_DIR must be an existing directory")
        self.database = str(state_dir / "sender-gateway.sqlite3")

        cli_path = _required("SO_SENDER_CLI_PATH")
        cli_python = _required("SO_SENDER_CLI_PYTHON")
        if not os.path.isabs(cli_python) or not os.path.isfile(cli_python):
            raise RuntimeError("SO_SENDER_CLI_PYTHON must be an existing absolute path")

        # The CLI itself permits 420s for a normal send. Leave bounded
        # process/cleanup margin rather than killing it at 150s.
        send_timeout = _number("SO_SENDER_SEND_TIMEOUT_SECONDS", 450.0)
        job_timeout = _number("SO_SENDER_JOB_TIMEOUT_SECONDS", 470.0)
        lease_seconds = _number("SO_SENDER_WORKER_LEASE_SECONDS", 500.0)
        read_timeout = _number("SO_SENDER_READ_TIMEOUT_SECONDS", 10.0)
        if send_timeout >= job_timeout:
            raise RuntimeError(
                "SO_SENDER_SEND_TIMEOUT_SECONDS must be less than "
                "SO_SENDER_JOB_TIMEOUT_SECONDS"
            )
        if lease_seconds <= job_timeout:
            raise RuntimeError(
                "SO_SENDER_WORKER_LEASE_SECONDS must exceed "
                "SO_SENDER_JOB_TIMEOUT_SECONDS"
            )
        self._drain_timeout = _worker_drain_timeout(
            send_timeout, lease_seconds, read_timeout
        )

        self.store = JobStore(self.database, lease_seconds=lease_seconds)
        # Account identities come from the credential directory, not a code whitelist.
        # New acctN directories are picked up on the next service start.
        account_names = discover_accounts(accounts_dir)
        if not account_names:
            raise RuntimeError("no account directories with cookies.json and access_token.txt")
        self.router = AccountRouter(self.store, accounts=tuple(account_names))
        # read/list 独立槽位：长 send 占满 send 槽时读取不被饿死（Owner 裁决）。
        # send/create 的容量与语义仍由 SO_SENDER_MAX_CONCURRENCY 决定，不变。
        read_concurrency = _integer("SO_SENDER_READ_CONCURRENCY", 2)
        self.adapter = ChatGPTSenderAdapter(
            cli_path,
            timeout=send_timeout,
            read_timeout=read_timeout,
            max_concurrency=_integer("SO_SENDER_MAX_CONCURRENCY", 1),
            read_concurrency=read_concurrency,
            read_semaphore=threading.BoundedSemaphore(read_concurrency),
            # Ownership is read from SQLite (session_owners) only; the JSON
            # registry is history and never a runtime input.
            owner_lookup=self.router.get_owner,
            allowed_accounts=self.router.active_accounts,
            prompt_dir=str(state_dir / "prompts"),
        )
        # The adapter's public constructor uses sys.executable. The deployment
        # contract pins a separate CLI interpreter, so set its argv prefix here.
        self.adapter._argv_prefix = (cli_python, os.path.abspath(cli_path))

        self.verifier = IdentityVerifier(
            self.database,
            _read_secret(IDENTITY_KEY_PATH, expected_bytes=32),
            _read_secret(_required("SO_SENDER_SERVICE_TOKEN_FILE")),
        )

        self.worker = Worker(
            self.store,
            self.router,
            send=lambda **kw: _adapter_call(self.adapter, "send", router=self.router, **kw),
            create_session=lambda **kw: _adapter_call(
                self.adapter, "create_session", router=self.router, **kw
            ),
            lease_seconds=lease_seconds,
        )
        self._stop = threading.Event()
        # Worker idle poll interval (seconds). Lower = lower claim latency.
        self._poll_seconds = _number(
            "SO_SENDER_WORKER_POLL_SECONDS", 0.05, minimum=0.01
        )
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="sender-worker", daemon=False
        )
        self._retention_days = _integer("SO_SENDER_JOB_RETENTION_DAYS", 14)
        self._log_dir = os.environ.get("SO_SENDER_LOG_DIR", "").strip()
        if self._log_dir:
            Path(self._log_dir).mkdir(parents=True, exist_ok=True, mode=0o700)

    def _worker_loop(self) -> None:
        last_reconcile = 0.0
        last_retention = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if now - last_reconcile >= 5:
                    # No safe remote probe is exposed by this CLI adapter.
                    # Expired sends therefore become OUTCOME_UNKNOWN, never resend.
                    self.worker.reconcile_expired_leases()
                    last_reconcile = now
                if now - last_retention >= 3600:
                    cutoff = (
                        datetime.now(timezone.utc)
                        - timedelta(days=self._retention_days)
                    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    self.store.prune_completed(cutoff)
                    last_retention = now
                if not self.worker.run_once():
                    self._stop.wait(self._poll_seconds)
            except Exception:
                logging.exception("sender worker loop failed")
                self._stop.wait(1)

    def start_worker(self) -> None:
        self._worker_thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=self._drain_timeout)
        if self._worker_thread.is_alive():
            # Never close the adapter/SQLite pool underneath a live send. The
            # non-daemon worker keeps the process alive until systemd's longer
            # cgroup stop deadline is reached; expired leases remain fail-closed.
            raise RuntimeError(
                "Sender worker did not drain within %.1fs" % self._drain_timeout
            )
        self.adapter.close()
        self.store.close()


def _status_for(code: str) -> int:
    return {
        "INVALID_ARGUMENT": 400,
        "UNAUTHENTICATED": 401,
        "PERMISSION_DENIED": 403,
        "NOT_FOUND": 404,
        "QUARANTINED_SESSION": 404,
        "CONFLICT": 409,
        "RESOURCE_EXHAUSTED": 429,
        "DEPENDENCY_UNAVAILABLE": 503,
        "TIMEOUT": 504,
    }.get(code, 500)


def make_handler(app: SenderApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "so-sender/1"

        def log_message(self, fmt, *args):
            logging.info("%s - %s", self.address_string(), fmt % args)

        def _json(self, status: int, payload: dict, headers: dict | None = None) -> None:
            raw = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()

            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                # The caller gave up on a slow upstream; not a server fault.
                return

        def _identity(self) -> tuple[str, str]:
            try:
                return app.verifier.verify(self)
            except PermissionError as exc:
                raise GatewayError(
                    str(exc), code=ErrorCode.UNAUTHENTICATED
                ) from exc

        def _body(self) -> dict:
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit():
                raise GatewayError(
                    "Content-Length is required", code=ErrorCode.INVALID_ARGUMENT
                )
            length = int(raw_length)
            if length < 1 or length > MAX_BODY_BYTES:
                raise GatewayError(
                    "request body size is invalid", code=ErrorCode.INVALID_ARGUMENT
                )
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise GatewayError(
                    "request body must be valid JSON",
                    code=ErrorCode.INVALID_ARGUMENT,
                ) from exc
            if not isinstance(value, dict):
                raise GatewayError(
                    "request body must be a JSON object",
                    code=ErrorCode.INVALID_ARGUMENT,
                )
            return value

        def _dispatch(self) -> None:
            try:
                principal_id, tenant_id = self._identity()
                parsed = urlsplit(self.path)
                path = parsed.path

                if self.command == "POST" and path == "/internal/v1/events/claim":
                    if (principal_id, tenant_id) != (
                        EVENT_DISPATCHER_PRINCIPAL,
                        EVENT_DISPATCHER_TENANT,
                    ):
                        raise GatewayError(
                            "event dispatcher identity required",
                            code=ErrorCode.PERMISSION_DENIED,
                        )
                    body = self._body()
                    items = app.store.claim_events(
                        body.get("worker_id"), limit=body.get("limit", 25)
                    )
                    self._json(200, {"items": items})
                    return

                match = re.fullmatch(r"/internal/v1/events/(evt_[0-9a-f]{32})/ack", path)
                if self.command == "POST" and match:
                    if (principal_id, tenant_id) != (
                        EVENT_DISPATCHER_PRINCIPAL,
                        EVENT_DISPATCHER_TENANT,
                    ):
                        raise GatewayError(
                            "event dispatcher identity required",
                            code=ErrorCode.PERMISSION_DENIED,
                        )
                    body = self._body()
                    acknowledged = app.store.ack_event(
                        match.group(1), body.get("lease_token")
                    )
                    if not acknowledged:
                        raise GatewayError(
                            "event lease is stale or unknown",
                            code=ErrorCode.CONFLICT,
                        )
                    self._json(200, {"acknowledged": True})
                    return

                if self.command == "POST" and path == "/internal/v1/jobs":
                    body = self._body()
                    operation = body.get("operation")
                    key = body.get("idempotency_key")
                    payload = body.get("payload")
                    try:
                        parsed_key = uuid.UUID(key)
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise GatewayError(
                            "idempotency_key must be a UUIDv4",
                            code=ErrorCode.INVALID_ARGUMENT,
                        ) from exc
                    if parsed_key.version != 4 or str(parsed_key) != key:
                        raise GatewayError(
                            "idempotency_key must be a canonical UUIDv4",
                            code=ErrorCode.INVALID_ARGUMENT,
                        )
                    if operation not in ("session.create", "session.send"):
                        raise GatewayError(
                            "operation must be session.create or session.send",
                            code=ErrorCode.INVALID_ARGUMENT,
                        )
                    if not isinstance(payload, dict):
                        raise GatewayError(
                            "payload must be an object",
                            code=ErrorCode.INVALID_ARGUMENT,
                        )
                    if operation == "session.send":
                        conversation_id = payload.get("conversation_id")
                        if not isinstance(conversation_id, str):
                            raise GatewayError(
                                "conversation_id is required",
                                code=ErrorCode.INVALID_ARGUMENT,
                            )
                        if not CONVERSATION_RE.fullmatch(conversation_id):
                            raise GatewayError(
                                "invalid conversation_id",
                                code=ErrorCode.INVALID_ARGUMENT,
                            )
                        if not isinstance(payload.get("prompt"), str) or not payload[
                            "prompt"
                        ]:
                            raise GatewayError(
                                "prompt is required",
                                code=ErrorCode.INVALID_ARGUMENT,
                            )
                        if len(payload["prompt"]) > 32768:
                            raise GatewayError(
                                "prompt exceeds 32768 characters",
                                code=ErrorCode.INVALID_ARGUMENT,
                            )
                        # Synchronous ownership gate before the 202: an unknown
                        # or foreign session is refused here, not by a later
                        # failed job.  The worker's reserve() still re-checks
                        # (the owner can change between enqueue and execution).
                        try:
                            app.router.require_owner(
                                conversation_id,
                                tenant_id=tenant_id,
                                principal_id=principal_id,
                                requested_account=payload.get("account"),
                            )
                        except GatewayError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - fail closed
                            raise GatewayError(
                                "session ownership lookup failed",
                                code=ErrorCode.DEPENDENCY_UNAVAILABLE,
                            ) from exc
                    job = app.store.enqueue(
                        principal_id=principal_id,
                        tenant_id=tenant_id,
                        operation=operation,
                        idempotency_key=key,
                        payload=payload,
                    )
                    self._json(202, job)
                    return

                match = re.fullmatch(r"/internal/v1/jobs/([^/]+)", path)
                if self.command == "GET" and match:
                    job_id = match.group(1)
                    if not JOB_RE.fullmatch(job_id):
                        raise GatewayError(
                            "invalid job_id", code=ErrorCode.INVALID_ARGUMENT
                        )
                    job = app.store.get(principal_id, tenant_id, job_id)
                    if job is None:
                        raise GatewayError("job not found", code=ErrorCode.NOT_FOUND)
                    self._json(200, job)
                    return

                if self.command == "GET" and path == "/internal/v1/sessions":
                    query = parse_qs(parsed.query, strict_parsing=False)
                    page_size = int(query.get("page_size", ["50"])[0])
                    cursor = query.get("cursor", [None])[0]
                    if not 1 <= page_size <= 100:
                        raise GatewayError(
                            "page_size must be in 1..100",
                            code=ErrorCode.INVALID_ARGUMENT,
                        )
                    if cursor is None:
                        offset = 0
                    elif cursor.isdigit() and len(cursor) <= 9:
                        offset = int(cursor)
                    else:
                        raise GatewayError(
                            "cursor must be a decimal offset",
                            code=ErrorCode.INVALID_ARGUMENT,
                        )
                    owners = app.router.list_owners(
                        tenant_id=tenant_id, principal_id=principal_id
                    )
                    window = owners[offset : offset + page_size]
                    next_offset = offset + len(window)
                    next_cursor = (
                        str(next_offset) if next_offset < len(owners) else None
                    )
                    self._json(
                        200,
                        {
                            "items": [
                                {
                                    "conversation_id": row["conversation_id"],
                                    "account": row["account"],
                                    "created_at": row["created_at"],
                                }
                                for row in window
                            ],
                            "next_cursor": next_cursor,
                        },
                    )
                    return

                match = re.fullmatch(
                    r"/internal/v1/sessions/([^/]+)/messages", path
                )
                if self.command == "GET" and match:
                    conversation_id = match.group(1)
                    if not CONVERSATION_RE.fullmatch(conversation_id):
                        raise GatewayError(
                            "invalid conversation_id",
                            code=ErrorCode.INVALID_ARGUMENT,
                        )
                    account = app.router.require_owner(
                        conversation_id,
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                    )
                    query = parse_qs(parsed.query, strict_parsing=False)
                    limit = int(query.get("limit", ["50"])[0])
                    after = query.get("after_message_id", [None])[0]
                    if not 1 <= limit <= 50:
                        raise GatewayError(
                            "limit must be in 1..50",
                            code=ErrorCode.INVALID_ARGUMENT,
                        )
                    result = _adapter_call(
                        app.adapter,
                        "read",
                        router=app.router,
                        conversation_id=conversation_id,
                        account=account,
                        after_message_id=after,
                        limit=limit,
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                    )
                    self._json(200, result)
                    return

                raise GatewayError("route not found", code=ErrorCode.NOT_FOUND)

            except GatewayError as exc:
                error = {"code": exc.code.value, "message": str(exc)}
                headers = {}
                if exc.retry_after_seconds is not None:
                    retry_after = max(1, int(float(exc.retry_after_seconds) + 0.999))
                    error["retry_after_seconds"] = retry_after
                    headers["Retry-After"] = str(retry_after)
                self._json(
                    _status_for(exc.code.value), {"error": error}, headers=headers
                )
            except (ValueError, TypeError) as exc:
                error = GatewayError(str(exc), code=ErrorCode.INVALID_ARGUMENT)
                self._json(400, {"error": {"code": error.code.value,
                                           "message": str(error)}})
            except Exception:
                # 真正的未知异常才带 traceback；SenderError 已在 _adapter_call
                # 边界记过一行结构化日志，不在此重复。
                _LOG.exception("sender_request_unhandled")
                self._json(
                    500,
                    {"error": {"code": "INTERNAL", "message": "internal error"}},
                )

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

    return Handler


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    app = SenderApplication()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        certfile=_required("SO_SENDER_TLS_CERT"),
        keyfile=_required("SO_SENDER_TLS_KEY"),
    )
    context.load_verify_locations(cafile=_required("SO_SENDER_TLS_CLIENT_CA"))
    context.verify_mode = ssl.CERT_REQUIRED

    server = ThreadingHTTPServer(
        (app.bind_host, app.bind_port), make_handler(app)
    )
    # server_close() waits for active handlers; daemon handlers could outlive
    # app.close() and race Adapter/SQLite teardown.
    server.daemon_threads = False
    server.block_on_close = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    app.start_worker()

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        logging.info("sender gateway listening on %s:%d", app.bind_host, app.bind_port)
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
