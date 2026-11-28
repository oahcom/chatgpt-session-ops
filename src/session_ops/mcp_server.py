"""MCP tool service and Streamable HTTP binding.

The service layer is independent of the MCP SDK. Only build_mcp_server()
imports the optional SDK.

Python source syntax: 3.11+
MCP SDK runtime: mcp==2.3.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from src.session_ops.authority import (
    AuthorityError,
    InvalidIdentifier,
    InvalidPrincipal,
    InvalidTransition,
    NotAuthorized,
    PrincipalDirectoryUnavailable,
    QuarantineNotCleared,
    SessionAlreadyLinked,
    UnconfirmedSession,
)
from src.session_ops.mcp_auth import (
    AuthContext,
    AuthenticationError,
    JWTVerifier,
    ScopeError,
    required_scopes_for_tool,
)


# ---------------------------------------------------------------------------
# Frozen limits
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE = 25
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 50
MAX_CURSOR_CHARS = 2048

MAX_CONTEXT_ENTRIES = 6
MAX_CONTEXT_CONTENT_CHARS = 2048
MAX_CONTEXT_CONTENT_BYTES = 8192
MAX_QUERY_CHARS = 2048
# Kept for parity with authority.py, but unused here: 4 * MAX_QUERY_CHARS ==
# MAX_QUERY_BYTES, so a UTF-8 byte check could never fire on a query that the
# schema maxLength already accepted. _validate_arguments bounds query length
# with the schema alone.
MAX_QUERY_BYTES = 8192

MAX_HTTP_BODY_BYTES = 65536
MAX_MCP_RESPONSE_BYTES = 65536
# Leave room for the JSON-RPC and CallToolResult envelopes.
MAX_STRUCTURED_CONTENT_BYTES = MAX_MCP_RESPONSE_BYTES - 2048

DB_DEADLINE_SECONDS = 3.0
ADAPTER_DEADLINE_SECONDS = 8.0
REQUEST_DEADLINE_SECONDS = 10.0
AUTH_VERIFY_DEADLINE_SECONDS = 3.5

MCP_SERVER_NAME = "chatgpt-session-ops"
MCP_SERVER_VERSION = "0.1.0"

# A bounded shared pool. A timed-out SQLite call is still bounded by the
# authority store's own SQLite timeout/busy_timeout configuration.
_DB_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="session-ops-db",
)

# Unauthenticated requests must not consume the event loop's shared default
# executor. Bound both active and queued verification work independently.
_AUTH_VERIFY_WORKERS = 4
_AUTH_VERIFY_MAX_INFLIGHT = 8
_AUTH_EXECUTOR = ThreadPoolExecutor(
    max_workers=_AUTH_VERIFY_WORKERS,
    thread_name_prefix="session-ops-auth",
)
_AUTH_VERIFY_SLOTS = threading.BoundedSemaphore(
    _AUTH_VERIFY_MAX_INFLIGHT
)


class _DeadlineExceeded(Exception):
    pass


class _ResourceExhausted(Exception):
    """A bounded service resource or payload limit was exceeded."""


# ---------------------------------------------------------------------------
# Tool schemas: frozen v0.1 surface
# ---------------------------------------------------------------------------

def _object_schema(
    properties: Dict[str, Dict[str, Any]],
    required: Sequence[str] = (),
) -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_ID = {"type": "string", "minLength": 1}
_WORK_ITEM_ID = {
    "type": "string",
    "pattern": r"^wi_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
}
_ATTEMPT_ID = {
    "type": "string",
    "pattern": r"^att_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
}
_PROMPT = {"type": "string", "minLength": 1, "maxLength": 40000}
_ACCOUNT = {"type": "string", "minLength": 1, "maxLength": 32}
_SESSION_ID = {
    "type": "string",
    "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
}
_LINK_ID = {
    "type": "string",
    "pattern": r"^sl_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
}
_PAGE_SIZE = {
    "type": "integer",
    "minimum": MIN_PAGE_SIZE,
    "maximum": MAX_PAGE_SIZE,
    "default": DEFAULT_PAGE_SIZE,
}
# maxLength is the single source of the cursor length limit.
_CURSOR = {"type": "string", "maxLength": MAX_CURSOR_CHARS}
_STATUS = {
    "type": "string",
    "enum": ["active", "paused", "completed", "cancelled"],
}
_GRANT_SCOPE = {"type": "string", "enum": ["read", "write"]}

TOOL_SCHEMAS = {
    "work_item.create": _object_schema({}),
    "work_item.get": _object_schema(
        {"work_item_id": _WORK_ITEM_ID},
        ("work_item_id",),
    ),
    "work_item.list": _object_schema(
        {
            "status": _STATUS,
            "page_size": _PAGE_SIZE,
            "cursor": _CURSOR,
        }
    ),
    "work_item.update": _object_schema(
        {
            "work_item_id": _WORK_ITEM_ID,
            "status": _STATUS,
            "expected_updated_at": _ID,
        },
        ("work_item_id", "status"),
    ),
    "work_item.grant": _object_schema(
        {
            "work_item_id": _WORK_ITEM_ID,
            "target_principal_id": _ID,
            "scope": _GRANT_SCOPE,
        },
        ("work_item_id", "target_principal_id", "scope"),
    ),
    "work_item.revoke": _object_schema(
        {
            "work_item_id": _WORK_ITEM_ID,
            "target_principal_id": _ID,
            "scope": _GRANT_SCOPE,
        },
        ("work_item_id", "target_principal_id", "scope"),
    ),
    "session.link": _object_schema(
        {
            "session_id": _SESSION_ID,
            "work_item_id": _WORK_ITEM_ID,
            "attempt_id": _ATTEMPT_ID,
        },
        ("session_id", "work_item_id"),
    ),
    "session.unlink": _object_schema(
        {
            "session_id": _SESSION_ID,
            "expected_link_id": _LINK_ID,
        },
        ("session_id",),
    ),
    "session.list": _object_schema(
        {
            "work_item_id": _WORK_ITEM_ID,
            "include_unlinked": {"type": "boolean", "default": False},
            "page_size": _PAGE_SIZE,
            "cursor": _CURSOR,
        },
        ("work_item_id",),
    ),
    "session.context": _object_schema(
        {
            "session_id": _SESSION_ID,
            "query": {
                "type": "string",
                "minLength": 1,
                # Single source of the query length limit: the validator's
                # string branch enforces this maxLength, so no separate
                # char/byte guard exists in _validate_arguments.
                "maxLength": MAX_QUERY_CHARS,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_CONTEXT_ENTRIES,
                "default": MAX_CONTEXT_ENTRIES,
            },
        },
        ("session_id", "query"),
    ),
    "chatgpt.session.send": _object_schema(
        {
            "work_item_id": _WORK_ITEM_ID,
            "conversation_id": _SESSION_ID,
            "prompt": _PROMPT,
            "account": _ACCOUNT,
            "attempt_id": _ATTEMPT_ID,
        },
        ("work_item_id", "prompt", "attempt_id"),
    ),
    "chatgpt.session.read": _object_schema(
        {
            "work_item_id": _WORK_ITEM_ID,
            "conversation_id": _SESSION_ID,
            "account": _ACCOUNT,
            "max_chars": {"type": "integer", "minimum": 256, "maximum": 32768, "default": 12000},
        },
        ("work_item_id", "conversation_id"),
    ),
}

TOOL_DESCRIPTIONS = {
    "work_item.create": "Create a WorkItem owned by the authenticated principal.",
    "work_item.get": "Get a WorkItem visible to the authenticated principal.",
    "work_item.list": "List visible WorkItems using keyset pagination.",
    "work_item.update": "Update a WorkItem status with optional optimistic concurrency.",
    "work_item.grant": "Grant a same-tenant principal read or write access.",
    "work_item.revoke": "Revoke a same-tenant principal read or write access.",
    "session.link": "Link a confirmed session to a visible WorkItem.",
    "session.unlink": "Unlink the active session link if it is visible.",
    "session.list": "List sessions linked to a visible WorkItem.",
    "session.context": "Retrieve memory only through the authorized active session link.",
    "chatgpt.session.send": "Send through the in-repository ChatGPT runtime. Requires a stable attempt_id; reuse the exact ID and request parameters to safely inspect or retry a timed-out call. Uncertain attempts are never automatically resent.",
    "chatgpt.session.read": "Read the current transcript from a ChatGPT session linked to the specified WorkItem.",
}


# ---------------------------------------------------------------------------
# Common validation, serialization, and envelopes
# ---------------------------------------------------------------------------

_ERROR_MESSAGES = {
    "INVALID_ARGUMENT": "invalid arguments",
    "UNAUTHENTICATED": "authentication failed",
    "PERMISSION_DENIED": "insufficient permission",
    "NOT_FOUND": "resource is not available",
    "CONFLICT": "operation conflicts with the current state",
    "QUARANTINED_SESSION": "session is not confirmed or is quarantined",
    "RESOURCE_EXHAUSTED": "resource limit exceeded",
    "DEPENDENCY_UNAVAILABLE": "required dependency is unavailable",
    "TIMEOUT": "operation timed out",
    "INTERNAL": "internal error",
}

_RETRYABLE = {
    "INVALID_ARGUMENT": False,
    "UNAUTHENTICATED": False,
    "PERMISSION_DENIED": False,
    "NOT_FOUND": False,
    "CONFLICT": False,
    "QUARANTINED_SESSION": False,
    "RESOURCE_EXHAUSTED": True,
    "DEPENDENCY_UNAVAILABLE": True,
    "TIMEOUT": True,
    "INTERNAL": False,
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _success(data: Any, request_id: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "meta": {
            "request_id": request_id,
            "contract_version": "0.1",
        },
    }


def _error(
    code: str,
    request_id: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in _ERROR_MESSAGES:
        code = "INTERNAL"
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": _ERROR_MESSAGES[code],
            "retryable": _RETRYABLE[code],
            "details": details or {},
        },
        "meta": {
            "request_id": request_id,
            "contract_version": "0.1",
        },
    }


def _validate_arguments(name: str, arguments: Any) -> Dict[str, Any]:
    if name not in TOOL_SCHEMAS:
        raise ValueError("unknown tool")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments must be an object")

    schema = TOOL_SCHEMAS[name]
    properties = schema["properties"]
    supplied = dict(arguments)

    # This rejects identity fields as well as every other undeclared field.
    if any(key not in properties for key in supplied):
        raise ValueError("unknown argument")
    for key in schema.get("required", ()):
        if key not in supplied:
            raise ValueError("required argument missing")

    for key, value in supplied.items():
        rule = properties[key]
        expected = rule.get("type")

        if expected == "string":
            if not isinstance(value, str):
                raise ValueError("string argument required")
            if len(value) < rule.get("minLength", 0):
                raise ValueError("string argument too short")
            if len(value) > rule.get("maxLength", 2 ** 31):
                raise ValueError("string argument too long")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError("string argument is not an allowed value")
            if "pattern" in rule:
                import re
                if re.fullmatch(rule["pattern"], value) is None:
                    raise ValueError("string argument has invalid format")

        elif expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("integer argument required")
            if value < rule.get("minimum", value):
                raise ValueError("integer argument below minimum")
            if value > rule.get("maximum", value):
                raise ValueError("integer argument above maximum")

        elif expected == "boolean":
            if not isinstance(value, bool):
                raise ValueError("boolean argument required")

    if "page_size" in properties and "page_size" not in supplied:
        supplied["page_size"] = DEFAULT_PAGE_SIZE
    if name == "session.list" and "include_unlinked" not in supplied:
        supplied["include_unlinked"] = False
    if name == "session.context" and "limit" not in supplied:
        supplied["limit"] = MAX_CONTEXT_ENTRIES

    # cursor and query length are bounded by the schema maxLength enforced in
    # the per-type string branch above, so there is no separate check here.
    if name == "session.context":
        query = supplied["query"]
        if not query.strip():
            raise ValueError("query must not be blank")

    return supplied


def _record_dict(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _record_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_record_dict(item) for item in value]
    return str(value)


def _page_dict(page: Any) -> Dict[str, Any]:
    return {
        "items": [_record_dict(item) for item in page],
        "next_cursor": getattr(page, "next_cursor", None),
    }


def _exception_code(exc: BaseException) -> Tuple[str, Dict[str, Any]]:
    if isinstance(exc, AuthenticationError):
        if getattr(exc, "category", "") == "dependency_unavailable":
            return "DEPENDENCY_UNAVAILABLE", {}
        return "UNAUTHENTICATED", {}

    if isinstance(exc, ScopeError):
        return "PERMISSION_DENIED", {
            "required_scopes": list(exc.required),
        }

    if isinstance(exc, NotAuthorized):
        return "NOT_FOUND", {}

    if isinstance(exc, (QuarantineNotCleared, UnconfirmedSession)):
        return "QUARANTINED_SESSION", {}

    if isinstance(exc, (InvalidTransition, SessionAlreadyLinked)):
        return "CONFLICT", {}

    if isinstance(exc, PrincipalDirectoryUnavailable):
        return "DEPENDENCY_UNAVAILABLE", {}

    if isinstance(exc, (InvalidIdentifier, InvalidPrincipal, ValueError, TypeError)):
        return "INVALID_ARGUMENT", {}

    if isinstance(exc, _DeadlineExceeded):
        return "TIMEOUT", {}

    if isinstance(exc, _ResourceExhausted):
        return "RESOURCE_EXHAUSTED", {}

    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            return "RESOURCE_EXHAUSTED", {}
        return "INTERNAL", {}

    # MemoryAdapterError is imported lazily with MemoryAdapterClient.
    adapter_code = getattr(exc, "code", None)
    if adapter_code == "ADAPTER_TIMEOUT":
        return "TIMEOUT", {}
    if adapter_code in ("ADAPTER_UNAVAILABLE", "ADAPTER_CRASHED"):
        return "DEPENDENCY_UNAVAILABLE", {}
    if adapter_code == "SCOPE_VIOLATION":
        return "INTERNAL", {}

    if isinstance(exc, TimeoutError):
        return "TIMEOUT", {}

    if isinstance(exc, AuthorityError):
        message = str(exc).lower()
        if "changed" in message or "conflict" in message:
            return "CONFLICT", {}
        return "INVALID_ARGUMENT", {}

    return "INTERNAL", {}


# ---------------------------------------------------------------------------
# SDK-independent service
# ---------------------------------------------------------------------------

class MCPToolService:
    """Dispatch MCP tools through WorkItem authority and the local ChatGPT runtime.

    ``memory_adapter`` is the command sequence used to start the Memory
    Adapter, for example ``["python", "-m", "some_adapter_module"]``.
    It is configuration, not a shared MemoryAdapterClient instance.
    """

    def __init__(
        self,
        store: Any,
        *,
        memory_adapter: Optional[Sequence[str]] = None,
    ):
        if store is None:
            raise ValueError("SecureWorkItemStore is required")
        self.store = store
        if memory_adapter is None:
            self.memory_adapter_command = None
        else:
            if isinstance(memory_adapter, (str, bytes)):
                raise ValueError("memory_adapter must be a command sequence")
            command = tuple(memory_adapter)
            if not command or not all(isinstance(part, str) and part for part in command):
                raise ValueError("memory_adapter command is invalid")
            self.memory_adapter_command = command

    @staticmethod
    def _run_db(function: Any) -> Any:
        future = _DB_EXECUTOR.submit(function)
        try:
            return future.result(timeout=DB_DEADLINE_SECONDS)
        except FutureTimeout:
            future.cancel()
            raise _DeadlineExceeded("database operation exceeded deadline")

    def dispatch(
        self,
        name: str,
        arguments: Mapping[str, Any],
        auth: AuthContext,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute one tool and always return the v0.1 envelope."""
        request_id = request_id or str(uuid.uuid4())
        started = time.monotonic()

        try:
            if name not in TOOL_SCHEMAS:
                raise ValueError("unknown tool")
            if not isinstance(auth, AuthContext):
                raise AuthenticationError("verified authentication context required")

            auth.require(*required_scopes_for_tool(name))
            args = _validate_arguments(name, arguments)
            principal = auth.principal

            if name == "work_item.create":
                result = self._run_db(
                    lambda: self.store.create_work_item(principal)
                )
                data = _record_dict(result)

            elif name == "work_item.get":
                result = self._run_db(
                    lambda: self.store.get_work_item(principal, args["work_item_id"])
                )
                data = _record_dict(result)

            elif name == "work_item.list":
                result = self._run_db(
                    lambda: self.store.list_work_items(
                        principal,
                        status=args.get("status"),
                        page_size=args["page_size"],
                        cursor=args.get("cursor"),
                    )
                )
                data = _page_dict(result)

            elif name == "work_item.update":
                result = self._run_db(
                    lambda: self.store.set_status(
                        principal,
                        args["work_item_id"],
                        args["status"],
                        expected_updated_at=args.get("expected_updated_at"),
                        request_id=request_id,
                    )
                )
                data = _record_dict(result)

            elif name == "work_item.grant":
                self._run_db(
                    lambda: self.store.grant(
                        principal,
                        args["work_item_id"],
                        args["target_principal_id"],
                        args["scope"],
                        request_id=request_id,
                    )
                )
                data = {
                    "work_item_id": args["work_item_id"],
                    "target_principal_id": args["target_principal_id"],
                    "scope": args["scope"],
                    "granted": True,
                }

            elif name == "work_item.revoke":
                self._run_db(
                    lambda: self.store.revoke(
                        principal,
                        args["work_item_id"],
                        args["target_principal_id"],
                        args["scope"],
                        request_id=request_id,
                    )
                )
                data = {
                    "work_item_id": args["work_item_id"],
                    "target_principal_id": args["target_principal_id"],
                    "scope": args["scope"],
                    "revoked": True,
                }

            elif name == "session.link":
                result = self._run_db(
                    lambda: self.store.link_session(
                        principal,
                        args["session_id"],
                        args["work_item_id"],
                        args.get("attempt_id"),
                        evidence=None,
                        request_id=request_id,
                    )
                )
                data = _record_dict(result)

            elif name == "session.unlink":
                result = self._run_db(
                    lambda: self.store.unlink_session(
                        principal,
                        args["session_id"],
                        expected_link_id=args.get("expected_link_id"),
                        request_id=request_id,
                    )
                )
                data = {
                    "unlinked": result is not None,
                    "link": _record_dict(result),
                }

            elif name == "session.list":
                result = self._run_db(
                    lambda: self.store.list_sessions(
                        principal,
                        args["work_item_id"],
                        include_unlinked=args["include_unlinked"],
                        page_size=args["page_size"],
                        cursor=args.get("cursor"),
                    )
                )
                data = _page_dict(result)

            elif name == "session.context":
                data = self._session_context(
                    principal=principal,
                    session_id=args["session_id"],
                    query=args["query"],
                    limit=args["limit"],
                    started=started,
                )

            elif name == "chatgpt.session.send":
                data = self._chatgpt_session_send(
                    principal=principal,
                    work_item_id=args["work_item_id"],
                    conversation_id=args.get("conversation_id"),
                    prompt=args["prompt"],
                    account=args.get("account"),
                    attempt_id=args.get("attempt_id"),
                )

            elif name == "chatgpt.session.read":
                data = self._chatgpt_session_read(
                    principal=principal,
                    work_item_id=args["work_item_id"],
                    conversation_id=args["conversation_id"],
                    account=args.get("account"),
                    max_chars=args.get("max_chars", 12000),
                )

            else:
                raise ValueError("unknown tool")

            envelope = _success(data, request_id)
            if len(_json_bytes(envelope)) > MAX_STRUCTURED_CONTENT_BYTES:
                return _error("RESOURCE_EXHAUSTED", request_id)
            return envelope

        except BaseException as exc:
            code, details = _exception_code(exc)
            return _error(code, request_id, details)

    def _chatgpt_session_send(
        self,
        *,
        principal: Any,
        work_item_id: str,
        conversation_id: Optional[str],
        prompt: str,
        account: Optional[str],
        attempt_id: Optional[str],
    ) -> Dict[str, Any]:
        """Send through the in-repository runtime with durable retry protection."""
        from scripts import chatgpt_send as runtime

        if not attempt_id:
            raise ValueError("attempt_id is required and must be reused for retries")

        explicit_account = None if account in (None, "", "auto") else account
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "work_item_id": work_item_id,
                    "conversation_id": conversation_id,
                    "prompt": prompt,
                    "account": explicit_account,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        created, attempt = self._run_db(
            lambda: self.store.reserve_send_attempt(
                principal,
                work_item_id,
                attempt_id,
                request_hash=request_hash,
            )
        )
        if not created:
            previous_status = attempt["status"]
            response_status = (
                "in_progress" if previous_status == "running" else previous_status
            )
            return {
                "work_item_id": work_item_id,
                "attempt_id": attempt_id,
                "conversation_id": attempt.get("conversation_id"),
                "account": attempt.get("account"),
                "status": response_status,
                "http_status": attempt.get("http_status"),
                "error": attempt.get("error"),
                "replayed": True,
            }

        prompt_path = None
        api = None
        resolved_account = None
        outcome_unknown = False
        attempt_error = None
        context = None
        try:
            # All local authorization and account checks happen before the
            # remote side effect. A failed preflight consumes this attempt ID
            # as failed, so a retry cannot accidentally send under new inputs.
            if conversation_id:
                active_link = self._run_db(
                    lambda: self.store.get_active_link(principal, conversation_id)
                )
                if active_link is None or active_link.work_item_id != work_item_id:
                    raise NotAuthorized()
                resolved = runtime.resolve_conversation_account(
                    conversation_id,
                    explicit_account=explicit_account,
                )
                if resolved.get("status") == "conflict":
                    raise NotAuthorized()
                if resolved.get("account") is None or (
                    explicit_account is not None and resolved.get("account") != explicit_account
                ):
                    raise InvalidIdentifier("conversation account is unknown or does not match; specify the linked account")
                resolved_account = resolved["account"]
                api = runtime.ChatGPTAPI(account=resolved_account)

            fd, prompt_path = tempfile.mkstemp(prefix="mcp-prompt-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(prompt)
        except BaseException as exc:
            try:
                self._run_db(
                    lambda: self.store.complete_send_attempt(
                        principal,
                        work_item_id,
                        attempt_id,
                        status="failed",
                        error=type(exc).__name__,
                    )
                )
            except BaseException:
                pass
            if prompt_path is not None:
                try:
                    os.unlink(prompt_path)
                except FileNotFoundError:
                    pass
            raise

        try:
            if conversation_id:
                # Existing active link is already trusted. Reuse it; do not
                # create a second landing event or attempt to link the session again.
                result = runtime.send_prompt(api, conversation_id, prompt_path)
                values = tuple(result) if isinstance(result, (tuple, list)) else ()
                http_status = values[0] if values else None
                conv_ref = values[2] if len(values) > 2 else None
                resolved_conversation_id = conversation_id
                link = active_link
                link_error = None
                if bool(getattr(conv_ref, "mismatch", False)):
                    outcome_unknown = True
                    actual = getattr(conv_ref, "actual_conv_id", "") or "unknown"
                    reason = getattr(conv_ref, "reason", "") or "conversation identity mismatch"
                    link_error = "conversation outcome unknown: %s; actual=%s" % (reason, actual)
                    attempt_error = "conversation_identity_mismatch actual=%s" % actual
                    # ConvRef mismatch is produced only after the remote POST
                    # returned HTTP 200; identity uncertainty is not a safe retry.
                    http_status = 200
                elif http_status == 0 and bool(getattr(api, "last_send_started", False)):
                    outcome_unknown = True
                    link_error = "send outcome unknown after POST transport failure"
            else:
                context = {
                    "store": self.store,
                    "principal": principal,
                    "work_item_id": work_item_id,
                    "attempt_id": attempt_id,
                }
                runtime.create_chat_rotating(
                    prompt_path,
                    work_item_context=context,
                    account=explicit_account,
                )
                link = context.get("link")
                link_error = context.get("link_error")
                http_status = context.get("status")
                resolved_conversation_id = context.get("conversation_id")
                resolved_account = context.get("account")
                outcome_unknown = bool(context.get("outcome_unknown"))
                if outcome_unknown:
                    actual = context.get("actual_conv_id")
                    attempt_error = (
                        "conversation_identity_mismatch actual=%s" % actual
                        if actual else
                        "outcome_unknown_http_%s" % http_status
                        if http_status is not None else
                        "outcome_unknown_no_response"
                    )
                elif link_error:
                    attempt_error = "session_link_failed"

            if outcome_unknown:
                status = "outcome_unknown"
            elif http_status == 200 and link is not None:
                status = "sent"
            elif http_status == 200 and resolved_conversation_id:
                status = "sent_unlinked"
            elif (
                http_status is None and (conversation_id is not None or resolved_account is not None)
            ) or http_status == 200 or (
                isinstance(http_status, int) and http_status in (408, 425) or
                isinstance(http_status, int) and http_status >= 500
            ):
                # A timeout, server error, or accepted response without a
                # conversation ID does not prove non-delivery.
                status = "outcome_unknown"
            else:
                status = "failed"

            if isinstance(link_error, str) and link_error:
                attempt_error = link_error[:1000]
            elif status == "sent_unlinked":
                attempt_error = "session_link_not_confirmed"
            elif status == "outcome_unknown":
                attempt_error = "remote_send_outcome_unknown (status=%s)" % http_status
            elif status == "failed":
                attempt_error = "send_failed (status=%s)" % http_status
            else:
                attempt_error = None
            if status == "outcome_unknown" and attempt_error is None:
                attempt_error = (
                    "outcome_unknown_http_%s" % http_status
                    if http_status is not None else
                    "outcome_unknown_no_response"
                )
            elif status == "sent_unlinked" and attempt_error is None:
                attempt_error = "session_link_failed"

            record = self._run_db(
                lambda: self.store.complete_send_attempt(
                    principal,
                    work_item_id,
                    attempt_id,
                    status=status,
                    conversation_id=resolved_conversation_id,
                    account=resolved_account,
                    http_status=http_status,
                    error=attempt_error,
                )
            )
            return {
                "work_item_id": work_item_id,
                "attempt_id": attempt_id,
                "conversation_id": record.get("conversation_id"),
                "account": record.get("account"),
                "status": record["status"],
                "http_status": record.get("http_status"),
                "error": record.get("error"),
                "replayed": False,
            }
        except BaseException as exc:
            # Only failures after a POST may have reached ChatGPT are unknown.
            # Authorization/account/prompt preflight failures are definitely
            # non-deliveries and must not poison the WorkItem send slot.
            remote_send_started = bool(getattr(api, "last_send_started", False))
            if context is not None:
                context_api = context.get("api")
                remote_send_started = remote_send_started or bool(
                    getattr(context_api, "last_send_started", False)
                ) or bool(context.get("outcome_unknown"))
            attempt_status = "outcome_unknown" if remote_send_started else "failed"
            try:
                self._run_db(
                    lambda: self.store.complete_send_attempt(
                        principal,
                        work_item_id,
                        attempt_id,
                        status=attempt_status,
                        error=type(exc).__name__,
                    )
                )
            except BaseException:
                pass
            raise
        finally:
            if prompt_path is not None:
                try:
                    os.unlink(prompt_path)
                except FileNotFoundError:
                    pass

    def _chatgpt_session_read(
        self,
        *,
        principal: Any,
        work_item_id: str,
        conversation_id: str,
        account: Optional[str],
        max_chars: int,
    ) -> Dict[str, Any]:
        """Read the linked ChatGPT conversation directly through ChatGPTAPI."""
        from scripts import chatgpt_send as runtime

        link = self._run_db(
            lambda: self.store.get_session_link_for_work_item(
                principal, work_item_id, conversation_id
            )
        )
        if link is None:
            raise NotAuthorized()
        explicit_account = None if account in (None, "", "auto") else account
        resolved = runtime.resolve_conversation_account(
            conversation_id,
            explicit_account=explicit_account,
        )
        if resolved.get("status") == "conflict":
            raise NotAuthorized()
        if resolved.get("account") is None:
            raise InvalidIdentifier("conversation account is unknown; specify account")
        api = runtime.ChatGPTAPI(account=resolved["account"])
        tail, _body = api.live_tail(conversation_id, tries=2)
        if tail is None:
            return {
                "work_item_id": work_item_id,
                "conversation_id": conversation_id,
                "account": resolved["account"],
                "status": "unavailable",
                "http_status": getattr(api, "last_status", None),
                "text": "",
                "truncated": False,
            }
        text = str(tail)
        return {
            "work_item_id": work_item_id,
            "conversation_id": conversation_id,
            "account": resolved["account"],
            "status": "ok",
            "http_status": getattr(api, "last_status", None),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    def _session_context(
        self,
        *,
        principal: Any,
        session_id: str,
        query: str,
        limit: int,
        started: float,
    ) -> Dict[str, Any]:
        if self.memory_adapter_command is None:
            raise AuthenticationError(
                "memory adapter is not configured",
                category="dependency_unavailable",
            )

        # The work_item_id is derived only from the authority-checked active
        # link. The caller cannot choose or override it.
        link = self._run_db(
            lambda: self.store.get_active_link(principal, session_id)
        )
        if link is None:
            raise QuarantineNotCleared(
                "an authorized active session link is required"
            )

        remaining = REQUEST_DEADLINE_SECONDS - (time.monotonic() - started)
        adapter_timeout = min(ADAPTER_DEADLINE_SECONDS, remaining)
        if adapter_timeout <= 0:
            raise _DeadlineExceeded("request deadline exceeded")

        # Construct a new client for every call. Never mutate a shared client's
        # timeout_seconds; concurrent requests therefore cannot race on it.
        from src.session_ops.memory_adapter_client import (
            MemoryAdapterClient,
        )

        client = MemoryAdapterClient(
            self.memory_adapter_command,
            timeout_seconds=adapter_timeout,
        )
        retrieved = client.retrieve(
            work_item_id=link.work_item_id,
            session_id=session_id,
            query=query,
            limit=min(limit, MAX_CONTEXT_ENTRIES),
        )

        entries = []
        for entry in retrieved.entries[:MAX_CONTEXT_ENTRIES]:
            content = entry.content
            if (
                len(content) > MAX_CONTEXT_CONTENT_CHARS
                or len(content.encode("utf-8")) > MAX_CONTEXT_CONTENT_BYTES
            ):
                raise _ResourceExhausted(
                    "memory adapter returned oversized content"
                )

            entries.append(
                {
                    "memory_id": entry.memory_id,
                    "session_id": entry.session_id,
                    "content": content,
                    "provenance": _record_dict(entry.provenance),
                    "created_at": entry.created_at,
                    "score": entry.score,
                    "trust": "untrusted_historical_context",
                }
            )

        return {
            "work_item_id": link.work_item_id,
            "session_id": session_id,
            "entries": entries,
        }


# ---------------------------------------------------------------------------
# Streamable HTTP authentication and SDK binding
# ---------------------------------------------------------------------------

def build_mcp_server(
    service: MCPToolService,
    token_provider: JWTVerifier,
    *,
    events_store: Any = None,
) -> Any:
    """Build and return the authenticated Streamable HTTP ASGI application.

    ``token_provider`` is one shared JWTVerifier instance. It is not called
    with zero arguments and has no ``.verifier`` attribute contract. The
    bearer token is extracted independently from every HTTP request.

    The verifier's JWKSProvider uses a lock-protected cache. Per-request
    AuthContext objects are stored only in that request's ASGI scope state.
    """
    if not isinstance(service, MCPToolService):
        raise TypeError("service must be MCPToolService")
    if not callable(getattr(token_provider, "verify", None)):
        raise TypeError("token_provider must be a JWTVerifier instance")
    # The optional SDK is deliberately imported only here.
    from mcp import types
    from mcp.server import Server
    from mcp.server.transport_security import TransportSecuritySettings

    resource = urlsplit(token_provider.audience)
    if resource.scheme != "https" or not resource.netloc:
        raise ValueError("JWTVerifier audience must be an HTTPS resource URI")

    endpoint_path = resource.path or "/mcp"
    if not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    metadata_path = "/.well-known/oauth-protected-resource" + endpoint_path
    resource_metadata_url = (
        resource.scheme + "://" + resource.netloc + metadata_path
    )
    resource_origin = resource.scheme + "://" + resource.netloc

    all_scopes = (
        "session_ops.work_item.read",
        "session_ops.work_item.write",
        "session_ops.session.read",
        "session_ops.session.write",
        "session_ops.context.read",
        # Events subscriptions have a separate lifecycle authority. Keep
        # these scopes explicit in the OAuth challenge rather than silently
        # aliasing them to generic session write access.
        "session_ops.chatgpt.session.read",
        "session_ops.chatgpt.session.write",
    )
    challenge_scope = " ".join(all_scopes)

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        tools = [
            types.Tool(
                name=name,
                description=TOOL_DESCRIPTIONS[name],
                input_schema=TOOL_SCHEMAS[name],
            )
            for name in TOOL_SCHEMAS
        ]
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        request_id = str(ctx.request_id or uuid.uuid4())
        request = getattr(ctx, "request", None)
        auth = None

        if request is not None:
            state = request.scope.get("state", {})
            if isinstance(state, dict):
                auth = state.get("session_ops_auth")

        if not isinstance(auth, AuthContext):
            envelope = _error("UNAUTHENTICATED", request_id)
        else:
            arguments = params.arguments if params.arguments is not None else {}
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                None,
                lambda: service.dispatch(
                    params.name,
                    arguments,
                    auth,
                    request_id=request_id,
                ),
            )
            try:
                envelope = await asyncio.wait_for(
                    future,
                    timeout=REQUEST_DEADLINE_SECONDS,
                )
            except asyncio.TimeoutError:
                envelope = _error("TIMEOUT", request_id)

        # Keep the text fallback small; the complete contract envelope is in
        # structured_content and is size-checked by MCPToolService.
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=(
                        "Tool completed."
                        if envelope["ok"]
                        else envelope["error"]["message"]
                    ),
                )
            ],
            structured_content=envelope,
            is_error=not envelope["ok"],
        )

    server = Server(
        MCP_SERVER_NAME,
        version=MCP_SERVER_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    if events_store is not None:
        # MCP SDK 2.3.0 supports custom request handlers and context middleware,
        # but its ServerCapabilities model predates the ChatGPT MCP Events field.
        # Advertise the field after SDK serialization, without monkey-patching
        # the SDK's Pydantic models or changing non-event deployments.
        from src.session_ops.mcp_events import CallbackEndpointError, build_event_request_models

        EventsListParams, EventsSubscribeParams, EventsUnsubscribeParams = (
            build_event_request_models()
        )

        def _event_auth(ctx: Any) -> Any:
            request = getattr(ctx, "request", None)
            if request is None:
                return None
            state = request.scope.get("state", {})
            return state.get("session_ops_auth") if isinstance(state, dict) else None

        async def on_events_list(ctx: Any, params: Any) -> Any:
            auth = _event_auth(ctx)
            if not isinstance(auth, AuthContext):
                raise AuthenticationError("verified authentication context required")
            events_store._identity(auth)
            if not ({"session_ops.chatgpt.session.read", "session_ops.chatgpt.session.write"} & set(auth.scopes)):
                raise ScopeError(("session_ops.chatgpt.session.read",))
            return {"events": events_store.event_catalog()}

        async def on_events_subscribe(ctx: Any, params: Any) -> Any:
            auth = _event_auth(ctx)
            if not isinstance(auth, AuthContext):
                raise AuthenticationError("verified authentication context required")
            if "session_ops.chatgpt.session.write" not in auth.scopes:
                raise ScopeError(("session_ops.chatgpt.session.write",))
            def create_subscription() -> Any:
                # Event rows and subscriptions are scoped to the verified
                # tenant/principal in MCPEventsStore; no external job service
                # is involved in subscription authorization.
                return events_store.subscribe(
                    auth,
                    params.model_dump(by_alias=True, exclude_unset=True),
                )

            try:
                return await asyncio.to_thread(create_subscription)
            except CallbackEndpointError as exc:
                from mcp.shared.exceptions import MCPError
                raise MCPError(
                    code=-32015,
                    message="Callback endpoint verification failed",
                    data={"reason": exc.reason},
                ) from exc
            except ValueError as exc:
                from mcp.shared.exceptions import MCPError
                raise MCPError(code=-32602, message="Invalid event subscription", data={"reason": "invalid_subscription"}) from exc

        async def on_events_unsubscribe(ctx: Any, params: Any) -> Any:
            auth = _event_auth(ctx)
            if not isinstance(auth, AuthContext):
                raise AuthenticationError("verified authentication context required")
            if "session_ops.chatgpt.session.write" not in auth.scopes:
                raise ScopeError(("session_ops.chatgpt.session.write",))
            def remove_subscription() -> Any:
                return events_store.unsubscribe(
                    auth,
                    params.model_dump(by_alias=True, exclude_unset=True),
                )

            return await asyncio.to_thread(remove_subscription)

        server.add_request_handler("events/list", EventsListParams, on_events_list)
        server.add_request_handler("events/subscribe", EventsSubscribeParams, on_events_subscribe)
        server.add_request_handler("events/unsubscribe", EventsUnsubscribeParams, on_events_unsubscribe)

        async def advertise_events_capability(ctx: Any, call_next: Any) -> Any:
            result = await call_next(ctx)
            if ctx.method in ("initialize", "server/discover") and isinstance(result, dict):
                capabilities = result.get("capabilities")
                if isinstance(capabilities, dict):
                    result["capabilities"] = {**capabilities, "events": {}}
            return result

        server.middleware.append(advertise_events_capability)

    # The SDK enforces the request-body limit in its Streamable HTTP transport.
    # Host/origin checks remain enabled for the public resource and local tests.
    allowed_hosts = [
        resource.netloc,
        resource.netloc + ":*",
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]
    allowed_origins = [
        resource_origin,
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    app = server.streamable_http_app(
        streamable_http_path=endpoint_path,
        max_request_body_size=MAX_HTTP_BODY_BYTES,
        transport_security=transport_security,
    )

    class _BearerAuthASGIMiddleware:
        """Authenticate each HTTP exchange before MCP protocol dispatch."""

        def __init__(self, inner: Any) -> None:
            self.inner = inner

        @staticmethod
        async def _respond(
            send: Any,
            status: int,
            body: bytes,
            extra_headers: Sequence[Tuple[bytes, bytes]] = (),
        ) -> None:
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ]
            headers.extend(extra_headers)
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": headers,
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                }
            )

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            path = scope.get("path", "")
            endpoint_prefix = endpoint_path.rstrip("/")
            is_endpoint_path = (
                path == endpoint_path
                or path.startswith(endpoint_prefix + "/")
            )
            if scope.get("type") != "http" or not is_endpoint_path:
                await self.inner(scope, receive, send)
                return

            headers = {}
            for key, value in scope.get("headers", ()):
                headers[key.decode("latin-1").lower()] = value.decode("latin-1")

            authorization = headers.get("authorization")
            challenge = (
                'Bearer realm="session-ops", resource_metadata="'
                + resource_metadata_url
                + '", scope="'
                + challenge_scope
                + '"'
            )

            if authorization is None:
                await self._respond(
                    send,
                    401,
                    b"",
                    ((b"www-authenticate", challenge.encode("latin-1")),),
                )
                return

            parts = authorization.split(None, 1)
            if (
                len(parts) != 2
                or parts[0].lower() != "bearer"
                or not parts[1].strip()
                or any(ch.isspace() for ch in parts[1].strip())
            ):
                invalid_challenge = challenge + ', error="invalid_token"'
                await self._respond(
                    send,
                    401,
                    b"",
                    ((b"www-authenticate", invalid_challenge.encode("latin-1")),),
                )
                return

            token = parts[1].strip()
            loop = asyncio.get_running_loop()
            if not _AUTH_VERIFY_SLOTS.acquire(blocking=False):
                envelope = _error(
                    "DEPENDENCY_UNAVAILABLE", str(uuid.uuid4())
                )
                await self._respond(send, 503, _json_bytes(envelope))
                return

            try:
                # Use the dedicated bounded pool. Attach the release callback
                # to the concurrent Future, not the asyncio wrapper: a timeout
                # must not free capacity while its worker is still running.
                verify_job = _AUTH_EXECUTOR.submit(
                    token_provider.verify,
                    token,
                )
                verify_job.add_done_callback(
                    lambda _job: _AUTH_VERIFY_SLOTS.release()
                )
                verify_future = asyncio.wrap_future(
                    verify_job,
                    loop=loop,
                )
            except Exception:
                _AUTH_VERIFY_SLOTS.release()
                envelope = _error("INTERNAL", str(uuid.uuid4()))
                await self._respond(send, 500, _json_bytes(envelope))
                return
            try:
                auth = await asyncio.wait_for(
                    verify_future,
                    timeout=AUTH_VERIFY_DEADLINE_SECONDS,
                )
            except asyncio.TimeoutError:
                envelope = _error("DEPENDENCY_UNAVAILABLE", str(uuid.uuid4()))
                await self._respond(send, 503, _json_bytes(envelope))
                return
            except AuthenticationError as exc:
                if getattr(exc, "category", "") == "dependency_unavailable":
                    envelope = _error("DEPENDENCY_UNAVAILABLE", str(uuid.uuid4()))
                    await self._respond(send, 503, _json_bytes(envelope))
                    return
                invalid_challenge = challenge + ', error="invalid_token"'
                await self._respond(
                    send,
                    401,
                    b"",
                    ((b"www-authenticate", invalid_challenge.encode("latin-1")),),
                )
                return
            except Exception:
                envelope = _error("INTERNAL", str(uuid.uuid4()))
                await self._respond(send, 500, _json_bytes(envelope))
                return

            if not isinstance(auth, AuthContext):
                invalid_challenge = challenge + ', error="invalid_token"'
                await self._respond(
                    send,
                    401,
                    b"",
                    ((b"www-authenticate", invalid_challenge.encode("latin-1")),),
                )
                return

            # ASGI scope is request-local. Never write identity to the shared
            # verifier, service, Server, or middleware instance.
            state = scope.setdefault("state", {})
            state["session_ops_auth"] = auth
            await self.inner(scope, receive, send)

    return _BearerAuthASGIMiddleware(app)
