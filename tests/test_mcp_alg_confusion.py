"""Offline tests for JWT algorithm-confusion rejection.

Row 4 of the security acceptance matrix was CODE_ONLY: the guards existed and
nothing fed them. These cases feed them, and each one asserts the key lookup
never happened -- a rejection that still fetches a key is not a rejection that
happens before the key lookup.
"""

import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock

from src.session_ops.mcp_auth import (
    AuthenticationError,
    JWTVerifier,
)

ISSUER = "https://issuer.example/realms/session-ops"
AUDIENCE = "session-ops"
NOW = 2_000_000_000

# Deterministic 2048-bit RSA modulus bytes, represented by the mocked JWKS
# entry. These same public-key bytes are used as the HMAC secret in the HS256
# confusion case, which is exactly what the classic attack does.
RSA_MODULUS = b"\x80" + b"\x00" * 255
RSA_EXPONENT = (65537).to_bytes(3, "big")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def encode_segment(value: dict) -> str:
    return b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def make_token(header: dict, signature: bytes = b"fake-signature", hmac_key=None):
    header_segment = encode_segment(header)
    claims_segment = encode_segment(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "alice",
            "tenant_id": "tenant1",
            "scope": "session_ops.work_item.read",
            "exp": NOW + 1000,
            "iat": NOW - 100,
        }
    )

    signing_input = (header_segment + "." + claims_segment).encode("ascii")

    if hmac_key is not None:
        signature = hmac.new(hmac_key, signing_input, hashlib.sha256).digest()

    return header_segment + "." + claims_segment + "." + b64url(signature)


class JWTAlgorithmConfusionTests(unittest.TestCase):
    def setUp(self):
        self.provider = Mock()
        self.provider.issuer = ISSUER
        self.provider.get_jwk = Mock(
            return_value={
                "kid": "test-key",
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "n": b64url(RSA_MODULUS),
                "e": b64url(RSA_EXPONENT),
            }
        )

        self.verifier = JWTVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            realm_namespace="test",
            jwks_provider=self.provider,
        )

    def assert_rejected_before_key_lookup(self, token):
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(token, now=NOW)

        self.provider.get_jwk.assert_not_called()

    def test_none_algorithm_with_empty_signature(self):
        # Rejected, but by parse_jwt's empty-segment rule rather than by the
        # algorithm guard. Kept because the outcome is what matters, and
        # documented because it is not evidence for the guard.
        token = make_token({"alg": "none", "kid": "test-key"}, signature=b"")
        self.assert_rejected_before_key_lookup(token)

    def test_none_algorithm_with_forged_signature(self):
        # This one IS evidence for the algorithm guard: parse_jwt accepts it.
        token = make_token({"alg": "none", "kid": "test-key"})
        self.assert_rejected_before_key_lookup(token)

    def test_hs256_using_rsa_public_modulus_as_hmac_key(self):
        token = make_token(
            {"alg": "HS256", "kid": "test-key"}, hmac_key=RSA_MODULUS
        )
        self.assert_rejected_before_key_lookup(token)

    def test_lowercase_rs256_is_rejected(self):
        token = make_token({"alg": "rs256", "kid": "test-key"})
        self.assert_rejected_before_key_lookup(token)

    def test_missing_alg_is_rejected(self):
        token = make_token({"kid": "test-key"})
        self.assert_rejected_before_key_lookup(token)

    def test_crit_header_is_rejected_before_key_lookup(self):
        token = make_token(
            {"alg": "RS256", "kid": "test-key", "crit": ["unsupported"]}
        )
        self.assert_rejected_before_key_lookup(token)

    def test_unexpected_typ_is_rejected_before_key_lookup(self):
        token = make_token({"alg": "RS256", "kid": "test-key", "typ": "JWE"})
        self.assert_rejected_before_key_lookup(token)


if __name__ == "__main__":
    unittest.main()
