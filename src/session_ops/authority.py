"""Transactional authorization boundary for WorkItem and Session resources.

Python 3.8+, standard library only. MCP handlers must call this module rather
than WorkItemStore directly.

Landing evidence is deliberately not accepted as a caller-created object.
The trusted Session Ops landing/send path records an outcome through the
private _record_trusted_landing() entry point. link_session() consumes that
record once, in the same transaction as the link mutation. Python privacy is
not a process security boundary: production must ensure only trusted landing
code can call that entry point and must protect the SQLite file from untrusted
writers.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple, Union

from src.session_ops.work_items import (
    ALLOWED_TRANSITIONS,
    STATUSES,
    InvalidTransition,
    SessionAlreadyLinked,
    SessionLink,
    TERMINAL_STATUSES,
    WorkItem,
    WorkItemStatus,
    WorkItemStore,
)

Scope = str
SCOPES = ("read", "write")

CONVERSATION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
PRINCIPAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")
WORK_ITEM_ID_RE = re.compile(r"wi_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
LINK_ID_RE = re.compile(r"sl_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 50
MAX_CURSOR_LENGTH = 2048
MAX_CONTEXT_ENTRIES = 6
MAX_CONTEXT_CONTENT_CHARS = 2048
MAX_CONTEXT_CONTENT_BYTES = 8192
MAX_QUERY_CHARS = 2048
MAX_QUERY_BYTES = 8192
MAX_HTTP_BODY_BYTES = 65536
MAX_MCP_RESPONSE_BYTES = 65536
DB_TIMEOUT_SECONDS = 3.0
ADAPTER_TIMEOUT_SECONDS = 8.0
REQUEST_TIMEOUT_SECONDS = 10.0


AUTHZ_DDL = """
CREATE TABLE IF NOT EXISTS work_item_owners (
    work_item_id TEXT PRIMARY KEY NOT NULL,
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    owner_principal_id TEXT NOT NULL
        CHECK (length(trim(owner_principal_id)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (work_item_id) REFERENCES work_items(work_item_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS work_item_grants (
    work_item_id TEXT NOT NULL,
    principal_id TEXT NOT NULL CHECK (length(trim(principal_id)) > 0),
    scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    granted_at TEXT NOT NULL,
    PRIMARY KEY (work_item_id, principal_id, scope),
    FOREIGN KEY (work_item_id) REFERENCES work_items(work_item_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS authority_landing_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL,
    match TEXT NOT NULL CHECK (match IN ('confirmed', 'mismatch', 'ambiguous')),
    evidence_complete INTEGER NOT NULL CHECK (evidence_complete IN (0, 1)),
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_work_item_owners_tenant
ON work_item_owners(tenant_id, owner_principal_id);

CREATE INDEX IF NOT EXISTS ix_work_item_grants_principal
ON work_item_grants(principal_id, work_item_id, scope);

CREATE INDEX IF NOT EXISTS ix_authority_landing_pending
ON authority_landing_events(session_id, consumed_at, created_at);

CREATE TABLE IF NOT EXISTS authority_audit_events (
    audit_id TEXT PRIMARY KEY NOT NULL,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    action TEXT NOT NULL,
    work_item_id TEXT,
    session_id TEXT,
    target_principal_id TEXT,
    scope TEXT,
    outcome TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_authority_audit_work_item
ON authority_audit_events(work_item_id, created_at, audit_id);

CREATE TRIGGER IF NOT EXISTS trg_work_item_owners_immutable
BEFORE UPDATE OF work_item_id, tenant_id, owner_principal_id
ON work_item_owners
BEGIN
    SELECT RAISE(ABORT, 'work item ownership is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_owners_no_delete
BEFORE DELETE ON work_item_owners
BEGIN
    SELECT RAISE(ABORT, 'work item ownership cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_authority_audit_no_update
BEFORE UPDATE ON authority_audit_events
BEGIN
    SELECT RAISE(ABORT, 'authority audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_authority_audit_no_delete
BEFORE DELETE ON authority_audit_events
BEGIN
    SELECT RAISE(ABORT, 'authority audit is append-only');
END;
"""


class AuthorityError(Exception):
    """Base exception for authority-layer failures."""


class InvalidPrincipal(AuthorityError):
    """A tenant or principal identifier is malformed."""


class InvalidIdentifier(AuthorityError):
    """A resource identifier is malformed."""


class NotAuthorized(AuthorityError):
    """A resource is not visible to the caller, or the required scope is absent.

    Deliberately has one fixed public message and details shape for missing,
    cross-tenant, and ungranted resources.
    """

    MESSAGE = "resource is not available"
    DETAILS = {}

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)
        self.code = "NOT_FOUND"
        self.details = {}


class UnconfirmedSession(AuthorityError):
    """The session identifier is not in the accepted canonical form."""


class QuarantineNotCleared(AuthorityError):
    """No valid, complete, unused trusted landing record exists."""


class PrincipalDirectoryUnavailable(AuthorityError):
    """The configured principal directory cannot validate a grant target."""


@dataclass(frozen=True)
class Principal:
    """Authenticated principal, constructed from verified identity claims."""

    tenant_id: str
    principal_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not PRINCIPAL_ID_RE.fullmatch(
            self.tenant_id
        ):
            raise InvalidPrincipal("tenant_id is malformed")
        if not isinstance(self.principal_id, str) or not PRINCIPAL_ID_RE.fullmatch(
            self.principal_id
        ):
            raise InvalidPrincipal("principal_id is malformed")


@dataclass(frozen=True)
class PrincipalRecord:
    """Trusted directory record for a grant recipient."""

    principal_id: str
    tenant_id: str
    enabled: bool = True


class PrincipalDirectory(Protocol):
    """Resolve a principal from a trusted identity directory."""

    def resolve(self, principal_id: str) -> Optional[PrincipalRecord]:
        ...


class DenyAllPrincipalDirectory:
    """Secure default: no principal can be granted until a directory is wired."""

    def resolve(self, principal_id: str) -> Optional[PrincipalRecord]:
        return None


class StaticPrincipalDirectory:
    """Small in-memory directory useful for tests and controlled deployments."""

    def __init__(self, records: Iterable[PrincipalRecord]) -> None:
        self._records = {record.principal_id: record for record in records}

    def resolve(self, principal_id: str) -> Optional[PrincipalRecord]:
        return self._records.get(principal_id)


class Page(list):
    """List-compatible page with an opaque keyset continuation cursor."""

    def __init__(self, values: Iterable[Any] = (), next_cursor: Optional[str] = None):
        super().__init__(values)
        self.next_cursor = next_cursor


@dataclass(frozen=True)
class OrphanReport:
    """Historical WorkItem rows that have no owner record."""

    work_item_ids: Tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.work_item_ids)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%06dZ" % (
        int((time.time() % 1) * 1000000),
    )


def _validate_principal_id(value: str, field: str = "principal_id") -> str:
    if not isinstance(value, str) or not PRINCIPAL_ID_RE.fullmatch(value):
        raise InvalidPrincipal("%s is malformed" % field)
    return value


def validate_conversation_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not CONVERSATION_ID_RE.fullmatch(session_id):
        raise UnconfirmedSession("session_id must be a canonical lowercase UUID")
    return session_id


def validate_work_item_id(work_item_id: str) -> str:
    if not isinstance(work_item_id, str) or not WORK_ITEM_ID_RE.fullmatch(work_item_id):
        raise InvalidIdentifier("work_item_id is malformed")
    return work_item_id


def validate_link_id(link_id: str) -> str:
    if not isinstance(link_id, str) or not LINK_ID_RE.fullmatch(link_id):
        raise InvalidIdentifier("link_id is malformed")
    return link_id


def _validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise AuthorityError("scope must be 'read' or 'write'")
    return scope


def _validate_page_size(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise AuthorityError("page size must be an integer")
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise AuthorityError("page size must be between 1 and 50")
    return limit


def _encode_cursor(payload: Dict[str, str]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    value = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(value) > MAX_CURSOR_LENGTH:
        raise AuthorityError("cursor exceeds 2048 characters")
    return value


def _decode_cursor(cursor: Optional[str], expected_kind: str) -> Optional[Dict[str, str]]:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise AuthorityError("cursor is invalid or exceeds 2048 characters")
    try:
        padded = cursor + ("=" * ((4 - len(cursor) % 4) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, TypeError):
        raise AuthorityError("cursor is invalid")
    if not isinstance(value, dict) or value.get("kind") != expected_kind:
        raise AuthorityError("cursor is invalid")
    if set(value) != ({"kind", "updated_at", "work_item_id"}
                      if expected_kind == "work_items"
                      else {"kind", "linked_at", "link_id"}):
        raise AuthorityError("cursor is invalid")
    if not all(isinstance(v, str) for v in value.values()):
        raise AuthorityError("cursor is invalid")
    return value


def _row_to_work_item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        work_item_id=row["work_item_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_session_link(row: sqlite3.Row) -> SessionLink:
    return SessionLink(
        link_id=row["link_id"],
        session_id=row["session_id"],
        work_item_id=row["work_item_id"],
        linked_at=row["linked_at"],
        unlinked_at=row["unlinked_at"],
    )


class SecureWorkItemStore:
    """WorkItemStore plus transactional ownership, grants, links and audit."""

    def __init__(
        self,
        database_path: Path,
        store: Optional[WorkItemStore] = None,
        principal_directory: Optional[PrincipalDirectory] = None,
    ) -> None:
        self.database_path = Path(database_path)
        self._store = store or WorkItemStore(self.database_path)
        self._principal_directory = principal_directory or DenyAllPrincipalDirectory()

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
            str(self.database_path),
            isolation_level=None,
            timeout=DB_TIMEOUT_SECONDS,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 3000")
        return conn

    def audit_orphans(self) -> OrphanReport:
        """Report pre-existing WorkItems with no owner; never deletes or repairs."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT w.work_item_id FROM work_items AS w "
                "LEFT JOIN work_item_owners AS o "
                "ON o.work_item_id = w.work_item_id "
                "WHERE o.work_item_id IS NULL ORDER BY w.work_item_id"
            ).fetchall()
            return OrphanReport(tuple(row["work_item_id"] for row in rows))
        finally:
            conn.close()

    def _audit(
        self,
        conn: sqlite3.Connection,
        principal: Principal,
        action: str,
        work_item_id: Optional[str] = None,
        session_id: Optional[str] = None,
        target_principal_id: Optional[str] = None,
        scope: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        conn.execute(
            "INSERT INTO authority_audit_events "
            "(audit_id, tenant_id, principal_id, action, work_item_id, "
            "session_id, target_principal_id, scope, outcome, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?)",
            (
                "ae_" + str(uuid.uuid4()),
                principal.tenant_id,
                principal.principal_id,
                action,
                work_item_id,
                session_id,
                target_principal_id,
                scope,
                request_id,
                _utc_now(),
            ),
        )

    def list_audit_events(self, work_item_id: str) -> List[Dict[str, Any]]:
        """Administrative/test inspection helper; not intended as a remote tool."""
        validate_work_item_id(work_item_id)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT audit_id, tenant_id, principal_id, action, work_item_id, "
                "session_id, target_principal_id, scope, outcome, request_id, created_at "
                "FROM authority_audit_events WHERE work_item_id = ? "
                "ORDER BY created_at, audit_id",
                (work_item_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _require(
        self,
        conn: sqlite3.Connection,
        principal: Principal,
        work_item_id: str,
        scope: Scope,
    ) -> None:
        row = conn.execute(
            "SELECT o.tenant_id, o.owner_principal_id "
            "FROM work_item_owners AS o "
            "JOIN work_items AS w ON w.work_item_id = o.work_item_id "
            "WHERE o.work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        if row is None or row["tenant_id"] != principal.tenant_id:
            raise NotAuthorized()
        if row["owner_principal_id"] == principal.principal_id:
            return
        grant = conn.execute(
            "SELECT 1 FROM work_item_grants "
            "WHERE work_item_id = ? AND principal_id = ? AND scope = ?",
            (work_item_id, principal.principal_id, scope),
        ).fetchone()
        if grant is None:
            raise NotAuthorized()

    def _require_owner(
        self, conn: sqlite3.Connection, principal: Principal, work_item_id: str
    ) -> None:
        row = conn.execute(
            "SELECT tenant_id, owner_principal_id FROM work_item_owners "
            "WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        if (
            row is None
            or row["tenant_id"] != principal.tenant_id
            or row["owner_principal_id"] != principal.principal_id
        ):
            raise NotAuthorized()

    def _resolve_grant_target(self, principal: Principal, target: str) -> PrincipalRecord:
        _validate_principal_id(target, "target_principal_id")
        try:
            record = self._principal_directory.resolve(target)
        except Exception as exc:
            raise PrincipalDirectoryUnavailable("principal directory lookup failed") from exc
        if (
            record is None
            or not record.enabled
            or record.principal_id != target
            or record.tenant_id != principal.tenant_id
        ):
            raise InvalidPrincipal("grant target is unknown, disabled, or outside tenant")
        return record

    # -- WorkItem operations -------------------------------------------------

    def create_work_item(self, principal: Principal) -> WorkItem:
        """Create WorkItem and owner row atomically; no intermediate orphan."""
        now = _utc_now()
        work_item_id = "wi_" + str(uuid.uuid4())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO work_items(work_item_id, status, created_at, updated_at) "
                "VALUES (?, 'active', ?, ?)",
                (work_item_id, now, now),
            )
            conn.execute(
                "INSERT INTO work_item_owners"
                "(work_item_id, tenant_id, owner_principal_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (work_item_id, principal.tenant_id, principal.principal_id, now),
            )
            self._audit(conn, principal, "work_item.create", work_item_id=work_item_id)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return WorkItem(work_item_id, "active", now, now)

    def get_work_item(self, principal: Principal, work_item_id: str) -> WorkItem:
        validate_work_item_id(work_item_id)
        conn = self._connect()
        try:
            self._require(conn, principal, work_item_id, "read")
            row = conn.execute(
                "SELECT work_item_id, status, created_at, updated_at "
                "FROM work_items WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if row is None:
                raise NotAuthorized()
            return _row_to_work_item(row)
        finally:
            conn.close()

    def list_work_items(
        self,
        principal: Principal,
        *,
        status: Optional[WorkItemStatus] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
        page_size: Optional[int] = None,
    ) -> Page:
        """Keyset-paginated visible WorkItems; legacy limit remains supported."""
        if page_size is not None:
            if limit != DEFAULT_PAGE_SIZE:
                raise AuthorityError("specify either limit or page_size, not both")
            limit = page_size
        limit = _validate_page_size(limit)
        if status is not None and status not in STATUSES:
            raise AuthorityError("unknown WorkItem status")
        decoded = _decode_cursor(cursor, "work_items")
        conn = self._connect()
        try:
            sql = (
                "SELECT w.work_item_id, w.status, w.created_at, w.updated_at "
                "FROM work_items AS w "
                "JOIN work_item_owners AS o ON o.work_item_id = w.work_item_id "
                "WHERE o.tenant_id = ? AND (o.owner_principal_id = ? OR EXISTS "
                "(SELECT 1 FROM work_item_grants AS g "
                "WHERE g.work_item_id = w.work_item_id "
                "AND g.principal_id = ? AND g.scope = 'read'))"
            )
            params: List[Any] = [
                principal.tenant_id, principal.principal_id, principal.principal_id
            ]
            if status is not None:
                sql += " AND w.status = ?"
                params.append(status)
            if decoded is not None:
                sql += " AND (w.updated_at < ? OR "
                sql += "(w.updated_at = ? AND w.work_item_id > ?))"
                params.extend(
                    [decoded["updated_at"], decoded["updated_at"], decoded["work_item_id"]]
                )
            sql += " ORDER BY w.updated_at DESC, w.work_item_id ASC LIMIT ?"
            params.append(limit + 1)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                {
                    "kind": "work_items",
                    "updated_at": last["updated_at"],
                    "work_item_id": last["work_item_id"],
                }
            )
        return Page((_row_to_work_item(row) for row in visible), next_cursor)

    def set_status(
        self,
        principal: Principal,
        work_item_id: str,
        status: WorkItemStatus,
        expected_updated_at: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> WorkItem:
        validate_work_item_id(work_item_id)
        if status not in STATUSES:
            raise InvalidTransition("unknown status: %r" % (status,))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._require(conn, principal, work_item_id, "write")
            row = conn.execute(
                "SELECT status, updated_at FROM work_items WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if row is None:
                raise NotAuthorized()
            if expected_updated_at is not None and row["updated_at"] != expected_updated_at:
                raise AuthorityError("work item has changed; refresh and retry")
            current = row["status"]
            if current != status:
                if current in TERMINAL_STATUSES:
                    raise InvalidTransition(
                        "terminal work item %s cannot transition to %s"
                        % (work_item_id, status)
                    )
                if status not in ALLOWED_TRANSITIONS[current]:
                    raise InvalidTransition(
                        "transition %s -> %s is not allowed" % (current, status)
                    )
                conn.execute(
                    "UPDATE work_items SET status = ?, updated_at = ? "
                    "WHERE work_item_id = ?",
                    (status, _utc_now(), work_item_id),
                )
            self._audit(
                conn, principal, "work_item.update", work_item_id=work_item_id,
                request_id=request_id,
            )
            stored = conn.execute(
                "SELECT work_item_id, status, created_at, updated_at "
                "FROM work_items WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            conn.execute("COMMIT")
            return _row_to_work_item(stored)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- Grants --------------------------------------------------------------

    def grant(
        self,
        principal: Principal,
        work_item_id: str,
        target_principal_id: str,
        scope: Scope,
        request_id: Optional[str] = None,
    ) -> None:
        validate_work_item_id(work_item_id)
        _validate_scope(scope)
        self._resolve_grant_target(principal, target_principal_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, principal, work_item_id)
            conn.execute(
                "INSERT INTO work_item_grants"
                "(work_item_id, principal_id, scope, granted_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(work_item_id, principal_id, scope) DO NOTHING",
                (work_item_id, target_principal_id, scope, _utc_now()),
            )
            self._audit(
                conn, principal, "work_item.grant", work_item_id=work_item_id,
                target_principal_id=target_principal_id, scope=scope,
                request_id=request_id,
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def revoke(
        self,
        principal: Principal,
        work_item_id: str,
        target_principal_id: str,
        scope: Scope,
        request_id: Optional[str] = None,
    ) -> None:
        validate_work_item_id(work_item_id)
        _validate_scope(scope)
        _validate_principal_id(target_principal_id, "target_principal_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, principal, work_item_id)
            conn.execute(
                "DELETE FROM work_item_grants "
                "WHERE work_item_id = ? AND principal_id = ? AND scope = ?",
                (work_item_id, target_principal_id, scope),
            )
            self._audit(
                conn, principal, "work_item.revoke", work_item_id=work_item_id,
                target_principal_id=target_principal_id, scope=scope,
                request_id=request_id,
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- Trusted landing evidence and Session links -------------------------

    def _record_trusted_landing(
        self,
        session_id: str,
        match: str,
        evidence_complete: bool,
    ) -> str:
        """Internal trusted-sender hook; do not expose as an MCP operation.

        The caller must be the real landing/send pipeline after it has verified
        the actual conversation identity and F-13 quarantine evidence.
        """
        validate_conversation_id(session_id)
        if match not in ("confirmed", "mismatch", "ambiguous"):
            raise QuarantineNotCleared("unknown landing match result")
        if not isinstance(evidence_complete, bool):
            raise QuarantineNotCleared("evidence_complete must be boolean")
        event_id = "le_" + str(uuid.uuid4())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO authority_landing_events"
                "(event_id, session_id, match, evidence_complete, created_at, consumed_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (event_id, session_id, match, int(evidence_complete), _utc_now()),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return event_id

    def link_session(
        self,
        principal: Principal,
        session_id: str,
        work_item_id: str,
        evidence: Any = None,
        request_id: Optional[str] = None,
    ) -> SessionLink:
        """Link using a pending trusted landing event, never caller evidence.

        `evidence` remains only for source compatibility. Supplying any value
        is rejected; old caller-created LandingEvidence objects are not trusted.
        """
        validate_conversation_id(session_id)
        validate_work_item_id(work_item_id)
        if evidence is not None:
            raise QuarantineNotCleared(
                "caller-supplied landing evidence is not accepted"
            )
        conn = self._connect()
        link_id = "sl_" + str(uuid.uuid4())
        now = _utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._require(conn, principal, work_item_id, "write")
            item = conn.execute(
                "SELECT status FROM work_items WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if item is None:
                raise NotAuthorized()
            if item["status"] in TERMINAL_STATUSES:
                raise InvalidTransition("terminal WorkItem cannot receive a Session link")
            landing = conn.execute(
                "SELECT event_id, match, evidence_complete "
                "FROM authority_landing_events "
                "WHERE session_id = ? AND consumed_at IS NULL "
                "ORDER BY created_at DESC, event_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if (
                landing is None
                or landing["match"] != "confirmed"
                or landing["evidence_complete"] != 1
            ):
                raise QuarantineNotCleared(
                    "a confirmed, complete, unused landing record is required"
                )
            conn.execute(
                "UPDATE authority_landing_events SET consumed_at = ? "
                "WHERE event_id = ? AND consumed_at IS NULL",
                (now, landing["event_id"]),
            )
            conn.execute(
                "INSERT INTO session_links"
                "(link_id, session_id, work_item_id, linked_at, unlinked_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (link_id, session_id, work_item_id, now),
            )
            self._audit(
                conn, principal, "session.link", work_item_id=work_item_id,
                session_id=session_id, request_id=request_id,
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise SessionAlreadyLinked("session already has an active WorkItem link") from exc
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return SessionLink(link_id, session_id, work_item_id, now, None)

    def unlink_session(
        self,
        principal: Principal,
        session_id: str,
        expected_link_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Optional[SessionLink]:
        """Unlink atomically; absent and unauthorized links both return None."""
        validate_conversation_id(session_id)
        if expected_link_id is not None:
            validate_link_id(expected_link_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT link_id, session_id, work_item_id, linked_at, unlinked_at "
                "FROM session_links WHERE session_id = ? AND unlinked_at IS NULL",
                (session_id,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            if expected_link_id is not None and row["link_id"] != expected_link_id:
                conn.execute("COMMIT")
                return None
            try:
                self._require(conn, principal, row["work_item_id"], "write")
            except NotAuthorized:
                conn.execute("COMMIT")
                return None
            now = _utc_now()
            changed = conn.execute(
                "UPDATE session_links SET unlinked_at = ? "
                "WHERE link_id = ? AND unlinked_at IS NULL",
                (now, row["link_id"]),
            ).rowcount
            if changed != 1:
                raise AuthorityError("active link changed during unlink")
            self._audit(
                conn, principal, "session.unlink",
                work_item_id=row["work_item_id"], session_id=session_id,
                request_id=request_id,
            )
            conn.execute("COMMIT")
            return SessionLink(
                row["link_id"], row["session_id"], row["work_item_id"],
                row["linked_at"], now,
            )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def list_sessions(
        self,
        principal: Principal,
        work_item_id: str,
        *,
        include_unlinked: bool = False,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
        page_size: Optional[int] = None,
    ) -> Page:
        validate_work_item_id(work_item_id)
        if page_size is not None:
            if limit != DEFAULT_PAGE_SIZE:
                raise AuthorityError("specify either limit or page_size, not both")
            limit = page_size
        limit = _validate_page_size(limit)
        decoded = _decode_cursor(cursor, "sessions")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            self._require(conn, principal, work_item_id, "read")
            sql = (
                "SELECT link_id, session_id, work_item_id, linked_at, unlinked_at "
                "FROM session_links WHERE work_item_id = ?"
            )
            params: List[Any] = [work_item_id]
            if not include_unlinked:
                sql += " AND unlinked_at IS NULL"
            if decoded is not None:
                sql += " AND (linked_at > ? OR (linked_at = ? AND link_id > ?))"
                params.extend(
                    [decoded["linked_at"], decoded["linked_at"], decoded["link_id"]]
                )
            sql += " ORDER BY linked_at ASC, link_id ASC LIMIT ?"
            params.append(limit + 1)
            rows = conn.execute(sql, params).fetchall()
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                {
                    "kind": "sessions",
                    "linked_at": last["linked_at"],
                    "link_id": last["link_id"],
                }
            )
        return Page((_row_to_session_link(row) for row in visible), next_cursor)

    def get_active_link(
        self, principal: Principal, session_id: str
    ) -> Optional[SessionLink]:
        """Return a visible active link; missing and invisible both return None."""
        validate_conversation_id(session_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT link_id, session_id, work_item_id, linked_at, unlinked_at "
                "FROM session_links WHERE session_id = ? AND unlinked_at IS NULL",
                (session_id,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            try:
                self._require(conn, principal, row["work_item_id"], "read")
            except NotAuthorized:
                conn.execute("COMMIT")
                return None
            result = _row_to_session_link(row)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
