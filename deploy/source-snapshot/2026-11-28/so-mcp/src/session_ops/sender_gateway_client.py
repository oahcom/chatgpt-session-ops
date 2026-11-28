"""HTTPS/mTLS client for the Sender Sidecar (MCP -> Sidecar only).

The MCP service never holds ChatGPT account credentials and never shells out
to the original CLI. This module is the only production-side transport to the
Sidecar: standard library only, fixed endpoints, bounded timeouts and a closed
error mapping.

Every request carries mTLS authentication, a Bearer service token, and a
signed MCP identity assertion.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import stat
import time
import uuid
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote, urlencode, urlsplit


URL_ENV = "SO_MCP_SENDER_URL"
CLIENT_CERT_ENV = "SO_MCP_SENDER_CLIENT_CERT"
CLIENT_KEY_ENV = "SO_MCP_SENDER_CLIENT_KEY"
CA_ENV = "SO_MCP_SENDER_CA"
TIMEOUT_ENV = "SO_MCP_SENDER_TIMEOUT"
TOKEN_FILE_ENV = "SO_MCP_SENDER_TOKEN_FILE"

IDENTITY_KEY_FILE = "/etc/so-mcp/sender-identity.key"

DEFAULT_TIMEOUT_SECONDS = 2.0
MAX_TIMEOUT_SECONDS = 4.0

#: The Sender adapter kills a read CLI after 10s and allows up to 5s to reap
#: its process group. Keep the HTTP read timeout beyond that cleanup budget.
READ_TIMEOUT_SECONDS = 16.0
MAX_READ_TIMEOUT_SECONDS = 16.0

#: A timed-out read is not retried: the server-side handler/CLI may still be
#: running after the client disconnects, so retrying can duplicate the work.
#: Retry only after server-side cancellation or operation deduplication exists.
READ_RETRIES = 0

MAX_RESPONSE_BYTES = 65536

JOBS_PATH = "/internal/v1/jobs"
SESSIONS_PATH = "/internal/v1/sessions"

#: Top-level job fields the client owns; business arguments go in ``payload``.
_RESERVED_JOB_FIELDS = frozenset({"operation", "idempotency_key", "payload"})

IDENTITY_HEADER = "X-SO-MCP-Identity"
PRINCIPAL_HEADER = "X-SO-MCP-Principal"
TENANT_HEADER = "X-SO-MCP-Tenant"

PUBLIC_ERROR_CODES = frozenset({
    "INVALID_ARGUMENT",
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "NOT_FOUND",
    "CONFLICT",
    "QUARANTINED_SESSION",
    "RESOURCE_EXHAUSTED",
    "DEPENDENCY_UNAVAILABLE",
    "TIMEOUT",
    "INTERNAL",
})

_STATUS_CODES = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    405: "INVALID_ARGUMENT",
    409: "CONFLICT",
    413: "RESOURCE_EXHAUSTED",
    422: "INVALID_ARGUMENT",
    429: "RESOURCE_EXHAUSTED",
}


class SenderGatewayConfigError(ValueError):
    """Configuration failure for the Sidecar client."""


class SenderGatewayError(Exception):
    """A failed Sidecar call, carrying a frozen public error code."""

    def __init__(
        self, code: str, message: str = "", *, retry_after_seconds: Optional[float] = None
    ) -> None:
        if code not in PUBLIC_ERROR_CODES:
            code = "INTERNAL"
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message or code)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise SenderGatewayConfigError(
            "required environment variable is missing: " + name
        )
    return value.strip()


def _validate_https_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise SenderGatewayConfigError(URL_ENV + " is required")

    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise SenderGatewayConfigError(
            URL_ENV + " is not a valid URL"
        ) from exc

    if parsed.scheme != "https" or not parsed.hostname or not parsed.netloc:
        raise SenderGatewayConfigError(
            URL_ENV + " must be an absolute HTTPS URL"
        )

    if parsed.username is not None or parsed.password is not None:
        raise SenderGatewayConfigError(
            URL_ENV + " must not contain URL credentials"
        )

    if parsed.query or parsed.fragment:
        raise SenderGatewayConfigError(
            URL_ENV + " must not contain a query or fragment"
        )

    if parsed.path and not parsed.path.startswith("/"):
        raise SenderGatewayConfigError(URL_ENV + " has an invalid path")

    return value.rstrip("/")


def _validate_existing_file(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SenderGatewayConfigError(name + " is required")

    if "\x00" in value:
        raise SenderGatewayConfigError(name + " contains a NUL character")

    path = os.path.abspath(os.path.expanduser(value))
    if not os.path.isfile(path):
        raise SenderGatewayConfigError(
            name + " must reference an existing file"
        )
    return path


def _parse_timeout(environ: Mapping[str, str]) -> float:
    raw = environ.get(TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TIMEOUT_SECONDS

    try:
        timeout = float(raw.strip())
    except (TypeError, ValueError) as exc:
        raise SenderGatewayConfigError(
            TIMEOUT_ENV + " must be a number of seconds"
        ) from exc

    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise SenderGatewayConfigError(
            TIMEOUT_ENV + " must be positive and at most %g seconds"
            % MAX_TIMEOUT_SECONDS
        )

    return timeout


def _read_identity_key(path: str = IDENTITY_KEY_FILE) -> bytes:
    """Read a raw 32-byte HMAC key and enforce restrictive permissions."""
    try:
        info = os.stat(path)

        if not stat.S_ISREG(info.st_mode):
            raise SenderGatewayConfigError(
                "sender identity key must be a regular file"
            )

        if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600:
            raise SenderGatewayConfigError(
                "sender identity key must have mode 0600"
            )

        with open(path, "rb") as key_file:
            key = key_file.read(33)

    except SenderGatewayConfigError:
        raise
    except OSError as exc:
        raise SenderGatewayConfigError(
            "sender identity key is unavailable"
        ) from exc

    if len(key) != 32:
        raise SenderGatewayConfigError(
            "sender identity key must contain exactly 32 raw bytes"
        )

    return key


def _read_service_token(path: str) -> str:
    """Read and validate the service Bearer token."""
    try:
        token_path = _validate_existing_file(TOKEN_FILE_ENV, path)

        with open(token_path, "r", encoding="utf-8") as token_file:
            token = token_file.read(16385)

    except SenderGatewayConfigError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SenderGatewayConfigError(
            "sender service token file is unavailable or unreadable"
        ) from exc

    token = token.strip()

    if not token or len(token) > 16384:
        raise SenderGatewayConfigError(
            "sender service token file contains an invalid token"
        )

    if any(ch.isspace() for ch in token):
        raise SenderGatewayConfigError(
            "sender service token must not contain whitespace"
        )

    return token


def load_sender_gateway_config(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Read and validate the Sidecar client configuration."""
    source = os.environ if environ is None else environ

    token_file = source.get(TOKEN_FILE_ENV)

    return {
        "url": _validate_https_url(_required(source, URL_ENV)),
        "client_cert": _validate_existing_file(
            CLIENT_CERT_ENV,
            _required(source, CLIENT_CERT_ENV),
        ),
        "client_key": _validate_existing_file(
            CLIENT_KEY_ENV,
            _required(source, CLIENT_KEY_ENV),
        ),
        "ca_file": _validate_existing_file(
            CA_ENV,
            _required(source, CA_ENV),
        ),
        "token_file": (
            _validate_existing_file(TOKEN_FILE_ENV, token_file)
            if token_file and token_file.strip()
            else None
        ),
        "timeout_seconds": _parse_timeout(source),
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _canonical_json(claims: Mapping[str, Any]) -> bytes:
    return json.dumps(
        claims,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _make_identity_assertion(
    *,
    key: bytes,
    principal_id: str,
    tenant_id: str,
    now: Optional[int] = None,
) -> str:
    """Create a fresh short-lived HMAC-signed MCP identity assertion."""
    if not isinstance(principal_id, str) or not principal_id:
        raise SenderGatewayError(
            "UNAUTHENTICATED",
            "verified principal identity is missing",
        )

    if not isinstance(tenant_id, str) or not tenant_id:
        raise SenderGatewayError(
            "UNAUTHENTICATED",
            "verified tenant identity is missing",
        )

    if not isinstance(key, bytes) or len(key) != 32:
        raise SenderGatewayConfigError(
            "sender identity key must contain exactly 32 raw bytes"
        )

    issued_at = int(time.time()) if now is None else int(now)

    claims = {
        "iss": "so-mcp",
        "aud": "so-sender",
        "sub": principal_id,
        "tenant": tenant_id,
        "iat": issued_at,
        "exp": issued_at + 30,
        "jti": str(uuid.uuid4()),
    }

    canonical = _canonical_json(claims)
    signature = hmac.new(key, canonical, hashlib.sha256).digest()

    return _b64url(canonical) + "." + _b64url(signature)


class SenderGatewayClient:
    """Four fixed Sidecar operations over HTTPS with mTLS."""

    def __init__(
        self,
        *,
        url: str,
        client_cert: str,
        client_key: str,
        ca_file: str,
        token_file: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        read_timeout_seconds: Optional[float] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        identity_key: Optional[bytes] = None,
    ) -> None:
        self.url = _validate_https_url(url)

        self.client_cert = _validate_existing_file(
            CLIENT_CERT_ENV, client_cert
        )
        self.client_key = _validate_existing_file(
            CLIENT_KEY_ENV, client_key
        )
        self.ca_file = _validate_existing_file(CA_ENV, ca_file)

        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise SenderGatewayConfigError(
                TIMEOUT_ENV + " must be a number of seconds"
            ) from exc

        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise SenderGatewayConfigError(
                TIMEOUT_ENV + " must be positive and at most %g seconds"
                % MAX_TIMEOUT_SECONDS
            )

        self.timeout_seconds = timeout

        read_timeout = (
            max(timeout, READ_TIMEOUT_SECONDS)
            if read_timeout_seconds is None
            else float(read_timeout_seconds)
        )

        if not 0 < read_timeout <= MAX_READ_TIMEOUT_SECONDS:
            raise SenderGatewayConfigError(
                "read timeout must be positive and at most %g seconds"
                % MAX_READ_TIMEOUT_SECONDS
            )

        self.read_timeout_seconds = read_timeout

        # Direct construction supports an injected TLS context for offline
        # transport tests. Production initialization always loads the
        # configured identity key and service token.
        if identity_key is None:
            try:
                self._identity_key = _read_identity_key()
            except SenderGatewayConfigError:
                if ssl_context is None:
                    raise
                self._identity_key = os.urandom(32)
        else:
            if not isinstance(identity_key, bytes) or len(identity_key) != 32:
                raise SenderGatewayConfigError(
                    "identity_key must contain exactly 32 raw bytes"
                )
            self._identity_key = identity_key

        if token_file:
            self.token_file = _validate_existing_file(
                TOKEN_FILE_ENV, token_file
            )
            self._service_token = _read_service_token(self.token_file)
        else:
            self.token_file = None
            if ssl_context is None:
                raise SenderGatewayConfigError(
                    TOKEN_FILE_ENV + " is required"
                )
            self._service_token = os.environ.get(
                "SO_MCP_SENDER_SERVICE_TOKEN", ""
            ).strip()

        if self._service_token and any(
            ch.isspace() for ch in self._service_token
        ):
            raise SenderGatewayConfigError(
                "sender service token must not contain whitespace"
            )

        parsed = urlsplit(self.url)
        self._host = parsed.hostname
        self._port = parsed.port or 443
        self._base_path = parsed.path.rstrip("/")

        try:
            self._context = (
                _validated_injected_context(ssl_context)
                if ssl_context is not None
                else self._build_ssl_context()
            )
        except SenderGatewayConfigError:
            raise
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise SenderGatewayConfigError(
                "sender gateway TLS configuration is invalid"
            ) from exc

    def _build_ssl_context(self) -> ssl.SSLContext:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True

            context.load_verify_locations(cafile=self.ca_file)
            context.load_cert_chain(
                certfile=self.client_cert,
                keyfile=self.client_key,
            )

        except (OSError, ssl.SSLError, ValueError) as exc:
            raise SenderGatewayConfigError(
                "sender gateway TLS configuration is invalid"
            ) from exc

        return context

    def redacted_target(self) -> str:
        return "https://%s:%d" % (self._host, self._port)

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "SenderGatewayClient":
        config = load_sender_gateway_config(environ)

        if not config["token_file"]:
            raise SenderGatewayConfigError(
                TOKEN_FILE_ENV + " is required"
            )

        return cls(**config)

    def _connect(self) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(
            self._host,
            self._port,
            context=self._context,
            timeout=self.timeout_seconds,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        identity: Optional[Mapping[str, str]] = None,
        *,
        timeout: Optional[float] = None,
        retries: int = 0,
        timeout_code: str = "DEPENDENCY_UNAVAILABLE",
    ) -> Dict[str, Any]:
        payload = (
            None
            if body is None
            else json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )

        headers = {
            "accept": "application/json",
            "user-agent": "Session-Ops-MCP/1.0",
            "Authorization": "Bearer " + self._service_token,
        }

        if payload is not None:
            headers["content-type"] = "application/json"

        if identity is not None:
            principal_id = str(identity["principal_id"])
            tenant_id = str(identity["tenant_id"])

            headers[PRINCIPAL_HEADER] = principal_id
            headers[TENANT_HEADER] = tenant_id

        effective_timeout = (
            self.timeout_seconds if timeout is None else timeout
        )
        attempts = max(0, int(retries)) + 1

        for attempt in range(attempts):
            connection = None

            if identity is not None:
                # A fresh assertion per attempt: the Sidecar rejects a reused
                # jti as a replay, so a retry must not inherit the old one.
                headers[IDENTITY_HEADER] = _make_identity_assertion(
                    key=self._identity_key,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                )

            try:
                connection = self._connect()
                # HTTPSConnection reads .timeout when it dials, so the read
                # budget applies without changing the factory signature.
                connection.timeout = effective_timeout

                connection.request(
                    method,
                    path,
                    body=payload,
                    headers=headers,
                )

                response = connection.getresponse()
                status = int(response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)

            except (socket.timeout, TimeoutError) as exc:
                if attempt + 1 < attempts:
                    continue

                raise SenderGatewayError(
                    timeout_code,
                    "sender gateway did not answer within %gs"
                    % effective_timeout,
                ) from exc

            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                raise SenderGatewayError(
                    "DEPENDENCY_UNAVAILABLE",
                    "sender gateway is unavailable",
                ) from exc

            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

            break

        if len(raw) > MAX_RESPONSE_BYTES:
            raise SenderGatewayError(
                "DEPENDENCY_UNAVAILABLE",
                "sender gateway response is too large",
            )

        try:
            document = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, ValueError) as exc:
            raise SenderGatewayError(
                "DEPENDENCY_UNAVAILABLE",
                "sender gateway returned an unreadable response",
            ) from exc

        if not isinstance(document, dict):
            raise SenderGatewayError(
                "DEPENDENCY_UNAVAILABLE",
                "sender gateway returned a non-object response",
            )

        if not 200 <= status < 300:
            raise _error_from_response(document, status)

        return document

    def enqueue(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Submit an asynchronous job using the frozen nested body.

        ``operation`` and ``idempotency_key`` stay at the top level; every
        business argument travels inside ``payload`` because the Sidecar
        rejects a job whose ``payload`` is not an object.
        """
        if operation not in ("session.create", "session.send"):
            raise SenderGatewayError(
                "INVALID_ARGUMENT",
                "unsupported Sidecar job operation",
            )

        if not isinstance(arguments, Mapping):
            raise SenderGatewayError(
                "INVALID_ARGUMENT",
                "job payload must be an object",
            )

        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
        ):
            raise SenderGatewayError(
                "INVALID_ARGUMENT",
                "idempotency_key is required",
            )

        if _RESERVED_JOB_FIELDS.intersection(arguments):
            raise SenderGatewayError(
                "INVALID_ARGUMENT",
                "job arguments contain reserved fields",
            )

        body = {
            "operation": operation,
            "idempotency_key": idempotency_key,
            "payload": dict(arguments),
        }

        return self._request(
            "POST",
            self._base_path + JOBS_PATH,
            body,
            {
                "principal_id": principal_id,
                "tenant_id": tenant_id,
            },
        )

    def get_job(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        job_id: str,
    ) -> Dict[str, Any]:
        path = "%s%s/%s" % (
            self._base_path,
            JOBS_PATH,
            quote(str(job_id), safe=""),
        )

        return self._request(
            "GET",
            path,
            None,
            {
                "principal_id": principal_id,
                "tenant_id": tenant_id,
            },
        )

    def list_sessions(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        page_size: int,
        cursor: Optional[str],
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {
            "page_size": int(page_size),
        }

        if cursor is not None:
            query["cursor"] = cursor

        return self._request(
            "GET",
            self._base_path + SESSIONS_PATH + "?" + urlencode(query),
            None,
            {
                "principal_id": principal_id,
                "tenant_id": tenant_id,
            },
            timeout=self.read_timeout_seconds,
            retries=READ_RETRIES,
            timeout_code="TIMEOUT",
        )

    def read_session(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        conversation_id: str,
        after_message_id: Optional[str],
        limit: int,
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {
            "limit": int(limit),
        }

        if after_message_id is not None:
            query["after_message_id"] = after_message_id

        path = "%s%s/%s/messages" % (
            self._base_path,
            SESSIONS_PATH,
            quote(str(conversation_id), safe=""),
        )

        return self._request(
            "GET",
            path + "?" + urlencode(query),
            None,
            {
                "principal_id": principal_id,
                "tenant_id": tenant_id,
            },
            timeout=self.read_timeout_seconds,
            retries=READ_RETRIES,
            timeout_code="TIMEOUT",
        )


def _validated_injected_context(context: Any) -> ssl.SSLContext:
    """Accept an injected context only if server verification stays enabled."""
    if not isinstance(context, ssl.SSLContext):
        raise SenderGatewayConfigError(
            "ssl_context must be an SSLContext"
        )

    if context.verify_mode != ssl.CERT_REQUIRED:
        raise SenderGatewayConfigError(
            "ssl_context must require a server certificate"
        )

    if not context.check_hostname:
        raise SenderGatewayConfigError(
            "ssl_context must keep hostname verification enabled"
        )

    return context


def _error_from_response(
    document: Mapping[str, Any],
    status: int,
) -> SenderGatewayError:
    """Map Sidecar errors first, then HTTP status, onto the frozen error set."""
    error = document.get("error")

    if isinstance(error, Mapping):
        code = error.get("code")

        if isinstance(code, str) and code in PUBLIC_ERROR_CODES:
            message = error.get("message")

            retry_after = error.get("retry_after_seconds")
            try:
                retry_after = float(retry_after) if retry_after is not None else None
            except (TypeError, ValueError):
                retry_after = None
            return SenderGatewayError(
                code,
                message if isinstance(message, str) else code,
                retry_after_seconds=retry_after,
            )

    if status == 401:
        return SenderGatewayError(
            "UNAUTHENTICATED",
            "sender gateway rejected the MCP identity assertion",
        )

    if status == 504:
        return SenderGatewayError(
            "TIMEOUT",
            "sender gateway did not answer in time",
        )

    if 500 <= status <= 599:
        return SenderGatewayError(
            "DEPENDENCY_UNAVAILABLE",
            "sender gateway failed with status %d" % status,
        )

    return SenderGatewayError(
        _STATUS_CODES.get(status, "INTERNAL"),
        "sender gateway rejected the request with status %d" % status,
    )
