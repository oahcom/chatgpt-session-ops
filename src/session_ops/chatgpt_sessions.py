"""SQLite ownership and idempotency records for direct ChatGPT MCP calls.

This is deliberately not a job store: calls execute synchronously in the MCP
process and only their ownership/idempotency receipts are persisted.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

_CONVERSATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_ACCOUNT_ID = re.compile(r"^(?:default|acct[1-9][0-9]*)$")


class ChatGPTSessionRegistryError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ChatGPTSessionRegistry:
    """Tenant/principal-bound account ownership stored beside MCP authority."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.database_path), timeout=3.0, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 3000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chatgpt_session_owners (
                    conversation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    account TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_chatgpt_owner_principal
                    ON chatgpt_session_owners(tenant_id, principal_id, conversation_id);
                CREATE TABLE IF NOT EXISTS chatgpt_mcp_idempotency (
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'unknown')),
                    response_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, principal_id, operation, idempotency_key)
                );
                """
            )
        finally:
            conn.close()

    def register_session(
        self, conversation_id: str, tenant_id: str, principal_id: str, account: str
    ) -> None:
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(conversation_id):
            raise ChatGPTSessionRegistryError("INVALID_ARGUMENT", "invalid conversation id")
        if not isinstance(account, str) or not _ACCOUNT_ID.fullmatch(account):
            raise ChatGPTSessionRegistryError("DEPENDENCY_UNAVAILABLE", "CLI returned an invalid account")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT tenant_id, principal_id, account FROM chatgpt_session_owners "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["tenant_id"] == tenant_id
                    and row["principal_id"] == principal_id
                    and row["account"] == account
                ):
                    conn.execute("COMMIT")
                    return
                raise ChatGPTSessionRegistryError("CONFLICT", "conversation already has an owner")
            conn.execute(
                "INSERT INTO chatgpt_session_owners "
                "(conversation_id, tenant_id, principal_id, account, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, tenant_id, principal_id, account, _utc_now()),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_owner(self, conversation_id: str) -> dict[str, str] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT tenant_id, principal_id, account FROM chatgpt_session_owners "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def list_owned(self, tenant_id: str, principal_id: str) -> set[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT conversation_id FROM chatgpt_session_owners "
                "WHERE tenant_id = ? AND principal_id = ?",
                (tenant_id, principal_id),
            ).fetchall()
            return {row["conversation_id"] for row in rows}
        finally:
            conn.close()

    def begin_idempotent(
        self,
        tenant_id: str,
        principal_id: str,
        operation: str,
        key: str | None,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if key is None:
            return None
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_sha256, state, response_json FROM chatgpt_mcp_idempotency "
                "WHERE tenant_id = ? AND principal_id = ? AND operation = ? AND idempotency_key = ?",
                (tenant_id, principal_id, operation, key),
            ).fetchone()
            if row is not None:
                if row["payload_sha256"] != digest:
                    raise ChatGPTSessionRegistryError("CONFLICT", "idempotency key reused with different arguments")
                if row["state"] == "succeeded" and row["response_json"]:
                    conn.execute("COMMIT")
                    return json.loads(row["response_json"])
                raise ChatGPTSessionRegistryError(
                    "CONFLICT", "previous operation is still running or has an unknown outcome; do not repeat it"
                )
            conn.execute(
                "INSERT INTO chatgpt_mcp_idempotency "
                "(tenant_id, principal_id, operation, idempotency_key, payload_sha256, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (tenant_id, principal_id, operation, key, digest, _utc_now()),
            )
            conn.execute("COMMIT")
            return None
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def complete_idempotent(
        self, tenant_id: str, principal_id: str, operation: str, key: str | None, response: Mapping[str, Any]
    ) -> None:
        if key is None:
            return
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE chatgpt_mcp_idempotency SET state = 'succeeded', response_json = ?, updated_at = ? "
                "WHERE tenant_id = ? AND principal_id = ? AND operation = ? AND idempotency_key = ?",
                (
                    json.dumps(response, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    _utc_now(), tenant_id, principal_id, operation, key,
                ),
            )
        finally:
            conn.close()

    def mark_unknown(
        self, tenant_id: str, principal_id: str, operation: str, key: str | None
    ) -> None:
        if key is None:
            return
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE chatgpt_mcp_idempotency SET state = 'unknown', updated_at = ? "
                "WHERE tenant_id = ? AND principal_id = ? AND operation = ? AND idempotency_key = ? "
                "AND state = 'running'",
                (_utc_now(), tenant_id, principal_id, operation, key),
            )
        finally:
            conn.close()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%06dZ" % (
        int((time.time() % 1) * 1_000_000),
    )
