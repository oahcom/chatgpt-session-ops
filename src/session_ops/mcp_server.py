"""MCP tool service and Streamable HTTP binding.

The service layer is independent of the MCP SDK. Only build_mcp_server()
imports the optional SDK.

Python source syntax: 3.11+
MCP SDK runtime: mcp==2.3.0
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
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
from src.session_ops.chatgpt_cli import ChatGPTCLIAdapter, SenderError
from src.session_ops.chatgpt_sessions import (
    ChatGPTSessionRegistry,
    ChatGPTSessionRegistryError,
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
CHATGPT_WRITE_DEADLINE_SECONDS = 1905.0
CHATGPT_READ_DEADLINE_SECONDS = 30.0
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
_ACCOUNT = {"type": "string", "pattern": r"^(?:auto|default|acct[1-9][0-9]*)$", "default": "auto"}
_PROMPT = {"type": "string", "minLength": 1, "maxLength": 32768}
_MESSAGE_ID = {"type": "string", "minLength": 1, "maxLength": 256}
_READ_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}
_IDEMPOTENCY_KEY = {"type": "string", "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
_LIST_CURSOR = {"type": "string", "pattern": r"^[0-9]{1,10}$", "maxLength": 10}

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
    "chatgpt.session.create": _object_schema({
        "account": _ACCOUNT,
        "idempotency_key": _IDEMPOTENCY_KEY,
    }),
    "chatgpt.session.list": _object_schema({
        "page_size": _PAGE_SIZE,
        "cursor": _LIST_CURSOR,
    }),
    "chatgpt.session.send": _object_schema({
        "conversation_id": _SESSION_ID,
        "prompt": _PROMPT,
        "account": _ACCOUNT,
        "idempotency_key": _IDEMPOTENCY_KEY,
    }, ("conversation_id", "prompt")),
    "chatgpt.session.read": _object_schema({
        "conversation_id": _SESSION_ID,
        "after_message_id": _MESSAGE_ID,
        "limit": _READ_LIMIT,
    }, ("conversation_id",)),
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
    "chatgpt.session.create": "Synchronously create a ChatGPT session using the local CLI and return its conversation id.",
    "chatgpt.session.list": "List ChatGPT sessions owned by the authenticated principal.",
    "chatgpt.session.send": "Synchronously send a prompt through the local CLI and return the completed send receipt.",
    "chatgpt.session.read": "Read messages from a ChatGPT session owned by the authenticated principal.",
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
    if name in ("chatgpt.session.create", "chatgpt.session.send") and "account" not in supplied:
        supplied["account"] = "auto"
    if name == "chatgpt.session.list" and "page_size" not in supplied:
        supplied["page_size"] = DEFAULT_PAGE_SIZE
    if name == "chatgpt.session.read" and "limit" not in supplied:
        supplied["limit"] = 20

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
    if isinstance(exc, SenderError):
        details = {"detail": exc.detail} if exc.detail else {}
        if exc.upstream_status is not None:
            details["upstream_status"] = exc.upstream_status
        if exc.retry_after_seconds is not None:
            details["retry_after_seconds"] = exc.retry_after_seconds
        return exc.code, details

    if isinstance(exc, ChatGPTSessionRegistryError):
        return exc.code, {}

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
    """Dispatch frozen MCP tools through SecureWorkItemStore only.

    ``memory_adapter`` is the command sequence used to start the Memory
    Adapter, for example ``["python", "-m", "some_adapter_module"]``.
    It is configuration, not a shared MemoryAdapterClient instance.
    """

    def __init__(self, store: Any, *, memory_adapter: Optional[Sequence[str]] = None, chatgpt_cli_path: Optional[str] = None, legacy_sender_db_path: Optional[str] = None):
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
        self.chatgpt_registry = None
        self.chatgpt_cli = None
        if chatgpt_cli_path is not None:
            if not isinstance(chatgpt_cli_path, str) or not chatgpt_cli_path:
                raise ValueError("chatgpt_cli_path is invalid")
            self.chatgpt_registry = ChatGPTSessionRegistry(store.database_path)
            self.chatgpt_registry.initialize()
            self.chatgpt_legacy_owner_imported = 0
            if legacy_sender_db_path is not None:
                self.chatgpt_legacy_owner_imported = self.chatgpt_registry.migrate_legacy_owners(
                    legacy_sender_db_path
                )
            cli_root = __import__("os").path.dirname(__import__("os").path.dirname(chatgpt_cli_path))
            self.chatgpt_cli = ChatGPTCLIAdapter(
                chatgpt_cli_path,
                timeout=1900.0,
                read_timeout=20.0,
                max_concurrency=4,
                read_concurrency=2,
                owner_lookup=self.chatgpt_registry.get_owner,
                cwd=cli_root,
            )

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

            elif name == "chatgpt.session.create":
                self._require_chatgpt_cli()
                key = args.get("idempotency_key")
                cached = self.chatgpt_registry.begin_idempotent(
                    principal.tenant_id, principal.principal_id,
                    name, key, {"account": args["account"]},
                )
                if cached is not None:
                    data = cached
                else:
                    try:
                        data = self.chatgpt_cli.create_session(args["account"])
                        self.chatgpt_registry.register_session(
                            data["conversation_id"], principal.tenant_id,
                            principal.principal_id, data["account"],
                        )
                        self.chatgpt_registry.complete_idempotent(
                            principal.tenant_id, principal.principal_id, name, key, data
                        )
                    except BaseException:
                        self.chatgpt_registry.mark_unknown(
                            principal.tenant_id, principal.principal_id, name, key
                        )
                        raise

            elif name == "chatgpt.session.send":
                self._require_chatgpt_cli()
                key = args.get("idempotency_key")
                payload = {
                    "conversation_id": args.get("conversation_id"),
                    "prompt": args["prompt"],
                    "account": args["account"],
                }
                cached = self.chatgpt_registry.begin_idempotent(
                    principal.tenant_id, principal.principal_id, name, key, payload
                )
                if cached is not None:
                    data = cached
                else:
                    try:
                        data = self.chatgpt_cli.send(
                            args.get("conversation_id"), args["account"], args["prompt"],
                            tenant_id=principal.tenant_id,
                            principal_id=principal.principal_id,
                        )
                        self.chatgpt_registry.complete_idempotent(
                            principal.tenant_id, principal.principal_id, name, key, data
                        )
                    except BaseException:
                        self.chatgpt_registry.mark_unknown(
                            principal.tenant_id, principal.principal_id, name, key
                        )
                        raise

            elif name == "chatgpt.session.read":
                self._require_chatgpt_cli()
                data = self.chatgpt_cli.read(
                    args["conversation_id"], None, args.get("after_message_id"),
                    args["limit"], tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                )

            elif name == "chatgpt.session.list":
                self._require_chatgpt_cli()
                all_items = []
                partial = False
                list_errors = []
                next_cursor = None
                for _ in range(100):
                    page = self.chatgpt_cli.list_sessions(100, next_cursor)
                    all_items.extend(page["items"])
                    partial = partial or page.get("partial", False)
                    list_errors.extend(page.get("errors", []))
                    next_cursor = page.get("next_cursor")
                    if next_cursor is None:
                        break
                else:
                    raise _ResourceExhausted("ChatGPT session listing exceeded its page bound")
                owned = self.chatgpt_registry.list_owned(
                    principal.tenant_id, principal.principal_id
                )
                visible = [item for item in all_items if item["conversation_id"] in owned]
                offset = int(args.get("cursor") or "0")
                size = args["page_size"]
                window = visible[offset:offset + size]
                end = offset + size
                data = {
                    "items": window,
                    "next_cursor": str(end) if end < len(visible) else None,
                    "partial": partial,
                    "errors": list_errors[:10],
                }

            else:
                raise ValueError("unknown tool")

            envelope = _success(data, request_id)
            if len(_json_bytes(envelope)) > MAX_STRUCTURED_CONTENT_BYTES:
                return _error("RESOURCE_EXHAUSTED", request_id)
            return envelope

        except BaseException as exc:
            code, details = _exception_code(exc)
            return _error(code, request_id, details)

    def _require_chatgpt_cli(self) -> None:
        if self.chatgpt_cli is None or self.chatgpt_registry is None:
            raise AuthenticationError(
                "ChatGPT CLI is not configured",
                category="dependency_unavailable",
            )

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

def build_mcp_server(service: MCPToolService, token_provider: JWTVerifier) -> Any:
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
            if params.name in ("chatgpt.session.create", "chatgpt.session.send"):
                tool_timeout = CHATGPT_WRITE_DEADLINE_SECONDS
            elif params.name in ("chatgpt.session.read", "chatgpt.session.list"):
                tool_timeout = CHATGPT_READ_DEADLINE_SECONDS
            else:
                tool_timeout = REQUEST_DEADLINE_SECONDS
            try:
                envelope = await asyncio.wait_for(
                    future,
                    timeout=tool_timeout,
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
