"""Execution loop for the sender sidecar (PR-1).

The worker never touches the ChatGPT CLI: it is handed two callables
(``send`` and ``create_session``) and knows nothing about subprocesses,
cookies or file layouts.  PR-2 supplies the real adapter; tests supply fakes.

Rules enforced here (report section 2.6, section 1.2):

* one in-flight send per account -- delegated to the partial unique index via
  ``AccountRouter.reserve``, so a second job for a busy account is put back in
  the queue and *nothing* is sent;
* a ``running`` row whose lease expired is reconciled first, and an unknown
  remote outcome becomes ``failed / OUTCOME_UNKNOWN`` -- never a resend;
* any unexpected exception out of the injected callable means "the remote may
  have received it", which is also ``OUTCOME_UNKNOWN``;
* a declared ``GatewayError`` (429, dependency down, ...) fails the job with
  that code and is never retried automatically.

Run sample::

    worker = Worker(store, router, send=adapter.send,
                    create_session=adapter.create_session)
    worker.reconcile_expired_leases(probe=adapter.probe_send)
    worker.run_until_idle()
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Iterable, Mapping

from .account_router import AccountRouter, Reservation
from .job_store import JobStore
from .models import (
    AccountBusy,
    DependencyUnavailable,
    ErrorCode,
    GatewayError,
    JobStateError,
    Operation,
    OwnershipPersistFailed,
    SessionConflict,
    sanitize_result,
)

#: ``send(*, conversation_id, account, prompt, tenant_id, principal_id) -> dict``
SendFn = Callable[..., Mapping[str, Any]]
#: ``create_session(*, account) -> dict`` with a ``conversation_id`` key
CreateSessionFn = Callable[..., Mapping[str, Any]]
#: ``probe(job) -> {"sent": bool, "result": dict} | None``; ``None`` = unknown
ProbeFn = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


ACCOUNT_BUSY_BACKOFF_SECONDS = 5.0


class Worker:
    """Claims jobs and drives them to a terminal state."""

    def __init__(
        self,
        store: JobStore,
        router: AccountRouter,
        *,
        worker_id: str | None = None,
        send: SendFn | None = None,
        create_session: CreateSessionFn | None = None,
        lease_seconds: float | None = None,
        now=None,
    ) -> None:
        self._store = store
        self._router = router
        self._worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self._send = send
        self._create_session = create_session
        self._lease_seconds = (
            self._store.lease_seconds if lease_seconds is None else float(lease_seconds)
        )
        self._now = now or store.now

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # -- loops ---------------------------------------------------------

    def run_once(self) -> bool:
        """Claim at most one job; return False when idle or execution yields."""

        job = self._store.claim_next(
            self._worker_id, lease_seconds=self._lease_seconds
        )
        if job is None:
            return False
        # Preserve the execution/yield signal: AccountBusy releases the job
        # without a side effect and must make the production loop back off.
        return self._execute(job)

    def run_until_idle(self, *, max_iterations: int = 500) -> int:
        """Drain the queue; stop when empty or when an account slot is busy."""

        processed = 0
        for _ in range(int(max_iterations)):
            job = self._store.claim_next(
                self._worker_id, lease_seconds=self._lease_seconds
            )
            if job is None:
                break
            processed += 1
            if not self._execute(job):
                break
        return processed

    # -- lease reconciliation ------------------------------------------

    def reconcile_expired_leases(self, *, probe: ProbeFn | None = None) -> int:
        """Resolve expired leases.  Unknown outcome fails; it is never resent.

        ``probe`` may confirm the remote state.  Anything other than an
        explicit ``{"sent": True, "result": ...}`` verdict -- including a
        ``None`` return or an exception -- is treated as unknown, because the
        whole point is that the prompt may already be on the wire.
        """

        reconciled = 0
        for job in self._store.expired_leases(now=self._now()):
            if not self._store.steal_expired_lease(
                job["job_id"],
                self._worker_id,
                lease_seconds=self._lease_seconds,
                now=self._now(),
            ):
                continue  # another worker owns the reconciliation
            verdict: Mapping[str, Any] | None = None
            if probe is not None:
                try:
                    verdict = probe(job)
                except Exception:  # noqa: BLE001 - unknown stays unknown
                    verdict = None
            if (
                isinstance(verdict, Mapping)
                and verdict.get("sent") is True
                and isinstance(verdict.get("result"), Mapping)
            ):
                self._settle(job["job_id"], "complete", dict(verdict["result"]))
            else:
                self._settle(job["job_id"], "fail", ErrorCode.OUTCOME_UNKNOWN)
            reconciled += 1
        return reconciled

    # -- one job -------------------------------------------------------

    def _execute(self, job: Mapping[str, Any]) -> bool:
        job_id = job["job_id"]
        try:
            reservation = self._router.reserve(
                tenant_id=job["tenant_id"],
                principal_id=job["principal_id"],
                requested_account=job["requested_account"],
                job_id=job_id,
                conversation_id=job["conversation_id"],
            )
        except AccountBusy:
            # Another job is already sending on the chosen account.  Nothing
            # has been sent: requeue and yield the pass.
            self._store.release(
                job_id, delay_seconds=ACCOUNT_BUSY_BACKOFF_SECONDS
            )
            return False
        except GatewayError as exc:
            self._settle(job_id, "fail", exc.code)
            return True

        try:
            if job["operation"] == Operation.SESSION_CREATE.value:
                result = self._create_only(job, reservation)
            else:
                result = self._send_prompt(job, reservation)
        except GatewayError as exc:
            # A mutating CLI operation may have reached the remote before its
            # local process timed out. Missing/malformed receipt is also unknown.
            mutating = job["operation"] in (
                Operation.SESSION_CREATE.value, Operation.SESSION_SEND.value
            )
            uncertain = mutating and (
                exc.code is ErrorCode.TIMEOUT
                or (exc.code is ErrorCode.INTERNAL and "receipt" in str(exc).lower())
            )
            self._settle(job_id, "fail", exc.code, outcome_unknown=uncertain)
            return True
        except Exception:  # noqa: BLE001 - the remote may have received it
            self._settle(job_id, "fail", ErrorCode.OUTCOME_UNKNOWN)
            return True

        if job["operation"] in (
            Operation.SESSION_CREATE.value, Operation.SESSION_SEND.value
        ) and not (isinstance(result, Mapping) and result.get("turn_id")):
            # A successful CLI exit without a correlated turn receipt is not
            # sufficient evidence to publish a succeeded mutation Job.
            self._settle(job_id, "fail", ErrorCode.OUTCOME_UNKNOWN)
            return True
        self._settle(job_id, "complete", result)
        return True

    def _create_only(
        self, job: Mapping[str, Any], reservation: Reservation
    ) -> dict[str, Any]:
        created = self._create_conversation(
            job, reservation.account, return_result=True
        )
        return sanitize_result(created)

    def _send_prompt(
        self, job: Mapping[str, Any], reservation: Reservation
    ) -> dict[str, Any]:
        conversation_id = job["conversation_id"]
        if conversation_id is None:
            conversation_id = self._create_conversation(job, reservation.account)
        if self._send is None:
            raise DependencyUnavailable("no send callable is configured")
        sent = self._send(
            conversation_id=conversation_id,
            account=reservation.account,
            prompt=job["prompt"],
            # 执行边界复核归属：身份来自已认证的 job，不接受调用方覆盖。
            tenant_id=job["tenant_id"],
            principal_id=job["principal_id"],
        )
        result = sanitize_result(sent)
        result["conversation_id"] = conversation_id
        result["account"] = reservation.account
        return result

    def _create_conversation(
        self, job: Mapping[str, Any], account: str, *, return_result: bool = False
    ) -> str | dict[str, Any]:
        if self._create_session is None:
            raise DependencyUnavailable("no create_session callable is configured")
        created = self._create_session(account=account)
        conversation_id = (created or {}).get("conversation_id")
        if not conversation_id:
            raise DependencyUnavailable(
                "create_session returned no conversation_id"
            )
        # Ownership is written before anything is sent into the conversation.
        # If the owner row cannot be persisted the remote conversation already
        # exists, so this fails closed: the id is kept on the job row for
        # audit/compensation and never becomes readable (no owner row).
        try:
            self._router.bind_session(
                conversation_id=conversation_id,
                tenant_id=job["tenant_id"],
                principal_id=job["principal_id"],
                account=account,
            )
        except SessionConflict:
            raise
        except Exception as exc:  # noqa: BLE001 - any store failure fails closed
            try:
                self._store.attach_conversation(job["job_id"], conversation_id)
            except Exception:  # noqa: BLE001 - best effort, the code is stable
                pass
            raise OwnershipPersistFailed(
                "owner row for %s could not be persisted (%s); the remote "
                "conversation is an orphan" % (conversation_id, type(exc).__name__)
            ) from exc
        # The job row now knows which conversation it produced.
        self._store.attach_conversation(job["job_id"], conversation_id)
        if return_result:
            result = sanitize_result(created)
            result["conversation_id"] = conversation_id
            result["account"] = account
            return result
        return conversation_id

    # -- settling ------------------------------------------------------

    def _settle(
        self, job_id: str, action: str, value: Any, *, outcome_unknown: bool = False
    ) -> None:
        """Apply a transition; a row already settled elsewhere is not rewritten."""

        try:
            if action == "complete":
                self._store.complete(job_id, dict(value))
            else:
                self._store.fail(job_id, value, outcome_unknown=outcome_unknown)
        except JobStateError:
            # Somebody else already moved the row.  Terminal states stay put.
            return


def execute_jobs(
    store: JobStore,
    router: AccountRouter,
    jobs: Iterable[Mapping[str, Any]],
    **kwargs: Any,
) -> int:  # pragma: no cover - convenience helper, not used by tests
    """Convenience wrapper: reconcile, then drain once per id in ``jobs``."""

    worker = Worker(store, router, **kwargs)
    worker.reconcile_expired_leases()
    return worker.run_until_idle()
