"""MCP tool service and Streamable HTTP binding.

The service layer is independent of the MCP SDK. Only build_mcp_server()
imports the optional SDK.

Python source syntax: 3.11+
MCP SDK runtime: mcp==2.3.0
"""

from __future__ import annotations

import asyncio
import json
import re
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
from src.session_ops.sender_gateway_client import (
    PUBLIC_ERROR_CODES,
    SenderGatewayClient,
    SenderGatewayConfigError,
    SenderGatewayError,
)


DEFAULT_PAGE_SIZE = 25
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 50
MAX_CURSOR_CHARS = 2048

MAX_CONTEXT_ENTRIES = 6
MAX_CONTEXT_CONTENT_CHARS = 2048
MAX_CONTEXT_CONTENT_BYTES = 8192
MAX_QUERY_CHARS = 2048
MAX_QUERY_BYTES = 8192

MAX_HTTP_BODY_BYTES = 65536
MAX_MCP_RESPONSE_BYTES = 65536
MAX_STRUCTURED_CONTENT_BYTES = MAX_MCP_RESPONSE_BYTES - 2048

DB_DEADLINE_SECONDS = 3.0
ADAPTER_DEADLINE_SECONDS = 8.0
REQUEST_DEADLINE_SECONDS = 10.0
SENDER_READ_REQUEST_DEADLINE_SECONDS = 20.0
AUTH_VERIFY_DEADLINE_SECONDS = 3.5

MCP_SERVER_NAME = "chatgpt-session-ops"
MCP_SERVER_VERSION = "0.1.0"

_DB_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="session-ops-db",
)

_AUTH_VERIFY_WORKERS = 4
_AUTH_VERIFY_MAX_INFLIGHT = 8
_AUTH_EXECUTOR = ThreadPoolExecutor(
    max_workers=_AUTH_VERIFY_WORKERS,
    thread_name_prefix="session-ops-auth",
)
_AUTH_VERIFY_SLOTS = threading.BoundedSemaphore(
    _AUTH_VERIFY_MAX_INFLIGHT
)

# Sender read/list calls can outlive the MCP request when the HTTP client
# times out. Keep them off asyncio's shared default executor and bound both
# active and submitted work. Slots are released only when the real worker
# thread exits, not when its awaiting MCP coroutine is cancelled.
_SENDER_READ_WORKERS = 2
_SENDER_READ_EXECUTOR = ThreadPoolExecutor(
    max_workers=_SENDER_READ_WORKERS,
    thread_name_prefix="session-ops-sender-read",
)
_SENDER_READ_SLOTS = threading.BoundedSemaphore(_SENDER_READ_WORKERS)


class _DeadlineExceeded(Exception):
    pass


class _ResourceExhausted(Exception):
    pass


def _submit_sender_read(function: Any):
    """Submit one bounded read; admission is immediate and fail-fast."""
    if not _SENDER_READ_SLOTS.acquire(blocking=False):
        raise _ResourceExhausted("Sender read capacity is occupied")
    try:
        future = _SENDER_READ_EXECUTOR.submit(function)
    except BaseException:
        _SENDER_READ_SLOTS.release()
        raise
    # This callback belongs to the concurrent Future, so timeout/cancellation
    # of the asyncio waiter cannot release capacity while the thread is alive.
    future.add_done_callback(lambda _future: _SENDER_READ_SLOTS.release())
    return future


async def _run_sender_read(function: Any, *, timeout: float):
    """Await a read without cancelling its underlying bounded worker thread."""
    concurrent_future = _submit_sender_read(function)
    async_future = asyncio.wrap_future(concurrent_future)
    return await asyncio.wait_for(asyncio.shield(async_future), timeout=timeout)


class _ProtocolError(Exception):
    pass


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

_CURSOR = {"type": "string", "maxLength": MAX_CURSOR_CHARS}

_STATUS = {
    "type": "string",
    "enum": ["active", "paused", "completed", "cancelled"],
}

_GRANT_SCOPE = {"type": "string", "enum": ["read", "write"]}

CHATGPT_ACCOUNTS = (
    "auto",
    "acct1",
    "acct2",
    "acct3",
    "acct4",
    "acct5",
    "acct6",
)

_ACCOUNT = {
    "type": "string",
    "enum": list(CHATGPT_ACCOUNTS),
    "default": "auto",
}

_JOB_ID = {
    "type": "string",
    "pattern": (
        r"^cgj_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}"
        r"-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
}

_PROMPT = {
    "type": "string",
    "minLength": 1,
    "maxLength": 32768,
}

_MESSAGE_ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": 256,
}

_READ_LIMIT = {
    "type": "integer",
    "minimum": 1,
    "maximum": 50,
    "default": 20,
}

_IDEMPOTENCY_KEY = {
    "type": "string",
    "pattern": (
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}"
        r"-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
}


TOOL_SCHEMAS = {
    "work_item.create": _object_schema({}),
    "work_item.get": _object_schema(
        {"work_item_id": _WORK_ITEM_ID},
        ("work_item_id",),
    ),
    "work_item.list": _object_schema({
        "status": _STATUS,
        "page_size": _PAGE_SIZE,
        "cursor": _CURSOR,
    }),
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

    # The caller owns the stable key and must reuse it when recovering a
    # lost enqueue acknowledgement. A fresh key per invocation is not a retry.
    "chatgpt.session.create": _object_schema(
        {"account": _ACCOUNT, "idempotency_key": _IDEMPOTENCY_KEY},
        ("idempotency_key",),
    ),
    "chatgpt.session.list": _object_schema({
        "page_size": _PAGE_SIZE,
        "cursor": _CURSOR,
    }),
    "chatgpt.session.send": _object_schema(
        {
            "conversation_id": _SESSION_ID,
            "prompt": _PROMPT,
            "account": _ACCOUNT,
            "idempotency_key": _IDEMPOTENCY_KEY,
        },
        ("prompt", "idempotency_key"),
    ),
    "chatgpt.job.get": _object_schema(
        {"job_id": _JOB_ID},
        ("job_id",),
    ),
    "chatgpt.session.read": _object_schema(
        {
            "conversation_id": _SESSION_ID,
            "after_message_id": _MESSAGE_ID,
            "limit": _READ_LIMIT,
        },
        ("conversation_id",),
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
    "chatgpt.session.create": (
        "Queue creation of a remote ChatGPT session. Supply a UUIDv4 idempotency_key and reuse that exact key only to recover the same enqueue attempt."
    ),
    "chatgpt.session.list": (
        "List remote ChatGPT sessions owned by the authenticated principal."
    ),
    "chatgpt.session.send": (
        "Queue a prompt for a remote ChatGPT session and return a job id. "
        "Success means the job was persisted, not that the remote answered. Supply a UUIDv4 idempotency_key and reuse that exact key only to recover the same enqueue attempt."
    ),
    "chatgpt.job.get": "Get the status and result summary of a queued ChatGPT job.",
    "chatgpt.session.read": (
        "Read harvested messages from a remote ChatGPT session owned by the "
        "authenticated principal."
    ),
}


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
    "OWNERSHIP_PERSIST_FAILED": "session owner could not be persisted",
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
    "OWNERSHIP_PERSIST_FAILED": False,
}


#: Secret shapes that must never reach ``error.details``.
#:
#: Order matters: URL userinfo and query first (they hide ``?token=`` before
#: the key-value rule can trip over the URL), then the auth schemes, then
#: ``key<sep>value`` in both quoted (JSON) and bare (header/query) forms.
#:
#: Every quantifier is bounded on purpose. The first version used
#: ``(?:[a-z0-9]+[-_])*`` and ``(?:(?!\2).)*``, which are O(n^2) per start
#: offset: a 64 KiB body took 18.6 s. The bounds are the real fix; the prefix
#: slice in ``_redact`` is only the belt to that suspenders.
_KEY_NAME = (
    r"(?:[a-z0-9]{1,16}[-_]){0,4}"
    r"(?:token|password|passwd|secret|api[-_]?key|apikey|hmac|signature"
    r"|authorization|cookie|key)"
)

#: ``=``, ``=>`` and ``:`` / ``::`` / ``::=`` are the same delimiter in the wild.
_SEPARATOR = r"\s*(?::+=?|=>|=)\s*"

_KV_QUOTED = re.compile(
    r"(?i)([\"']?" + _KEY_NAME + r"[\"']?" + _SEPARATOR + r")"
    r"([\"'])(?:(?!\2).){0,2048}\2"
)

_KV_BARE = re.compile(
    r"(?i)\b(" + _KEY_NAME + r")" + _SEPARATOR
    + r"(?!bearer\b|basic\b|digest\b)[^\s,;&\"'}\]\[]+"
)

#: Three dot-separated segments: a JWT, whose ``.`` the char class excludes.
#: The lookbehind keeps the engine from restarting inside one long run (that
#: alone was O(n^2): 44 ms of the 65 KiB budget), and the segment caps bound
#: the backtracking that remains.
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"[A-Za-z0-9_-]{8,1024}\.[A-Za-z0-9_-]{8,1024}\.[A-Za-z0-9_-]{1,1024}"
)

#: Module paths, host names and identifiers share the dotted shape, so a shape
#: match alone is not evidence. A real JWT is either long (header 36 + payload
#: + signature 43 > 64 chars) or starts with ``eyJ`` -- base64url of ``{"``,
#: which is how every JWT header begins. Trade-off: a short, non-``eyJ`` token
#: is left readable; among the dotted strings we do see, that is always a
#: path or a host, never a credential.
JWT_MIN_TOTAL_CHARS = 64


def _mask_jwt(match: "re.Match[str]") -> str:
    value = match.group(0)

    if len(value) >= JWT_MIN_TOTAL_CHARS or value.startswith("eyJ"):
        return "[redacted]"

    return value

_ENTROPY = re.compile(r"[A-Za-z0-9+/=_~-]{32,}")

_ENTROPY_CLASSES = (
    re.compile(r"[A-Z]"),
    re.compile(r"[a-z]"),
    re.compile(r"[0-9]"),
)

#: A run counts as a credential only if it carries a base64 symbol, or both an
#: uppercase letter and a digit. CamelCase exception names (32+ chars, no
#: digit) and ordinary lowercase prose are therefore left readable. Trade-off:
#: a pure lowercase hex token is no longer caught here, but it always arrives
#: as ``token=<hex>``, which _KV_BARE already covers.
_HAS_BASE64_SYMBOL = re.compile(r"[+/=~]")
_HAS_UPPER = re.compile(r"[A-Z]")
_HAS_DIGIT = re.compile(r"[0-9]")


def _mask_entropy(match: "re.Match[str]") -> str:
    """Mask a long opaque token, keeping long identifiers readable."""
    value = match.group(0)
    classes = sum(1 for pattern in _ENTROPY_CLASSES if pattern.search(value))

    if classes < 2:
        return value

    if _HAS_BASE64_SYMBOL.search(value):
        return "[redacted]"

    if _HAS_UPPER.search(value) and _HAS_DIGIT.search(value):
        return "[redacted]"

    return value


_REDACTIONS = (
    (
        # Any scheme, including scheme-relative ``//user:pw@host``. No
        # lookbehind: it was defeated by a preceding word character
        # (``x//user:pw@``), and the scheme prefix is bounded to keep this
        # linear. Accepted collateral: ``path=/a//b@c`` becomes
        # ``/a//[redacted]@c`` -- a bare ``//x@`` in prose is rare enough to
        # trade for zero missed userinfo.
        re.compile(r"(?i)((?:[a-z][a-z0-9+.\-]{0,15}:)?//)([^/@\s]*)@"),
        r"\1[redacted]@",
    ),
    (
        # Drop the query string of any URL: it is where tokens travel.
        re.compile(r"(?i)\b(https?://[^\s?#]+)\?[^\s#\"']*"),
        r"\1?[redacted]",
    ),
    (
        re.compile(r"(?i)\b(bearer|basic|digest)\s+[^\s,;\"']+"),
        r"\1 [redacted]",
    ),
    (
        _JWT,
        _mask_jwt,
    ),
    (
        _KV_QUOTED,
        lambda match: match.group(1) + match.group(2) + "[redacted]" + match.group(2),
    ),
    (
        _KV_BARE,
        r"\1=[redacted]",
    ),
    (
        _ENTROPY,
        _mask_entropy,
    ),
)

#: Owner cap on ``error.details.detail``.
MAX_DETAIL_CHARS = 512

#: A rejected call reports at most this many argument errors.
MAX_ARGUMENT_ERRORS = 10

#: Regex work is capped before the 512-char trim. A secret straddling this
#: boundary can be cut in half, which is why the quantifier bounds above are
#: the primary defence and this slice is only the fallback.
MAX_REDACT_INPUT_CHARS = 8192


def _redact(text: str) -> str:
    """Strip credentials, URL query strings and excess length from text."""
    if not isinstance(text, str) or not text:
        return ""

    text = text[:MAX_REDACT_INPUT_CHARS]

    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)

    return text[:MAX_DETAIL_CHARS]


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


class _ArgumentError(ValueError):
    """A schema rejection that names the fields, never the supplied values."""

    def __init__(self, errors: Sequence[Mapping[str, str]]) -> None:
        self.errors = [dict(error) for error in list(errors)[:MAX_ARGUMENT_ERRORS]]
        super().__init__(
            "; ".join(
                "%s: %s" % (error["field"], error["reason"])
                for error in self.errors
            )
        )


def _field_path(prefix: str, name: str) -> str:
    """Dotted path for nested arguments; flat schemas pass an empty prefix."""
    return name if not prefix else prefix + "." + name


def _check_value(
    path: str,
    value: Any,
    rule: Mapping[str, Any],
    errors: list,
) -> None:
    """Record every constraint ``value`` breaks, without quoting ``value``."""
    expected = rule.get("type")

    if expected == "string":
        if not isinstance(value, str):
            errors.append({
                "field": path,
                "reason": "type mismatch: expected string",
            })
            return

        minimum = rule.get("minLength", 0)
        if len(value) < minimum:
            errors.append({
                "field": path,
                "reason": "too short: minLength %d" % minimum,
            })

        maximum = rule.get("maxLength")
        if maximum is not None and len(value) > maximum:
            errors.append({
                "field": path,
                "reason": "too long: maxLength %d" % maximum,
            })

        if "enum" in rule and value not in rule["enum"]:
            errors.append({"field": path, "reason": "not in enum"})

        if "pattern" in rule and re.fullmatch(rule["pattern"], value) is None:
            errors.append({
                "field": path,
                "reason": "does not match pattern %s" % rule["pattern"],
            })

    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append({
                "field": path,
                "reason": "type mismatch: expected integer",
            })
            return

        if "minimum" in rule and value < rule["minimum"]:
            errors.append({
                "field": path,
                "reason": "below minimum %d" % rule["minimum"],
            })

        if "maximum" in rule and value > rule["maximum"]:
            errors.append({
                "field": path,
                "reason": "above maximum %d" % rule["maximum"],
            })

    elif expected == "boolean":
        if not isinstance(value, bool):
            errors.append({
                "field": path,
                "reason": "type mismatch: expected boolean",
            })


def _argument_details(exc: _ArgumentError) -> Dict[str, Any]:
    """Owner shape: ``detail`` (first error) plus the full ``errors`` list."""
    errors = [
        {"field": _redact(error["field"]), "reason": _redact(error["reason"])}
        for error in exc.errors
    ]

    if not errors:
        return {}

    return {
        "detail": "%s: %s" % (errors[0]["field"], errors[0]["reason"]),
        "errors": errors,
    }


def _validate_arguments(name: str, arguments: Any) -> Dict[str, Any]:
    if name not in TOOL_SCHEMAS:
        raise ValueError("unknown tool")

    if arguments is None:
        arguments = {}

    if not isinstance(arguments, Mapping):
        raise _ArgumentError([
            {"field": "arguments", "reason": "type mismatch: expected object"}
        ])

    schema = TOOL_SCHEMAS[name]
    properties = schema["properties"]
    supplied = dict(arguments)
    errors: list = []

    for key in supplied:
        if key not in properties:
            errors.append({"field": _field_path("", key), "reason": "unexpected field"})

    for key in schema.get("required", ()):
        if key not in supplied:
            errors.append({"field": _field_path("", key), "reason": "missing required"})

    if errors:
        raise _ArgumentError(errors)

    for key, value in supplied.items():
        _check_value(_field_path("", key), value, properties[key], errors)

    if errors:
        raise _ArgumentError(errors)

    if "page_size" in properties and "page_size" not in supplied:
        supplied["page_size"] = DEFAULT_PAGE_SIZE

    if name == "session.list" and "include_unlinked" not in supplied:
        supplied["include_unlinked"] = False

    if name == "session.context" and "limit" not in supplied:
        supplied["limit"] = MAX_CONTEXT_ENTRIES

    if "account" in properties and "account" not in supplied:
        supplied["account"] = "auto"

    if name == "chatgpt.session.read" and "limit" not in supplied:
        supplied["limit"] = 20

    cursor = supplied.get("cursor")
    if cursor is not None and len(cursor) > MAX_CURSOR_CHARS:
        raise _ArgumentError([{
            "field": "cursor",
            "reason": "too long: max %d characters" % MAX_CURSOR_CHARS,
        }])

    if name == "session.context":
        query = supplied["query"]

        if not query.strip():
            raise _ArgumentError([
                {"field": "query", "reason": "too short: must not be blank"}
            ])

        if len(query) > MAX_QUERY_CHARS:
            raise _ArgumentError([{
                "field": "query",
                "reason": "too long: max %d characters" % MAX_QUERY_CHARS,
            }])

        if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise _ArgumentError([{
                "field": "query",
                "reason": "too long: max %d bytes" % MAX_QUERY_BYTES,
            }])

    return supplied


def _record_dict(value: Any) -> Any:
    if value is None:
        return None

    if is_dataclass(value):
        return asdict(value)

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(key): _record_dict(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [_record_dict(item) for item in value]

    return str(value)


def _page_dict(page: Any) -> Dict[str, Any]:
    return {
        "items": [_record_dict(item) for item in page],
        "next_cursor": getattr(page, "next_cursor", None),
    }


JOB_STATUSES = ("queued", "running", "succeeded", "failed")
MESSAGE_ROLES = ("user", "assistant")


def _require_object(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise _ProtocolError("dependency response must be an object")
    return payload


def _require_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    max_chars: int = 1024,
) -> str:
    value = payload.get(key)

    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise _ProtocolError(
            "dependency response field %s is invalid" % key
        )

    return value


def _optional_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    max_chars: int = 1024,
) -> Optional[str]:
    value = payload.get(key)

    if value is None:
        return None

    if not isinstance(value, str) or len(value) > max_chars:
        raise _ProtocolError(
            "dependency response field %s is invalid" % key
        )

    return value


def _optional_int(
    payload: Mapping[str, Any],
    key: str,
) -> Optional[int]:
    value = payload.get(key)

    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        raise _ProtocolError(
            "dependency response field %s is invalid" % key
        )

    return value


def _job_status(payload: Mapping[str, Any]) -> str:
    status = payload.get("status")

    if status not in JOB_STATUSES:
        raise _ProtocolError("dependency response job status is invalid")

    return status


def _project_job_submission(
    payload: Any,
    operation: str,
) -> Dict[str, Any]:
    payload = _require_object(payload)

    projected = {
        "job_id": _require_text(payload, "job_id", max_chars=64),
        "operation": operation,
        "status": _job_status(payload),
        "created_at": _require_text(payload, "created_at", max_chars=64),
    }

    if operation == "session.send":
        projected["conversation_id"] = _optional_text(
            payload,
            "conversation_id",
            max_chars=64,
        )

    return projected


def _project_job(payload: Any) -> Dict[str, Any]:
    payload = _require_object(payload)

    result = payload.get("result")

    if result is not None:
        result = _require_object(result)
        receipt_durable = result.get("receipt_durable", False)
        if not isinstance(receipt_durable, bool):
            raise _ProtocolError("dependency job receipt_durable is malformed")
        reply_sha256 = _optional_text(result, "reply_sha256", max_chars=64)
        if reply_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", reply_sha256):
            raise _ProtocolError("dependency job reply_sha256 is malformed")
        result = {
            "message_id": _optional_text(result, "message_id", max_chars=256),
            "assistant_message_count": _optional_int(
                result, "assistant_message_count"
            ),
            "response_chars": _optional_int(result, "response_chars"),
            "turn_id": _optional_text(result, "turn_id", max_chars=64),
            "reply_state": _optional_text(result, "reply_state", max_chars=32),
            "reply_source": _optional_text(result, "reply_source", max_chars=32),
            "reply_chars": _optional_int(result, "reply_chars"),
            "reply_sha256": reply_sha256,
            "receipt_durable": receipt_durable,
            "receipt_version": _optional_int(result, "receipt_version"),
        }

    error = payload.get("error")

    if error is not None:
        error = _require_object(error)
        retryable = error.get("retryable")

        if not isinstance(retryable, bool):
            raise _ProtocolError("dependency job error is malformed")

        error = {
            "code": _require_text(error, "code", max_chars=64),
            "retryable": retryable,
        }

    outcome_unknown = payload.get("outcome_unknown", False)
    if not isinstance(outcome_unknown, bool):
        raise _ProtocolError("dependency job outcome_unknown is malformed")
    if outcome_unknown and error is not None:
        error["retryable"] = False

    return {
        "job_id": _require_text(payload, "job_id", max_chars=64),
        "operation": _require_text(payload, "operation", max_chars=32),
        "status": _job_status(payload),
        "conversation_id": _optional_text(
            payload, "conversation_id", max_chars=64
        ),
        "created_at": _require_text(payload, "created_at", max_chars=64),
        "started_at": _optional_text(payload, "started_at", max_chars=64),
        "completed_at": _optional_text(payload, "completed_at", max_chars=64),
        "result": result,
        "error": error,
        "outcome_unknown": outcome_unknown,
    }


def _project_sessions(payload: Any) -> Dict[str, Any]:
    payload = _require_object(payload)
    items = payload.get("items")

    if not isinstance(items, list):
        raise _ProtocolError("dependency session list has no items")

    projected = []

    for item in items:
        item = _require_object(item)

        projected.append({
            "conversation_id": _require_text(
                item, "conversation_id", max_chars=64
            ),
            "account": _optional_text(item, "account", max_chars=32),
            "created_at": _optional_text(item, "created_at", max_chars=64),
            "last_message_at": _optional_text(
                item, "last_message_at", max_chars=64
            ),
            "message_count": _optional_int(item, "message_count"),
        })

    return {
        "items": projected,
        "next_cursor": _optional_text(
            payload, "next_cursor", max_chars=MAX_CURSOR_CHARS
        ),
    }


def _project_messages(payload: Any) -> Dict[str, Any]:
    payload = _require_object(payload)
    messages = payload.get("messages")

    if not isinstance(messages, list):
        raise _ProtocolError("dependency message list has no messages")

    projected = []

    for message in messages:
        message = _require_object(message)
        role = message.get("role")

        if role not in MESSAGE_ROLES:
            raise _ProtocolError("dependency message role is invalid")

        content = message.get("content")

        if not isinstance(content, str):
            raise _ProtocolError("dependency message content is invalid")

        projected.append({
            "message_id": _require_text(
                message, "message_id", max_chars=256
            ),
            "role": role,
            "content": content,
            "created_at": _optional_text(
                message, "created_at", max_chars=64
            ),
        })

    complete = payload.get("complete")

    if not isinstance(complete, bool):
        raise _ProtocolError(
            "dependency did not report harvest completeness"
        )

    return {
        "conversation_id": _require_text(
            payload, "conversation_id", max_chars=64
        ),
        "messages": projected,
        "next_after_message_id": _optional_text(
            payload, "next_after_message_id", max_chars=256
        ),
        "complete": complete,
    }


def _detail(detail: str) -> Dict[str, Any]:
    """Owner-frozen ``error.details`` shape for a real exception message."""
    return {"detail": detail} if detail else {}


def _gateway_detail(exc: SenderGatewayError) -> str:
    """Keep the Sidecar's real message, minus its redundant ``CODE: `` prefix."""
    text = getattr(exc, "message", "") or ""

    head, separator, tail = text.partition(": ")

    if separator and tail and head in PUBLIC_ERROR_CODES:
        text = tail

    return _redact(text)


def _exception_code(exc: BaseException) -> Tuple[str, Dict[str, Any]]:
    if isinstance(exc, _ProtocolError):
        return "INTERNAL", {}

    if isinstance(exc, _ArgumentError):
        # Argument rejection is the one place where naming the field is the
        # whole diagnostic value; the value itself is never echoed.
        return "INVALID_ARGUMENT", _argument_details(exc)

    if isinstance(exc, SenderGatewayError):
        code = getattr(exc, "code", None)
        detail = _detail(_gateway_detail(exc))

        if code in _ERROR_MESSAGES:
            return code, detail

        return "INTERNAL", detail

    if isinstance(exc, SenderGatewayConfigError):
        return "DEPENDENCY_UNAVAILABLE", {}

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

    if isinstance(
        exc,
        (InvalidIdentifier, InvalidPrincipal, ValueError, TypeError),
    ):
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


class MCPToolService:
    """Dispatch frozen MCP tools through the authorized service layer."""

    _CHATGPT_TOOLS = frozenset({
        "chatgpt.session.create",
        "chatgpt.session.list",
        "chatgpt.session.send",
        "chatgpt.job.get",
        "chatgpt.session.read",
    })

    def __init__(
        self,
        store: Any,
        *,
        memory_adapter: Optional[Sequence[str]] = None,
        sender_gateway: Any = None,
    ):
        if store is None:
            raise ValueError("SecureWorkItemStore is required")

        self.store = store

        if memory_adapter is None:
            self.memory_adapter_command = None
        else:
            if isinstance(memory_adapter, (str, bytes)):
                raise ValueError(
                    "memory_adapter must be a command sequence"
                )

            command = tuple(memory_adapter)

            if not command or not all(
                isinstance(part, str) and part
                for part in command
            ):
                raise ValueError("memory_adapter command is invalid")

            self.memory_adapter_command = command

        self.sender_gateway = None
        self._sender_gateway_lock = threading.Lock()
        self._sender_gateway_initialized = sender_gateway is not None

        if sender_gateway is not None:
            self._validate_sender_gateway(sender_gateway)
            self.sender_gateway = sender_gateway

    @staticmethod
    def _validate_sender_gateway(sender_gateway: Any) -> None:
        required_methods = (
            "enqueue",
            "get_job",
            "list_sessions",
            "read_session",
        )

        if not all(
            callable(getattr(sender_gateway, method, None))
            for method in required_methods
        ):
            raise ValueError(
                "sender_gateway must implement the SenderGatewayClient "
                "interface"
            )

    @staticmethod
    def _run_db(function: Any) -> Any:
        future = _DB_EXECUTOR.submit(function)

        try:
            return future.result(timeout=DB_DEADLINE_SECONDS)
        except FutureTimeout:
            future.cancel()
            raise _DeadlineExceeded(
                "database operation exceeded deadline"
            )

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
                raise AuthenticationError(
                    "verified authentication context required"
                )

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
                    lambda: self.store.get_work_item(
                        principal,
                        args["work_item_id"],
                    )
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
                data = _project_job_submission(
                    self._sender_gateway().enqueue(
                        principal_id=principal.principal_id,
                        tenant_id=principal.tenant_id,
                        operation="session.create",
                        arguments={
                            "account": args["account"],
                        },
                        idempotency_key=args["idempotency_key"],
                    ),
                    "session.create",
                )

            elif name == "chatgpt.session.list":
                data = _project_sessions(
                    self._sender_gateway().list_sessions(
                        principal_id=principal.principal_id,
                        tenant_id=principal.tenant_id,
                        page_size=args["page_size"],
                        cursor=args.get("cursor"),
                    )
                )

            elif name == "chatgpt.session.send":
                send_arguments = {
                    "account": args["account"],
                    "prompt": args["prompt"],
                }

                if args.get("conversation_id") is not None:
                    send_arguments["conversation_id"] = (
                        args["conversation_id"]
                    )

                data = _project_job_submission(
                    self._sender_gateway().enqueue(
                        principal_id=principal.principal_id,
                        tenant_id=principal.tenant_id,
                        operation="session.send",
                        arguments=send_arguments,
                        idempotency_key=args["idempotency_key"],
                    ),
                    "session.send",
                )

            elif name == "chatgpt.job.get":
                data = _project_job(
                    self._sender_gateway().get_job(
                        principal_id=principal.principal_id,
                        tenant_id=principal.tenant_id,
                        job_id=args["job_id"],
                    )
                )

            elif name == "chatgpt.session.read":
                data = _project_messages(
                    self._sender_gateway().read_session(
                        principal_id=principal.principal_id,
                        tenant_id=principal.tenant_id,
                        conversation_id=args["conversation_id"],
                        after_message_id=args.get("after_message_id"),
                        limit=args["limit"],
                    )
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

    def _sender_gateway(self) -> Any:
        """Initialize the Sidecar client exactly once, on first use."""
        if self.sender_gateway is not None:
            return self.sender_gateway

        with self._sender_gateway_lock:
            if self.sender_gateway is not None:
                return self.sender_gateway

            if self._sender_gateway_initialized:
                raise SenderGatewayError(
                    "DEPENDENCY_UNAVAILABLE",
                    "sender gateway is unavailable",
                )

            self._sender_gateway_initialized = True

            try:
                client = SenderGatewayClient.from_env()
                self._validate_sender_gateway(client)

            except Exception as exc:
                raise SenderGatewayError(
                    "DEPENDENCY_UNAVAILABLE",
                    "sender gateway configuration or TLS initialization failed",
                ) from exc

            self.sender_gateway = client
            return client

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

        link = self._run_db(
            lambda: self.store.get_active_link(principal, session_id)
        )

        if link is None:
            raise QuarantineNotCleared(
                "an authorized active session link is required"
            )

        remaining = REQUEST_DEADLINE_SECONDS - (
            time.monotonic() - started
        )

        adapter_timeout = min(
            ADAPTER_DEADLINE_SECONDS,
            remaining,
        )

        if adapter_timeout <= 0:
            raise _DeadlineExceeded("request deadline exceeded")

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
                or len(content.encode("utf-8"))
                > MAX_CONTEXT_CONTENT_BYTES
            ):
                raise _ResourceExhausted(
                    "memory adapter returned oversized content"
                )

            entries.append({
                "memory_id": entry.memory_id,
                "session_id": entry.session_id,
                "content": content,
                "provenance": _record_dict(entry.provenance),
                "created_at": entry.created_at,
                "score": entry.score,
                "trust": "untrusted_historical_context",
            })

        return {
            "work_item_id": link.work_item_id,
            "session_id": session_id,
            "entries": entries,
        }


def build_mcp_server(
    service: MCPToolService,
    token_provider: JWTVerifier,
    *,
    events_store: Any = None,
    sender_gateway: Any = None,
) -> Any:
    """Build and return the authenticated Streamable HTTP ASGI application."""
    if not isinstance(service, MCPToolService):
        raise TypeError("service must be MCPToolService")

    if not callable(getattr(token_provider, "verify", None)):
        raise TypeError("token_provider must be a JWTVerifier instance")

    from mcp import types
    from mcp.server import Server
    from mcp.server.transport_security import TransportSecuritySettings

    resource = urlsplit(token_provider.audience)

    if resource.scheme != "https" or not resource.netloc:
        raise ValueError(
            "JWTVerifier audience must be an HTTPS resource URI"
        )

    endpoint_path = resource.path or "/mcp"

    if not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    metadata_path = (
        "/.well-known/oauth-protected-resource" + endpoint_path
    )

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
        "session_ops.access.manage",
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
            arguments = (
                params.arguments
                if params.arguments is not None
                else {}
            )

            loop = asyncio.get_running_loop()

            dispatch = lambda: service.dispatch(
                params.name,
                arguments,
                auth,
                request_id=request_id,
            )

            try:
                if params.name in ("chatgpt.session.read", "chatgpt.session.list"):
                    envelope = await _run_sender_read(
                        dispatch, timeout=SENDER_READ_REQUEST_DEADLINE_SECONDS
                    )
                else:
                    future = loop.run_in_executor(None, dispatch)
                    envelope = await asyncio.wait_for(
                        future, timeout=REQUEST_DEADLINE_SECONDS
                    )

            except _ResourceExhausted:
                envelope = _error("RESOURCE_EXHAUSTED", request_id)
            except asyncio.TimeoutError:
                envelope = _error("TIMEOUT", request_id)

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
            arguments = params.arguments or {}
            job_id = arguments.get("job_id")
            if job_id is not None:
                if sender_gateway is None:
                    from mcp.shared.exceptions import MCPError
                    raise MCPError(code=-32003, message="Event authorization service unavailable")
                try:
                    sender_gateway.get_job(
                        principal_id=auth.principal.principal_id,
                        tenant_id=auth.principal.tenant_id,
                        job_id=job_id,
                    )
                except Exception as exc:
                    if getattr(exc, "code", None) in ("NOT_FOUND", "PERMISSION_DENIED"):
                        from mcp.shared.exceptions import MCPError
                        raise MCPError(
                            code=-32602,
                            message="Invalid event subscription",
                            data={"reason": "job_not_accessible"},
                        ) from exc
                    raise
            try:
                return events_store.subscribe(
                    auth,
                    params.model_dump(by_alias=True, exclude_unset=True),
                )
            except CallbackEndpointError as exc:
                from mcp.shared.exceptions import MCPError
                raise MCPError(
                    code=-32015,
                    message="Callback endpoint verification failed",
                    data={"reason": exc.reason},
                ) from exc
            except ValueError as exc:
                from mcp.shared.exceptions import MCPError
                raise MCPError(
                    code=-32602,
                    message="Invalid event subscription",
                    data={"reason": "invalid_subscription"},
                ) from exc

        async def on_events_unsubscribe(ctx: Any, params: Any) -> Any:
            auth = _event_auth(ctx)
            if not isinstance(auth, AuthContext):
                raise AuthenticationError("verified authentication context required")
            if "session_ops.chatgpt.session.write" not in auth.scopes:
                raise ScopeError(("session_ops.chatgpt.session.write",))
            return events_store.unsubscribe(
                auth,
                params.model_dump(by_alias=True, exclude_unset=True),
            )

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

            await send({
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            })

            await send({
                "type": "http.response.body",
                "body": body,
            })

        async def __call__(
            self,
            scope: Any,
            receive: Any,
            send: Any,
        ) -> None:
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
                headers[key.decode("latin-1").lower()] = (
                    value.decode("latin-1")
                )

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
                    ((
                        b"www-authenticate",
                        challenge.encode("latin-1"),
                    ),),
                )
                return

            parts = authorization.split(None, 1)

            if (
                len(parts) != 2
                or parts[0].lower() != "bearer"
                or not parts[1].strip()
                or any(ch.isspace() for ch in parts[1].strip())
            ):
                invalid_challenge = (
                    challenge + ', error="invalid_token"'
                )

                await self._respond(
                    send,
                    401,
                    b"",
                    ((
                        b"www-authenticate",
                        invalid_challenge.encode("latin-1"),
                    ),),
                )
                return

            token = parts[1].strip()
            loop = asyncio.get_running_loop()

            if not _AUTH_VERIFY_SLOTS.acquire(blocking=False):
                envelope = _error(
                    "DEPENDENCY_UNAVAILABLE",
                    str(uuid.uuid4()),
                )

                await self._respond(
                    send,
                    503,
                    _json_bytes(envelope),
                )
                return

            try:
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

                envelope = _error(
                    "INTERNAL",
                    str(uuid.uuid4()),
                )

                await self._respond(
                    send,
                    500,
                    _json_bytes(envelope),
                )
                return

            try:
                auth = await asyncio.wait_for(
                    verify_future,
                    timeout=AUTH_VERIFY_DEADLINE_SECONDS,
                )

            except asyncio.TimeoutError:
                envelope = _error(
                    "DEPENDENCY_UNAVAILABLE",
                    str(uuid.uuid4()),
                )

                await self._respond(
                    send,
                    503,
                    _json_bytes(envelope),
                )
                return

            except AuthenticationError as exc:
                if getattr(exc, "category", "") == "dependency_unavailable":
                    envelope = _error(
                        "DEPENDENCY_UNAVAILABLE",
                        str(uuid.uuid4()),
                    )

                    await self._respond(
                        send,
                        503,
                        _json_bytes(envelope),
                    )
                    return

                invalid_challenge = (
                    challenge + ', error="invalid_token"'
                )

                await self._respond(
                    send,
                    401,
                    b"",
                    ((
                        b"www-authenticate",
                        invalid_challenge.encode("latin-1"),
                    ),),
                )
                return

            except Exception:
                envelope = _error(
                    "INTERNAL",
                    str(uuid.uuid4()),
                )

                await self._respond(
                    send,
                    500,
                    _json_bytes(envelope),
                )
                return

            if not isinstance(auth, AuthContext):
                invalid_challenge = (
                    challenge + ', error="invalid_token"'
                )

                await self._respond(
                    send,
                    401,
                    b"",
                    ((
                        b"www-authenticate",
                        invalid_challenge.encode("latin-1"),
                    ),),
                )
                return

            state = scope.setdefault("state", {})
            state["session_ops_auth"] = auth

            await self.inner(scope, receive, send)

    return _BearerAuthASGIMiddleware(app)
