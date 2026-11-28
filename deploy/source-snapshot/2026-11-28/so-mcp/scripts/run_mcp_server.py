"""Production entry point for the Session Ops MCP service.

The MCP SDK is imported only inside build_mcp_server(), preserving the
repository's SDK isolation boundary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

import uvicorn

from src.session_ops.authority import (
    PRINCIPAL_ID_RE,
    PrincipalRecord,
    SecureWorkItemStore,
    StaticPrincipalDirectory,
)
from src.session_ops.mcp_auth import JWTVerifier
from src.session_ops.mcp_server import MCPToolService, build_mcp_server
from src.session_ops.mcp_events import MCPEventsStore
from src.session_ops.mcp_event_collector import MCPEventCollector
from src.session_ops.mcp_event_dispatcher import MCPEventDispatcher
from src.session_ops.sender_gateway_client import SenderGatewayClient


SUPPORTED_SCOPES = [
    "session_ops.work_item.read",
    "session_ops.work_item.write",
    "session_ops.session.read",
    "session_ops.session.write",
    "session_ops.context.read",
    "session_ops.access.manage",
    "session_ops.chatgpt.session.read",
    "session_ops.chatgpt.session.write",
]

MEMORY_STDIO_ENV = "SO_MCP_MEMORY_STDIO"
EVENTS_KEY_FILE_ENV = "SO_MCP_EVENTS_KEY_FILE"
EVENTS_DB_ENV = "SO_MCP_EVENTS_DB"
PRINCIPAL_DIRECTORY_ENV = "SO_MCP_PRINCIPAL_DIRECTORY"

#: The only keys a principal-directory entry may carry.
PRINCIPAL_DIRECTORY_FIELDS = frozenset({"principal_id", "tenant_id", "enabled"})

_SECRET_ARGUMENT_RE = re.compile(
    r"(token|password|passwd|secret|credential|"
    r"api[-_]?key|private[-_]?key|authorization)",
    re.IGNORECASE,
)


def required_env(name: str) -> str:
    """Read a required non-empty environment variable."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError("required environment variable is missing: " + name)
    return value.strip()


def _resolve_memory_executable(executable: str) -> str:
    """Resolve argv[0] and fail early unless it is an executable file."""
    if not executable or not executable.strip():
        raise RuntimeError(
            MEMORY_STDIO_ENV + " command has an empty executable path"
        )

    if "\x00" in executable:
        raise RuntimeError(
            MEMORY_STDIO_ENV + " executable path contains a NUL character"
        )

    # A path containing a separator is interpreted relative to the service
    # working directory unless it is already absolute.
    if os.path.isabs(executable) or os.path.dirname(executable):
        candidate = Path(executable).expanduser()

        if not candidate.exists():
            raise RuntimeError(
                "memory adapter executable does not exist: %s"
                % candidate
            )

        if not candidate.is_file():
            raise RuntimeError(
                "memory adapter executable is not a regular file: %s"
                % candidate
            )

        if not os.access(str(candidate), os.X_OK):
            raise RuntimeError(
                "memory adapter executable is not executable: %s"
                % candidate
            )

        return str(candidate.resolve())

    # A bare executable name is resolved using the service process PATH.
    resolved = shutil.which(executable)
    if resolved is not None:
        candidate = Path(resolved)

        if not candidate.is_file():
            raise RuntimeError(
                "memory adapter executable is not a regular file: %s"
                % candidate
            )

        if not os.access(str(candidate), os.X_OK):
            raise RuntimeError(
                "memory adapter executable is not executable: %s"
                % candidate
            )

        return str(candidate.resolve())

    # Distinguish an existing but non-executable PATH entry from a missing
    # executable so the startup error identifies the actual problem.
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        search_directory = directory or os.curdir
        candidate = Path(search_directory) / executable

        if candidate.exists():
            if not candidate.is_file():
                raise RuntimeError(
                    "memory adapter executable is not a regular file: %s"
                    % candidate
                )

            if not os.access(str(candidate), os.X_OK):
                raise RuntimeError(
                    "memory adapter executable is not executable: %s"
                    % candidate
                )

    raise RuntimeError(
        "memory adapter executable was not found on PATH: %s"
        % executable
    )


def load_memory_adapter_command() -> Any:
    """Load an optional JSON argv array for the Memory Adapter.

    Unset environment variable:
        Return None and leave session.context unavailable.

    Set environment variable:
        It must contain a non-empty JSON array of non-empty strings.
        Invalid configuration is a startup error; it is never silently
        converted to None.
    """
    raw = os.environ.get(MEMORY_STDIO_ENV)

    if raw is None:
        return None

    try:
        command = json.loads(raw)
    except (TypeError, ValueError) as exc:
        # JSONDecodeError only carries line/column in its own message, so the
        # position would otherwise be buried in a traceback. The raw value is
        # deliberately not echoed: it may contain a path or a credential.
        raise RuntimeError(
            MEMORY_STDIO_ENV
            + " must contain a valid JSON array of strings: %s" % exc
        ) from exc

    if not isinstance(command, list) or not command:
        raise RuntimeError(
            MEMORY_STDIO_ENV
            + " must be a non-empty JSON array of command arguments"
        )

    for index, part in enumerate(command):
        if not isinstance(part, str) or not part:
            raise RuntimeError(
                MEMORY_STDIO_ENV
                + " argument %d must be a non-empty string" % index
            )
        if "\x00" in part:
            raise RuntimeError(
                MEMORY_STDIO_ENV
                + " argument %d contains a NUL character" % index
            )

    command[0] = _resolve_memory_executable(command[0])
    return command


def _redact_memory_command(command: Any) -> str:
    """Render the effective argv for logs without exposing flagged secrets."""
    if command is None:
        return "disabled"

    redacted = []
    redact_next = False

    for argument in command:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue

        if argument.startswith("-") and "=" in argument:
            option, value = argument.split("=", 1)
            if _SECRET_ARGUMENT_RE.search(option):
                redacted.append(option + "=<redacted>")
            else:
                redacted.append(argument)
            continue

        redacted.append(argument)

        if argument.startswith("-") and _SECRET_ARGUMENT_RE.search(argument):
            redact_next = True

    return json.dumps(redacted, ensure_ascii=False)


def load_principal_directory() -> Optional[StaticPrincipalDirectory]:
    """Load the optional principal directory that grant targets resolve against.

    Unset or empty:
        Return None so ``SecureWorkItemStore`` keeps ``DenyAllPrincipalDirectory``
        and every grant is rejected. Behaviour is unchanged.

    Set:
        It must contain a JSON array of
        ``{"principal_id": str, "tenant_id": str, "enabled": bool}``.
        Anything malformed is a startup error: silently degrading to deny-all
        would surface as a mysterious "grant failed" at call time instead.
    """
    raw = os.environ.get(PRINCIPAL_DIRECTORY_ENV, "").strip()

    if not raw:
        return None

    try:
        entries = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            PRINCIPAL_DIRECTORY_ENV
            + " must contain a valid JSON array: %s" % exc
        ) from exc

    if not isinstance(entries, list):
        raise RuntimeError(PRINCIPAL_DIRECTORY_ENV + " must be a JSON array")

    if not entries:
        # An empty array denies every grant, which is indistinguishable from a
        # typo; refusing to start beats a silent fail-closed surprise.
        raise RuntimeError(
            PRINCIPAL_DIRECTORY_ENV
            + " must not be an empty array: empty array is indistinguishable"
            " from unconfigured; remove the variable instead"
        )

    records = []

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(
                PRINCIPAL_DIRECTORY_ENV
                + " entry %d must be a JSON object" % index
            )

        unknown = sorted(set(entry) - PRINCIPAL_DIRECTORY_FIELDS)
        if unknown:
            # A misspelled "disabled" would silently default to enabled=true.
            raise RuntimeError(
                PRINCIPAL_DIRECTORY_ENV
                + " entry %d has an unknown field: %s"
                % (index, ", ".join(unknown))
            )

        for field in ("principal_id", "tenant_id"):
            value = entry.get(field)
            if not isinstance(value, str) or not value:
                raise RuntimeError(
                    PRINCIPAL_DIRECTORY_ENV
                    + " entry %d is missing a non-empty string %s"
                    % (index, field)
                )

        principal_id = entry["principal_id"]
        if not PRINCIPAL_ID_RE.fullmatch(principal_id):
            raise RuntimeError(
                PRINCIPAL_DIRECTORY_ENV
                + " entry %d has an invalid principal_id: %r"
                % (index, principal_id)
            )

        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RuntimeError(
                PRINCIPAL_DIRECTORY_ENV
                + " entry %d has a non-boolean enabled: %r"
                % (index, enabled)
            )

        records.append(
            PrincipalRecord(
                principal_id=principal_id,
                tenant_id=entry["tenant_id"],
                enabled=enabled,
            )
        )

    logging.getLogger("so-mcp").info(
        "Principal directory: %d entries", len(records)
    )
    return StaticPrincipalDirectory(records)


def _validate_https_url(name: str, value: str, *, require_path: bool) -> str:
    """Validate an absolute HTTPS URL without silently rewriting its value."""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(name + " is not a valid URL") from exc

    if parsed.scheme != "https" or not parsed.hostname or not parsed.netloc:
        raise RuntimeError(name + " must be an absolute HTTPS URL")

    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError(name + " must not contain URL credentials")

    if parsed.query or parsed.fragment:
        raise RuntimeError(name + " must not contain a query or fragment")

    if require_path and (not parsed.path or parsed.path == "/"):
        raise RuntimeError(name + " must contain a non-root resource path")

    return value


def _realm_namespace_from_issuer(issuer: str) -> str:
    """Derive the Keycloak realm name from a standard issuer path."""
    parts = [part for part in urlsplit(issuer).path.split("/") if part]

    try:
        index = parts.index("realms")
        realm = parts[index + 1]
    except (ValueError, IndexError):
        raise RuntimeError(
            "cannot derive realm namespace from SO_MCP_ISSUER; "
            "set SO_MCP_REALM_NAMESPACE explicitly"
        )

    if not realm:
        raise RuntimeError("derived Keycloak realm namespace is empty")

    return realm


def load_config() -> dict[str, Any]:
    """Load and validate all deployment configuration."""
    issuer = _validate_https_url(
        "SO_MCP_ISSUER",
        required_env("SO_MCP_ISSUER"),
        require_path=True,
    ).rstrip("/")

    audience = _validate_https_url(
        "SO_MCP_AUDIENCE",
        required_env("SO_MCP_AUDIENCE"),
        require_path=True,
    )

    database_path = required_env("SO_MCP_DB")
    if database_path == ":memory:":
        raise RuntimeError("SO_MCP_DB must be a persistent filesystem path")

    tenant_claim = required_env("SO_MCP_TENANT_CLAIM")

    realm_namespace = os.environ.get("SO_MCP_REALM_NAMESPACE", "").strip()
    if not realm_namespace:
        realm_namespace = _realm_namespace_from_issuer(issuer)

    host = os.environ.get("SO_MCP_HOST", "127.0.0.1").strip()
    if not host:
        raise RuntimeError("SO_MCP_HOST must not be empty")

    try:
        port = int(os.environ.get("SO_MCP_PORT", "8791"))
    except ValueError as exc:
        raise RuntimeError("SO_MCP_PORT must be an integer") from exc

    if not 1 <= port <= 65535:
        raise RuntimeError("SO_MCP_PORT must be between 1 and 65535")

    log_level = os.environ.get("UVICORN_LOG_LEVEL", "info").strip().lower()
    if log_level not in {
        "critical",
        "error",
        "warning",
        "info",
        "debug",
        "trace",
    }:
        raise RuntimeError("UVICORN_LOG_LEVEL is invalid")

    discovery_url = os.environ.get("SO_MCP_DISCOVERY_URL", "").strip() or None

    memory_adapter_command = load_memory_adapter_command()

    config = {
        "issuer": issuer,
        "audience": audience,
        "database_path": database_path,
        "tenant_claim": tenant_claim,
        "realm_namespace": realm_namespace,
        "host": host,
        "port": port,
        "log_level": log_level,
        "discovery_url": discovery_url,
        "memory_adapter_command": memory_adapter_command,
    }

    # Only present when SO_MCP_PRINCIPAL_DIRECTORY is set, so the unset case
    # stays byte-identical to the deny-all default.
    principal_directory = load_principal_directory()
    if principal_directory is not None:
        config["principal_directory"] = principal_directory

    return config


def _without_non_bearer_authorization(scope: dict[str, Any]) -> dict[str, Any]:
    """Remove non-Bearer or empty/malformed Authorization headers.

    The downstream MCP authentication middleware interprets a missing
    Authorization header as an unauthenticated request and emits a pure
    Bearer challenge. Only a non-empty, syntactically single-token Bearer
    credential is allowed to reach JWT verification.
    """
    headers = list(scope.get("headers", ()))
    authorization_values = [
        value
        for key, value in headers
        if key.lower() == b"authorization"
    ]

    if not authorization_values:
        return scope

    keep_bearer = False

    # Multiple Authorization headers are ambiguous; do not select one.
    if len(authorization_values) == 1:
        try:
            value = authorization_values[0].decode("latin-1").strip()
        except UnicodeError:
            value = ""

        parts = value.split()
        keep_bearer = (
            len(parts) == 2
            and parts[0].lower() == "bearer"
            and bool(parts[1])
            and not any(char.isspace() for char in parts[1])
        )

    if keep_bearer:
        return scope

    clean_scope = dict(scope)
    clean_scope["headers"] = [
        (key, value)
        for key, value in headers
        if key.lower() != b"authorization"
    ]
    return clean_scope


def _add_access_manage_to_challenge(value: bytes) -> bytes:
    """Add access.manage to the existing Bearer challenge scope parameter."""
    match = re.search(rb'(?i)\bscope="([^"]*)"', value)
    if match is None:
        return value

    current = match.group(1).decode("ascii", errors="strict")
    scopes = current.split()

    if "session_ops.access.manage" not in scopes:
        scopes.append("session_ops.access.manage")

    replacement = " ".join(scopes).encode("ascii")
    return value[:match.start(1)] + replacement + value[match.end(1):]


def _load_events_encryption_key(path_value: str) -> bytes:
    """Read a raw 32-byte key owned by the MCP service and private to it."""
    path = Path(path_value)
    if not path.is_absolute():
        raise RuntimeError(EVENTS_KEY_FILE_ENV + " must be an absolute path")
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(EVENTS_KEY_FILE_ENV + " must reference a regular file")
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RuntimeError(EVENTS_KEY_FILE_ENV + " must be owned by the MCP service user and mode 0400/0600")
        key = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(EVENTS_KEY_FILE_ENV + " is unavailable") from exc
    if len(key) != 32:
        raise RuntimeError(EVENTS_KEY_FILE_ENV + " must contain exactly 32 raw bytes")
    return key


class DeploymentASGI:
    """Add health and RFC 9728 metadata endpoints around the MCP ASGI app."""

    def __init__(
        self,
        inner: Any,
        *,
        issuer: str,
        audience: str,
        background_tasks: Optional[Sequence[Any]] = None,
    ) -> None:
        self.inner = inner
        self.issuer = issuer
        self.audience = audience
        self.background_tasks = tuple(background_tasks or ())

        resource = urlsplit(audience)
        self.endpoint_path = resource.path
        self.metadata_path = (
            "/.well-known/oauth-protected-resource" + resource.path
        )

        self._health_body = json.dumps(
            {"status": "ok"},
            separators=(",", ":"),
        ).encode("utf-8")

        self._metadata_body = json.dumps(
            {
                "resource": audience,
                "authorization_servers": [issuer],
                "scopes_supported": SUPPORTED_SCOPES,
                "bearer_methods_supported": ["header"],
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    async def _respond(
        scope: dict[str, Any],
        send: Any,
        status: int,
        body: bytes,
    ) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )

        # HEAD carries the GET representation's Content-Length but no body.
        response_body = b"" if scope.get("method") == "HEAD" else body
        await send(
            {
                "type": "http.response.body",
                "body": response_body,
            }
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan" and self.background_tasks:
            started_tasks: list[Any] = []
            stopped_tasks: set[int] = set()

            async def receive_with_task_lifecycle() -> dict[str, Any]:
                message = await receive()
                if message.get("type") == "lifespan.startup":
                    for task in self.background_tasks:
                        task.start()
                        started_tasks.append(task)
                elif message.get("type") == "lifespan.shutdown":
                    for task in reversed(started_tasks):
                        task.stop()
                        stopped_tasks.add(id(task))
                return message

            try:
                await self.inner(scope, receive_with_task_lifecycle, send)
            finally:
                for task in reversed(started_tasks):
                    if id(task) not in stopped_tasks:
                        task.stop()
            return

        if scope.get("type") != "http":
            await self.inner(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")

        if path == "/healthz" and method in ("GET", "HEAD"):
            await self._respond(
                scope,
                send,
                200,
                self._health_body,
            )
            return

        if path == self.metadata_path and method in ("GET", "HEAD"):
            await self._respond(
                scope,
                send,
                200,
                self._metadata_body,
            )
            return

        inner_scope = scope
        if path == self.endpoint_path:
            inner_scope = _without_non_bearer_authorization(scope)

        async def send_with_contract_scopes(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = []
                for name, value in message.get("headers", ()):
                    if name.lower() == b"www-authenticate":
                        value = _add_access_manage_to_challenge(value)
                    headers.append((name, value))

                updated = dict(message)
                updated["headers"] = headers
                await send(updated)
                return

            await send(message)

        await self.inner(
            inner_scope,
            receive,
            send_with_contract_scopes,
        )


def create_application(
    config: dict[str, Any],
    *,
    sender_gateway: Any = None,
) -> Any:
    """Initialize persistent storage and construct the authenticated app.

    The Sender Sidecar client is built from ``SO_MCP_SENDER_*`` before the app
    is constructed, so missing mTLS material fails startup instead of the
    first ChatGPT tool call.
    """
    store = SecureWorkItemStore(
        Path(config["database_path"]),
        principal_directory=config.get("principal_directory"),
    )
    store.initialize()

    verifier = JWTVerifier(
        issuer=config["issuer"],
        audience=config["audience"],
        realm_namespace=config["realm_namespace"],
        tenant_claim=config["tenant_claim"],
        discovery_url=config["discovery_url"],
    )

    if sender_gateway is None:
        sender_gateway = SenderGatewayClient.from_env()

    logging.getLogger("so-mcp").info(
        "Sender gateway: %s",
        sender_gateway.redacted_target(),
    )

    service = MCPToolService(
        store,
        memory_adapter=config.get("memory_adapter_command"),
        sender_gateway=sender_gateway,
    )

    events_key_file = os.environ.get(EVENTS_KEY_FILE_ENV, "").strip()
    events_db_path = os.environ.get(EVENTS_DB_ENV, "").strip()
    if events_db_path and not events_key_file:
        raise RuntimeError(EVENTS_DB_ENV + " requires " + EVENTS_KEY_FILE_ENV)

    events_store = None
    background_tasks: list[Any] = []
    if events_key_file:
        encryption_key = _load_events_encryption_key(events_key_file)
        if events_db_path and not Path(events_db_path).is_absolute():
            raise RuntimeError(EVENTS_DB_ENV + " must be an absolute path")
        if not events_db_path:
            events_db_path = str(
                Path(config["database_path"]).parent / "events" / "events.sqlite3"
            )
        if not callable(getattr(sender_gateway, "claim_events", None)) or not callable(
            getattr(sender_gateway, "ack_event", None)
        ) or not callable(getattr(sender_gateway, "get_job", None)):
            raise RuntimeError("SenderGatewayClient lacks MCP Events claim/ack/authorization methods")
        events_store = MCPEventsStore(
            events_db_path,
            encryption_key=encryption_key,
        )
        collector = MCPEventCollector(
            sender_gateway,
            events_store,
            worker_id="mcp-events-" + uuid.uuid4().hex,
        )
        def authorize_event(*, tenant_id: str, principal_id: str, job_id: str) -> bool:
            try:
                sender_gateway.get_job(
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    job_id=job_id,
                )
                return True
            except Exception as exc:
                if getattr(exc, "code", None) in ("NOT_FOUND", "PERMISSION_DENIED"):
                    return False
                raise

        background_tasks.append(
            MCPEventDispatcher(
                collector,
                events_store,
                authorize=authorize_event,
            )
        )

    mcp_app = build_mcp_server(
        service,
        verifier,
        events_store=events_store,
        sender_gateway=sender_gateway,
    )

    return DeploymentASGI(
        mcp_app,
        issuer=config["issuer"],
        audience=config["audience"],
        background_tasks=background_tasks,
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    logger = logging.getLogger("so-mcp")

    try:
        config = load_config()
        app = create_application(config)
    except Exception:
        logger.exception("Session Ops MCP startup failed")
        raise

    logger.info(
        "Starting Session Ops MCP: issuer=%s audience=%s host=%s port=%d "
        "internal_discovery=%s",
        config["issuer"],
        config["audience"],
        config["host"],
        config["port"],
        bool(config["discovery_url"]),
    )

    logger.info(
        "Memory adapter command: %s",
        _redact_memory_command(config["memory_adapter_command"]),
    )

    uvicorn.run(
        app,
        host=config["host"],
        port=config["port"],
        log_level=config["log_level"],
        access_log=True,
    )


if __name__ == "__main__":
    main()
