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
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn

from src.session_ops.authority import SecureWorkItemStore
from src.session_ops.mcp_auth import JWTVerifier
from src.session_ops.mcp_server import MCPToolService, build_mcp_server


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
        raise RuntimeError(
            MEMORY_STDIO_ENV + " must contain a valid JSON array of strings"
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
    # Loopback-only is a security contract (docs/DEPLOY-so-mcp.md section 2a):
    # this process is a private service behind a TLS-terminating reverse proxy
    # and must never be reachable from another host. Reject everything except
    # the two loopback spellings before the value can reach uvicorn.run().
    if host not in {"127.0.0.1", "::1"}:
        raise RuntimeError(
            "SO_MCP_HOST must not be empty and must be loopback-only; "
            "only 127.0.0.1 or ::1 are allowed by the deployment security "
            f"contract (got {host!r})"
        )

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

    default_chatgpt_cli = Path(__file__).resolve().parents[1] / "scripts" / "chatgpt_send.py"
    configured_chatgpt_cli = os.environ.get("SO_MCP_CHATGPT_CLI", "").strip()
    chatgpt_cli_path = Path(configured_chatgpt_cli or default_chatgpt_cli).expanduser()
    if not chatgpt_cli_path.is_absolute():
        raise RuntimeError("SO_MCP_CHATGPT_CLI must be an absolute path")
    if not chatgpt_cli_path.is_file() or not os.access(str(chatgpt_cli_path), os.R_OK):
        raise RuntimeError("ChatGPT CLI script is unavailable")
    chatgpt_cli_path = str(chatgpt_cli_path.resolve())

    return {
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
        "chatgpt_cli_path": chatgpt_cli_path,
    }


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


class DeploymentASGI:
    """Add health and RFC 9728 metadata endpoints around the MCP ASGI app."""

    def __init__(
        self,
        inner: Any,
        *,
        issuer: str,
        audience: str,
    ) -> None:
        self.inner = inner
        self.issuer = issuer
        self.audience = audience

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


def create_application(config: dict[str, Any]) -> Any:
    """Initialize persistent storage and construct the authenticated app."""
    store = SecureWorkItemStore(
        Path(config["database_path"]),
    )
    store.initialize()

    verifier = JWTVerifier(
        issuer=config["issuer"],
        audience=config["audience"],
        realm_namespace=config["realm_namespace"],
        tenant_claim=config["tenant_claim"],
        discovery_url=config["discovery_url"],
    )

    service = MCPToolService(
        store,
        memory_adapter=config.get("memory_adapter_command"),
        chatgpt_cli_path=config.get("chatgpt_cli_path"),
    )
    mcp_app = build_mcp_server(service, verifier)

    return DeploymentASGI(
        mcp_app,
        issuer=config["issuer"],
        audience=config["audience"],
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
    logger.info("Direct ChatGPT CLI: %s", config["chatgpt_cli_path"])

    uvicorn.run(
        app,
        host=config["host"],
        port=config["port"],
        log_level=config["log_level"],
        access_log=True,
    )


if __name__ == "__main__":
    main()
