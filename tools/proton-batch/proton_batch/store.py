# -*- coding: utf-8 -*-
"""账号库：SQLite + 凭据加密（Windows DPAPI，其他平台降级为可识别前缀的 base64）。

设计要点：
- 每次操作独立连接，避免跨线程 sqlite 句柄问题。
- 更新字段走白名单，不接受调用方拼 SQL。
- 所有状态变更同时写 events 表，便于断点续跑与事后审计。
"""
from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import os
import sqlite3
import sys
import threading
from pathlib import Path

# ---------------------------------------------------------------- 凭据加解密

_DPAPI_AVAILABLE = sys.platform == "win32"


def _dpapi_call(func_name: str, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    fn = getattr(crypt32, func_name)
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError(f"{func_name} 失败: {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def encrypt_secret(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.encode("utf-8")
    if _DPAPI_AVAILABLE:
        try:
            return "v1dpapi:" + base64.b64encode(_dpapi_call("CryptProtectData", raw)).decode("ascii")
        except OSError:
            pass
    return "v1plain:" + base64.b64encode(raw).decode("ascii")


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("v1dpapi:"):
        blob = base64.b64decode(value[len("v1dpapi:"):])
        return _dpapi_call("CryptUnprotectData", blob).decode("utf-8")
    if value.startswith("v1plain:"):
        return base64.b64decode(value[len("v1plain:"):]).decode("utf-8")
    return value  # 兼容早期明文记录


# ---------------------------------------------------------------- 库

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    proton_username    TEXT UNIQUE,
    proton_email       TEXT,
    proton_password    TEXT,
    proton_user_id     TEXT,
    proton_status      TEXT NOT NULL DEFAULT 'new',
    proton_error       TEXT,
    chatgpt_email      TEXT,
    chatgpt_password   TEXT,
    chatgpt_status     TEXT NOT NULL DEFAULT 'none',
    chatgpt_account_id TEXT,
    chatgpt_session    TEXT,
    chatgpt_error      TEXT,
    proxy              TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proton_status ON accounts(proton_status);
CREATE INDEX IF NOT EXISTS idx_chatgpt_status ON accounts(chatgpt_status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    ts         TEXT NOT NULL,
    stage      TEXT NOT NULL,
    level      TEXT NOT NULL,
    message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id);
"""

UPDATABLE = {
    "proton_username", "proton_email", "proton_password", "proton_user_id",
    "proton_status", "proton_error", "chatgpt_email", "chatgpt_password",
    "chatgpt_status", "chatgpt_account_id", "chatgpt_session", "chatgpt_error",
    "proxy", "attempts",
    "proton_recovery", "recovery_status", "credential_status",
}


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | os.PathLike):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as conn:
            conn.executescript(SCHEMA)
            self._ensure_columns(conn)

    @staticmethod
    def _ensure_columns(conn) -> None:
        """增量补列：凭据完整性三件套（proton_status / recovery_status / credential_status）。"""
        have = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        for name, ddl in (
            ("proton_recovery", "TEXT"),
            ("recovery_status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("credential_status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ):
            if name not in have:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}")

    @contextlib.contextmanager
    def _db(self):
        """每次操作独立连接；退出时 commit 并 close（Windows 上必须关句柄）。"""
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 写

    def add_proton(self, username: str, email: str, password: str,
                   proxy: str | None = None) -> int:
        now = _now()
        with self._lock, self._db() as conn:
            cur = conn.execute(
                """INSERT INTO accounts
                   (proton_username, proton_email, proton_password, proton_status,
                    proxy, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
                (username, email, encrypt_secret(password), proxy, now, now),
            )
            account_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO events (account_id, ts, stage, level, message) VALUES (?,?,?,?,?)",
                (account_id, now, "proton", "info", f"创建本地记录 username={username}"),
            )
        return account_id

    def update(self, account_id: int, **fields) -> None:
        clean = {k: v for k, v in fields.items() if k in UPDATABLE}
        if not clean:
            return
        for key in ("proton_password", "chatgpt_password", "proton_recovery"):
            if key in clean and clean[key]:
                clean[key] = encrypt_secret(clean[key])
        clean["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in clean)
        with self._lock, self._db() as conn:
            conn.execute(f"UPDATE accounts SET {sets} WHERE id=?",
                         (*clean.values(), account_id))

    def event(self, account_id: int | None, stage: str, level: str, message: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO events (account_id, ts, stage, level, message) VALUES (?,?,?,?,?)",
                (account_id, _now(), stage, level, message),
            )

    # ------------------------------------------------------------ 读

    def get(self, account_id: int) -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def find_by_proton(self, username: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE proton_username=?", (username,)).fetchone()
        return dict(row) if row else None

    def list(self, proton_status: str | None = None,
             chatgpt_status: str | None = None, limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM accounts WHERE 1=1"
        args: list = []
        if proton_status:
            sql += " AND proton_status=?"
            args.append(proton_status)
        if chatgpt_status:
            sql += " AND chatgpt_status=?"
            args.append(chatgpt_status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._db() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def events(self, account_id: int, limit: int = 50) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE account_id=? ORDER BY id DESC LIMIT ?",
                (account_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._db() as conn:
            proton = {
                r["k"]: r["n"] for r in conn.execute(
                    "SELECT proton_status AS k, COUNT(*) AS n FROM accounts GROUP BY proton_status"
                ).fetchall()
            }
            chatgpt = {
                r["k"]: r["n"] for r in conn.execute(
                    "SELECT chatgpt_status AS k, COUNT(*) AS n FROM accounts GROUP BY chatgpt_status"
                ).fetchall()
            }
        return {"proton": proton, "chatgpt": chatgpt, "total": sum(proton.values())}

