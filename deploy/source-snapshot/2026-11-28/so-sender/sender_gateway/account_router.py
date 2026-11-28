"""Account selection and session ownership for the sender sidecar (PR-1).

Report section 1.5 fixes the routing table; section 2.6 fixes the concurrency
rules; section 5.2 problem B adds caller ownership.  This module implements
exactly that:

======================  =============================================
situation               behaviour
======================  =============================================
new session + ``auto``  rotate over the pool (SQLite transaction)
new session + account   use that account, or ``RESOURCE_EXHAUSTED``
existing session        the recorded owner's account, always
existing session + a    ``CONFLICT`` (never a silent switch)
different account
no owner row            ``QUARANTINED_SESSION``
another principal       ``NOT_FOUND`` (existence is not leaked)
every account cooling   ``RESOURCE_EXHAUSTED``
``nologin``             rejected at construction and on every path
======================  =============================================

Rotation state lives in SQLite, never in a JSON file, and the read-modify-write
happens inside one ``BEGIN IMMEDIATE`` transaction.  That is the fix for the
old ``max+1`` style allocation: two workers can no longer read the same
counter and hand out the same account.

Account pinning and ownership writes are composed into the *same* transaction
as the job claim via :meth:`JobStore.assign_account` / :meth:`JobStore.bind_session_owner`
and their ``txn=`` parameter.  The one case that cannot be a single
transaction is a brand-new conversation: its id does not exist until the
remote create returns, so rotation (transaction A) and the owner insert
(transaction B) are necessarily separate.  See docs/SENDER-GATEWAY.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import random
from pathlib import Path
from typing import Sequence

from .job_store import JobStore
from .models import (
    AccountBusy,
    ErrorCode,
    GatewayError,
    ResourceExhausted,
    SessionConflict,
    SessionNotFound,
    SessionQuarantined,
    StoreError,
    add_seconds,
    utc_now_iso,
)

#: The six sending accounts of report section 1.5.  ``nologin`` is the
#: anonymous browser fallback that section 5.1 forbids; it must never appear.
# acct4 is auth-revoked and acct5 is operator-disabled. Keep both out of
# every automatic and pinned reservation until an explicit health recovery.
class AccountCoolingDown(ResourceExhausted):
    """Retryable account cooldown carrying the remaining delay."""

    def __init__(self, account: str | None, retry_after_seconds: float) -> None:
        self.account = account
        delay = max(1.0, float(retry_after_seconds))
        label = account or "all active accounts"
        super().__init__(
            f"{label} rate limited; retry in {delay:.1f}s",
            retry_after_seconds=delay,
        )
        self.retry_after_seconds = delay


DEFAULT_ACCOUNTS: tuple[str, ...] = (
    "acct1", "acct2", "acct3", "acct4", "acct5", "acct6",
)

FORBIDDEN_ACCOUNT_MARKER = "nologin"
AUTO = "auto"


def discover_accounts(accounts_dir: str | Path) -> tuple[str, ...]:
    """Return credential-backed acctN directories in numeric order."""
    root = Path(accounts_dir)
    if not root.is_dir():
        return ()
    return tuple(sorted(
        (
            entry.name for entry in root.iterdir()
            if entry.is_dir()
            and entry.name.startswith("acct")
            and entry.name[4:].isdigit()
            and (entry / "cookies.json").is_file()
            and (entry / "access_token.txt").is_file()
        ),
        key=lambda name: int(name[4:]),
    ))

_ROUTER_SCHEMA: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS account_rotation (
        pool TEXT PRIMARY KEY,
        last_index INTEGER NOT NULL,
        last_account TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_cooldowns (
        account TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        until_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_health (
        account TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK (state IN ('active', 'auth_unavailable', 'operator_disabled')),
        reason TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

_DEFAULT_ACCOUNT_HEALTH = (
    ("acct4", "auth_unavailable", "upstream HTTP 401; awaiting authorized reauthentication and read-only probe"),
    ("acct5", "operator_disabled", "operator disabled; do not reauthenticate"),
)
_ACCOUNT_HEALTH_STATES = frozenset(("active", "auth_unavailable", "operator_disabled"))


@dataclass(frozen=True)
class Reservation:
    """The routing decision for one job."""

    account: str
    conversation_id: str | None
    new_session: bool

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "conversation_id": self.conversation_id,
            "new_session": self.new_session,
        }


class AccountRouter:
    """Chooses the account for a job and guards conversation ownership."""

    def __init__(
        self,
        store: JobStore,
        *,
        accounts: Sequence[str] = DEFAULT_ACCOUNTS,
        pool: str = "default",
        cooldown_seconds: float = 60.0,
        now=None,
        jitter_fn=None,
    ) -> None:
        candidates = tuple(accounts)
        if not candidates:
            raise ValueError("AccountRouter needs at least one account")
        for account in candidates:
            if not isinstance(account, str) or not account.strip():
                raise ValueError(f"invalid account {account!r}")
            if FORBIDDEN_ACCOUNT_MARKER in account.lower():
                raise ValueError(
                    f"account {account!r} is the forbidden anonymous fallback"
                )
        self._store = store
        self._accounts = candidates
        self._pool = pool
        self._cooldown_seconds = float(cooldown_seconds)
        self._jitter_fn = jitter_fn or random.uniform
        self._now = now or store.now
        self._ensure_schema()

    # -- setup ---------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._store.transaction() as txn:
            for statement in _ROUTER_SCHEMA:
                txn.execute(statement)
            rotation_columns = {
                row["name"] for row in txn.fetchall("PRAGMA table_info(account_rotation)")
            }
            if "last_account" not in rotation_columns:
                txn.execute("ALTER TABLE account_rotation ADD COLUMN last_account TEXT")
                txn.execute("UPDATE account_rotation SET last_index = -1, last_account = NULL")
            now = self._now()
            for account, state, reason in _DEFAULT_ACCOUNT_HEALTH:
                txn.execute(
                    "INSERT INTO account_health (account, state, reason, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(account) DO NOTHING",
                    (account, state, reason, now),
                )

    @property
    def accounts(self) -> tuple[str, ...]:
        return self._accounts

    def active_accounts(self) -> tuple[str, ...]:
        with self._store.transaction() as txn:
            return tuple(a for a in self._accounts if self._account_state(a, txn) == "active")

    def get_account_health(self, account: str) -> dict:
        account = self._validate_requested(account)
        with self._store.transaction() as txn:
            row = txn.fetchone("SELECT state, reason, updated_at FROM account_health WHERE account = ?", (account,))
            if row is None:
                return {"account": account, "state": "active", "reason": "", "updated_at": None}
            return {"account": account, **dict(row)}

    def _account_state(self, account: str, txn) -> str:
        row = txn.fetchone("SELECT state FROM account_health WHERE account = ?", (account,))
        return "active" if row is None else row["state"]

    def _assert_active(self, account: str, txn) -> None:
        state = self._account_state(account, txn)
        if state != "active":
            raise ResourceExhausted(f"account {account!r} is unavailable ({state})")

    def _set_health(self, account: str, state: str, reason: str) -> None:
        account = self._validate_requested(account)
        if state not in _ACCOUNT_HEALTH_STATES:
            raise StoreError("invalid account health state", code=ErrorCode.INVALID_ARGUMENT)
        with self._store.transaction() as txn:
            txn.execute(
                "INSERT INTO account_health (account, state, reason, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(account) DO UPDATE SET state=excluded.state, reason=excluded.reason, updated_at=excluded.updated_at",
                (account, state, str(reason), self._now()),
            )

    def mark_auth_unavailable(self, account: str, *, reason: str = "upstream HTTP 401") -> None:
        self._set_health(account, "auth_unavailable", reason)

    def mark_operator_disabled(self, account: str, *, reason: str = "operator disabled") -> None:
        self._set_health(account, "operator_disabled", reason)

    def restore_account(self, account: str, *, operator_authorized: bool = False, probe_ok: bool = False) -> None:
        if operator_authorized is not True or probe_ok is not True:
            raise StoreError("account restore requires authorized reauthentication and a successful read-only probe", code=ErrorCode.PERMISSION_DENIED)
        self._set_health(account, "active", "authorized reauthentication and read-only probe passed")

    # -- validation ----------------------------------------------------

    def _validate_requested(self, requested_account: str | None) -> str:
        if not isinstance(requested_account, str) or not requested_account.strip():
            raise StoreError(
                "account must be a non-empty string",
                code=ErrorCode.INVALID_ARGUMENT,
            )
        if FORBIDDEN_ACCOUNT_MARKER in requested_account.lower():
            raise StoreError(
                "'nologin' is the forbidden anonymous fallback",
                code=ErrorCode.INVALID_ARGUMENT,
            )
        if requested_account not in self._accounts:
            raise StoreError(
                f"unknown account {requested_account!r}",
                code=ErrorCode.INVALID_ARGUMENT,
            )
        return requested_account

    # -- cooldowns (429) -----------------------------------------------

    def mark_rate_limited(
        self,
        account: str,
        *,
        reason: str = "upstream HTTP 429",
        cooldown_seconds: float | None = None,
    ) -> float:
        """Persist a bounded Retry-After with small positive jitter."""
        account = self._validate_requested(account)
        seconds = self._cooldown_seconds if cooldown_seconds is None else float(cooldown_seconds)
        if not math.isfinite(seconds):
            seconds = self._cooldown_seconds
        seconds = max(1.0, min(3600.0, seconds))
        jitter_cap = min(5.0, seconds * 0.1)
        jitter = max(0.0, min(jitter_cap, float(self._jitter_fn(0.0, jitter_cap))))
        effective_seconds = min(3600.0, seconds + jitter)
        until_at = add_seconds(self._now(), effective_seconds)
        with self._store.transaction() as txn:
            txn.execute(
                """
                INSERT INTO account_cooldowns (account, reason, until_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account) DO UPDATE
                    SET reason = excluded.reason, until_at = excluded.until_at
                """,
                (account, reason, until_at),
            )
        return effective_seconds

    def clear_rate_limit(self, account: str) -> None:
        account = self._validate_requested(account)
        with self._store.transaction() as txn:
            txn.execute("DELETE FROM account_cooldowns WHERE account = ?", (account,))

    def _cooldown_remaining_seconds(self, account: str, txn) -> float:
        row = txn.fetchone(
            "SELECT until_at FROM account_cooldowns WHERE account = ?", (account,)
        )
        if row is None:
            return 0.0
        try:
            until = datetime.fromisoformat(row["until_at"].replace("Z", "+00:00"))
            now = datetime.fromisoformat(self._now().replace("Z", "+00:00"))
            return max(0.0, (until - now).total_seconds())
        except (TypeError, ValueError):
            return self._cooldown_seconds

    def _is_cooling(self, account: str, txn) -> bool:
        return self._cooldown_remaining_seconds(account, txn) > 0.0

    # -- rotation ------------------------------------------------------

    def next_account(
        self,
        *,
        requested_account: str = AUTO,
        txn=None,
    ) -> str:
        """Return the next account, rotating atomically when asked for auto."""

        if txn is not None:
            return self._next_account(requested_account, txn)
        with self._store.transaction() as handle:
            return self._next_account(requested_account, handle)

    def _next_account(self, requested_account: str | None, txn) -> str:
        if requested_account not in (None, AUTO):
            account = self._validate_requested(requested_account)
            self._assert_active(account, txn)
            remaining = self._cooldown_remaining_seconds(account, txn)
            if remaining > 0:
                raise AccountCoolingDown(account, remaining)
            return account
        row = txn.fetchone("SELECT last_index, last_account FROM account_rotation WHERE pool = ?", (self._pool,))
        if row is None:
            start = 0
        elif row["last_account"] in self._accounts:
            start = (self._accounts.index(row["last_account"]) + 1) % len(self._accounts)
        else:
            previous = int(row["last_index"])
            start = (previous + 1) % len(self._accounts) if previous >= 0 else 0
        ordered = self._accounts[start:] + self._accounts[:start]
        active = [a for a in ordered if self._account_state(a, txn) == "active"]
        if not active:
            raise ResourceExhausted("no active account is available")
        available = [a for a in active if not self._is_cooling(a, txn)]
        if not available:
            remaining = min(self._cooldown_remaining_seconds(a, txn) for a in active)
            raise AccountCoolingDown(None, remaining)
        account = available[0]
        index = self._accounts.index(account)
        now = self._now()
        if row is None:
            txn.execute("INSERT INTO account_rotation (pool, last_index, last_account, updated_at) VALUES (?, ?, ?, ?)", (self._pool, index, account, now))
        else:
            cursor = txn.execute("UPDATE account_rotation SET last_index=?, last_account=?, updated_at=? WHERE pool=?", (index, account, now, self._pool))
            if cursor.rowcount != 1:
                raise GatewayError("rotation row disappeared", code=ErrorCode.INTERNAL)
        return account

    # -- routing -------------------------------------------------------

    def reserve(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        requested_account: str = AUTO,
        job_id: str,
        conversation_id: str | None = None,
    ) -> Reservation:
        """Pick the account and pin the job to it in one transaction."""

        for name, value in (
            ("tenant_id", tenant_id),
            ("principal_id", principal_id),
            ("job_id", job_id),
        ):
            if not isinstance(value, str) or not value:
                raise StoreError(
                    f"{name} is required", code=ErrorCode.INVALID_ARGUMENT
                )
        requested = requested_account if requested_account else AUTO

        with self._store.transaction() as txn:
            if conversation_id is not None:
                owner = self._store.get_session_owner(conversation_id, txn=txn)
                if owner is None:
                    raise SessionQuarantined(
                        f"conversation {conversation_id} has no trusted owner"
                    )
                if (
                    owner["tenant_id"] != tenant_id
                    or owner["principal_id"] != principal_id
                ):
                    # Another principal's session: do not leak that it exists.
                    raise SessionNotFound(f"conversation {conversation_id} not found")
                account = owner["account"]
                self._assert_active(account, txn)
                if requested not in (AUTO, account):
                    raise SessionConflict(
                        f"conversation {conversation_id} belongs to account "
                        f"{account!r}, not {requested!r}"
                    )
                remaining = self._cooldown_remaining_seconds(account, txn)
                if remaining > 0:
                    # No failover: the ownership row is immutable.
                    raise AccountCoolingDown(account, remaining)
                self._store.assign_account(job_id, account, txn=txn)
                return Reservation(
                    account=account,
                    conversation_id=conversation_id,
                    new_session=False,
                )

            if requested not in (AUTO, None):
                account = self._next_account(requested, txn)
                self._store.assign_account(job_id, account, txn=txn)
                return Reservation(account=account, conversation_id=None, new_session=True)

            last_error: Exception | None = None
            for _ in range(len(self._accounts)):
                account = self._next_account(AUTO, txn)
                try:
                    self._store.assign_account(job_id, account, txn=txn)
                except AccountBusy as exc:
                    last_error = exc
                    continue
                return Reservation(
                    account=account, conversation_id=None, new_session=True
                )
            if last_error is not None:
                raise last_error
            raise ResourceExhausted("no account is available")  # pragma: no cover

    # -- ownership -----------------------------------------------------

    def bind_session(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        principal_id: str,
        account: str,
    ) -> dict:
        """Bind a freshly created conversation to its owner and account."""

        account = self._validate_requested(account)
        with self._store.transaction() as txn:
            self._assert_active(account, txn)
            existing = self._store.get_session_owner(conversation_id, txn=txn)
            if existing is not None:
                if (
                    existing["tenant_id"] != tenant_id
                    or existing["principal_id"] != principal_id
                    or existing["account"] != account
                ):
                    raise SessionConflict(
                        f"conversation {conversation_id} is already bound to "
                        f"{existing['account']!r}"
                    )
                return existing
            return self._store.bind_session_owner(
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                account=account,
                txn=txn,
            )

    def bind_new_session(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        principal_id: str,
        account: str,
        job_id: str,
    ) -> Reservation:
        """Rotation *and* binding in one transaction.

        For the case where the conversation id is already known before the
        account is chosen (e.g. re-attaching a job to an id the adapter handed
        back).  ``bind_session`` is the two-step variant.
        """

        account = self._validate_requested(account)
        with self._store.transaction() as txn:
            self._assert_active(account, txn)
            self._store.bind_session_owner(
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                account=account,
                txn=txn,
            )
            self._store.assign_account(job_id, account, txn=txn)
        return Reservation(
            account=account, conversation_id=conversation_id, new_session=True
        )

    def get_owner(self, conversation_id: str) -> dict | None:
        return self._store.get_session_owner(conversation_id)

    def require_owner(
        self,
        conversation_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        requested_account: str = AUTO,
    ) -> str:
        """Resolve a session's account from SQLite, or refuse.

        This is the single read-only authority check shared by the HTTP read
        path and the adapter: ``session_owners`` is the only source, the
        tenant/principal triple is enforced, and there is no JSON fallback.
        """

        for name, value in (
            ("conversation_id", conversation_id),
            ("tenant_id", tenant_id),
            ("principal_id", principal_id),
        ):
            if not isinstance(value, str) or not value:
                raise StoreError(
                    f"{name} is required", code=ErrorCode.INVALID_ARGUMENT
                )
        owner = self._store.get_session_owner(conversation_id)
        if owner is None:
            raise SessionQuarantined(
                f"conversation {conversation_id} has no trusted owner"
            )
        if (
            owner["tenant_id"] != tenant_id
            or owner["principal_id"] != principal_id
        ):
            # Another principal's session: do not leak that it exists.
            raise SessionNotFound(f"conversation {conversation_id} not found")
        account = owner["account"]
        if requested_account not in (None, AUTO, account):
            raise SessionConflict(
                f"conversation {conversation_id} belongs to account "
                f"{account!r}, not {requested_account!r}"
            )
        with self._store.transaction() as txn:
            self._assert_active(account, txn)
        return account

    def list_owners(self, *, tenant_id: str, principal_id: str) -> list[dict]:
        return self._store.list_session_owners(
            tenant_id=tenant_id, principal_id=principal_id
        )


def rotation_snapshot(store: JobStore, pool: str = "default") -> dict | None:
    """Read-only helper for diagnostics and tests."""

    rows = store._query(  # noqa: SLF001 - diagnostics helper
        "SELECT pool, last_index, updated_at FROM account_rotation WHERE pool = ?",
        (pool,),
    )
    return None if not rows else dict(rows[0])


def default_now() -> str:  # pragma: no cover - convenience re-export
    return utc_now_iso()
