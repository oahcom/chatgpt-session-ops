"""Authorization layer for exposing WorkItem/Session state outside the process.

`work_items.WorkItemStore` owns identity and lifecycle but knows nothing about
who is asking. This module adds the missing authority dimension required before
any remote MCP tool may touch that state:

  * every WorkItem has exactly one owning (tenant, principal);
  * ownership is immutable, because a WorkItem's identity is immutable;
  * shared access exists only through explicit grants, never by inference;
  * a Session link must be backed by confirmed conversation identity and
    complete landing evidence (F-13), or it fails closed and nothing is written.

Only the standard library is used. The MCP layer must go through this module and
never call WorkItemStore directly.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.session_ops.work_items import (
    SessionLink,
    WorkItem,
    WorkItemStatus,
    WorkItemStore,
)

Scope = Literal["read", "write"]

SCOPES: tuple[Scope, ...] = ("read", "write")

# A confirmed ChatGPT conversation id is a canonical hyphenated UUID. Matching on
# shape is what rejects the identities the contract forbids: titles, prompt text,
# account ids ("acct3"), tab ids, and free-form guesses. Shape alone cannot tell
# a conversation id from some other UUID, which is why landing evidence is also
# required -- see require_landing_clearance().
CONVERSATION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

PRINCIPAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")

AUTHZ_DDL = """
CREATE TABLE IF NOT EXISTS work_item_owners (
    work_item_id TEXT PRIMARY KEY NOT NULL,
    tenant_id TEXT NOT NULL
        CHECK (length(trim(tenant_id)) > 0),
    owner_principal_id TEXT NOT NULL
        CHECK (length(trim(owner_principal_id)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (work_item_id)
        REFERENCES work_items(work_item_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS work_item_grants (
    work_item_id TEXT NOT NULL,
    principal_id TEXT NOT NULL
        CHECK (length(trim(principal_id)) > 0),
    scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    granted_at TEXT NOT NULL,
    PRIMARY KEY (work_item_id, principal_id, scope),
    FOREIGN KEY (work_item_id)
        REFERENCES work_items(work_item_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_work_item_owners_tenant
ON work_item_owners(tenant_id, owner_principal_id);

CREATE TRIGGER IF NOT EXISTS trg_work_item_owners_immutable
BEFORE UPDATE OF work_item_id, tenant_id, owner_principal_id
ON work_item_owners
BEGIN
    SELECT RAISE(ABORT, 'work item ownership is immutable');
END;
"""


class AuthorityError(Exception):
    """Base class for authorization failures."""


class InvalidPrincipal(AuthorityError):
    """Tenant or principal id is empty or malformed."""


class NotAuthorized(AuthorityError):
    """The caller holds no grant for this WorkItem and does not own it."""


class UnconfirmedSession(AuthorityError):
    """Session id is not a confirmed conversation id."""


class QuarantineNotCleared(AuthorityError):
    """Landing evidence is missing, mismatched, or incomplete."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Derived from a verified token, never from tool args."""

    tenant_id: str
    principal_id: str

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "principal_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not PRINCIPAL_ID_RE.match(value):
                raise InvalidPrincipal(
                    "%s must match %s, got %r"
                    % (field_name, PRINCIPAL_ID_RE.pattern, value)
                )


@dataclass(frozen=True)
class LandingEvidence:
    """What the sending path observed when the reply landed.

    `match` mirrors F-13: a 409 landing mismatch or an ambiguous candidate must
    never produce a SessionLink. `quarantine_evidence_complete` must be true
    even when match is "confirmed", because incomplete quarantine evidence means
    the outcome cannot be trusted.
    """

    conversation_id: str
    match: Literal["confirmed", "mismatch", "ambiguous"]
    quarantine_evidence_complete: bool


def validate_conversation_id(session_id: str) -> str:
    if not isinstance(session_id, str):
        raise UnconfirmedSession("session_id must be a string")
    if not CONVERSATION_ID_RE.match(session_id):
        raise UnconfirmedSession(
            "session_id is not a confirmed conversation id (canonical UUID shape"
            " required; titles, prompt text, account ids and device ids are"
            " rejected): %r" % (session_id,)
        )
    return session_id


def require_landing_clearance(
    expected_session_id: str, evidence: LandingEvidence | None
) -> None:
    """Fail closed unless the landing is confirmed and fully evidenced."""
    if evidence is None:
        raise QuarantineNotCleared(
            "landing evidence is required before a session can be linked"
        )
    if evidence.match != "confirmed":
        raise QuarantineNotCleared(
            "landing match is %r; only 'confirmed' may create a session link"
            % (evidence.match,)
        )
    if not evidence.quarantine_evidence_complete:
        raise QuarantineNotCleared(
            "quarantine evidence is incomplete; refusing to link"
        )
    if evidence.conversation_id != expected_session_id:
        raise QuarantineNotCleared(
            "landing evidence covers %r but the link targets %r"
            % (evidence.conversation_id, expected_session_id)
        )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"


class SecureWorkItemStore:
    """WorkItemStore plus ownership, grants and link preconditions."""

    def __init__(self, database_path: Path, store: WorkItemStore | None = None) -> None:
        self.database_path = Path(database_path)
        self._store = store or WorkItemStore(self.database_path)

    def initialize(self) -> None:
        self._store.initialize()
        conn = self._connect()
        try:
            conn.executescript(AUTHZ_DDL)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.database_path), isolation_level=None, timeout=5.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    # -- ownership ---------------------------------------------------------

    def _owner_row(self, conn: sqlite3.Connection, work_item_id: str):
        return conn.execute(
            "SELECT tenant_id, owner_principal_id FROM work_item_owners"
            " WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()

    def _scopes_for(
        self, conn: sqlite3.Connection, principal: Principal, work_item_id: str
    ) -> frozenset[str]:
        owner = self._owner_row(conn, work_item_id)
        if owner is None:
            return frozenset()
        if owner["tenant_id"] != principal.tenant_id:
            return frozenset()
        if owner["owner_principal_id"] == principal.principal_id:
            return frozenset(("read", "write"))
        rows = conn.execute(
            "SELECT scope FROM work_item_grants"
            " WHERE work_item_id = ? AND principal_id = ?",
            (work_item_id, principal.principal_id),
        ).fetchall()
        return frozenset(row["scope"] for row in rows)

    def _require(
        self, conn: sqlite3.Connection, principal: Principal, work_item_id: str,
        scope: Scope,
    ) -> None:
        granted = self._scopes_for(conn, principal, work_item_id)
        if scope in granted:
            return
        if not granted:
            # Same answer for "does not exist" and "not yours": a caller must not
            # be able to probe for the existence of another tenant's WorkItems.
            raise NotAuthorized("no access to work item %r" % (work_item_id,))
        raise NotAuthorized(
            "scope %r not granted on work item %r" % (scope, work_item_id)
        )

    # -- WorkItem operations ----------------------------------------------

    def create_work_item(self, principal: Principal) -> WorkItem:
        item = self._store.create_work_item()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO work_item_owners"
                    " (work_item_id, tenant_id, owner_principal_id, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (item.work_item_id, principal.tenant_id,
                     principal.principal_id, _utc_now()),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except BaseException:
            # Never leave an unowned WorkItem behind: an ownerless row would be
            # unreachable by every principal, including the one that made it.
            try:
                self._store.set_status(item.work_item_id, "cancelled")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return item

    def get_work_item(self, principal: Principal, work_item_id: str) -> WorkItem:
        conn = self._connect()
        try:
            self._require(conn, principal, work_item_id, "read")
        finally:
            conn.close()
        item = self._store.get_work_item(work_item_id)
        if item is None:
            raise NotAuthorized("no access to work item %r" % (work_item_id,))
        return item

    def list_work_items(
        self, principal: Principal, *, status: WorkItemStatus | None = None,
        limit: int = 50,
    ) -> list[WorkItem]:
        if limit < 1 or limit > 200:
            raise AuthorityError("limit must be between 1 and 200")
        conn = self._connect()
        try:
            sql = (
                "SELECT w.work_item_id, w.status, w.created_at, w.updated_at"
                " FROM work_items w"
                " LEFT JOIN work_item_owners o ON o.work_item_id = w.work_item_id"
                " LEFT JOIN work_item_grants g"
                "   ON g.work_item_id = w.work_item_id AND g.principal_id = ?"
                " WHERE o.tenant_id = ?"
                "   AND (o.owner_principal_id = ? OR g.scope = 'read')"
            )
            params: list[object] = [
                principal.principal_id, principal.tenant_id, principal.principal_id
            ]
            if status is not None:
                sql += " AND w.status = ?"
                params.append(status)
            sql += " ORDER BY w.updated_at DESC, w.work_item_id LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [
            WorkItem(
                work_item_id=row["work_item_id"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def set_status(
        self, principal: Principal, work_item_id: str, status: WorkItemStatus
    ) -> WorkItem:
        conn = self._connect()
        try:
            self._require(conn, principal, work_item_id, "write")
        finally:
            conn.close()
        return self._store.set_status(work_item_id, status)

    # -- grants -------------------------------------------------------------

    def grant(
        self, principal: Principal, work_item_id: str, target_principal_id: str,
        scope: Scope,
    ) -> None:
        if scope not in SCOPES:
            raise AuthorityError("unknown scope: %r" % (scope,))
        if not PRINCIPAL_ID_RE.match(target_principal_id or ""):
            raise InvalidPrincipal("invalid target principal %r"
                                   % (target_principal_id,))
        conn = self._connect()
        try:
            self._require(conn, principal, work_item_id, "write")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO work_item_grants"
                    " (work_item_id, principal_id, scope, granted_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(work_item_id, principal_id, scope) DO NOTHING",
                    (work_item_id, target_principal_id, scope, _utc_now()),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def revoke(
        self, principal: Principal, work_item_id: str, target_principal_id: str,
        scope: Scope,
    ) -> None:
        conn = self._connect()
        try:
            self._require(conn, principal, work_item_id, "write")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM work_item_grants"
                    " WHERE work_item_id = ? AND principal_id = ? AND scope = ?",
                    (work_item_id, target_principal_id, scope),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # -- Session operations -------------------------------------------------

    def link_session(
        self, principal: Principal, session_id: str, work_item_id: str,
        evidence: LandingEvidence | None,
    ) -> SessionLink:
        validate_conversation_id(session_id)
        conn = self._connect()
        try:
            self._require(conn, principal, work_item_id, "write")
        finally:
            conn.close()
        # Checked last so an unprivileged caller cannot use the error to learn
        # whether a given conversation already has a link.
        require_landing_clearance(session_id, evidence)
        return self._store.link_session(session_id, work_item_id)

    def unlink_session(self, principal: Principal, session_id: str) -> SessionLink | None:
        validate_conversation_id(session_id)
        existing = self._store.get_active_link(session_id)
        if existing is None:
            return None
        conn = self._connect()
        try:
            self._require(conn, principal, existing.work_item_id, "write")
        finally:
            conn.close()
        return self._store.unlink_session(session_id)

    def list_sessions(
        self, principal: Principal, work_item_id: str, *,
        include_unlinked: bool = False,
    ) -> list[SessionLink]:
        conn = self._connect()
        try:
            self._require(conn, principal, work_item_id, "read")
        finally:
            conn.close()
        return self._store.list_sessions(
            work_item_id, include_unlinked=include_unlinked
        )

    def get_active_link(self, principal: Principal, session_id: str) -> SessionLink | None:
        validate_conversation_id(session_id)
        link = self._store.get_active_link(session_id)
        if link is None:
            return None
        conn = self._connect()
        try:
            self._require(conn, principal, link.work_item_id, "read")
        except NotAuthorized:
            return None
        finally:
            conn.close()
        return link
