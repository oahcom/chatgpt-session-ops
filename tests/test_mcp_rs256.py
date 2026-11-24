"""Offline tests for the actual RS256 signature-verification path.

These tests generate real RSA keys and exercise JWTVerifier.verify() end to
end. They deliberately do not mock verify_rs256_signature or the RSA public
key implementation.

Why this file exists: before it, deleting the entire public_key.verify(...)
call left the full suite green. Every algorithm-confusion case rejects before
the key is ever fetched, so nothing reached the one step that decides whether a
token is genuine.
"""

import base64
import builtins
import json
import unittest
from unittest.mock import patch

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError:  # pragma: no cover - dependency gate
    raise unittest.SkipTest(
        "RS256 tests require the optional cryptography package"
    )

from src.session_ops.mcp_auth import (
    AuthenticationError,
    JWTVerifier,
)

ISSUER = "https://issuer.example/realms/session-ops"
AUDIENCE = "session-ops"
REALM_NAMESPACE = "test"
KID = "generated-rsa-key"
NOW = 2_000_000_000


def b64url(value: bytes) -> str:
    """Encode bytes as unpadded Base64URL."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def make_jwk(public_key, *, kid=KID):
    """Export a real RSA public key as the minimal RS256 JWK."""
    numbers = public_key.public_numbers()
    modulus = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    exponent = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        "kid": kid,
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "n": b64url(modulus),
        "e": b64url(exponent),
    }


def encode_segment(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url(raw)


def make_signing_input():
    """Return a valid RS256 header/claims signing input."""
    header = {"alg": "RS256", "kid": KID, "typ": "JWT"}
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice",
        "tenant_id": "tenant1",
        "scope": "session_ops.work_item.read session_ops.session.read",
        "exp": NOW + 1000,
        "iat": NOW - 100,
    }
    return (encode_segment(header) + "." + encode_segment(claims)).encode("ascii")


def make_token(signing_input: bytes, signature: bytes) -> str:
    return signing_input.decode("ascii") + "." + b64url(signature)


def sign_rs256(private_key, signing_input: bytes) -> bytes:
    return private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())


class StaticJWKSProvider:
    """A deterministic provider returning one supplied JWK."""

    def __init__(self, jwk):
        self.issuer = ISSUER
        self.jwk = jwk
        self.requested_kids = []

    def get_jwk(self, kid):
        self.requested_kids.append(kid)
        if kid != self.jwk["kid"]:
            raise AuthenticationError("no matching RS256 signing key")
        return dict(self.jwk)


class RS256VerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the same keys across all cases. The small key exists only for
        # the explicit minimum-modulus-size rejection case.
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.other_private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.small_private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=1024
        )

        cls.good_jwk = make_jwk(cls.private_key.public_key())
        cls.other_jwk = make_jwk(cls.other_private_key.public_key())
        cls.small_jwk = make_jwk(cls.small_private_key.public_key())

        cls.signing_input = make_signing_input()
        cls.good_signature = sign_rs256(cls.private_key, cls.signing_input)
        cls.good_token = make_token(cls.signing_input, cls.good_signature)

    def make_verifier(self, jwk=None):
        provider = StaticJWKSProvider(self.good_jwk if jwk is None else jwk)
        verifier = JWTVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            realm_namespace=REALM_NAMESPACE,
            jwks_provider=provider,
        )
        return verifier, provider

    def test_valid_rs256_signature_returns_claim_mapped_auth_context(self):
        verifier, provider = self.make_verifier()

        auth = verifier.verify(self.good_token, now=NOW)

        self.assertEqual(auth.principal.principal_id, "test:alice")
        self.assertEqual(auth.principal.tenant_id, "tenant1")
        self.assertEqual(
            auth.scopes,
            frozenset(
                {
                    "session_ops.work_item.read",
                    "session_ops.session.read",
                }
            ),
        )
        self.assertEqual(auth.issuer, ISSUER)
        self.assertEqual(auth.subject, "alice")
        self.assertEqual(auth.realm_namespace, REALM_NAMESPACE)
        self.assertEqual(provider.requested_kids, [KID])

    def test_one_byte_signature_tampering_is_rejected(self):
        verifier, _ = self.make_verifier()

        tampered = bytearray(self.good_signature)
        tampered[0] ^= 0x01
        token = make_token(self.signing_input, bytes(tampered))

        with self.assertRaisesRegex(
            AuthenticationError, "signature verification failed"
        ):
            verifier.verify(token, now=NOW)

    def test_signature_from_another_rsa_key_is_rejected(self):
        verifier, _ = self.make_verifier()

        wrong_signature = sign_rs256(self.other_private_key, self.signing_input)
        token = make_token(self.signing_input, wrong_signature)

        with self.assertRaisesRegex(
            AuthenticationError, "signature verification failed"
        ):
            verifier.verify(token, now=NOW)

    def test_sha1_signature_is_not_accepted_as_rs256(self):
        verifier, _ = self.make_verifier()

        sha1_signature = self.private_key.sign(
            self.signing_input, padding.PKCS1v15(), hashes.SHA1()
        )
        token = make_token(self.signing_input, sha1_signature)

        with self.assertRaisesRegex(
            AuthenticationError, "signature verification failed"
        ):
            verifier.verify(token, now=NOW)

    def test_1024_bit_rsa_modulus_is_rejected(self):
        verifier, _ = self.make_verifier(self.small_jwk)

        with self.assertRaisesRegex(
            AuthenticationError, "RSA signing key is too small"
        ):
            verifier.verify(self.good_token, now=NOW)

    def test_even_rsa_public_exponent_is_rejected(self):
        jwk = dict(self.good_jwk)
        jwk["e"] = b64url((4).to_bytes(1, "big"))
        verifier, _ = self.make_verifier(jwk)

        with self.assertRaisesRegex(
            AuthenticationError, "RSA public exponent is invalid"
        ):
            verifier.verify(self.good_token, now=NOW)

    def test_rsa_public_exponent_one_is_rejected(self):
        jwk = dict(self.good_jwk)
        jwk["e"] = b64url((1).to_bytes(1, "big"))
        verifier, _ = self.make_verifier(jwk)

        with self.assertRaisesRegex(
            AuthenticationError, "RSA public exponent is invalid"
        ):
            verifier.verify(self.good_token, now=NOW)

    def test_missing_cryptography_is_classified_as_dependency_unavailable(self):
        verifier, _ = self.make_verifier()
        real_import = builtins.__import__

        def import_without_cryptography(name, *args, **kwargs):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ImportError("cryptography intentionally unavailable")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_cryptography):
            with self.assertRaises(AuthenticationError) as caught:
                verifier.verify(self.good_token, now=NOW)

        self.assertEqual(caught.exception.category, "dependency_unavailable")


if __name__ == "__main__":
    unittest.main()

# This module is skipped only when the optional cryptography dependency is
# absent; with cryptography installed, implementation failures must remain
# visible as test failures.
