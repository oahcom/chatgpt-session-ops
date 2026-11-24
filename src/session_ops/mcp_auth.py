"""Authentication boundary for the optional Session Ops MCP service.

This module has no MCP SDK dependency. It validates RS256 JWTs, maps trusted
claims to the authority Principal type, and enforces per-tool OAuth scopes.

Only the cryptographic RSA operation requires the optional ``cryptography``
package. Network access is synchronous and injectable for deterministic tests.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse, urlunparse

from src.session_ops.authority import InvalidPrincipal, Principal


MAX_TOKEN_BYTES = 16384
MAX_JWKS_BYTES = 1048576
MAX_DISCOVERY_BYTES = 65536
DEFAULT_HTTP_TIMEOUT_SECONDS = 3.0
DEFAULT_CACHE_SECONDS = 300.0
DEFAULT_CLOCK_SKEW_SECONDS = 30.0
MIN_RSA_BITS = 2048

_SCOPE_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,128}$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


class AuthenticationError(Exception):
    """The bearer token could not be authenticated.

    ``category`` is suitable for internal classification and diagnostics.
    Do not expose detailed messages to an unauthenticated caller.
    """

    def __init__(self, message: str, category: str = "invalid_token") -> None:
        super().__init__(message)
        self.category = category
        self.code = "UNAUTHENTICATED"


class ScopeError(Exception):
    """The authenticated principal lacks one or more required scopes."""

    def __init__(self, required: Sequence[str]) -> None:
        self.required = tuple(required)
        super().__init__("required OAuth scope is missing")
        self.code = "PERMISSION_DENIED"


class AuthConfigurationError(ValueError):
    """The verifier configuration is invalid."""


class JWKSFetchError(AuthenticationError):
    """The issuer discovery or JWKS endpoint could not be reached."""

    def __init__(self, message: str) -> None:
        super().__init__(message, category="dependency_unavailable")


class AuthContext:
    """Verified identity and OAuth scopes for one bearer token."""

    __slots__ = ("principal", "scopes", "issuer", "subject", "realm_namespace")

    def __init__(
        self,
        principal: Principal,
        scopes: Sequence[str],
        issuer: str,
        subject: str,
        realm_namespace: str,
    ) -> None:
        self.principal = principal
        self.scopes = frozenset(scopes)
        self.issuer = issuer
        self.subject = subject
        self.realm_namespace = realm_namespace

    def require(self, *required_scopes: str) -> None:
        require_scopes(self, required_scopes)


# The scope contract is intentionally explicit. Each tool receives only the
# minimum read/write capabilities it needs. Grant/revoke require both write
# authority and the dedicated access-management capability.
TOOL_REQUIRED_SCOPES = {
    "work_item.create": ("session_ops.work_item.write",),
    "work_item.get": ("session_ops.work_item.read",),
    "work_item.list": ("session_ops.work_item.read",),
    "work_item.update": ("session_ops.work_item.write",),
    "work_item.grant": (
        "session_ops.work_item.write",
        "session_ops.access.manage",
    ),
    "work_item.revoke": (
        "session_ops.work_item.write",
        "session_ops.access.manage",
    ),
    "session.link": (
        "session_ops.session.write",
        "session_ops.work_item.write",
    ),
    "session.unlink": (
        "session_ops.session.write",
        "session_ops.work_item.write",
    ),
    "session.list": (
        "session_ops.session.read",
        "session_ops.work_item.read",
    ),
    "session.context": (
        "session_ops.context.read",
        "session_ops.session.read",
        "session_ops.work_item.read",
    ),
}


def required_scopes_for_tool(tool_name: str) -> Tuple[str, ...]:
    """Return the minimum scope tuple for a known MCP tool."""
    try:
        return TOOL_REQUIRED_SCOPES[tool_name]
    except KeyError as exc:
        raise ValueError("unknown MCP tool: %s" % tool_name) from exc


def require_scopes(
    auth: AuthContext,
    required_scopes: Sequence[str],
) -> None:
    """Raise ScopeError unless every required scope is present."""
    if not isinstance(auth, AuthContext):
        raise AuthenticationError("verified authentication context required")

    required = tuple(required_scopes)
    if not required:
        return

    missing = [scope for scope in required if scope not in auth.scopes]
    if missing:
        raise ScopeError(required)


def require_tool_scope(auth: AuthContext, tool_name: str) -> None:
    """Enforce the minimum scope set declared for a tool."""
    require_scopes(auth, required_scopes_for_tool(tool_name))


def _reject_duplicate_json_keys(pairs: list) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_json_object(raw: bytes, description: str) -> Dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise AuthenticationError(
            "%s is not valid JSON" % description
        ) from exc

    if not isinstance(value, dict):
        raise AuthenticationError(
            "%s must be a JSON object" % description
        )
    return value


def _decode_base64url(value: str) -> bytes:
    """Decode a strict, unpadded or correctly padded Base64URL segment."""
    if not isinstance(value, str) or not value:
        raise AuthenticationError("JWT segment is empty")

    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise AuthenticationError("JWT segment is not valid Base64URL")

    unpadded = value.rstrip("=")
    if len(unpadded) % 4 == 1:
        raise AuthenticationError("JWT segment has invalid Base64URL length")

    padded = unpadded + ("=" * ((4 - len(unpadded) % 4) % 4))
    try:
        decoded = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeError) as exc:
        raise AuthenticationError("JWT segment is not valid Base64URL") from exc

    # Reject non-canonical encodings and unexpected padding.
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != unpadded:
        raise AuthenticationError("JWT segment is not canonically encoded")
    return decoded


def parse_jwt(token: str) -> Tuple[Dict[str, Any], Dict[str, Any], bytes, bytes]:
    """Parse compact JWT syntax without trusting or verifying its contents.

    Returns (header, claims, signature, signing_input). This function is pure
    and does not require cryptography or the MCP SDK.
    """
    if not isinstance(token, str):
        raise AuthenticationError("bearer token must be text")

    try:
        encoded = token.encode("ascii")
    except UnicodeError as exc:
        raise AuthenticationError("bearer token must be ASCII") from exc

    if not encoded or len(encoded) > MAX_TOKEN_BYTES:
        raise AuthenticationError("bearer token is empty or too large")

    parts = token.split(".")
    if len(parts) != 3:
        raise AuthenticationError("JWT must contain three segments")

    header_raw = _decode_base64url(parts[0])
    claims_raw = _decode_base64url(parts[1])
    signature = _decode_base64url(parts[2])

    header = _parse_json_object(header_raw, "JWT header")
    claims = _parse_json_object(claims_raw, "JWT claims")
    signing_input = (parts[0] + "." + parts[1]).encode("ascii")

    return header, claims, signature, signing_input


def select_jwk(
    jwks_document: Mapping[str, Any],
    kid: str,
) -> Dict[str, Any]:
    """Select exactly one usable RS256 signing JWK by key ID.

    This pure function never fetches or refreshes keys. Duplicate matching
    ``kid`` values are rejected rather than selected ambiguously.
    """
    if not isinstance(jwks_document, Mapping):
        raise AuthenticationError("JWKS document must be an object")

    keys = jwks_document.get("keys")
    if not isinstance(keys, list):
        raise AuthenticationError("JWKS document has no keys array")

    matches = []
    for key in keys:
        if not isinstance(key, dict):
            continue
        if key.get("kid") != kid:
            continue
        if key.get("kty") != "RSA":
            continue
        if key.get("use", "sig") != "sig":
            continue
        if key.get("alg", "RS256") != "RS256":
            continue
        if not isinstance(key.get("n"), str):
            continue
        if not isinstance(key.get("e"), str):
            continue
        matches.append(key)

    if len(matches) != 1:
        if not matches:
            raise AuthenticationError("no matching RS256 signing key")
        raise AuthenticationError("JWKS contains duplicate matching key IDs")

    return dict(matches[0])


def scopes_from_claims(claims: Mapping[str, Any]) -> frozenset:
    """Parse the OAuth ``scope`` claim as a space-delimited scope string."""
    value = claims.get("scope")

    if not isinstance(value, str):
        raise AuthenticationError("scope claim must be a string")

    if not value or value != value.strip():
        raise AuthenticationError("scope claim is empty or malformed")

    scopes = value.split(" ")
    if any(not scope or not _SCOPE_TOKEN_RE.fullmatch(scope) for scope in scopes):
        raise AuthenticationError("scope claim contains an invalid scope")

    if len(scopes) != len(set(scopes)):
        raise AuthenticationError("scope claim contains duplicate scopes")

    return frozenset(scopes)


def _audience_matches(claim: Any, expected_audience: str) -> bool:
    if isinstance(claim, str):
        return claim == expected_audience

    if isinstance(claim, list):
        return (
            bool(claim)
            and all(isinstance(item, str) and item for item in claim)
            and expected_audience in claim
        )

    return False


def _numeric_date(
    claims: Mapping[str, Any],
    name: str,
    required: bool,
) -> Optional[float]:
    value = claims.get(name)
    if value is None:
        if required:
            raise AuthenticationError("%s claim is required" % name)
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuthenticationError("%s claim must be a NumericDate" % name)

    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuthenticationError("%s claim is invalid" % name) from exc

    if result != result or result in (float("inf"), float("-inf")):
        raise AuthenticationError("%s claim is not finite" % name)

    return result


def validate_registered_claims(
    claims: Mapping[str, Any],
    *,
    issuer: str,
    audience: str,
    now: Optional[float] = None,
    clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
) -> None:
    """Validate issuer, audience, and JWT NumericDate claims."""
    if claims.get("iss") != issuer:
        raise AuthenticationError("issuer mismatch")

    if not _audience_matches(claims.get("aud"), audience):
        raise AuthenticationError("audience mismatch")

    current_time = time.time() if now is None else float(now)
    skew = float(clock_skew_seconds)
    if skew < 0:
        raise ValueError("clock skew must not be negative")

    expiration = _numeric_date(claims, "exp", required=True)
    not_before = _numeric_date(claims, "nbf", required=False)
    issued_at = _numeric_date(claims, "iat", required=False)

    if current_time >= expiration + skew:
        raise AuthenticationError("token has expired")

    if not_before is not None and current_time + skew < not_before:
        raise AuthenticationError("token is not active yet")

    if issued_at is not None and issued_at > current_time + skew:
        raise AuthenticationError("token was issued in the future")


def principal_from_claims(
    claims: Mapping[str, Any],
    *,
    expected_issuer: str,
    expected_audience: str,
    realm_namespace: str,
    tenant_claim: str = "tenant_id",
) -> AuthContext:
    """Map already-verified claims to an authority Principal.

    The tenant comes only from the configured trusted claim. The subject is
    namespaced by the configured Keycloak realm namespace to prevent equal
    ``sub`` values from different realms colliding in the authority database.
    """
    if claims.get("iss") != expected_issuer:
        raise AuthenticationError("issuer mismatch")

    if not _audience_matches(claims.get("aud"), expected_audience):
        raise AuthenticationError("audience mismatch")

    if not isinstance(realm_namespace, str):
        raise AuthenticationError("realm namespace is not configured")
    if not _NAMESPACE_RE.fullmatch(realm_namespace):
        raise AuthenticationError("realm namespace is malformed")

    if not isinstance(tenant_claim, str) or not tenant_claim:
        raise AuthenticationError("tenant claim name is not configured")

    subject = claims.get("sub")
    tenant_id = claims.get(tenant_claim)

    if not isinstance(subject, str) or not subject:
        raise AuthenticationError("sub claim is required")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise AuthenticationError("trusted tenant claim is required")

    # Principal.principal_id is limited to 128 safe ASCII characters by the
    # authority layer. Reject overflow; never truncate or normalize identity.
    namespaced_subject = realm_namespace + ":" + subject
    if len(namespaced_subject) > 128:
        raise AuthenticationError("namespaced subject exceeds principal limit")

    try:
        principal = Principal(
            tenant_id=tenant_id,
            principal_id=namespaced_subject,
        )
    except (InvalidPrincipal, TypeError, ValueError) as exc:
        raise AuthenticationError("principal claims are malformed") from exc

    return AuthContext(
        principal=principal,
        scopes=scopes_from_claims(claims),
        issuer=expected_issuer,
        subject=subject,
        realm_namespace=realm_namespace,
    )


def _validate_https_url(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuthenticationError("%s URL is missing" % description)

    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise AuthenticationError("%s URL is malformed" % description) from exc

    if parsed.scheme != "https" or not parsed.netloc:
        raise AuthenticationError("%s URL must use HTTPS" % description)
    if parsed.username is not None or parsed.password is not None:
        raise AuthenticationError("%s URL must not contain credentials" % description)
    if parsed.fragment:
        raise AuthenticationError("%s URL must not contain a fragment" % description)
    return value


def _validate_public_https_url(value: Any, description: str) -> str:
    """Require HTTPS and reject destinations resolving to non-public IPs."""
    value = _validate_https_url(value, description)
    parsed = urlparse(value)
    host = parsed.hostname
    if not host:
        raise AuthenticationError("%s URL has no hostname" % description)

    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            # Residual DNS-rebinding risk: this validation lookup is separate from
            # the HTTP client's later connection lookup, so DNS can return a
            # different address at connect time. This check is not a complete
            # SSRF defense. A stronger mitigation pins the validated IP for the
            # connection while preserving TLS SNI and certificate-hostname
            # verification (or equivalently verifies the connected peer against
            # the validated address and certificate identity).
            records = socket.getaddrinfo(
                host,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise AuthenticationError(
                "%s hostname could not be resolved" % description
            ) from exc
        addresses = []
        for record in records:
            try:
                addresses.append(ipaddress.ip_address(record[4][0]))
            except ValueError as exc:
                raise AuthenticationError(
                    "%s hostname resolved to an invalid address" % description
                ) from exc

    if not addresses:
        raise AuthenticationError("%s hostname has no addresses" % description)

    # Fail closed if even one DNS answer is private, loopback, link-local,
    # reserved, multicast, unspecified, or otherwise non-global.
    if any(not address.is_global for address in addresses):
        raise AuthenticationError(
            "%s URL resolves to a non-public network address" % description
        )
    return value


def _validate_discovery_override_url(value: Any) -> str:
    """Validate an explicit discovery URL.

    Plain HTTP is permitted only for loopback addresses. Remote discovery
    overrides must use HTTPS and therefore retain normal certificate checks.
    """
    if not isinstance(value, str) or not value:
        raise AuthConfigurationError("discovery URL must be a non-empty string")

    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise AuthConfigurationError("discovery URL is malformed") from exc

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AuthConfigurationError("discovery URL must be absolute HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise AuthConfigurationError("discovery URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise AuthConfigurationError("discovery URL must not contain query or fragment")
    if not parsed.path.endswith("/.well-known/openid-configuration"):
        raise AuthConfigurationError(
            "discovery URL must end with /.well-known/openid-configuration"
        )

    if parsed.scheme == "http":
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise AuthConfigurationError(
                "plain HTTP discovery is allowed only on loopback"
            )

    return value


def _validate_fetch_url(value: str, description: str) -> None:
    """Validate fetch URLs; HTTP is restricted to loopback."""
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise AuthenticationError("%s URL is malformed" % description) from exc

    if parsed.scheme == "https" and parsed.netloc:
        pass
    elif parsed.scheme == "http" and parsed.netloc:
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise AuthenticationError("plain HTTP fetch is allowed only on loopback")
    else:
        raise AuthenticationError("%s URL must use HTTPS or loopback HTTP" % description)

    if parsed.username is not None or parsed.password is not None:
        raise AuthenticationError("%s URL must not contain credentials" % description)
    if parsed.fragment:
        raise AuthenticationError("%s URL must not contain a fragment" % description)


def _default_json_fetcher(url: str, timeout: float, max_bytes: int) -> Dict[str, Any]:
    """Fetch one bounded JSON document using the stdlib.

    HTTPS is accepted normally. HTTP is accepted only for loopback URLs, which
    is used when a configured internal discovery URL maps the issuer's JWKS
    path onto the local Keycloak listener.
    """
    _validate_fetch_url(url, "request")
    if urlparse(url).scheme == "https":
        _validate_public_https_url(url, "request")

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Session-Ops-MCP/1.0",
        },
        method="GET",
    )

    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req, fp, code, msg, headers, newurl
        ):
            return None

    opener = urllib.request.build_opener(_NoRedirectHandler)

    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            _validate_fetch_url(final_url, "response")
            if urlparse(final_url).scheme == "https":
                _validate_public_https_url(final_url, "response")

            original = urlparse(url)
            final = urlparse(final_url)
            if original.scheme == "http" and (
                final.scheme != original.scheme
                or final.netloc.lower() != original.netloc.lower()
            ):
                raise AuthenticationError(
                    "internal identity endpoint redirected to another origin"
                )

            raw = response.read(max_bytes + 1)
    except AuthenticationError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JWKSFetchError("identity endpoint request failed") from exc
    except Exception as exc:
        raise JWKSFetchError("identity endpoint request failed") from exc

    if len(raw) > max_bytes:
        raise AuthenticationError("identity endpoint response is too large")

    return _parse_json_object(raw, "identity endpoint response")


class JWKSProvider:
    """OIDC discovery and JWKS cache with bounded refresh behavior.

    ``fetch_json`` is injectable and must have signature
    ``fetch_json(url, timeout, max_bytes) -> Mapping``. It can be a simple
    deterministic test fake; no MCP SDK or event loop is involved.

    When ``discovery_url`` is configured, discovery is fetched from that
    address, while the discovery document's issuer is still required to equal
    the configured public issuer. The discovered public JWKS URI must share
    the issuer's origin; only then is its origin mapped to the internal
    discovery origin. The JWT issuer claim is never rewritten.
    """

    def __init__(
        self,
        issuer: str,
        *,
        discovery_url: Optional[str] = None,
        fetch_json: Optional[Callable[[str, float, int], Mapping[str, Any]]] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        _validate_https_url(self.issuer, "issuer")

        self._discovery_override = discovery_url is not None
        if discovery_url is None:
            self.discovery_url = (
                self.issuer + "/.well-known/openid-configuration"
            )
        else:
            self.discovery_url = _validate_discovery_override_url(discovery_url)

            expected_path = (
                urlparse(self.issuer).path.rstrip("/")
                + "/.well-known/openid-configuration"
            )
            if urlparse(self.discovery_url).path != expected_path:
                raise AuthConfigurationError(
                    "discovery URL path does not match configured issuer path"
                )

        self.fetch_json = fetch_json or _default_json_fetcher
        self.timeout_seconds = float(timeout_seconds)
        self.cache_seconds = float(cache_seconds)

        if self.timeout_seconds <= 0:
            raise AuthConfigurationError("HTTP timeout must be positive")
        if self.cache_seconds < 0:
            raise AuthConfigurationError("cache duration must not be negative")

        self._lock = threading.RLock()
        self._jwks_uri = None
        self._jwks = None
        self._expires_at = 0.0
        # A single refresh may run at a time, but the network operation must
        # never run while _lock is held.
        self._refresh_in_progress = False
        self._refresh_retry_at = 0.0
        self._refresh_backoff_seconds = 5.0

    def _fetch_document(self, url: str, max_bytes: int) -> Dict[str, Any]:
        try:
            document = self.fetch_json(
                url,
                self.timeout_seconds,
                max_bytes,
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise JWKSFetchError("identity endpoint request failed") from exc

        if not isinstance(document, Mapping):
            raise AuthenticationError("identity endpoint returned a non-object")
        return dict(document)

    def _discover_jwks_uri(self) -> str:
        document = self._fetch_document(
            self.discovery_url,
            MAX_DISCOVERY_BYTES,
        )

        if document.get("issuer") != self.issuer:
            raise AuthenticationError("OIDC discovery issuer mismatch")

        public_jwks_uri = _validate_public_https_url(
            document.get("jwks_uri"),
            "JWKS",
        )

        issuer_parts = urlparse(self.issuer)
        jwks_parts = urlparse(public_jwks_uri)

        if (
            jwks_parts.scheme.lower() != issuer_parts.scheme.lower()
            or jwks_parts.netloc.lower() != issuer_parts.netloc.lower()
        ):
            raise AuthenticationError(
                "discovered JWKS origin does not match configured issuer"
            )

        if not self._discovery_override:
            return public_jwks_uri

        internal_parts = urlparse(self.discovery_url)

        # Preserve the Keycloak JWKS path and query, changing only the network
        # origin to the explicitly configured internal discovery origin.
        internal_jwks_uri = urlunparse(
            (
                internal_parts.scheme,
                internal_parts.netloc,
                jwks_parts.path,
                jwks_parts.params,
                jwks_parts.query,
                "",
            )
        )
        if urlparse(internal_jwks_uri).scheme == "https":
            _validate_public_https_url(internal_jwks_uri, "internal JWKS")
        return internal_jwks_uri

    def _download_jwks(self) -> Dict[str, Any]:
        if self._jwks_uri is None:
            self._jwks_uri = self._discover_jwks_uri()

        document = self._fetch_document(
            self._jwks_uri,
            MAX_JWKS_BYTES,
        )

        keys = document.get("keys")
        if not isinstance(keys, list):
            raise AuthenticationError("JWKS document has no keys array")

        # Validate the top-level structure now; select_jwk validates the
        # requested key and rejects ambiguous duplicate key IDs.
        return document

    def get_jwk(self, kid: str) -> Dict[str, Any]:
        """Return a matching JWK without allowing attacker-controlled kid
        values to trigger refreshes against a fresh cache.

        Refresh coordination is protected by _lock; discovery/JWKS network
        I/O is deliberately performed after releasing it.
        """
        if not isinstance(kid, str) or not kid:
            raise AuthenticationError("JWT kid header is required")

        with self._lock:
            now = time.monotonic()
            cache_is_fresh = self._jwks is not None and now < self._expires_at

            if cache_is_fresh:
                # A miss on a fresh cache is an invalid/unknown kid, not a
                # signal to perform attacker-triggered network I/O.
                return select_jwk(self._jwks, kid)

            if self._refresh_in_progress:
                raise JWKSFetchError("JWKS refresh is already in progress")

            if now < self._refresh_retry_at:
                raise JWKSFetchError("JWKS refresh is in backoff")

            self._refresh_in_progress = True

        # Never hold _lock across discovery or JWKS network I/O.
        try:
            refreshed = self._download_jwks()
        except Exception:
            with self._lock:
                self._refresh_in_progress = False
                self._refresh_retry_at = (
                    time.monotonic() + self._refresh_backoff_seconds
                )
            raise

        with self._lock:
            self._jwks = refreshed
            self._expires_at = time.monotonic() + self.cache_seconds
            self._refresh_retry_at = 0.0
            self._refresh_in_progress = False
            return select_jwk(self._jwks, kid)


def _rsa_public_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    """Construct a cryptography RSA public key from an RSA JWK."""
    try:
        from cryptography.hazmat.primitives.asymmetric.rsa import (
            RSAPublicNumbers,
        )
    except ImportError as exc:
        raise AuthenticationError(
            "RS256 verification requires the cryptography package",
            category="dependency_unavailable",
        ) from exc

    try:
        modulus_bytes = _decode_base64url(jwk["n"])
        exponent_bytes = _decode_base64url(jwk["e"])
        modulus = int.from_bytes(modulus_bytes, "big")
        exponent = int.from_bytes(exponent_bytes, "big")

        if modulus.bit_length() < MIN_RSA_BITS:
            raise AuthenticationError("RSA signing key is too small")
        if exponent < 3 or exponent % 2 == 0:
            raise AuthenticationError("RSA public exponent is invalid")

        return RSAPublicNumbers(exponent, modulus).public_key()
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError("RSA JWK is malformed") from exc


def verify_rs256_signature(
    signing_input: bytes,
    signature: bytes,
    jwk: Mapping[str, Any],
) -> None:
    """Verify RSASSA-PKCS1-v1_5 with SHA-256; fail closed on every error."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise AuthenticationError(
            "RS256 verification requires the cryptography package",
            category="dependency_unavailable",
        ) from exc

    public_key = _rsa_public_key_from_jwk(jwk)
    try:
        public_key.verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise AuthenticationError("JWT signature verification failed") from exc
    except Exception as exc:
        raise AuthenticationError("JWT signature verification failed") from exc


class JWTVerifier:
    """Verify OIDC RS256 bearer tokens and return a trusted AuthContext."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        realm_namespace: str,
        tenant_claim: str = "tenant_id",
        jwks_provider: Optional[JWKSProvider] = None,
        discovery_url: Optional[str] = None,
        fetch_json: Optional[
            Callable[[str, float, int], Mapping[str, Any]]
        ] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
    ) -> None:
        if not isinstance(issuer, str) or not issuer:
            raise AuthConfigurationError("issuer is required")
        if not isinstance(audience, str) or not audience:
            raise AuthConfigurationError("audience is required")
        if not isinstance(realm_namespace, str) or not _NAMESPACE_RE.fullmatch(
            realm_namespace
        ):
            raise AuthConfigurationError("valid realm_namespace is required")
        if not isinstance(tenant_claim, str) or not tenant_claim:
            raise AuthConfigurationError("tenant_claim is required")
        if jwks_provider is not None and discovery_url is not None:
            raise AuthConfigurationError(
                "provide jwks_provider or discovery_url, not both"
            )

        self.issuer = issuer.rstrip("/")
        _validate_https_url(self.issuer, "issuer")
        self.audience = audience
        self.realm_namespace = realm_namespace
        self.tenant_claim = tenant_claim
        self.clock_skew_seconds = float(clock_skew_seconds)

        if self.clock_skew_seconds < 0:
            raise AuthConfigurationError("clock skew must not be negative")

        self.jwks_provider = jwks_provider or JWKSProvider(
            self.issuer,
            discovery_url=discovery_url,
            fetch_json=fetch_json,
            timeout_seconds=timeout_seconds,
            cache_seconds=cache_seconds,
        )

        if self.jwks_provider.issuer != self.issuer:
            raise AuthConfigurationError(
                "JWKS provider issuer does not match verifier issuer"
            )

    def verify(self, token: str, now: Optional[float] = None) -> AuthContext:
        """Authenticate one compact JWT and return its principal and scopes."""
        header, claims, signature, signing_input = parse_jwt(token)

        # Algorithm confusion is rejected before any key lookup.
        if header.get("alg") != "RS256":
            raise AuthenticationError("only RS256 JWTs are accepted")
        if header.get("typ") not in (None, "JWT"):
            raise AuthenticationError("unexpected JWT typ header")
        if "crit" in header:
            raise AuthenticationError("critical JWT headers are unsupported")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationError("JWT kid header is required")

        validate_registered_claims(
            claims,
            issuer=self.issuer,
            audience=self.audience,
            now=now,
            clock_skew_seconds=self.clock_skew_seconds,
        )

        jwk = self.jwks_provider.get_jwk(kid)
        verify_rs256_signature(signing_input, signature, jwk)

        return principal_from_claims(
            claims,
            expected_issuer=self.issuer,
            expected_audience=self.audience,
            realm_namespace=self.realm_namespace,
            tenant_claim=self.tenant_claim,
        )
