"""SQLite-backed MCP Events subscription and webhook delivery implementation.

The service launcher enables this module only when a private encryption key
file is explicitly configured. WorkItem terminal transitions insert completion
events into this same SQLite database and transaction; delivery is handled by
the in-process dispatcher.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from src.session_ops.authority import UnconfirmedSession, validate_conversation_id
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.session_ops.mcp_auth import (
    _PinnedHTTPSHandler,
    _resolve_validated_address,
)

MAX_EVENT_BYTES = 256 * 1024
MAX_CALLBACK_RESPONSE_BYTES = 4096
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TTL_SECONDS = 90 * 24 * 60 * 60
MAX_DELIVERY_ATTEMPTS = 5
EVENT_SCHEMA_VERSION = 2
DELIVERY_LEASE_SECONDS = 30
_KEY_CHECK_CONTEXT = b"chatgpt-session-ops:mcp-events:encryption-key-check:v1"


class CallbackEndpointError(Exception):
    """A callback could not be verified; mapped to MCP CallbackEndpointError."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _secret_key(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("delivery.secret must use the whsec_ prefix")
    encoded = secret[6:]
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("delivery.secret is not valid base64") from exc
    if not 24 <= len(key) <= 64:
        raise ValueError("delivery.secret must decode to 24..64 bytes")
    return key


def _signed_headers(
    subscription_id: str,
    secret: str,
    event_id: str,
    body: bytes,
    *,
    previous_secret: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signed = event_id.encode("utf-8") + b"." + timestamp.encode("ascii") + b"." + body
    signatures = [base64.b64encode(hmac.digest(_secret_key(secret), signed, "sha256")).decode("ascii")]
    if previous_secret is not None:
        signatures.append(base64.b64encode(hmac.digest(_secret_key(previous_secret), signed, "sha256")).decode("ascii"))
    return {
        "Content-Type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": " ".join("v1," + item for item in signatures),
        "X-MCP-Subscription-Id": subscription_id,
    }


def _post_public_https(url: str, body: bytes, headers: Mapping[str, str], timeout: float = 5.0) -> tuple[int, bytes]:
    """POST to a pinned public HTTPS address; proxies and redirects are disabled."""
    pinned_ip = _resolve_validated_address(url, "MCP Events callback")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            return None

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirect(),
        _PinnedHTTPSHandler(pinned_ip),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise OSError("callback redirect is forbidden")
            return int(response.status), response.read(MAX_CALLBACK_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # HTTPError is the response object for 3xx/4xx/5xx; no redirect is followed.
        return int(exc.code), exc.read(MAX_CALLBACK_RESPONSE_BYTES + 1)


class MCPEventsStore:
    """SQLite-backed subscriptions and transactional completion-event delivery."""

    def __init__(
        self,
        database: str,
        *,
        post_json: Callable[[str, bytes, Mapping[str, str]], tuple[int, bytes]] | None = None,
        clock: Callable[[], float] = time.time,
        encryption_key: bytes | None = None,
    ) -> None:
        if database == ":memory:":
            raise ValueError("MCP Events requires a file-backed SQLite database")
        self.database = database
        self.post_json = post_json or _post_public_https
        self.clock = clock
        self._metrics_lock = threading.Lock()
        self._process_metrics = {
            "callback_count": 0,
            "callback_latency_ms_total": 0,
            "callback_latency_ms_max": 0,
            "callback_status_2xx": 0,
            "callback_status_3xx": 0,
            "callback_status_4xx": 0,
            "callback_status_5xx": 0,
            "callback_status_410": 0,
            "callback_status_413": 0,
            "secret_decryption_failures": 0,
            "sqlite_busy_errors": 0,
        }
        if encryption_key is None or not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise ValueError("a 32-byte encryption_key is required for subscription secret storage")
        self._cipher = AESGCM(encryption_key)
        self._key_check = hmac.digest(encryption_key, _KEY_CHECK_CONTEXT, "sha256")
        if database != ":memory:":
            path = Path(database)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.parent.stat().st_mode & 0o077:
                raise PermissionError("MCP Events database directory must not be accessible by group or others")
            if path.exists() and path.stat().st_mode & 0o077:
                raise PermissionError("MCP Events database must not be accessible by group or others")
        with self._db() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS event_key_metadata (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    key_check BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_schema_metadata (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_subscriptions (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    callback_url TEXT NOT NULL,
                    secret_enc BLOB NOT NULL,
                    secret_prev_enc BLOB,
                    secret_prev_until INTEGER,
                    expires_at INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    generation INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    UNIQUE(tenant_id, principal_id, name, arguments_json, callback_url)
                );
                CREATE TABLE IF NOT EXISTS callback_verifications (
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    callback_url TEXT NOT NULL,
                    verified_until INTEGER NOT NULL,
                    PRIMARY KEY(tenant_id, principal_id, callback_url)
                );
                CREATE TABLE IF NOT EXISTS event_outbox (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_deliveries (
                    event_id TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    last_status INTEGER,
                    lease_until INTEGER,
                    lease_token TEXT,
                    generation INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(event_id, subscription_id),
                    FOREIGN KEY(event_id) REFERENCES event_outbox(event_id),
                    FOREIGN KEY(subscription_id) REFERENCES event_subscriptions(id) ON DELETE CASCADE
                );
                """
            )
            subscription_columns = {row["name"] for row in db.execute("PRAGMA table_info(event_subscriptions)")}
            if "generation" not in subscription_columns:
                db.execute("ALTER TABLE event_subscriptions ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
            delivery_columns = {row["name"] for row in db.execute("PRAGMA table_info(event_deliveries)")}
            if "generation" not in delivery_columns:
                db.execute("ALTER TABLE event_deliveries ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
            key_metadata = db.execute(
                "SELECT key_check FROM event_key_metadata WHERE singleton=1"
            ).fetchone()
            if key_metadata is None:
                # Upgrade older event databases only after proving that the
                # supplied key can decrypt every existing subscription secret.
                encrypted_rows = db.execute(
                    "SELECT id, secret_enc, secret_prev_enc FROM event_subscriptions"
                ).fetchall()
                try:
                    for encrypted_row in encrypted_rows:
                        self._decrypt_secret(encrypted_row["id"], encrypted_row["secret_enc"])
                        if encrypted_row["secret_prev_enc"] is not None:
                            self._decrypt_secret(encrypted_row["id"], encrypted_row["secret_prev_enc"])
                except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
                    raise RuntimeError(
                        "MCP Events encryption key does not decrypt existing subscriptions; "
                        "restore the matching key or require re-subscription"
                    ) from exc
                db.execute(
                    "INSERT INTO event_key_metadata(singleton, key_check) VALUES (1, ?)",
                    (self._key_check,),
                )
            elif (
                not isinstance(key_metadata["key_check"], bytes)
                or len(key_metadata["key_check"]) != 32
                or not hmac.compare_digest(key_metadata["key_check"], self._key_check)
            ):
                raise RuntimeError(
                    "MCP Events encryption key does not match the database; "
                    "restore the matching key or require re-subscription"
                )
            schema_metadata = db.execute(
                "SELECT version FROM event_schema_metadata WHERE singleton=1"
            ).fetchone()
            if schema_metadata is None or int(schema_metadata["version"]) < EVENT_SCHEMA_VERSION:
                # Version 2 removes the application-defined destination
                # conversation_id filter. ChatGPT's callback URL is the
                # destination bound to the subscribed Work chat. Old
                # subscriptions used the former, misleading filter semantics;
                # deactivate them and require ChatGPT to subscribe again.
                legacy_ids = db.execute(
                    "SELECT id FROM event_subscriptions WHERE active=1"
                ).fetchall()
                for legacy in legacy_ids:
                    db.execute(
                        "UPDATE event_subscriptions SET active=0, generation=generation+1 WHERE id=?",
                        (legacy["id"],),
                    )
                    db.execute(
                        "UPDATE event_deliveries SET status='failed', last_status=410, next_attempt_at=0, lease_until=NULL, lease_token=NULL WHERE subscription_id=? AND status IN ('pending','sending')",
                        (legacy["id"],),
                    )
                db.execute(
                    "INSERT INTO event_schema_metadata(singleton, version) VALUES (1, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version",
                    (EVENT_SCHEMA_VERSION,),
                )
            elif int(schema_metadata["version"]) > EVENT_SCHEMA_VERSION:
                raise RuntimeError("MCP Events database schema is newer than this server")
        if database != ":memory:":
            os.chmod(database, 0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception as exc:
            if isinstance(exc, sqlite3.OperationalError) and (
                "locked" in str(exc).lower() or "busy" in str(exc).lower()
            ):
                self._increment_metric("sqlite_busy_errors")
            db.rollback()
            raise
        finally:
            db.close()

    def _increment_metric(self, name: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self._process_metrics[name] = self._process_metrics.get(name, 0) + amount

    def _post_with_metrics(
        self, url: str, body: bytes, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        started = time.monotonic()
        status = 599
        try:
            status, response = self.post_json(url, body, headers)
            return status, response
        finally:
            elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            with self._metrics_lock:
                self._process_metrics["callback_count"] += 1
                self._process_metrics["callback_latency_ms_total"] += elapsed_ms
                self._process_metrics["callback_latency_ms_max"] = max(
                    self._process_metrics["callback_latency_ms_max"], elapsed_ms
                )
                status_code = status if isinstance(status, int) and not isinstance(status, bool) else 599
                status_class = status_code // 100
                metric = "callback_status_%dxx" % status_class
                if metric in self._process_metrics:
                    self._process_metrics[metric] += 1
                if status_code == 410:
                    self._process_metrics["callback_status_410"] += 1
                elif status_code == 413:
                    self._process_metrics["callback_status_413"] += 1

    def health_snapshot(self) -> dict[str, Any]:
        """Return bounded aggregate health data; never expose IDs, URLs, or payloads."""
        now = float(self.clock())
        with self._db() as db:
            delivery_rows = db.execute(
                "SELECT status, count(*) AS count FROM event_deliveries GROUP BY status"
            ).fetchall()
            delivery_counts = {row["status"]: int(row["count"]) for row in delivery_rows}
            subscription_row = db.execute(
                """SELECT
                     SUM(CASE WHEN active=1 AND (expires_at IS NULL OR expires_at>?) THEN 1 ELSE 0 END) AS active,
                     SUM(CASE WHEN active=1 AND expires_at IS NOT NULL AND expires_at<=? THEN 1 ELSE 0 END) AS expired,
                     SUM(CASE WHEN active=0 THEN 1 ELSE 0 END) AS inactive
                   FROM event_subscriptions""",
                (int(now), int(now)),
            ).fetchone()
            oldest_row = db.execute(
                """SELECT MIN(e.created_at) AS oldest
                   FROM event_deliveries d JOIN event_outbox e ON e.event_id=d.event_id
                   WHERE d.status IN ('pending','sending')"""
            ).fetchone()
            attempts = db.execute(
                "SELECT COALESCE(SUM(attempts), 0) FROM event_deliveries"
            ).fetchone()[0]
            exhausted = db.execute(
                "SELECT count(*) FROM event_deliveries WHERE status='failed' AND attempts>=?",
                (MAX_DELIVERY_ATTEMPTS,),
            ).fetchone()[0]
            last_status_rows = db.execute(
                """SELECT CASE
                     WHEN last_status BETWEEN 200 AND 299 THEN '2xx'
                     WHEN last_status BETWEEN 300 AND 399 THEN '3xx'
                     WHEN last_status BETWEEN 400 AND 499 THEN '4xx'
                     WHEN last_status BETWEEN 500 AND 599 THEN '5xx'
                     ELSE 'other' END AS status_class, count(*) AS count
                   FROM event_deliveries WHERE last_status IS NOT NULL
                   GROUP BY status_class"""
            ).fetchall()
            outbox_count = int(db.execute("SELECT count(*) FROM event_outbox").fetchone()[0])
        database_path = Path(self.database)
        try:
            database_bytes = database_path.stat().st_size
        except OSError:
            database_bytes = 0
        try:
            wal_bytes = Path(str(database_path) + "-wal").stat().st_size
        except OSError:
            wal_bytes = 0
        with self._metrics_lock:
            process_metrics = dict(self._process_metrics)
        oldest = oldest_row["oldest"]
        return {
            "outbox": {
                "events": outbox_count,
                "pending": delivery_counts.get("pending", 0),
                "sending": delivery_counts.get("sending", 0),
                "delivered": delivery_counts.get("delivered", 0),
                "failed": delivery_counts.get("failed", 0),
                "retry_exhausted": int(exhausted),
                "attempts": int(attempts),
                "oldest_pending_age_seconds": (
                    max(0, int(now - float(oldest))) if oldest is not None else 0
                ),
                "last_status_class": {
                    row["status_class"]: int(row["count"]) for row in last_status_rows
                },
            },
            "subscriptions": {
                "active": int(subscription_row["active"] or 0),
                "expired": int(subscription_row["expired"] or 0),
                "inactive": int(subscription_row["inactive"] or 0),
            },
            "callbacks": process_metrics,
            "storage": {
                "database_bytes": database_bytes,
                "wal_bytes": wal_bytes,
            },
        }

    def _encrypt_secret(self, subscription_id: str, secret: str) -> bytes:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, secret.encode("utf-8"), subscription_id.encode("utf-8"))
        return nonce + ciphertext

    def _decrypt_secret(self, subscription_id: str, encrypted: bytes) -> str:
        if not isinstance(encrypted, bytes) or len(encrypted) < 29:
            raise ValueError("stored webhook secret is malformed")
        plaintext = self._cipher.decrypt(encrypted[:12], encrypted[12:], subscription_id.encode("utf-8"))
        return plaintext.decode("utf-8")

    @staticmethod
    def _identity(auth: Any) -> tuple[str, str]:
        principal = getattr(auth, "principal", None)
        tenant_id = getattr(principal, "tenant_id", None)
        principal_id = getattr(principal, "principal_id", None)
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(principal_id, str) or not principal_id:
            raise PermissionError("verified MCP principal is required")
        return tenant_id, principal_id

    @staticmethod
    def event_catalog() -> list[dict[str, Any]]:
        return [{
            "name": "chatgpt.job.completed",
            "description": "A WorkItem owned by the connected account has reached a terminal state. ChatGPT delivers the webhook to the Work chat that created the subscription.",
            "delivery": ["webhook"],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Optional WorkItem ID filter. Omit it to receive all WorkItem completion events owned by this authenticated principal in the subscribed Work chat."}
                },
                "additionalProperties": False,
            },
            "payloadSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "source_conversation_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                    "status": {"type": "string", "enum": ["succeeded", "failed"]},
                    "summary": {"type": "string"},
                    "completed_at": {"type": "string", "format": "date-time"},
                    "error_code": {"type": "string"},
                    "outcome_unknown": {"type": "boolean"},
                },
                "required": ["job_id", "status", "summary", "completed_at"],
                "additionalProperties": False,
            },
        }]

    def _verify_callback(self, tenant_id: str, principal_id: str, callback_url: str, secret: str, subscription_id: str) -> None:
        now = int(self.clock())
        with self._db() as db:
            row = db.execute(
                "SELECT verified_until FROM callback_verifications WHERE tenant_id=? AND principal_id=? AND callback_url=?",
                (tenant_id, principal_id, callback_url),
            ).fetchone()
            if row and row["verified_until"] > now:
                return
        challenge = secrets.token_urlsafe(32)
        body = _canonical_json({"type": "verification", "challenge": challenge}).encode("utf-8")
        webhook_id = "msg_verification_" + uuid.uuid4().hex
        headers = _signed_headers(subscription_id, secret, webhook_id, body)
        try:
            status, response_body = self._post_with_metrics(callback_url, body, headers)
        except (TimeoutError, OSError) as exc:
            raise CallbackEndpointError("timeout" if isinstance(exc, TimeoutError) else "unreachable") from exc
        except Exception as exc:
            raise CallbackEndpointError("unreachable") from exc
        if not 200 <= status < 300 or len(response_body) > MAX_CALLBACK_RESPONSE_BYTES:
            raise CallbackEndpointError("challenge_failed")
        try:
            echoed = json.loads(response_body.decode("utf-8")).get("challenge")
        except (UnicodeError, ValueError, AttributeError) as exc:
            raise CallbackEndpointError("challenge_failed") from exc
        if not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
            raise CallbackEndpointError("challenge_failed")
        with self._db() as db:
            db.execute(
                "INSERT INTO callback_verifications VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, principal_id, callback_url) DO UPDATE SET verified_until=excluded.verified_until",
                (tenant_id, principal_id, callback_url, now + 24 * 60 * 60),
            )

    def subscribe(self, auth: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        request_started_at = float(self.clock())
        tenant_id, principal_id = self._identity(auth)
        name = params.get("name")
        arguments = params.get("arguments") or {}
        delivery = params.get("delivery")
        if name != "chatgpt.job.completed" or not isinstance(arguments, dict):
            raise ValueError("unsupported event or invalid arguments")
        if set(arguments) - {"job_id"} or (
            "job_id" in arguments
            and (not isinstance(arguments["job_id"], str) or not arguments["job_id"])
        ):
            raise ValueError("only an optional job_id filter is supported for chatgpt.job.completed")
        job_id = arguments.get("job_id")
        if job_id is not None:
            # Completion events are owned and published by the WorkItem
            # owner. A delegate may subscribe to its own events, but a
            # job-specific filter for someone else's WorkItem would never
            # receive a delivery and must be rejected before callback I/O.
            with self._db() as db:
                authority_schema = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_item_owners'"
                ).fetchone()
                owner = (
                    db.execute(
                        "SELECT tenant_id, owner_principal_id FROM work_item_owners WHERE work_item_id=?",
                        (job_id,),
                    ).fetchone()
                    if authority_schema is not None else None
                )
            if authority_schema is not None and (
                owner is None
                or owner["tenant_id"] != tenant_id
                or owner["owner_principal_id"] != principal_id
            ):
                raise ValueError("job_id filter must reference a WorkItem owned by the subscriber")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise ValueError("webhook delivery is required")
        callback_url, secret = delivery.get("url"), delivery.get("secret")
        if not isinstance(callback_url, str):
            raise ValueError("delivery.url is required")
        # Resolve and validate before any outbound request; _post_public_https pins again at connect time.
        try:
            _resolve_validated_address(callback_url, "MCP Events callback")
        except Exception as exc:
            raise CallbackEndpointError("unreachable") from exc
        _secret_key(secret)
        canonical_arguments = _canonical_json(arguments)
        identity = _canonical_json([tenant_id, principal_id, name, canonical_arguments, callback_url])
        subscription_id = "sub_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        ttl_ms = params.get("ttlMs", "__omitted__")
        if ttl_ms == "__omitted__":
            expires_at = int(self.clock()) + DEFAULT_TTL_SECONDS
        elif ttl_ms is None:
            expires_at = None
        else:
            if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or not 60_000 <= ttl_ms <= MAX_TTL_SECONDS * 1000:
                raise ValueError("ttlMs is outside the supported range")
            expires_at = int(self.clock()) + ttl_ms // 1000
        self._verify_callback(tenant_id, principal_id, callback_url, secret, subscription_id)
        encrypted_secret = self._encrypt_secret(subscription_id, secret)
        now = int(self.clock())
        previous_secret_enc = None
        previous_secret_until = None
        generation = 1
        with self._db() as db:
            existing = db.execute(
                "SELECT secret_enc, secret_prev_enc, secret_prev_until, active, generation FROM event_subscriptions WHERE id=?",
                (subscription_id,),
            ).fetchone()
            if existing is not None:
                generation = int(existing["generation"])
                if existing["active"] == 0:
                    # Unsubscribe already advanced the generation.
                    db.execute(
                        """UPDATE event_deliveries SET status='failed', last_status=410,
                           next_attempt_at=0, lease_until=NULL, lease_token=NULL
                           WHERE subscription_id=? AND status IN ('pending','sending')""",
                        (subscription_id,),
                    )
                existing_secret = self._decrypt_secret(subscription_id, existing["secret_enc"])
                if not hmac.compare_digest(existing_secret, secret):
                    previous_secret_enc = existing["secret_enc"]
                    previous_secret_until = now + 24 * 60 * 60
                elif existing["secret_prev_until"] is not None and existing["secret_prev_until"] > now:
                    previous_secret_enc = existing["secret_prev_enc"]
                    previous_secret_until = existing["secret_prev_until"]
            db.execute(
                """INSERT INTO event_subscriptions
                (id, tenant_id, principal_id, name, arguments_json, callback_url, secret_enc,
                 secret_prev_enc, secret_prev_until, expires_at, active, generation, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(tenant_id, principal_id, name, arguments_json, callback_url)
                DO UPDATE SET secret_enc=excluded.secret_enc,
                              secret_prev_enc=excluded.secret_prev_enc,
                              secret_prev_until=excluded.secret_prev_until,
                              expires_at=excluded.expires_at, active=1,
                              generation=excluded.generation""",
                (subscription_id, tenant_id, principal_id, name, canonical_arguments, callback_url,
                 encrypted_secret, previous_secret_enc, previous_secret_until, expires_at, generation, now),
            )
            # Recover only completions racing with this subscribe request.
            recent_events = db.execute(
                """SELECT event_id, data_json FROM event_outbox
                   WHERE tenant_id=? AND principal_id=? AND name=? AND created_at>=?
                   ORDER BY created_at""",
                (tenant_id, principal_id, name, request_started_at),
            ).fetchall()
            for event in recent_events:
                event_data = json.loads(event["data_json"])
                # ChatGPT's callback URL is bound to the subscribed Work chat;
                # event arguments are filters only, never routing instructions.
                # With no job_id filter, this subscription receives every
                # completion event for the authenticated principal.
                if arguments.get("job_id") not in (None, event_data.get("job_id")):
                    continue
                db.execute(
                    """INSERT OR IGNORE INTO event_deliveries(event_id, subscription_id, status, generation)
                       VALUES (?, ?, 'pending', ?)""",
                    (event["event_id"], subscription_id, generation),
                )
        return {"id": subscription_id, "refreshBefore": (datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace("+00:00", "Z") if expires_at is not None else None), "cursor": None, "truncated": False}

    def unsubscribe(self, auth: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        tenant_id, principal_id = self._identity(auth)
        delivery = params.get("delivery") or {}
        arguments = _canonical_json(params.get("arguments") or {})
        with self._db() as db:
            row = db.execute(
                """SELECT id FROM event_subscriptions
                   WHERE tenant_id=? AND principal_id=? AND name=? AND arguments_json=? AND callback_url=?""",
                (tenant_id, principal_id, params.get("name"), arguments, delivery.get("url")),
            ).fetchone()
            if row is not None:
                db.execute("UPDATE event_subscriptions SET active=0, generation=generation+1 WHERE id=?", (row["id"],))
                db.execute(
                    """UPDATE event_deliveries SET status='failed', last_status=410,
                       next_attempt_at=0, lease_until=NULL, lease_token=NULL
                       WHERE subscription_id=? AND status IN ('pending','sending')""",
                    (row["id"],),
                )
        return {}

    def publish_job_completed(
        self,
        tenant_id: str,
        principal_id: str,
        job_id: str,
        summary: str,
        *,
        event_id: str | None = None,
        completed_at: str | None = None,
        source_conversation_ids: list[str] | None = None,
        status: str = "succeeded",
        error_code: str | None = None,
        outcome_unknown: bool | None = None,
    ) -> str:
        """Persist one completion event in its own transaction."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.publish_job_completed_in_transaction(
                db,
                tenant_id,
                principal_id,
                job_id,
                summary,
                event_id=event_id,
                completed_at=completed_at,
                source_conversation_ids=source_conversation_ids,
                status=status,
                error_code=error_code,
                outcome_unknown=outcome_unknown,
            )

    def publish_job_completed_in_transaction(
        self,
        db: sqlite3.Connection,
        tenant_id: str,
        principal_id: str,
        job_id: str,
        summary: str,
        *,
        event_id: str,
        completed_at: str,
        source_conversation_ids: list[str] | None = None,
        status: str = "succeeded",
        error_code: str | None = None,
        outcome_unknown: bool | None = None,
    ) -> str:
        """Insert an event using the caller's transaction (same SQLite file)."""
        if not all(isinstance(value, str) and value for value in (tenant_id, principal_id, job_id)):
            raise ValueError("tenant, principal, and job identifiers are required")
        if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
            raise ValueError("an active SQLite transaction is required")
        main_path = next(
            (row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"),
            "",
        )
        if not main_path or os.path.realpath(main_path) != os.path.realpath(self.database):
            raise ValueError("completion event and WorkItem must share the same SQLite database")
        if not isinstance(summary, str):
            raise ValueError("job summary must be text")
        if source_conversation_ids is not None:
            if (
                not isinstance(source_conversation_ids, list)
                or any(not isinstance(value, str) or not value for value in source_conversation_ids)
                or len(set(source_conversation_ids)) != len(source_conversation_ids)
            ):
                raise ValueError("source_conversation_ids must be a list of unique nonempty strings")
            try:
                source_conversation_ids = [validate_conversation_id(value) for value in source_conversation_ids]
            except UnconfirmedSession as exc:
                raise ValueError("source_conversation_ids must contain canonical lowercase UUIDs") from exc
        if status not in ("succeeded", "failed"):
            raise ValueError("job status must be succeeded or failed")
        if error_code is not None and (not isinstance(error_code, str) or not error_code):
            raise ValueError("error_code must be nonempty text")
        if outcome_unknown is not None and not isinstance(outcome_unknown, bool):
            raise ValueError("outcome_unknown must be boolean")
        now = float(self.clock())
        event_id = event_id or "evt_" + uuid.uuid4().hex
        if not isinstance(event_id, str) or not event_id.startswith("evt_") or len(event_id) > 128:
            raise ValueError("event_id is malformed")
        if completed_at is None:
            completed_at = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
        if not isinstance(completed_at, str):
            raise ValueError("completed_at must be an ISO timestamp")
        try:
            parsed_completed_at = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            if parsed_completed_at.tzinfo is None:
                raise ValueError("completed_at must include a timezone")
            event_created_at = parsed_completed_at.timestamp()
        except ValueError as exc:
            raise ValueError("completed_at must be a valid timezone-aware ISO timestamp") from exc
        data = {
            "job_id": job_id,
            "status": status,
            "summary": summary[:4000],
            "completed_at": completed_at,
        }
        if source_conversation_ids is not None:
            data["source_conversation_ids"] = source_conversation_ids
        if error_code is not None:
            data["error_code"] = error_code
        if outcome_unknown is not None:
            data["outcome_unknown"] = outcome_unknown
        canonical_data = _canonical_json(data)
        existing = db.execute(
            "SELECT tenant_id, principal_id, name, data_json FROM event_outbox WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if (existing["tenant_id"], existing["principal_id"], existing["name"], existing["data_json"]) != (
                tenant_id, principal_id, "chatgpt.job.completed", canonical_data
            ):
                raise ValueError("event_id was reused with a different event")
            return event_id
        db.execute(
            "INSERT INTO event_outbox VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, tenant_id, principal_id, "chatgpt.job.completed", canonical_data, event_created_at),
        )
        # The callback URL is the ChatGPT-supplied destination bound to the
        # subscribed Work chat. Subscriptions are owner-scoped; job_id is the
        # only application filter and may be omitted for owner-wide events.
        subscriptions = db.execute(
            "SELECT id, arguments_json, generation FROM event_subscriptions WHERE tenant_id=? AND principal_id=? AND name='chatgpt.job.completed' AND active=1 AND (expires_at IS NULL OR expires_at>?)",
            (tenant_id, principal_id, now),
        ).fetchall()
        for sub in subscriptions:
            filters = json.loads(sub["arguments_json"])
            if filters.get("job_id") not in (None, job_id):
                continue
            db.execute(
                "INSERT INTO event_deliveries(event_id, subscription_id, status, generation) VALUES (?, ?, 'pending', ?)",
                (event_id, sub["id"], sub["generation"]),
            )
        return event_id

    def cleanup_expired(self, *, retention_seconds: int = 30 * 24 * 60 * 60) -> dict[str, int]:
        """Expire subscriptions and prune terminal delivery history safely."""
        if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, int) or not 86400 <= retention_seconds <= 365 * 24 * 60 * 60:
            raise ValueError("retention_seconds must be in 1..365 days")
        now = int(self.clock())
        cutoff = now - retention_seconds
        with self._db() as db:
            expired = db.execute(
                "SELECT id FROM event_subscriptions WHERE active=1 AND expires_at IS NOT NULL AND expires_at<=?",
                (now,),
            ).fetchall()
            expired_ids = [row["id"] for row in expired]
            if expired_ids:
                placeholders = ",".join("?" for _ in expired_ids)
                db.execute(
                    f"UPDATE event_subscriptions SET active=0, generation=generation+1 WHERE id IN ({placeholders})",
                    expired_ids,
                )
                db.execute(
                    f"UPDATE event_deliveries SET status='failed', last_status=410, lease_until=NULL, lease_token=NULL WHERE subscription_id IN ({placeholders}) AND status IN ('pending','sending')",
                    expired_ids,
                )
            # Delete old terminal fan-out rows first to satisfy the event FK.
            deleted_deliveries = db.execute(
                """DELETE FROM event_deliveries
                   WHERE status IN ('delivered','failed')
                     AND event_id IN (SELECT event_id FROM event_outbox WHERE created_at<?)""",
                (cutoff,),
            ).rowcount
            deleted_events = db.execute(
                """DELETE FROM event_outbox AS e
                   WHERE e.created_at<?
                     AND NOT EXISTS (SELECT 1 FROM event_deliveries d WHERE d.event_id=e.event_id)""",
                (cutoff,),
            ).rowcount
            deleted_subscriptions = db.execute(
                """DELETE FROM event_subscriptions AS s
                   WHERE ((s.expires_at IS NOT NULL AND s.expires_at<=?)
                       OR (s.active=0 AND s.created_at<?))
                     AND NOT EXISTS (SELECT 1 FROM event_deliveries d WHERE d.subscription_id=s.id)""",
                (now, cutoff),
            ).rowcount
            deleted_verifications = db.execute(
                "DELETE FROM callback_verifications WHERE verified_until<=?",
                (now,),
            ).rowcount
        return {
            "expired_subscriptions": len(expired_ids),
            "deleted_deliveries": deleted_deliveries,
            "deleted_events": deleted_events,
            "deleted_subscriptions": deleted_subscriptions,
            "deleted_verifications": deleted_verifications,
        }

    def deliver_pending(self, *, limit: int = 100, authorize: Any = None) -> int:
        """Deliver bounded work with generation-fenced leases."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        # Bound the sequential batch to the 30s lease / 10s callback budget.
        now = int(self.clock())
        claimed: list[dict[str, Any]] = []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE event_deliveries
                   SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END,
                       last_status=CASE WHEN attempts>=? THEN 599 ELSE last_status END,
                       next_attempt_at=0, lease_until=NULL, lease_token=NULL
                   WHERE status='sending' AND lease_until<=?""",
                (MAX_DELIVERY_ATTEMPTS, MAX_DELIVERY_ATTEMPTS, now),
            )
            db.execute(
                """UPDATE event_deliveries SET status='failed', last_status=410,
                       next_attempt_at=0, lease_until=NULL, lease_token=NULL
                   WHERE status='pending' AND EXISTS (
                       SELECT 1 FROM event_subscriptions s WHERE s.id=event_deliveries.subscription_id
                         AND (s.active=0 OR s.generation!=event_deliveries.generation
                              OR (s.expires_at IS NOT NULL AND s.expires_at<=?)))""",
                (now,),
            )
            rows = db.execute(
                """SELECT d.event_id, d.subscription_id, d.attempts, d.generation,
                          s.callback_url, s.secret_enc, s.secret_prev_enc, s.secret_prev_until,
                          e.name, e.data_json, e.created_at, e.tenant_id, e.principal_id
                   FROM event_deliveries d JOIN event_subscriptions s ON s.id=d.subscription_id
                   JOIN event_outbox e ON e.event_id=d.event_id
                   WHERE d.status='pending' AND d.next_attempt_at<=? AND s.active=1
                     AND d.generation=s.generation AND d.attempts<?
                     AND (s.expires_at IS NULL OR s.expires_at>?)
                   ORDER BY e.created_at LIMIT ?""",
                (now, MAX_DELIVERY_ATTEMPTS, now, min(limit, 2)),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                attempts = int(row["attempts"]) + 1
                cur = db.execute(
                    """UPDATE event_deliveries SET status='sending', attempts=?, lease_until=?, lease_token=?
                       WHERE event_id=? AND subscription_id=? AND generation=? AND status='pending'""",
                    (attempts, now + DELIVERY_LEASE_SECONDS, token, row["event_id"], row["subscription_id"], row["generation"]),
                )
                if cur.rowcount == 1:
                    item = dict(row)
                    item["attempts"] = attempts
                    item["lease_token"] = token
                    claimed.append(item)

        delivered = 0
        for row in claimed:
            status = 599
            event_data = json.loads(row["data_json"])
            # The ChatGPT-provided callback URL routes this event to the
            # subscribed Work chat. Payload fields are data, never routing.
            event = {
                "eventId": row["event_id"], "name": row["name"],
                "timestamp": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat().replace("+00:00", "Z"),
                "data": event_data, "cursor": None,
            }
            body = _canonical_json(event).encode("utf-8")
            if len(body) > MAX_EVENT_BYTES:
                status = 413
            else:
                try:
                    if authorize is not None and authorize(
                        tenant_id=row["tenant_id"], principal_id=row["principal_id"],
                        job_id=event["data"].get("job_id"),
                    ) is not True:
                        raise PermissionError("event resource is no longer accessible")
                    current_time = int(self.clock())
                    with self._db() as db:
                        # Validate and renew in one conditional write. A
                        # SELECT followed by UPDATE leaves a race where a
                        # second worker can reclaim the expired lease between
                        # those statements while this worker still proceeds
                        # to external callback I/O using its stale snapshot.
                        renewed = db.execute(
                            """UPDATE event_deliveries SET lease_until=?
                               WHERE event_id=? AND subscription_id=? AND generation=?
                                 AND status='sending' AND lease_token=? AND lease_until>?
                                 AND EXISTS (
                                     SELECT 1 FROM event_subscriptions s
                                      WHERE s.id=event_deliveries.subscription_id
                                        AND s.active=1 AND s.generation=event_deliveries.generation
                                        AND (s.expires_at IS NULL OR s.expires_at>?)
                                 )""",
                            (current_time + DELIVERY_LEASE_SECONDS, row["event_id"],
                             row["subscription_id"], row["generation"], row["lease_token"],
                             current_time, current_time),
                        )
                        valid = renewed.rowcount == 1
                    if not valid:
                        # If this exact lease is still ours but the subscription
                        # expired during authorization, terminalize it. A lease
                        # stolen or invalidated by another lifecycle is untouched.
                        with self._db() as db:
                            db.execute(
                                """UPDATE event_deliveries SET status='failed', last_status=410,
                                          lease_until=NULL, lease_token=NULL
                                   WHERE event_id=? AND subscription_id=? AND generation=?
                                     AND status='sending' AND lease_token=?
                                     AND EXISTS (SELECT 1 FROM event_subscriptions s
                                                 WHERE s.id=? AND s.active=1
                                                   AND s.generation=? AND s.expires_at<=?)""",
                                (row["event_id"], row["subscription_id"], row["generation"],
                                 row["lease_token"], row["subscription_id"], row["generation"], current_time),
                            )
                        continue
                    try:
                        secret = self._decrypt_secret(row["subscription_id"], row["secret_enc"])
                        previous = None
                        if row["secret_prev_enc"] is not None and row["secret_prev_until"] is not None and row["secret_prev_until"] > current_time:
                            previous = self._decrypt_secret(row["subscription_id"], row["secret_prev_enc"])
                    except (InvalidTag, ValueError, UnicodeDecodeError):
                        self._increment_metric("secret_decryption_failures")
                        raise
                    headers = _signed_headers(row["subscription_id"], secret, row["event_id"], body, previous_secret=previous)
                    status, _ = self._post_with_metrics(row["callback_url"], body, headers)
                except PermissionError:
                    status = 403
                except Exception:
                    status = 599

            retryable = status not in (403, 410, 413) and (status == 599 or status in (408, 429) or status >= 500)
            attempts = int(row["attempts"])
            state = "delivered" if 200 <= status < 300 else (
                "pending" if retryable and attempts < MAX_DELIVERY_ATTEMPTS else "failed"
            )
            failure_time = int(self.clock())
            next_attempt = failure_time + min(3600, 2 ** attempts) if state == "pending" else 0
            with self._db() as db:
                updated = db.execute(
                    """UPDATE event_deliveries SET status=?, next_attempt_at=?, last_status=?,
                              lease_until=NULL, lease_token=NULL
                       WHERE event_id=? AND subscription_id=? AND generation=?
                         AND status='sending' AND lease_token=?""",
                    (state, next_attempt, status, row["event_id"], row["subscription_id"], row["generation"], row["lease_token"]),
                )
                if status == 410 and updated.rowcount == 1:
                    invalidated = db.execute(
                        """UPDATE event_subscriptions SET active=0, generation=generation+1
                           WHERE id=? AND active=1 AND generation=?""",
                        (row["subscription_id"], row["generation"]),
                    )
                    if invalidated.rowcount == 1:
                        db.execute(
                            """UPDATE event_deliveries SET status='failed', next_attempt_at=0, last_status=410,
                                      lease_until=NULL, lease_token=NULL
                               WHERE subscription_id=? AND generation=? AND status IN ('pending','sending')""",
                            (row["subscription_id"], row["generation"]),
                        )
            if state == "delivered":
                delivered += 1
        return delivered


def build_event_request_models() -> tuple[type, type, type]:
    """Create SDK request models lazily so the core package stays SDK-optional."""
    from pydantic import BaseModel, ConfigDict, Field
    from mcp.types import RequestParams
    class Delivery(BaseModel):
        model_config = ConfigDict(extra="forbid")
        mode: str
        url: str
        secret: str | None = None

    class EventsListParams(RequestParams):
        model_config = ConfigDict(extra="forbid")
        cursor: str | None = None

    class EventsSubscribeParams(RequestParams):
        model_config = ConfigDict(extra="forbid", populate_by_name=True)
        name: str
        arguments: dict[str, Any] = Field(default_factory=dict)
        delivery: Delivery
        cursor: str | None = None
        ttl_ms: int | None = Field(default=None, alias="ttlMs", ge=60000, le=MAX_TTL_SECONDS * 1000)

    class EventsUnsubscribeParams(RequestParams):
        model_config = ConfigDict(extra="forbid")
        name: str
        arguments: dict[str, Any] = Field(default_factory=dict)
        delivery: Delivery

    return EventsListParams, EventsSubscribeParams, EventsUnsubscribeParams
