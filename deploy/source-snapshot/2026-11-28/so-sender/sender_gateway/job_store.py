"""SQLite job store for the sender sidecar (PR-1).

Guarantees this module is responsible for:

* ``enqueue`` is durable *before* it returns: WAL journal, ``synchronous=FULL``
  and an explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` around the insert.
* ``enqueue`` is idempotent on ``(tenant_id, principal_id, idempotency_key)``.
  The unique index is load bearing -- the insert uses that index as its
  ``ON CONFLICT`` target, so dropping the index breaks the statement instead
  of silently allowing duplicates.  Same key + same payload returns the
  original job; same key + different payload raises ``CONFLICT``.
* ``claim_next`` is atomic: a single ``UPDATE ... RETURNING`` statement inside
  a write transaction, so two workers can never take the same row.
* Terminal states are terminal.  ``complete``/``fail`` only move rows out of
  ``running``; the guard is in the ``WHERE`` clause as well as the pre-check.
* Reads are isolated by ``tenant_id`` *and* ``principal_id``.  Both filters
  live in the ``WHERE`` clause of ``get``.
* At most one ``running`` job may hold a given ``resolved_account``.  That is
  enforced by a partial unique index, not by application bookkeeping.
* A ``running`` row whose ``lease_until`` has passed is *expired*, never
  silently retried: ``expired_leases`` + ``steal_expired_lease`` let the
  worker reconcile it into ``OUTCOME_UNKNOWN``.

Connections come from a fixed-capacity pool (``POOL_CAPACITY``): a caller
borrows one for the duration of a single statement or transaction and always
gives it back.  The ceiling is independent of request volume, so a
thread-per-request server cannot leak fds.  Concurrency still reaches real
SQLite locking rather than a Python mutex, and an exhausted pool waits a
bounded time before failing with ``RESOURCE_EXHAUSTED``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .models import (
    AccountBusy,
    ErrorCode,
    IdempotencyConflict,
    Job,
    JobStateError,
    JobStatus,
    Operation,
    SessionConflict,
    StoreError,
    add_seconds,
    canonical_json,
    new_job_id,
    utc_now_iso,
)

#: Report section 2.5, verbatim column order, plus the additions the same
#: section asks for (``idempotency_key``, ``attempt_count``, ``worker_id``,
#: ``outcome_unknown``) and ``payload_json``, the canonical payload used to
#: tell "same request retried" from "same key, different request".
_SCHEMA_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        operation TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        status TEXT NOT NULL,
        conversation_id TEXT,
        requested_account TEXT NOT NULL,
        resolved_account TEXT,
        prompt TEXT,
        result_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        lease_until TEXT,
        idempotency_key TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        worker_id TEXT,
        outcome_unknown INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_jobs_owner_created"
    " ON jobs(principal_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_status_created"
    " ON jobs(status, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_idempotency"
    " ON jobs(tenant_id, principal_id, idempotency_key)",
    # section 2.6: "每个账号最多一个正在执行的发送任务"
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_running_account"
    " ON jobs(resolved_account)"
    " WHERE status = 'running' AND resolved_account IS NOT NULL",
    # section 5.2 problem B: caller ownership, not just account routing
    """
    CREATE TABLE IF NOT EXISTS job_tombstones (
        job_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        conversation_id TEXT,
        requested_account TEXT NOT NULL,
        resolved_account TEXT,
        idempotency_key TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        error_code TEXT,
        outcome_unknown INTEGER NOT NULL DEFAULT 0,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT NOT NULL,
        UNIQUE(tenant_id, principal_id, idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_job_tombstones_owner"
    " ON job_tombstones(job_id, tenant_id, principal_id)",
    """
    CREATE TABLE IF NOT EXISTS session_owners (
        conversation_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        account TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_session_owners_owner"
    " ON session_owners(tenant_id, principal_id)",
    """
    CREATE TABLE IF NOT EXISTS job_event_outbox (
        event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
        tenant_id TEXT NOT NULL, principal_id TEXT NOT NULL,
        event_name TEXT NOT NULL, payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
        lease_until TEXT, lease_token TEXT, worker_id TEXT, delivered_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_job_event_outbox_due"
    " ON job_event_outbox(status, next_attempt_at, created_at)",
)

_INSERT_JOB = """
INSERT INTO jobs (
    job_id, operation, principal_id, tenant_id, status, conversation_id,
    requested_account, resolved_account, prompt, result_json, error_code,
    created_at, started_at, completed_at, lease_until, idempotency_key,
    attempt_count, worker_id, outcome_unknown, payload_json
) VALUES (
    ?, ?, ?, ?, 'queued', ?, ?, NULL, ?, NULL, NULL,
    ?, NULL, NULL, NULL, ?, 0, NULL, 0, ?
)
ON CONFLICT(tenant_id, principal_id, idempotency_key) DO NOTHING
"""


class _ConnectionPool:
    """Fixed-capacity pool of SQLite connections.

    One connection has at most one borrower at a time.  ``close()`` flips the
    pool shut: idle connections are closed immediately, and a connection that
    is still on loan is closed when it is handed back.  ``check_same_thread``
    is off on the connections, so they may move between request threads; that
    is safe because a connection never holds an open transaction while it sits
    idle here (``release`` rolls back anything a buggy borrower left behind).
    """

    __slots__ = (
        "_factory",
        "_capacity",
        "_wait_seconds",
        "_idle",
        "_leased",
        "_closed",
        "_cond",
    )

    def __init__(
        self,
        factory: Callable[[], sqlite3.Connection],
        *,
        capacity: int,
        wait_seconds: float,
    ) -> None:
        self._factory = factory
        self._capacity = max(1, int(capacity))
        self._wait_seconds = max(0.0, float(wait_seconds))
        self._idle: list[sqlite3.Connection] = []
        self._leased = 0
        self._closed = False
        self._cond = threading.Condition()

    def stats(self) -> tuple[int, int]:
        """``(idle, borrowed)``; their sum is the live connection count."""

        with self._cond:
            return len(self._idle), self._leased

    def acquire(self) -> sqlite3.Connection:
        deadline = time.monotonic() + self._wait_seconds
        with self._cond:
            while True:
                if self._closed:
                    raise StoreError(
                        "job store is closed", code=ErrorCode.RESOURCE_EXHAUSTED
                    )
                if self._idle:
                    self._leased += 1
                    return self._idle.pop()
                if self._leased < self._capacity:
                    self._leased += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StoreError(
                        "job store connection pool exhausted "
                        "(%d borrowed, capacity %d, waited %.1fs)"
                        % (self._leased, self._capacity, self._wait_seconds),
                        code=ErrorCode.RESOURCE_EXHAUSTED,
                    )
                self._cond.wait(remaining)
        try:
            return self._factory()
        except BaseException:
            self._release_slot()
            raise

    def release(self, conn: sqlite3.Connection) -> None:
        broken = False
        try:
            if conn.in_transaction:
                # A leaked transaction must never reach the next borrower.
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            broken = True
        with self._cond:
            self._leased -= 1
            discard = self._closed or broken
            if not discard:
                self._idle.append(conn)
            self._cond.notify()
        if discard:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - best effort
                pass

    def _release_slot(self) -> None:
        with self._cond:
            self._leased -= 1
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._cond.notify_all()
        for conn in idle:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - best effort
                pass


class _Txn:
    """Thin handle over a connection that already has a write transaction."""

    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())


class JobStore:
    """Durable, idempotent, concurrency-safe job queue."""

    #: section 1.2: a ``running`` job older than its lease must be reconciled.
    DEFAULT_LEASE_SECONDS = 60.0

    #: Hard ceiling on open SQLite connections, independent of request volume.
    POOL_CAPACITY = 4

    #: Seconds a caller waits for a free connection before RESOURCE_EXHAUSTED.
    POOL_WAIT_SECONDS = 30.0

    def __init__(
        self,
        path: str | Path,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now=None,
        pool_capacity: int | None = None,
        pool_wait_seconds: float | None = None,
    ) -> None:
        location = str(path)
        if location == ":memory:":
            raise StoreError(
                "JobStore needs a file path: WAL and pooled connections "
                "cannot share an in-memory database",
                code=ErrorCode.INVALID_ARGUMENT,
            )
        self.path = location
        Path(location).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lease_seconds = float(lease_seconds)
        self._now = now or utc_now_iso
        self._schema_lock = threading.Lock()
        self._pool = _ConnectionPool(
            self._open_connection,
            capacity=(
                self.POOL_CAPACITY if pool_capacity is None else pool_capacity
            ),
            wait_seconds=(
                self.POOL_WAIT_SECONDS
                if pool_wait_seconds is None
                else pool_wait_seconds
            ),
        )
        self._ensure_schema()

    # -- clock ---------------------------------------------------------

    def now(self) -> str:
        return self._now()

    @property
    def lease_seconds(self) -> float:
        return self._lease_seconds

    # -- connection plumbing -------------------------------------------

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            isolation_level=None,  # we drive BEGIN/COMMIT ourselves
            timeout=30.0,
            check_same_thread=False,  # pooled connections move between threads
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def _borrow(self) -> Iterator[sqlite3.Connection]:
        """Borrow one pooled connection; it is returned on every path."""

        conn = self._pool.acquire()
        try:
            yield conn
        finally:
            self._pool.release(conn)

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._borrow() as conn:
            return list(conn.execute(sql, params).fetchall())

    def close(self) -> None:
        """Refuse new borrows and close every idle connection.

        A connection still on loan is closed the moment it is returned.
        """

        self._pool.close()

    def _ensure_schema(self) -> None:
        with self._schema_lock, self._borrow() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(jobs)")
                }
                if "next_eligible_at" not in columns:
                    conn.execute("ALTER TABLE jobs ADD COLUMN next_eligible_at TEXT")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_jobs_queued_eligible "
                    "ON jobs(status, next_eligible_at, created_at)"
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextmanager
    def transaction(self) -> Iterator[_Txn]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT``; rollback on any exception.

        The whole transaction lives inside one borrow, so it can never straddle
        two connections.
        """

        with self._borrow() as conn:
            if conn.in_transaction:  # pragma: no cover - pool guarantees clean
                raise StoreError(
                    "nested transaction requested; compose through the txn handle",
                    code=ErrorCode.INTERNAL,
                )
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield _Txn(conn)
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - already unwound
                    pass
                raise
            try:
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover
                    pass
                raise

    # -- enqueue -------------------------------------------------------

    def enqueue(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict,
    ) -> dict:
        """Persist a new job and commit before returning.

        Returns the public projection of the created (or already existing) job.
        """

        for name, value in (
            ("principal_id", principal_id),
            ("tenant_id", tenant_id),
            ("idempotency_key", idempotency_key),
        ):
            if not isinstance(value, str) or not value.strip():
                raise StoreError(
                    f"{name} must be a non-empty string",
                    code=ErrorCode.INVALID_ARGUMENT,
                )
        try:
            operation_value = Operation(operation).value
        except ValueError as exc:
            raise StoreError(
                f"unknown operation {operation!r}", code=ErrorCode.INVALID_ARGUMENT
            ) from exc
        if not isinstance(payload, dict):
            raise StoreError("payload must be a mapping", code=ErrorCode.INVALID_ARGUMENT)

        canonical = canonical_json(payload)
        job_id = new_job_id()
        created_at = self._now()
        conversation_id = payload.get("conversation_id")
        requested_account = payload.get("account") or "auto"
        prompt = payload.get("prompt")

        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.transaction() as txn:
            tombstone = txn.fetchone(
                "SELECT * FROM job_tombstones WHERE tenant_id = ? AND principal_id = ?"
                " AND idempotency_key = ?",
                (tenant_id, principal_id, idempotency_key),
            )
            if tombstone is not None:
                if tombstone["payload_hash"] != payload_hash:
                    raise IdempotencyConflict(
                        f"idempotency_key {idempotency_key!r} was reused with a different payload"
                    )
                return self._tombstone_public(tombstone)
            cursor = txn.execute(
                _INSERT_JOB,
                (
                    job_id,
                    operation_value,
                    principal_id,
                    tenant_id,
                    conversation_id,
                    requested_account,
                    prompt,
                    created_at,
                    idempotency_key,
                    canonical,
                ),
            )
            if cursor.rowcount == 1:
                row = txn.fetchone("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
                return Job.from_row(row).public()

            # The unique index rejected the insert: this key already exists.
            row = txn.fetchone(
                "SELECT * FROM jobs WHERE tenant_id = ? AND principal_id = ?"
                " AND idempotency_key = ?",
                (tenant_id, principal_id, idempotency_key),
            )
            if row is None:  # pragma: no cover - defensive
                raise StoreError(
                    "idempotency conflict did not resolve to a stored job",
                    code=ErrorCode.INTERNAL,
                )
            stored = Job.from_row(row)
            if row["payload_json"] != canonical:
                raise IdempotencyConflict(
                    f"idempotency_key {idempotency_key!r} was reused with a "
                    "different payload"
                )
            return stored.public()

    # -- claiming ------------------------------------------------------

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: float | None = None,
        now: str | None = None,
    ) -> dict | None:
        """Atomically move the oldest ``queued`` job to ``running``.

        Returns the internal projection (it carries the prompt) or ``None``.
        """

        if not isinstance(worker_id, str) or not worker_id.strip():
            raise StoreError("worker_id is required", code=ErrorCode.INVALID_ARGUMENT)
        started_at = now or self._now()
        lease = float(lease_seconds if lease_seconds is not None else self._lease_seconds)
        lease_until = add_seconds(started_at, lease)

        with self.transaction() as txn:
            row = txn.fetchone(
                """
                UPDATE jobs
                   SET status = 'running',
                       worker_id = ?,
                       started_at = ?,
                       lease_until = ?,
                       next_eligible_at = NULL,
                       attempt_count = attempt_count + 1
                 WHERE job_id = (
                           SELECT job_id FROM jobs
                            WHERE status = 'queued'
                              AND (next_eligible_at IS NULL OR next_eligible_at <= ?)
                            ORDER BY created_at, rowid
                            LIMIT 1
                       )
                   AND status = 'queued'
                RETURNING *
                """,
                (worker_id, started_at, lease_until, started_at),
            )
            if row is None:
                return None
            return Job.from_row(row).internal()

    def attach_conversation(
        self,
        job_id: str,
        conversation_id: str,
        *,
        txn: _Txn | None = None,
    ) -> None:
        """Record the conversation a job created, without rebinding one."""

        if not isinstance(conversation_id, str) or not conversation_id:
            raise StoreError(
                "conversation_id is required", code=ErrorCode.INVALID_ARGUMENT
            )

        def _run(handle: _Txn) -> None:
            handle.execute(
                """
                UPDATE jobs
                   SET conversation_id = ?
                 WHERE job_id = ?
                   AND status = 'running'
                   AND conversation_id IS NULL
                """,
                (conversation_id, job_id),
            )

        if txn is not None:
            _run(txn)
            return
        with self.transaction() as handle:
            _run(handle)

    def release(self, job_id: str, *, delay_seconds: float = 0.0) -> None:
        """Put a ``running`` job back in the queue without counting a send.

        Used when the account already has a job in flight: the claim is
        yielded, nothing was sent, so the row returns to ``queued``.
        """

        delay_seconds = float(delay_seconds)
        if delay_seconds < 0:
            raise StoreError("delay_seconds must be >= 0", code=ErrorCode.INVALID_ARGUMENT)
        next_eligible_at = add_seconds(self._now(), delay_seconds)
        with self.transaction() as txn:
            cursor = txn.execute(
                """
                UPDATE jobs
                   SET status = 'queued',
                       worker_id = NULL,
                       started_at = NULL,
                       lease_until = NULL,
                       resolved_account = NULL,
                       next_eligible_at = ?
                 WHERE job_id = ? AND status = 'running'
                """,
                (next_eligible_at, job_id),
            )
            if cursor.rowcount != 1:
                raise JobStateError(
                    f"job {job_id} is not running", code=ErrorCode.CONFLICT
                )

    # -- transitions ---------------------------------------------------

    def complete(
        self,
        job_id: str,
        result: dict,
        *,
        now: str | None = None,
    ) -> None:
        """``running -> succeeded``.  Never moves any other state."""

        if not isinstance(result, dict):
            raise StoreError("result must be a mapping", code=ErrorCode.INVALID_ARGUMENT)
        if not isinstance(job_id, str) or not job_id:
            raise StoreError("job_id is required", code=ErrorCode.INVALID_ARGUMENT)
        completed_at = now or self._now()

        with self.transaction() as txn:
            row = txn.fetchone("SELECT status, tenant_id, principal_id FROM jobs WHERE job_id = ?", (job_id,))
            if row is None:
                raise JobStateError(f"unknown job {job_id}", code=ErrorCode.NOT_FOUND)
            status = row["status"]
            if status == JobStatus.SUCCEEDED.value:
                return  # idempotent: already in the requested terminal state
            if status != JobStatus.RUNNING.value:
                raise JobStateError(
                    f"job {job_id} is {status}; refusing to move it to succeeded",
                    code=ErrorCode.CONFLICT,
                )
            cursor = txn.execute(
                """
                UPDATE jobs
                   SET status = 'succeeded',
                       result_json = ?,
                       error_code = NULL,
                       completed_at = ?,
                       lease_until = NULL
                 WHERE job_id = ? AND status = 'running'
                """,
                (canonical_json(result), completed_at, job_id),
            )
            if cursor.rowcount != 1:  # pragma: no cover - guarded above
                raise JobStateError(
                    f"job {job_id} left the running state", code=ErrorCode.CONFLICT
                )
            event_id = "evt_" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]
            event_payload = {"job_id": job_id, "status": "succeeded",
                             "summary": "Job completed successfully.",
                             "completed_at": completed_at}
            txn.execute(
                """INSERT INTO job_event_outbox
                   (event_id, job_id, tenant_id, principal_id, event_name, payload_json,
                    created_at, status, attempts, next_attempt_at)
                   VALUES (?, ?, ?, ?, 'chatgpt.job.completed', ?, ?, 'pending', 0, ?)
                   ON CONFLICT(job_id) DO NOTHING""",
                (event_id, job_id, row["tenant_id"], row["principal_id"],
                 canonical_json(event_payload), completed_at, completed_at),
            )

    def fail(
        self,
        job_id: str,
        error_code: str,
        *,
        outcome_unknown: bool = False,
        now: str | None = None,
    ) -> None:
        """Fail a job while preserving side-effect certainty independently."""

        try:
            code = ErrorCode(error_code)
        except ValueError as exc:
            raise StoreError(
                f"unknown error code {error_code!r}", code=ErrorCode.INVALID_ARGUMENT
            ) from exc
        if not isinstance(job_id, str) or not job_id:
            raise StoreError("job_id is required", code=ErrorCode.INVALID_ARGUMENT)
        if not isinstance(outcome_unknown, bool):
            raise StoreError(
                "outcome_unknown must be a boolean",
                code=ErrorCode.INVALID_ARGUMENT,
            )
        outcome_unknown = outcome_unknown or code is ErrorCode.OUTCOME_UNKNOWN
        completed_at = now or self._now()

        with self.transaction() as txn:
            row = txn.fetchone(
                "SELECT status, error_code, outcome_unknown, tenant_id, principal_id FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            if row is None:
                raise JobStateError(f"unknown job {job_id}", code=ErrorCode.NOT_FOUND)
            status = row["status"]
            if status == JobStatus.FAILED.value:
                if (
                    row["error_code"] == code.value
                    and bool(row["outcome_unknown"]) == outcome_unknown
                ):
                    return  # idempotent
                raise JobStateError(
                    f"job {job_id} already failed with {row['error_code']}",
                    code=ErrorCode.CONFLICT,
                )
            if status != JobStatus.RUNNING.value:
                raise JobStateError(
                    f"job {job_id} is {status}; refusing to move it to failed",
                    code=ErrorCode.CONFLICT,
                )
            cursor = txn.execute(
                """
                UPDATE jobs
                   SET status = 'failed',
                       error_code = ?,
                       outcome_unknown = ?,
                       completed_at = ?,
                       lease_until = NULL
                 WHERE job_id = ? AND status = 'running'
                """,
                (
                    code.value,
                    1 if outcome_unknown else 0,
                    completed_at,
                    job_id,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - guarded above
                raise JobStateError(
                    f"job {job_id} left the running state", code=ErrorCode.CONFLICT
                )
            event_id = "evt_" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]
            event_payload = {"job_id": job_id, "status": "failed",
                             "summary": f"Job failed with error code {code.value}.",
                             "completed_at": completed_at, "error_code": code.value,
                             "outcome_unknown": outcome_unknown}
            txn.execute(
                """INSERT INTO job_event_outbox
                   (event_id, job_id, tenant_id, principal_id, event_name, payload_json,
                    created_at, status, attempts, next_attempt_at)
                   VALUES (?, ?, ?, ?, 'chatgpt.job.completed', ?, ?, 'pending', 0, ?)
                   ON CONFLICT(job_id) DO NOTHING""",
                (event_id, job_id, row["tenant_id"], row["principal_id"],
                 canonical_json(event_payload), completed_at, completed_at),
            )

    # -- lease reconciliation ------------------------------------------

    def expired_leases(self, *, now: str | None = None) -> list[dict]:
        """``running`` rows whose lease has passed, oldest first."""

        moment = now or self._now()
        rows = self._query(
            """
            SELECT * FROM jobs
             WHERE status = 'running'
               AND lease_until IS NOT NULL
               AND lease_until < ?
             ORDER BY lease_until, rowid
            """,
            (moment,),
        )
        return [Job.from_row(row).internal() for row in rows]

    def steal_expired_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: float | None = None,
        now: str | None = None,
    ) -> bool:
        """Take ownership of one expired lease; ``False`` if somebody won."""

        moment = now or self._now()
        lease = float(
            lease_seconds if lease_seconds is not None else self._lease_seconds
        )
        lease_until = add_seconds(moment, lease)
        with self.transaction() as txn:
            cursor = txn.execute(
                """
                UPDATE jobs
                   SET worker_id = ?, lease_until = ?
                 WHERE job_id = ?
                   AND status = 'running'
                   AND lease_until IS NOT NULL
                   AND lease_until < ?
                """,
                (worker_id, lease_until, job_id, moment),
            )
            return cursor.rowcount == 1

    # -- event outbox --------------------------------------------------

    def claim_events(self, worker_id: str, *, limit: int = 1,
                     lease_seconds: float = 30.0, now: str | None = None) -> list[dict]:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise StoreError("worker_id is required", code=ErrorCode.INVALID_ARGUMENT)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise StoreError("limit must be in 1..25", code=ErrorCode.INVALID_ARGUMENT)
        if not 1 <= float(lease_seconds) <= 300:
            raise StoreError("lease_seconds must be in 1..300", code=ErrorCode.INVALID_ARGUMENT)
        claimed_at = now or self._now()
        lease_until = add_seconds(claimed_at, float(lease_seconds))
        claimed = []
        with self.transaction() as txn:
            txn.execute("""UPDATE job_event_outbox SET status='pending', lease_until=NULL,
                         lease_token=NULL, worker_id=NULL
                         WHERE status='sending' AND lease_until<=?""", (claimed_at,))
            rows = txn.fetchall("""SELECT * FROM job_event_outbox WHERE status='pending'
                                  AND next_attempt_at<=? ORDER BY created_at,event_id LIMIT ?""",
                                (claimed_at, limit))
            for row in rows:
                token = uuid.uuid4().hex
                cur = txn.execute("""UPDATE job_event_outbox SET status='sending',
                    attempts=attempts+1, worker_id=?, lease_until=?, lease_token=?
                    WHERE event_id=? AND status='pending'""",
                    (worker_id, lease_until, token, row["event_id"]))
                if cur.rowcount == 1:
                    claimed.append({"event_id": row["event_id"], "job_id": row["job_id"],
                        "tenant_id": row["tenant_id"], "principal_id": row["principal_id"],
                        "name": row["event_name"], "payload": json.loads(row["payload_json"]),
                        "lease_token": token, "attempt": int(row["attempts"])+1})
        return claimed

    def ack_event(self, event_id: str, lease_token: str, *, now: str | None = None) -> bool:
        if not event_id or not lease_token:
            raise StoreError("event_id and lease_token are required", code=ErrorCode.INVALID_ARGUMENT)
        at = now or self._now()
        with self.transaction() as txn:
            row = txn.fetchone("SELECT status, lease_token, lease_until FROM job_event_outbox WHERE event_id=?", (event_id,))
            if row is None:
                return False
            if row["status"] == "delivered":
                return True
            if (row["status"] != "sending" or row["lease_token"] != lease_token
                    or row["lease_until"] is None or row["lease_until"] <= at):
                return False
            txn.execute("""UPDATE job_event_outbox SET status='delivered', delivered_at=?,
                lease_until=NULL, lease_token=NULL, worker_id=NULL
                WHERE event_id=? AND status='sending' AND lease_token=? AND lease_until>?""",
                (at, event_id, lease_token, at))
        return True

    # -- reads ---------------------------------------------------------

    def get(self, principal_id: str, tenant_id: str, job_id: str) -> dict | None:
        """Tenant- and principal-scoped read.  No cross-tenant leakage."""

        for name, value in (
            ("principal_id", principal_id),
            ("tenant_id", tenant_id),
            ("job_id", job_id),
        ):
            if not isinstance(value, str) or not value:
                raise StoreError(
                    f"{name} is required", code=ErrorCode.INVALID_ARGUMENT
                )
        rows = self._query(
            "SELECT * FROM jobs WHERE job_id = ? AND tenant_id = ? AND principal_id = ?",
            (job_id, tenant_id, principal_id),
        )
        if not rows:
            tombstones = self._query(
                "SELECT * FROM job_tombstones WHERE job_id = ? AND tenant_id = ?"
                " AND principal_id = ?",
                (job_id, tenant_id, principal_id),
            )
            if not tombstones:
                return None
            return self._tombstone_public(tombstones[0])
        return Job.from_row(rows[0]).public()

    @staticmethod
    def _tombstone_public(row) -> dict:
        error_code = row["error_code"]
        return {
            "job_id": row["job_id"],
            "operation": row["operation"],
            "status": row["status"],
            "conversation_id": row["conversation_id"],
            "requested_account": row["requested_account"],
            "resolved_account": row["resolved_account"],
            "result": None,
            "error": {"code": error_code, "retryable": False} if error_code else None,
            "outcome_unknown": bool(row["outcome_unknown"]),
            "attempt_count": int(row["attempt_count"] or 0),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    def prune_completed(self, before: str) -> int:
        """Replace expired terminal jobs with minimal permanent ownership/idempotency tombstones."""
        with self.transaction() as txn:
            rows = txn.fetchall(
                "SELECT * FROM jobs WHERE status IN ('succeeded','failed')"
                " AND completed_at IS NOT NULL AND completed_at < ?",
                (before,),
            )
            for row in rows:
                payload_hash = hashlib.sha256(
                    row["payload_json"].encode("utf-8")
                ).hexdigest()
                txn.execute(
                    """INSERT OR IGNORE INTO job_tombstones (
                        job_id, tenant_id, principal_id, operation, conversation_id,
                        requested_account, resolved_account, idempotency_key,
                        payload_hash, status, error_code, outcome_unknown,
                        attempt_count, created_at, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["job_id"], row["tenant_id"], row["principal_id"],
                        row["operation"], row["conversation_id"],
                        row["requested_account"], row["resolved_account"],
                        row["idempotency_key"], payload_hash, row["status"],
                        row["error_code"], row["outcome_unknown"],
                        row["attempt_count"], row["created_at"],
                        row["started_at"], row["completed_at"],
                    ),
                )
                saved = txn.fetchone(
                    "SELECT job_id, payload_hash FROM job_tombstones WHERE tenant_id=?"
                    " AND principal_id=? AND idempotency_key=?",
                    (row["tenant_id"], row["principal_id"], row["idempotency_key"]),
                )
                if saved is None or saved["job_id"] != row["job_id"] or saved["payload_hash"] != payload_hash:
                    raise StoreError(
                        "job tombstone identity conflict; refusing to delete source job",
                        code=ErrorCode.CONFLICT,
                    )
            cursor = txn.execute(
                "DELETE FROM jobs WHERE status IN ('succeeded','failed')"
                " AND completed_at IS NOT NULL AND completed_at < ?",
                (before,),
            )
            return cursor.rowcount

    def assign_account(
        self,
        job_id: str,
        account: str,
        *,
        txn: _Txn | None = None,
    ) -> None:
        """Pin a running job to an account inside the caller's transaction.

        The partial unique index ``ux_jobs_running_account`` rejects a second
        running job on the same account; that surfaces as ``AccountBusy``.
        """

        if not isinstance(account, str) or not account:
            raise StoreError("account is required", code=ErrorCode.INVALID_ARGUMENT)

        def _run(handle: _Txn) -> None:
            try:
                cursor = handle.execute(
                    "UPDATE jobs SET resolved_account = ?"
                    " WHERE job_id = ? AND status = 'running'",
                    (account, job_id),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountBusy(
                    f"account {account!r} already has a job in flight"
                ) from exc
            if cursor.rowcount != 1:
                raise JobStateError(
                    f"job {job_id} is not running", code=ErrorCode.CONFLICT
                )

        if txn is not None:
            _run(txn)
            return
        with self.transaction() as handle:
            _run(handle)

    # -- session ownership (section 5.2 problem B) ---------------------

    def get_session_owner(
        self, conversation_id: str, *, txn: _Txn | None = None
    ) -> dict | None:
        sql = "SELECT * FROM session_owners WHERE conversation_id = ?"
        params: Sequence[Any] = (conversation_id,)
        if txn is not None:
            row = txn.fetchone(sql, params)
        else:
            rows = self._query(sql, params)
            row = rows[0] if rows else None
        return None if row is None else dict(row)

    def bind_session_owner(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        principal_id: str,
        account: str,
        txn: _Txn | None = None,
    ) -> dict:
        """Insert the owner row, or verify the existing one matches.

        Idempotent for the same tuple; ``SessionConflict`` for a different
        owner or a different account.  An owner is never rewritten.
        """

        for name, value in (
            ("conversation_id", conversation_id),
            ("tenant_id", tenant_id),
            ("principal_id", principal_id),
            ("account", account),
        ):
            if not isinstance(value, str) or not value:
                raise StoreError(
                    f"{name} is required", code=ErrorCode.INVALID_ARGUMENT
                )
        created_at = self._now()

        def _run(handle: _Txn) -> dict:
            handle.execute(
                """
                INSERT INTO session_owners
                    (conversation_id, tenant_id, principal_id, account, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO NOTHING
                """,
                (conversation_id, tenant_id, principal_id, account, created_at),
            )
            row = handle.fetchone(
                "SELECT * FROM session_owners WHERE conversation_id = ?",
                (conversation_id,),
            )
            if row is None:  # pragma: no cover - defensive
                raise StoreError(
                    "owner row vanished during insert", code=ErrorCode.INTERNAL
                )
            owner = dict(row)
            if (
                owner["tenant_id"] != tenant_id
                or owner["principal_id"] != principal_id
                or owner["account"] != account
            ):
                raise SessionConflict(
                    f"conversation {conversation_id} is owned by "
                    f"{owner['tenant_id']}/{owner['principal_id']} on "
                    f"{owner['account']}"
                )
            return owner

        if txn is not None:
            return _run(txn)
        with self.transaction() as handle:
            return _run(handle)

    def list_session_owners(
        self, *, tenant_id: str, principal_id: str, limit: int = 200
    ) -> list[dict]:
        """``session.list`` is owner-scoped: never another principal's rows."""

        rows = self._query(
            """
            SELECT * FROM session_owners
             WHERE tenant_id = ? AND principal_id = ?
             ORDER BY created_at DESC, conversation_id
             LIMIT ?
            """,
            (tenant_id, principal_id, int(limit)),
        )
        return [dict(row) for row in rows]
