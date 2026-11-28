"""Shared value objects and error vocabulary for the sender sidecar (PR-1).

Standard library only.  Nothing here performs I/O, so ``job_store``,
``account_router`` and ``worker`` can share one vocabulary without importing
each other, the MCP server, or the 392KB ``chatgpt_send.py`` CLI.

Two shapes matter:

* :meth:`Job.public` -- the ``chatgpt.job.get`` projection.  It never contains
  the prompt, a cookie or an access token (report section 1.3 D).
* :meth:`Job.internal` -- adds the prompt and the lease plumbing.  Only the
  worker and the store see it.

The public error code set stays at the ten codes of v0.1 (section 1.6).
``OUTCOME_UNKNOWN`` and ``ACCOUNT_BUSY`` are sidecar-internal: the first is a
job error, the second is a routing signal that never leaves this process.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

JOB_ID_PREFIX = "cgj_"

#: Keys the sidecar is willing to persist from an adapter result.  Anything
#: else (tokens, raw remote payloads) is dropped before it reaches SQLite.
PERSISTED_RESULT_KEYS = (
    "message_id",
    "assistant_message_count",
    "response_chars",
    "conversation_id",
    "account",
    "turn_id",
    "reply_state",
    "reply_source",
    "reply_chars",
    "reply_sha256",
    "receipt_durable",
    "receipt_version",
)


def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""

    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Render a datetime in the fixed-width UTC format used by every column."""

    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def utc_now_iso() -> str:
    return to_iso(utc_now())


def parse_iso(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def add_seconds(timestamp: str, seconds: float) -> str:
    return to_iso(parse_iso(timestamp) + timedelta(seconds=float(seconds)))


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON used to compare two payloads byte for byte."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def new_job_id() -> str:
    return f"{JOB_ID_PREFIX}{uuid.uuid4()}"


def sanitize_result(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only the whitelisted, non-secret keys of an adapter result."""

    if not result:
        return {}
    return {key: result[key] for key in PERSISTED_RESULT_KEYS if key in result}


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


class Operation(str, Enum):
    SESSION_CREATE = "session.create"
    SESSION_SEND = "session.send"


class ErrorCode(str, Enum):
    # -- the ten public codes of v0.1 (section 1.6)
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    QUARANTINED_SESSION = "QUARANTINED_SESSION"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    INTERNAL = "INTERNAL"
    # -- added by the ownership fix (Owner decision 2026-11-27): the remote
    # conversation exists but its local owner row could not be persisted.
    OWNERSHIP_PERSIST_FAILED = "OWNERSHIP_PERSIST_FAILED"
    # -- sidecar internals, never returned by the MCP tools
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    ACCOUNT_BUSY = "ACCOUNT_BUSY"


_PUBLIC_CODES = frozenset(
    code.value
    for code in ErrorCode
    if code not in (ErrorCode.OUTCOME_UNKNOWN, ErrorCode.ACCOUNT_BUSY)
)

_RETRYABLE_CODES = frozenset(
    {ErrorCode.DEPENDENCY_UNAVAILABLE.value, ErrorCode.TIMEOUT.value}
)


def is_public_code(code: str | None) -> bool:
    return code in _PUBLIC_CODES


def is_retryable(code: str | None) -> bool:
    """``OUTCOME_UNKNOWN`` is explicitly not retryable (section 1.2)."""

    return code in _RETRYABLE_CODES


class GatewayError(Exception):
    """Base error carrying a stable ``ErrorCode``."""

    default_code: ErrorCode = ErrorCode.INTERNAL

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | str | None = None,
        upstream_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.code = ErrorCode(code) if code is not None else self.default_code
        self.upstream_status = upstream_status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message or self.code.value)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code.value}: {super().__str__()}"


class StoreError(GatewayError):
    """Bad arguments or an unusable database handle."""

    default_code = ErrorCode.INVALID_ARGUMENT


class JobStateError(GatewayError):
    """A transition the state machine forbids (terminal states never move)."""

    default_code = ErrorCode.CONFLICT


class IdempotencyConflict(GatewayError):
    """Same ``idempotency_key``, different payload (section 5.2 E)."""

    default_code = ErrorCode.CONFLICT


class AccountBusy(GatewayError):
    """The account already has one job in ``running`` (section 2.6)."""

    default_code = ErrorCode.ACCOUNT_BUSY


class SessionQuarantined(GatewayError):
    """The conversation has no trusted owner row (section 5.2 B)."""

    default_code = ErrorCode.QUARANTINED_SESSION


class SessionConflict(GatewayError):
    """Requested account contradicts the recorded owner (section 1.5)."""

    default_code = ErrorCode.CONFLICT


class SessionNotFound(GatewayError):
    """Unknown conversation, or one owned by somebody else."""

    default_code = ErrorCode.NOT_FOUND


class ResourceExhausted(GatewayError):
    """Every usable account is rate limited (section 1.5)."""

    default_code = ErrorCode.RESOURCE_EXHAUSTED


class DependencyUnavailable(GatewayError):
    """The adapter or its credentials are not usable right now."""

    default_code = ErrorCode.DEPENDENCY_UNAVAILABLE


class OwnershipPersistFailed(GatewayError):
    """The conversation was created remotely but its owner row was not stored.

    Fail closed: the job never reaches ``succeeded`` and the conversation is
    unreadable until an operator compensates.  The job row keeps the
    conversation id as the audit/compensation link.
    """

    default_code = ErrorCode.OWNERSHIP_PERSIST_FAILED


def _row_get(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):  # pragma: no cover - defensive
        return None


@dataclass(frozen=True)
class Job:
    """One row of the ``jobs`` table."""

    job_id: str
    operation: str
    principal_id: str
    tenant_id: str
    status: str
    requested_account: str
    created_at: str
    idempotency_key: str
    conversation_id: str | None = None
    resolved_account: str | None = None
    prompt: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    lease_until: str | None = None
    attempt_count: int = 0
    worker_id: str | None = None
    outcome_unknown: bool = False

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | Any) -> "Job":
        result_json = _row_get(row, "result_json")
        return cls(
            job_id=_row_get(row, "job_id"),
            operation=_row_get(row, "operation"),
            principal_id=_row_get(row, "principal_id"),
            tenant_id=_row_get(row, "tenant_id"),
            status=_row_get(row, "status"),
            requested_account=_row_get(row, "requested_account"),
            created_at=_row_get(row, "created_at"),
            idempotency_key=_row_get(row, "idempotency_key"),
            conversation_id=_row_get(row, "conversation_id"),
            resolved_account=_row_get(row, "resolved_account"),
            prompt=_row_get(row, "prompt"),
            result=json.loads(result_json) if result_json else None,
            error_code=_row_get(row, "error_code"),
            started_at=_row_get(row, "started_at"),
            completed_at=_row_get(row, "completed_at"),
            lease_until=_row_get(row, "lease_until"),
            attempt_count=int(_row_get(row, "attempt_count") or 0),
            worker_id=_row_get(row, "worker_id"),
            outcome_unknown=bool(_row_get(row, "outcome_unknown")),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value)

    def public(self) -> dict[str, Any]:
        """The ``chatgpt.job.get`` projection: never the prompt."""

        return {
            "job_id": self.job_id,
            "operation": self.operation,
            "status": self.status,
            "conversation_id": self.conversation_id,
            "requested_account": self.requested_account,
            "resolved_account": self.resolved_account,
            "result": self.result,
            "error": (
                {
                    "code": self.error_code,
                    "retryable": is_retryable(self.error_code)
                    and not self.outcome_unknown,
                }
                if self.error_code
                else None
            ),
            "outcome_unknown": self.outcome_unknown,
            "attempt_count": self.attempt_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def internal(self) -> dict[str, Any]:
        """Worker/store projection: adds the prompt and the lease plumbing."""

        payload = self.public()
        payload.update(
            {
                "prompt": self.prompt,
                "principal_id": self.principal_id,
                "tenant_id": self.tenant_id,
                "worker_id": self.worker_id,
                "lease_until": self.lease_until,
                "idempotency_key": self.idempotency_key,
            }
        )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return self.public()


@dataclass(frozen=True)
class SessionOwner:
    """One row of ``session_owners`` (report section 5.2 B)."""

    conversation_id: str
    tenant_id: str
    principal_id: str
    account: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | Any) -> "SessionOwner":
        return cls(
            conversation_id=_row_get(row, "conversation_id"),
            tenant_id=_row_get(row, "tenant_id"),
            principal_id=_row_get(row, "principal_id"),
            account=_row_get(row, "account"),
            created_at=_row_get(row, "created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "account": self.account,
            "created_at": self.created_at,
        }
