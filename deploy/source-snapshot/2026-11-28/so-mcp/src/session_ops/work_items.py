"""SO-owned WorkItem identity, lifecycle and Session link persistence.

WorkItem identity and lifecycle are authoritative in Session Ops. This module
uses only the Python standard library and a local SQLite database.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

WorkItemStatus = Literal["active", "paused", "completed", "cancelled"]

STATUSES: tuple[WorkItemStatus, ...] = (
    "active",
    "paused",
    "completed",
    "cancelled",
)

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "cancelled"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"paused", "completed", "cancelled"}),
    "paused": frozenset({"active", "completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

WORK_ITEM_ID_PREFIX = "wi_"

# Single source of truth for the Session ID rule (issue #109). The secure
# authority layer imports this instead of defining its own copy, so the two
# layers cannot drift.
CONVERSATION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

DEFAULT_DATABASE_PATH = (
    Path.home() / ".workbuddy" / "session-ops" / "work-items.sqlite3"
)


class WorkItemError(Exception):
    """Base class for WorkItem persistence errors."""


class InvalidWorkItemId(WorkItemError):
    """WorkItem ID is not wi_<canonical-lowercase-UUIDv4>."""


class InvalidSessionId(WorkItemError):
    """Session ID is not a canonical lowercase UUID."""


class WorkItemNotFound(WorkItemError):
    """No WorkItem exists for the given ID."""


class InvalidTransition(WorkItemError):
    """Requested status transition is not allowed."""


class SessionAlreadyLinked(WorkItemError):
    """Session already has an active WorkItem link."""


@dataclass(frozen=True)
class WorkItem:
    work_item_id: str
    status: WorkItemStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SessionLink:
    link_id: str
    session_id: str
    work_item_id: str
    linked_at: str
    unlinked_at: str | None


DDL = """
CREATE TABLE IF NOT EXISTS work_items (
    work_item_id TEXT PRIMARY KEY NOT NULL,
    status TEXT NOT NULL
        CHECK (
            status IN (
                'active',
                'paused',
                'completed',
                'cancelled'
            )
        ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        length(work_item_id) = 39
        AND substr(work_item_id, 1, 3) = 'wi_'
    )
);

CREATE TABLE IF NOT EXISTS session_links (
    link_id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL
        CHECK (length(trim(session_id)) > 0),
    work_item_id TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    unlinked_at TEXT,
    FOREIGN KEY (work_item_id)
        REFERENCES work_items(work_item_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CHECK (
        unlinked_at IS NULL
        OR unlinked_at >= linked_at
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS
    ux_session_links_one_active_work_item_per_session
ON session_links(session_id)
WHERE unlinked_at IS NULL;

CREATE INDEX IF NOT EXISTS
    ix_session_links_work_item_active
ON session_links(work_item_id, unlinked_at);

CREATE INDEX IF NOT EXISTS
    ix_session_links_session_history
ON session_links(session_id, linked_at);

CREATE INDEX IF NOT EXISTS
    ix_work_items_status_updated
ON work_items(status, updated_at);

CREATE TRIGGER IF NOT EXISTS
    trg_work_items_id_immutable
BEFORE UPDATE OF work_item_id ON work_items
BEGIN
    SELECT RAISE(ABORT, 'work_item_id is immutable');
END;

CREATE TRIGGER IF NOT EXISTS
    trg_session_links_identity_immutable
BEFORE UPDATE OF link_id, session_id, work_item_id, linked_at
ON session_links
BEGIN
    SELECT RAISE(ABORT, 'session link identity is immutable');
END;
"""


def new_work_item_id() -> str:
    return WORK_ITEM_ID_PREFIX + str(uuid.uuid4())


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_work_item_id(work_item_id: str) -> str:
    """Validate wi_<canonical-lowercase-UUIDv4> and return it unchanged."""
    if not isinstance(work_item_id, str):
        raise InvalidWorkItemId("work_item_id must be a string")
    if not work_item_id.startswith(WORK_ITEM_ID_PREFIX):
        raise InvalidWorkItemId("work_item_id must start with 'wi_'")
    suffix = work_item_id[len(WORK_ITEM_ID_PREFIX):]
    try:
        parsed = uuid.UUID(suffix)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidWorkItemId("work_item_id suffix is not a UUID: %s" % exc)
    if parsed.version != 4:
        raise InvalidWorkItemId("work_item_id suffix is not a UUIDv4")
    if str(parsed) != suffix:
        raise InvalidWorkItemId("work_item_id suffix is not canonical lowercase")
    if len(work_item_id) != 39:
        raise InvalidWorkItemId("work_item_id must be 39 characters")
    return work_item_id


def validate_session_id(session_id: str) -> str:
    """Validate a canonical lowercase UUID Session ID.

    Shares CONVERSATION_ID_RE with the secure authority layer
    (validate_conversation_id) so the raw store and the MCP boundary accept
    exactly the same strings; only the exception type differs per layer.
    """
    if not isinstance(session_id, str) or not CONVERSATION_ID_RE.fullmatch(
        session_id
    ):
        raise InvalidSessionId("session_id must be a canonical lowercase UUID")
    return session_id


class WorkItemStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.database_path), isolation_level=None, timeout=5.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(DDL)
        finally:
            conn.close()

    def create_work_item(self) -> WorkItem:
        work_item_id = new_work_item_id()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO work_items"
                    " (work_item_id, status, created_at, updated_at)"
                    " VALUES (?, 'active', ?, ?)",
                    (work_item_id, now, now),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        return WorkItem(
            work_item_id=work_item_id,
            status="active",
            created_at=now,
            updated_at=now,
        )

    def get_work_item(self, work_item_id: str) -> WorkItem | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT work_item_id, status, created_at, updated_at"
                " FROM work_items WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return WorkItem(
            work_item_id=row["work_item_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def set_status(self, work_item_id: str, status: WorkItemStatus) -> WorkItem:
        validate_work_item_id(work_item_id)
        if status not in STATUSES:
            raise InvalidTransition("unknown status: %r" % (status,))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM work_items WHERE work_item_id = ?",
                    (work_item_id,),
                ).fetchone()
                if row is None:
                    raise WorkItemNotFound(
                        "unknown work item: %s" % work_item_id
                    )
                current = row["status"]
                if current != status:
                    if current in TERMINAL_STATUSES:
                        raise InvalidTransition(
                            "terminal work item %s cannot transition to %s"
                            % (work_item_id, status)
                        )
                    if status not in ALLOWED_TRANSITIONS[current]:
                        raise InvalidTransition(
                            "transition %s -> %s is not allowed"
                            % (current, status)
                        )
                    conn.execute(
                        "UPDATE work_items SET status = ?, updated_at = ?"
                        " WHERE work_item_id = ?",
                        (status, _utc_now(), work_item_id),
                    )
                stored = self._select_work_item(conn, work_item_id)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        return stored

    @staticmethod
    def _select_work_item(
        conn: sqlite3.Connection, work_item_id: str
    ) -> WorkItem:
        row = conn.execute(
            "SELECT work_item_id, status, created_at, updated_at"
            " FROM work_items WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        return WorkItem(
            work_item_id=row["work_item_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def link_session(self, session_id: str, work_item_id: str) -> SessionLink:
        validate_session_id(session_id)
        validate_work_item_id(work_item_id)
        link_id = "sl_" + str(uuid.uuid4())
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM work_items WHERE work_item_id = ?",
                    (work_item_id,),
                ).fetchone()
                if row is None:
                    raise WorkItemNotFound(
                        "unknown work item: %s" % work_item_id
                    )
                if row["status"] in TERMINAL_STATUSES:
                    raise InvalidTransition(
                        "terminal work item %s cannot receive a session link"
                        % work_item_id
                    )
                active = conn.execute(
                    "SELECT link_id FROM session_links"
                    " WHERE session_id = ? AND unlinked_at IS NULL",
                    (session_id,),
                ).fetchone()
                if active is not None:
                    raise SessionAlreadyLinked(
                        "session %s already has an active work item link"
                        % session_id
                    )
                try:
                    conn.execute(
                        "INSERT INTO session_links"
                        " (link_id, session_id, work_item_id, linked_at,"
                        " unlinked_at) VALUES (?, ?, ?, ?, NULL)",
                        (link_id, session_id, work_item_id, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise SessionAlreadyLinked(
                        "session %s already has an active work item link (%s)"
                        % (session_id, exc)
                    )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        return SessionLink(
            link_id=link_id,
            session_id=session_id,
            work_item_id=work_item_id,
            linked_at=now,
            unlinked_at=None,
        )

    def unlink_session(self, session_id: str) -> SessionLink | None:
        validate_session_id(session_id)
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT link_id, session_id, work_item_id, linked_at,"
                    " unlinked_at FROM session_links"
                    " WHERE session_id = ? AND unlinked_at IS NULL",
                    (session_id,),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "UPDATE session_links SET unlinked_at = ?"
                    " WHERE link_id = ? AND unlinked_at IS NULL",
                    (now, row["link_id"]),
                )
                updated = conn.execute(
                    "SELECT link_id, session_id, work_item_id, linked_at,"
                    " unlinked_at FROM session_links WHERE link_id = ?",
                    (row["link_id"],),
                ).fetchone()
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        return SessionLink(
            link_id=updated["link_id"],
            session_id=updated["session_id"],
            work_item_id=updated["work_item_id"],
            linked_at=updated["linked_at"],
            unlinked_at=updated["unlinked_at"],
        )

    def get_active_link(self, session_id: str) -> SessionLink | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT link_id, session_id, work_item_id, linked_at,"
                " unlinked_at FROM session_links"
                " WHERE session_id = ? AND unlinked_at IS NULL",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_link(row)

    def list_sessions(
        self,
        work_item_id: str,
        *,
        include_unlinked: bool = False,
    ) -> list[SessionLink]:
        conn = self._connect()
        try:
            sql = (
                "SELECT link_id, session_id, work_item_id, linked_at,"
                " unlinked_at FROM session_links WHERE work_item_id = ?"
            )
            if not include_unlinked:
                sql += " AND unlinked_at IS NULL"
            sql += " ORDER BY linked_at, link_id"
            rows = conn.execute(sql, (work_item_id,)).fetchall()
        finally:
            conn.close()
        return [self._row_to_link(row) for row in rows if row is not None]

    @staticmethod
    def _row_to_link(row: sqlite3.Row | None) -> SessionLink | None:
        if row is None:
            return None
        return SessionLink(
            link_id=row["link_id"],
            session_id=row["session_id"],
            work_item_id=row["work_item_id"],
            linked_at=row["linked_at"],
            unlinked_at=row["unlinked_at"],
        )
