"""ChatGPT sender sidecar -- PR-1 (job persistence, routing, worker).

This package is deliberately not part of the MCP application tree
(``deploy/paths.txt`` does not list it) and it never imports the 392KB
``scripts/chatgpt_send.py`` CLI.  See ``docs/SENDER-GATEWAY.md``.
"""

from __future__ import annotations

from .account_router import (
    DEFAULT_ACCOUNTS,
    AccountRouter,
    Reservation,
)
from .job_store import JobStore
from .models import (
    AccountBusy,
    DependencyUnavailable,
    ErrorCode,
    GatewayError,
    IdempotencyConflict,
    Job,
    JobStateError,
    JobStatus,
    Operation,
    ResourceExhausted,
    SessionConflict,
    SessionNotFound,
    SessionOwner,
    SessionQuarantined,
    StoreError,
    new_job_id,
    utc_now_iso,
)
from .worker import Worker

__all__ = [
    "DEFAULT_ACCOUNTS",
    "AccountBusy",
    "AccountRouter",
    "DependencyUnavailable",
    "ErrorCode",
    "GatewayError",
    "IdempotencyConflict",
    "Job",
    "JobStateError",
    "JobStatus",
    "JobStore",
    "Operation",
    "Reservation",
    "ResourceExhausted",
    "SessionConflict",
    "SessionNotFound",
    "SessionOwner",
    "SessionQuarantined",
    "StoreError",
    "Worker",
    "new_job_id",
    "utc_now_iso",
]
