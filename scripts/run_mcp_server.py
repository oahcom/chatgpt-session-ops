"""Production entry point for the Session Ops MCP service.

The MCP SDK is imported only inside build_mcp_server(), preserving the
repository's SDK isolation boundary.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
]


def required_env(name: str) -> str:
    """Read a required non-empty environment variable."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError("required environment variable is missing: " + name)
    return value.strip()


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

    service = MCPToolService(store)
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

    uvicorn.run(
        app,
        host=config["host"],
        port=config["port"],
        log_level=config["log_level"],
        access_log=True,
    )


if __name__ == "__main__":
    main()
