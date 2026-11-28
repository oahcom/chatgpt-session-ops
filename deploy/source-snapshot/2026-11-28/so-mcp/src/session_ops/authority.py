"""Transactional authorization boundary for WorkItem and Session resources.

Python 3.11+, standard library only. MCP handlers must call this module rather
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
    CONVERSATION_ID_RE,
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
    work_item_id TEXT,
    attempt_id TEXT,
    match TEXT NOT NULL
        CHECK (match IN ('confirmed', 'mismatch', 'ambiguous')),
    evidence_complete INTEGER NOT NULL
        CHECK (evidence_complete IN (0, 1)),
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    rejected_at TEXT,
    rejection_reason TEXT,
    CHECK (
        (work_item_id IS NULL AND attempt_id IS NULL)
        OR
        (
            work_item_id IS NOT NULL
            AND length(trim(work_item_id)) > 0
            AND attempt_id IS NOT NULL
            AND length(trim(attempt_id)) > 0
        )
    ),
    CHECK (
        (rejected_at IS NULL AND rejection_reason IS NULL)
        OR
        (
            rejected_at IS NOT NULL
            AND rejection_reason IS NOT NULL
            AND length(trim(rejection_reason)) > 0
        )
    ),
    CHECK (consumed_at IS NULL OR rejected_at IS NULL)
);

CREATE INDEX IF NOT EXISTS ix_authority_landing_pending
ON authority_landing_events(session_id, consumed_at, created_at);

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
    link_id TEXT,
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
    """Validate a canonical lowercase UUID Session ID at the MCP boundary.

    Uses the same CONVERSATION_ID_RE rule as the raw store's
    work_items.validate_session_id (issue #109). Only the exception type
    differs, because the secure layer maps it to an MCP error.
    """
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
            audit_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(authority_audit_events)"
                ).fetchall()
            }
            if "link_id" not in audit_columns:
                conn.execute(
                    "ALTER TABLE authority_audit_events ADD COLUMN link_id TEXT"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_authority_audit_link "
                "ON authority_audit_events(link_id, action, created_at)"
            )

            # -- Trusted landing evidence identity migration ----------------
            #
            # Keep this migration separate from the terminal-link
            # reconciliation transaction below. It has its own atomic
            # boundary so a failed migration cannot leave a partially
            # upgraded landing-evidence schema.
            #
            # Legacy pending confirmed events without identity cannot be
            # safely attributed to a WorkItem or execution attempt.
            # Preserve them, but downgrade them to non-consumable ambiguous
            # evidence. Never infer an identity from session_links.

            conn.execute("BEGIN IMMEDIATE")
            try:
                def _landing_migration_snapshot():
                    total = conn.execute(
                        "SELECT COUNT(*) FROM authority_landing_events"
                    ).fetchone()[0]

                    distributions = {
                        (row["match"], row["consumed"]): row["count"]
                        for row in conn.execute(
                            "SELECT match, "
                            "CASE WHEN consumed_at IS NULL THEN 0 ELSE 1 END "
                            "AS consumed, COUNT(*) AS count "
                            "FROM authority_landing_events "
                            "GROUP BY match, consumed "
                            "ORDER BY match, consumed"
                        ).fetchall()
                    }

                    events_with_link = conn.execute(
                        "SELECT COUNT(*) FROM authority_landing_events AS e "
                        "WHERE e.consumed_at IS NOT NULL "
                        "AND EXISTS ("
                        "SELECT 1 FROM session_links AS s "
                        "WHERE s.session_id = e.session_id "
                        "AND s.linked_at = e.consumed_at"
                        ")"
                    ).fetchone()[0]

                    links_with_event = conn.execute(
                        "SELECT COUNT(*) FROM session_links AS s "
                        "WHERE EXISTS ("
                        "SELECT 1 FROM authority_landing_events AS e "
                        "WHERE e.session_id = s.session_id "
                        "AND e.consumed_at = s.linked_at"
                        ")"
                    ).fetchone()[0]

                    return {
                        "total": total,
                        "distributions": distributions,
                        "events_with_link": events_with_link,
                        "links_with_event": links_with_event,
                    }

                before = _landing_migration_snapshot()

                landing_columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(authority_landing_events)"
                    ).fetchall()
                }

                # Preserve the existing PRAGMA -> ALTER -> CREATE INDEX
                # migration convention. All changes are inside BEGIN IMMEDIATE.
                if "work_item_id" not in landing_columns:
                    conn.execute(
                        "ALTER TABLE authority_landing_events "
                        "ADD COLUMN work_item_id TEXT"
                    )

                if "attempt_id" not in landing_columns:
                    conn.execute(
                        "ALTER TABLE authority_landing_events "
                        "ADD COLUMN attempt_id TEXT"
                    )

                if "rejected_at" not in landing_columns:
                    conn.execute(
                        "ALTER TABLE authority_landing_events "
                        "ADD COLUMN rejected_at TEXT"
                    )

                if "rejection_reason" not in landing_columns:
                    conn.execute(
                        "ALTER TABLE authority_landing_events "
                        "ADD COLUMN rejection_reason TEXT"
                    )

                # Reject malformed partial identities rather than guessing
                # which side of an identity pair is authoritative.
                malformed_identity = conn.execute(
                    "SELECT COUNT(*) FROM authority_landing_events "
                    "WHERE (work_item_id IS NULL AND attempt_id IS NOT NULL) "
                    "OR (work_item_id IS NOT NULL AND attempt_id IS NULL) "
                    "OR (work_item_id IS NOT NULL "
                    "AND length(trim(work_item_id)) = 0) "
                    "OR (attempt_id IS NOT NULL "
                    "AND length(trim(attempt_id)) = 0)"
                ).fetchone()[0]

                if malformed_identity:
                    raise AuthorityError(
                        "landing evidence contains a partial or malformed identity"
                    )

                malformed_state = conn.execute(
                    "SELECT COUNT(*) FROM authority_landing_events "
                    "WHERE (consumed_at IS NOT NULL AND rejected_at IS NOT NULL) "
                    "OR (rejected_at IS NULL AND rejection_reason IS NOT NULL) "
                    "OR (rejected_at IS NOT NULL "
                    "AND (rejection_reason IS NULL "
                    "OR length(trim(rejection_reason)) = 0))"
                ).fetchone()[0]

                if malformed_state:
                    raise AuthorityError(
                        "landing evidence contains an inconsistent consumption state"
                    )

                # Count and downgrade only legacy-format pending confirmed
                # events. Identity-bearing events created by the new
                # producer are never modified by subsequent initialization.
                legacy_pending_count = conn.execute(
                    "SELECT COUNT(*) FROM authority_landing_events "
                    "WHERE work_item_id IS NULL "
                    "AND attempt_id IS NULL "
                    "AND consumed_at IS NULL "
                    "AND rejected_at IS NULL "
                    "AND match = 'confirmed'"
                ).fetchone()[0]

                changed = conn.execute(
                    "UPDATE authority_landing_events "
                    "SET match = 'ambiguous', evidence_complete = 0 "
                    "WHERE work_item_id IS NULL "
                    "AND attempt_id IS NULL "
                    "AND consumed_at IS NULL "
                    "AND rejected_at IS NULL "
                    "AND match = 'confirmed'"
                ).rowcount

                if changed != legacy_pending_count:
                    raise AuthorityError(
                        "landing evidence migration changed an unexpected row count"
                    )

                remaining_legacy_pending = conn.execute(
                    "SELECT COUNT(*) FROM authority_landing_events "
                    "WHERE work_item_id IS NULL "
                    "AND attempt_id IS NULL "
                    "AND consumed_at IS NULL "
                    "AND rejected_at IS NULL "
                    "AND match = 'confirmed'"
                ).fetchone()[0]

                if remaining_legacy_pending:
                    raise AuthorityError(
                        "legacy pending confirmed landing evidence remains"
                    )

                # Use a new index name: an existing database may already
                # have ix_authority_landing_pending with the old definition.
                # Do not DROP or rebuild the existing index.
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_authority_landing_pending_identity "
                    "ON authority_landing_events("
                    "work_item_id, attempt_id, session_id, "
                    "consumed_at, rejected_at, created_at, event_id)"
                )

                after = _landing_migration_snapshot()

                # Total event count must never change.
                if after["total"] != before["total"]:
                    raise AuthorityError(
                        "landing evidence migration changed total event count"
                    )

                # The only permitted distribution change is:
                # pending confirmed -> pending ambiguous.
                expected_distributions = dict(before["distributions"])

                if legacy_pending_count:
                    confirmed_key = ("confirmed", 0)
                    ambiguous_key = ("ambiguous", 0)

                    expected_distributions[confirmed_key] = (
                        expected_distributions.get(confirmed_key, 0)
                        - legacy_pending_count
                    )
                    expected_distributions[ambiguous_key] = (
                        expected_distributions.get(ambiguous_key, 0)
                        + legacy_pending_count
                    )

                    if expected_distributions.get(confirmed_key) == 0:
                        expected_distributions.pop(confirmed_key)

                if after["distributions"] != expected_distributions:
                    raise AuthorityError(
                        "landing evidence migration changed unexpected match/consumption distribution"
                    )

                # Historical consumed-event <-> session-link relationships
                # must remain unchanged. In particular, consumed_at and
                # historical linked_at values must not be rewritten.
                if (
                    after["events_with_link"] != before["events_with_link"]
                    or after["links_with_event"] != before["links_with_event"]
                ):
                    raise AuthorityError(
                        "landing evidence migration changed historical session-link correspondence"
                    )

                # Verify the legacy downgrade did not accidentally populate
                # identity or rejection metadata.
                invalid_legacy_rows = conn.execute(
                    "SELECT COUNT(*) FROM authority_landing_events "
                    "WHERE match = 'ambiguous' "
                    "AND evidence_complete = 0 "
                    "AND work_item_id IS NULL "
                    "AND attempt_id IS NULL "
                    "AND consumed_at IS NULL "
                    "AND rejected_at IS NULL "
                    "AND rejection_reason IS NULL"
                ).fetchone()[0]

                if invalid_legacy_rows < legacy_pending_count:
                    raise AuthorityError(
                        "landing evidence migration failed legacy-row verification"
                    )

                conn.execute("COMMIT")

            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("BEGIN IMMEDIATE")
            stale_links = conn.execute(
                "SELECT s.link_id, s.session_id, s.work_item_id "
                "FROM session_links AS s "
                "JOIN work_items AS w ON w.work_item_id = s.work_item_id "
                "WHERE w.status IN ('completed', 'cancelled') "
                "AND s.unlinked_at IS NULL "
                "ORDER BY s.work_item_id, s.linked_at, s.link_id"
            ).fetchall()
            for link in stale_links:
                changed_at = _utc_now()
                changed = conn.execute(
                    "UPDATE session_links SET unlinked_at = ? "
                    "WHERE link_id = ? AND unlinked_at IS NULL",
                    (changed_at, link["link_id"]),
                ).rowcount
                if changed != 1:
                    raise AuthorityError(
                        "active link changed during terminal-link reconciliation"
                    )
                self._audit(
                    conn,
                    Principal("system", "system:terminal-link-reconciler"),
                    "session.auto_unlink_terminal",
                    work_item_id=link["work_item_id"],
                    session_id=link["session_id"],
                    link_id=link["link_id"],
                    request_id="terminal-link-backfill",
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
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
        link_id: Optional[str] = None,
        target_principal_id: Optional[str] = None,
        scope: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        conn.execute(
            "INSERT INTO authority_audit_events "
            "(audit_id, tenant_id, principal_id, action, work_item_id, "
            "session_id, link_id, target_principal_id, scope, outcome, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?)",
            (
                "ae_" + str(uuid.uuid4()),
                principal.tenant_id,
                principal.principal_id,
                action,
                work_item_id,
                session_id,
                link_id,
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
                "session_id, link_id, target_principal_id, scope, outcome, request_id, created_at "
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
                changed_at = _utc_now()
                conn.execute(
                    "UPDATE work_items SET status = ?, updated_at = ? "
                    "WHERE work_item_id = ?",
                    (status, changed_at, work_item_id),
                )
                if status in TERMINAL_STATUSES:
                    active_links = conn.execute(
                        "SELECT link_id, session_id FROM session_links "
                        "WHERE work_item_id = ? AND unlinked_at IS NULL "
                        "ORDER BY linked_at, link_id",
                        (work_item_id,),
                    ).fetchall()
                    for link in active_links:
                        changed = conn.execute(
                            "UPDATE session_links SET unlinked_at = ? "
                            "WHERE link_id = ? AND unlinked_at IS NULL",
                            (changed_at, link["link_id"]),
                        ).rowcount
                        if changed != 1:
                            raise AuthorityError(
                                "active link changed during terminal transition"
                            )
                        self._audit(
                            conn, principal, "session.auto_unlink_terminal",
                            work_item_id=work_item_id,
                            session_id=link["session_id"],
                            link_id=link["link_id"],
                            request_id=request_id,
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
        work_item_id: str,
        attempt_id: str,
    ) -> str:
        """Record landing evidence bound to one WorkItem send attempt.

        This is an internal trusted-sender hook, not an MCP operation.
        The caller must be the real send pipeline after verifying the actual
        conversation identity, complete reply, and archived reply file.
        """
        validate_conversation_id(session_id)
        validate_work_item_id(work_item_id)

        if match not in ("confirmed", "mismatch", "ambiguous"):
            raise QuarantineNotCleared("unknown landing match result")
        if not isinstance(evidence_complete, bool):
            raise QuarantineNotCleared("evidence_complete must be boolean")
        if not isinstance(attempt_id, str) or not attempt_id.startswith("att_"):
            raise QuarantineNotCleared("attempt_id must use the att_ prefix")

        attempt_suffix = attempt_id[4:]
        try:
            parsed_attempt = uuid.UUID(attempt_suffix)
        except (ValueError, TypeError, AttributeError) as exc:
            raise QuarantineNotCleared(
                "attempt_id must contain a UUIDv4"
            ) from exc
        if (
            parsed_attempt.version != 4
            or str(parsed_attempt) != attempt_suffix
            or len(attempt_id) != 40
        ):
            raise QuarantineNotCleared(
                "attempt_id must be att_ followed by a canonical lowercase UUIDv4"
            )

        event_id = "le_" + str(uuid.uuid4())
        created_at = _utc_now()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO authority_landing_events (
                    event_id,
                    session_id,
                    match,
                    evidence_complete,
                    created_at,
                    consumed_at,
                    work_item_id,
                    attempt_id,
                    rejected_at,
                    rejection_reason
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL)
                """,
                (
                    event_id,
                    session_id,
                    match,
                    int(evidence_complete),
                    created_at,
                    work_item_id,
                    attempt_id,
                ),
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
        attempt_id: str,
        evidence: Any = None,
        request_id: Optional[str] = None,
    ) -> SessionLink:
        """Consume only evidence bound to this exact WorkItem attempt.

        Caller-supplied evidence remains unsupported. A confirmed event is
        consumed atomically with link creation and audit insertion.

        A same-session active-link conflict is a deterministic rejection.
        Its REJECTED terminal state is written only after the link transaction
        has rolled back, using a separate connection and transaction.
        """
        validate_conversation_id(session_id)
        validate_work_item_id(work_item_id)

        if not isinstance(attempt_id, str) or not attempt_id.startswith("att_"):
            raise QuarantineNotCleared("attempt_id must use the att_ prefix")
        try:
            parsed_attempt = uuid.UUID(attempt_id[4:])
        except (ValueError, TypeError, AttributeError) as exc:
            raise QuarantineNotCleared(
                "attempt_id must contain a UUIDv4"
            ) from exc
        if (
            parsed_attempt.version != 4
            or str(parsed_attempt) != attempt_id[4:]
            or len(attempt_id) != 40
        ):
            raise QuarantineNotCleared(
                "attempt_id must be att_ followed by a canonical lowercase UUIDv4"
            )

        if evidence is not None:
            raise QuarantineNotCleared(
                "caller-supplied landing evidence is not accepted"
            )

        def reject_after_rollback(event_id: str, reason: str) -> None:
            """Persist a deterministic rejection in its own transaction.

            Failure to persist the rejection is deliberately propagated.
            The caller must not report REJECTED unless this transaction commits.
            """
            rejected_at = _utc_now()
            reject_conn = self._connect()
            try:
                reject_conn.execute("BEGIN IMMEDIATE")
                changed = reject_conn.execute(
                    """
                    UPDATE authority_landing_events
                    SET rejected_at = ?, rejection_reason = ?
                    WHERE event_id = ?
                      AND session_id = ?
                      AND work_item_id = ?
                      AND attempt_id = ?
                      AND match = 'confirmed'
                      AND evidence_complete = 1
                      AND consumed_at IS NULL
                      AND rejected_at IS NULL
                    """,
                    (
                        rejected_at,
                        reason,
                        event_id,
                        session_id,
                        work_item_id,
                        attempt_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise AuthorityError(
                        "could not persist landing rejection: "
                        "the exact pending event was not updated"
                    )
                reject_conn.execute("COMMIT")
            except BaseException:
                if reject_conn.in_transaction:
                    reject_conn.execute("ROLLBACK")
                raise
            finally:
                reject_conn.close()

        def has_active_link() -> bool:
            check_conn = self._connect()
            try:
                row = check_conn.execute(
                    """
                    SELECT 1
                    FROM session_links
                    WHERE session_id = ? AND unlinked_at IS NULL
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                return row is not None
            finally:
                check_conn.close()

        conn = self._connect()
        link_id = "sl_" + str(uuid.uuid4())
        now = _utc_now()
        selected_event_id = None

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
                raise InvalidTransition(
                    "terminal WorkItem cannot receive a Session link"
                )

            landing = conn.execute(
                """
                SELECT event_id
                FROM authority_landing_events
                WHERE session_id = ?
                  AND work_item_id = ?
                  AND attempt_id = ?
                  AND match = 'confirmed'
                  AND evidence_complete = 1
                  AND consumed_at IS NULL
                  AND rejected_at IS NULL
                ORDER BY created_at DESC, event_id DESC
                LIMIT 1
                """,
                (session_id, work_item_id, attempt_id),
            ).fetchone()

            if landing is None:
                raise QuarantineNotCleared(
                    "a confirmed, complete, unused landing record for this "
                    "WorkItem attempt is required"
                )

            selected_event_id = landing["event_id"]

            # Check the deterministic conflict before consuming the evidence.
            # The unique index remains the race-safe final enforcement.
            active = conn.execute(
                """
                SELECT 1
                FROM session_links
                WHERE session_id = ? AND unlinked_at IS NULL
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if active is not None:
                raise SessionAlreadyLinked(
                    "session already has an active WorkItem link"
                )

            consumed = conn.execute(
                """
                UPDATE authority_landing_events
                SET consumed_at = ?
                WHERE event_id = ?
                  AND session_id = ?
                  AND work_item_id = ?
                  AND attempt_id = ?
                  AND match = 'confirmed'
                  AND evidence_complete = 1
                  AND consumed_at IS NULL
                  AND rejected_at IS NULL
                """,
                (
                    now,
                    selected_event_id,
                    session_id,
                    work_item_id,
                    attempt_id,
                ),
            ).rowcount
            if consumed != 1:
                raise QuarantineNotCleared(
                    "landing evidence was consumed or changed concurrently"
                )

            conn.execute(
                """
                INSERT INTO session_links (
                    link_id, session_id, work_item_id, linked_at, unlinked_at
                )
                VALUES (?, ?, ?, ?, NULL)
                """,
                (link_id, session_id, work_item_id, now),
            )

            self._audit(
                conn,
                principal,
                "session.link",
                work_item_id=work_item_id,
                session_id=session_id,
                link_id=link_id,
                request_id=request_id,
            )
            conn.execute("COMMIT")

        except SessionAlreadyLinked:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()
            conn = None

            if selected_event_id is None:
                raise

            reject_after_rollback(
                selected_event_id,
                "session_already_linked",
            )
            raise

        except sqlite3.IntegrityError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()
            conn = None

            # IntegrityError alone is not enough to classify this as a
            # duplicate active link. Confirm the exact session conflict
            # after rollback; otherwise preserve the original DB exception.
            if selected_event_id is not None and has_active_link():
                reject_after_rollback(
                    selected_event_id,
                    "session_already_linked",
                )
                raise SessionAlreadyLinked(
                    "session already has an active WorkItem link"
                ) from exc
            raise

        except BaseException:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        finally:
            if conn is not None:
                conn.close()

        return SessionLink(
            link_id,
            session_id,
            work_item_id,
            now,
            None,
        )

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
                link_id=row["link_id"], request_id=request_id,
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
                "SELECT s.link_id, s.session_id, s.work_item_id, "
                "s.linked_at, s.unlinked_at "
                "FROM session_links AS s "
                "JOIN work_items AS w ON w.work_item_id = s.work_item_id "
                "WHERE s.work_item_id = ?"
            )
            params: List[Any] = [work_item_id]
            if not include_unlinked:
                sql += " AND s.unlinked_at IS NULL "
                sql += "AND w.status NOT IN ('completed', 'cancelled')"
            if decoded is not None:
                sql += (
                    " AND (s.linked_at > ? "
                    "OR (s.linked_at = ? AND s.link_id > ?))"
                )
                params.extend(
                    [decoded["linked_at"], decoded["linked_at"], decoded["link_id"]]
                )
            sql += " ORDER BY s.linked_at ASC, s.link_id ASC LIMIT ?"
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
                "SELECT s.link_id, s.session_id, s.work_item_id, "
                "s.linked_at, s.unlinked_at "
                "FROM session_links AS s "
                "JOIN work_items AS w ON w.work_item_id = s.work_item_id "
                "WHERE s.session_id = ? AND s.unlinked_at IS NULL "
                "AND w.status NOT IN ('completed', 'cancelled')",
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
