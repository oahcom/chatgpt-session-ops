"""Offline guards for the crypto / JWK / verifier raises in mcp_auth.

Scope (issue #111): every ``raise`` in ``get_jwk``,
``_rsa_public_key_from_jwk``, ``verify_rs256_signature``, ``JWTVerifier.__init__``
and ``JWTVerifier.verify``. The three other groups (pure claims helpers, the
network fetchers, the URL validators) are covered elsewhere; this file touches
none of them.

Two rules drive the shape of every test here:

* Assert the *full* message. ``'JWT kid header is required'``,
  ``'JWT signature verification failed'`` and
  ``'RS256 verification requires the cryptography package'`` each appear at two
  sites, so a substring assertion can pass for the wrong raise and turn a
  survivor into a false KILL.
* Where two sites share a message, assert the code path as well -- which class
  or function produced it, or what side effect it left behind -- so the two
  sites stay distinguishable.

No network: the JWKS provider is always given an injected ``fetch_json`` and a
preset ``_jwks_uri``, and ``_validate_https_url`` only parses (it never
resolves), so nothing leaves the process.
"""

import base64
import builtins
import json
import time
import unittest
from unittest.mock import Mock, patch

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.session_ops import mcp_auth
from src.session_ops.mcp_auth import (
    AuthenticationError,
    AuthConfigurationError,
    JWKSFetchError,
    JWKSProvider,
    JWTVerifier,
    _rsa_public_key_from_jwk,
    verify_rs256_signature,
)

ISSUER = "https://issuer.example/realms/session-ops"
OTHER_ISSUER = "https://other.example/realms/session-ops"
AUDIENCE = "session-ops"
REALM_NAMESPACE = "test"
KID = "crypto-guard-key"
NOW = 2_000_000_000


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def encode_segment(value) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url(raw)


def make_jwk(public_key, *, kid=KID) -> dict:
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


def claims_dict(**overrides) -> dict:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice",
        "tenant_id": "tenant1",
        "scope": "session_ops.work_item.read",
        "exp": NOW + 1000,
        "iat": NOW - 100,
    }
    claims.update(overrides)
    return claims


def make_token(header: dict, claims=None, signature: bytes = b"forged") -> str:
    if claims is None:
        claims = claims_dict()
    signing_input = encode_segment(header) + "." + encode_segment(claims)
    return signing_input + "." + b64url(signature)


def provider_with_jwk(jwk, *, issuer=ISSUER) -> JWKSProvider:
    """A provider that can serve exactly one JWK without any network I/O."""
    provider = JWKSProvider(issuer, fetch_json=lambda url, timeout, limit: {})
    provider._jwks_uri = issuer + "/protocol/openid-connect/certs"
    provider._jwks = {"keys": [dict(jwk)]}
    provider._expires_at = 1e18
    return provider


class GetJwkTests(unittest.TestCase):
    """``JWKSProvider.get_jwk`` -- the four raises at 884/896/899/912."""

    def make_provider(self, fetch_json):
        provider = JWKSProvider(ISSUER, fetch_json=fetch_json)
        # Preset the URI so _download_jwks never runs discovery.
        provider._jwks_uri = ISSUER + "/protocol/openid-connect/certs"
        return provider

    def test_non_string_kid_is_rejected(self):
        provider = self.make_provider(lambda *a: {})
        with self.assertRaises(AuthenticationError) as caught:
            provider.get_jwk(123)
        self.assertEqual(str(caught.exception), "JWT kid header is required")

    def test_empty_kid_is_rejected(self):
        provider = self.make_provider(lambda *a: {})
        with self.assertRaises(AuthenticationError) as caught:
            provider.get_jwk("")
        self.assertEqual(str(caught.exception), "JWT kid header is required")

    def test_fresh_cache_miss_does_not_refresh(self):
        fetched = []
        provider = self.make_provider(lambda *a: fetched.append(a) or {})
        provider._jwks = {"keys": []}
        provider._expires_at = 1e18

        with self.assertRaises(AuthenticationError) as caught:
            provider.get_jwk("absent")

        self.assertEqual(
            str(caught.exception), "no matching RS256 signing key"
        )
        self.assertEqual(fetched, [])

    def test_fresh_cache_rejects_non_rsa_key(self):
        # kty is enforced by select_jwk, not by _rsa_public_key_from_jwk; the
        # fresh-cache path is where that shows up for get_jwk callers.
        provider = self.make_provider(lambda *a: {})
        provider._jwks = {
            "keys": [{"kid": KID, "kty": "EC", "n": "abc", "e": "AQAB"}]
        }
        provider._expires_at = 1e18

        with self.assertRaises(AuthenticationError) as caught:
            provider.get_jwk(KID)

        self.assertEqual(
            str(caught.exception), "no matching RS256 signing key"
        )

    def test_refresh_in_progress_is_rejected(self):
        provider = self.make_provider(lambda *a: {})
        provider._jwks = None
        provider._refresh_in_progress = True

        with self.assertRaises(JWKSFetchError) as caught:
            provider.get_jwk(KID)

        self.assertEqual(
            str(caught.exception), "JWKS refresh is already in progress"
        )
        self.assertEqual(caught.exception.category, "dependency_unavailable")

    def test_refresh_backoff_is_rejected(self):
        provider = self.make_provider(lambda *a: {})
        provider._jwks = None
        provider._refresh_retry_at = time.monotonic() + 100.0

        with self.assertRaises(JWKSFetchError) as caught:
            provider.get_jwk(KID)

        self.assertEqual(str(caught.exception), "JWKS refresh is in backoff")

    def test_stale_cache_refreshes_and_returns_key(self):
        key = {"kid": KID, "kty": "RSA", "n": "AQAB", "e": "AQAB"}
        provider = self.make_provider(lambda *a: {"keys": [key]})
        provider._jwks = None

        self.assertEqual(provider.get_jwk(KID), key)
        self.assertFalse(provider._refresh_in_progress)
        self.assertGreater(provider._expires_at, time.monotonic())

    def test_failed_refresh_propagates_and_records_backoff(self):
        # Line 912 is a bare ``raise`` inside ``except Exception``. If it were
        # deleted, the failure would be swallowed and the method would fall
        # through to the cache-update block with no ``refreshed`` value.
        def explode(url, timeout, limit):
            raise RuntimeError("transport exploded")

        provider = self.make_provider(explode)
        provider._jwks = None

        with self.assertRaises(JWKSFetchError) as caught:
            provider.get_jwk(KID)

        self.assertEqual(
            str(caught.exception), "identity endpoint request failed"
        )
        self.assertFalse(provider._refresh_in_progress)
        self.assertGreater(provider._refresh_retry_at, 0.0)


class RsaPublicKeyFromJwkTests(unittest.TestCase):
    """``_rsa_public_key_from_jwk`` -- the five raises at 929/941/943/947/949."""

    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.good_jwk = make_jwk(cls.private_key.public_key())
        cls.small_jwk = make_jwk(
            rsa.generate_private_key(
                public_exponent=65537, key_size=1024
            ).public_key()
        )

    def test_accept_path_returns_real_public_key(self):
        key = _rsa_public_key_from_jwk(self.good_jwk)
        self.assertEqual(
            key.public_numbers().n, self.private_key.public_key().public_numbers().n
        )

    def test_missing_cryptography_is_dependency_unavailable(self):
        real_import = builtins.__import__

        def block_rsa_module(name, *args, **kwargs):
            if name == "cryptography.hazmat.primitives.asymmetric.rsa":
                raise ImportError("rsa module intentionally unavailable")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=block_rsa_module):
            with self.assertRaises(AuthenticationError) as caught:
                _rsa_public_key_from_jwk(self.good_jwk)

        self.assertEqual(
            str(caught.exception),
            "RS256 verification requires the cryptography package",
        )
        self.assertEqual(caught.exception.category, "dependency_unavailable")

    def test_1024_bit_modulus_is_rejected(self):
        with self.assertRaises(AuthenticationError) as caught:
            _rsa_public_key_from_jwk(self.small_jwk)

        self.assertEqual(str(caught.exception), "RSA signing key is too small")

    def test_even_exponent_is_rejected(self):
        jwk = dict(self.good_jwk)
        jwk["e"] = b64url((4).to_bytes(1, "big"))
        with self.assertRaises(AuthenticationError) as caught:
            _rsa_public_key_from_jwk(jwk)
        self.assertEqual(
            str(caught.exception), "RSA public exponent is invalid"
        )

    def test_exponent_one_is_rejected(self):
        jwk = dict(self.good_jwk)
        jwk["e"] = b64url((1).to_bytes(1, "big"))
        with self.assertRaises(AuthenticationError) as caught:
            _rsa_public_key_from_jwk(jwk)
        self.assertEqual(
            str(caught.exception), "RSA public exponent is invalid"
        )

    def test_missing_modulus_is_malformed(self):
        jwk = dict(self.good_jwk)
        del jwk["n"]
        with self.assertRaises(AuthenticationError) as caught:
            _rsa_public_key_from_jwk(jwk)
        self.assertEqual(str(caught.exception), "RSA JWK is malformed")

    def test_missing_exponent_is_malformed(self):
        jwk = dict(self.good_jwk)
        del jwk["e"]
        with self.assertRaises(AuthenticationError) as caught:
            _rsa_public_key_from_jwk(jwk)
        self.assertEqual(str(caught.exception), "RSA JWK is malformed")

    def test_undecodable_base64url_is_re_raised_unwrapped(self):
        # _decode_base64url raises AuthenticationError; line 947 re-raises it
        # untouched. Without 947 it would be swallowed by the generic handler
        # and reported as "RSA JWK is malformed" instead.
        jwk = dict(self.good_jwk)
        jwk["n"] = "not*base64*"
        with self.assertRaises(AuthenticationError) as caught:
            _rsa_public_key_from_jwk(jwk)

        self.assertEqual(
            str(caught.exception), "JWT segment is not valid Base64URL"
        )
        self.assertNotEqual(str(caught.exception), "RSA JWK is malformed")

    def test_key_cryptography_itself_rejects_is_malformed(self):
        # exponent >= modulus: passes the local exponent check, then
        # RSAPublicNumbers.public_key() raises ValueError.
        jwk = dict(self.good_jwk)
        jwk["e"] = b64url((1 << 2100 | 1).to_bytes(263, "big"))
        with self.assertRaises(AuthenticationError) as caught:
            _rsa_public_key_from_jwk(jwk)

        self.assertEqual(str(caught.exception), "RSA JWK is malformed")


class VerifyRs256SignatureTests(unittest.TestCase):
    """``verify_rs256_signature`` -- the three raises at 963/977/979."""

    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.other_private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.jwk = make_jwk(cls.private_key.public_key())
        cls.signing_input = b"header.claims"
        cls.signature = cls.private_key.sign(
            cls.signing_input, padding.PKCS1v15(), hashes.SHA256()
        )

    def test_real_signature_passes(self):
        verify_rs256_signature(self.signing_input, self.signature, self.jwk)

    def test_tampered_signature_is_rejected(self):
        tampered = bytearray(self.signature)
        tampered[0] ^= 0x01
        with self.assertRaises(AuthenticationError) as caught:
            verify_rs256_signature(
                self.signing_input, bytes(tampered), self.jwk
            )
        self.assertEqual(
            str(caught.exception), "JWT signature verification failed"
        )

    def test_tampered_signing_input_is_rejected(self):
        with self.assertRaises(AuthenticationError) as caught:
            verify_rs256_signature(b"header.other", self.signature, self.jwk)
        self.assertEqual(
            str(caught.exception), "JWT signature verification failed"
        )

    def test_signature_from_another_key_is_rejected(self):
        wrong = self.other_private_key.sign(
            self.signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        with self.assertRaises(AuthenticationError) as caught:
            verify_rs256_signature(self.signing_input, wrong, self.jwk)
        self.assertEqual(
            str(caught.exception), "JWT signature verification failed"
        )

    def test_non_bytes_signature_is_rejected(self):
        # A str signature makes cryptography raise TypeError, which is the
        # generic branch at 979 rather than InvalidSignature at 977.
        with self.assertRaises(AuthenticationError) as caught:
            verify_rs256_signature(self.signing_input, "not-bytes", self.jwk)
        self.assertEqual(
            str(caught.exception), "JWT signature verification failed"
        )

    def test_invalid_signature_branch_is_used(self):
        fake_key = Mock()
        fake_key.verify.side_effect = InvalidSignature()
        with patch.object(
            mcp_auth, "_rsa_public_key_from_jwk", return_value=fake_key
        ):
            with self.assertRaises(AuthenticationError) as caught:
                verify_rs256_signature(self.signing_input, b"sig", self.jwk)
        self.assertEqual(
            str(caught.exception), "JWT signature verification failed"
        )

    def test_generic_exception_branch_is_used(self):
        fake_key = Mock()
        fake_key.verify.side_effect = ValueError("bad padding")
        with patch.object(
            mcp_auth, "_rsa_public_key_from_jwk", return_value=fake_key
        ):
            with self.assertRaises(AuthenticationError) as caught:
                verify_rs256_signature(self.signing_input, b"sig", self.jwk)
        self.assertEqual(
            str(caught.exception), "JWT signature verification failed"
        )

    def test_missing_cryptography_is_dependency_unavailable(self):
        # Same message as the raise in _rsa_public_key_from_jwk. Blocking only
        # ``cryptography.exceptions`` proves this one is the verifier's.
        real_import = builtins.__import__

        def block_exceptions(name, *args, **kwargs):
            if name == "cryptography.exceptions":
                raise ImportError("exceptions module intentionally unavailable")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=block_exceptions):
            with self.assertRaises(AuthenticationError) as caught:
                verify_rs256_signature(
                    self.signing_input, self.signature, self.jwk
                )

        self.assertEqual(
            str(caught.exception),
            "RS256 verification requires the cryptography package",
        )
        self.assertEqual(caught.exception.category, "dependency_unavailable")


class JWTVerifierInitTests(unittest.TestCase):
    """``JWTVerifier.__init__`` -- the seven raises at 1002/1004/1008/1010/1012/1024/1035."""

    def make_provider(self, issuer=ISSUER):
        provider = JWKSProvider(issuer, fetch_json=lambda *a: {})
        return provider

    def build(self, **overrides):
        kwargs = dict(
            issuer=ISSUER,
            audience=AUDIENCE,
            realm_namespace=REALM_NAMESPACE,
            jwks_provider=self.make_provider(),
        )
        kwargs.update(overrides)
        return JWTVerifier(**kwargs)

    def assert_config_error(self, message, **overrides):
        with self.assertRaises(AuthConfigurationError) as caught:
            self.build(**overrides)
        self.assertEqual(str(caught.exception), message)

    def test_accept_path(self):
        verifier = self.build()
        self.assertEqual(verifier.issuer, ISSUER)
        self.assertEqual(verifier.audience, AUDIENCE)
        self.assertEqual(verifier.realm_namespace, REALM_NAMESPACE)
        self.assertEqual(verifier.tenant_claim, "tenant_id")

    def test_non_string_issuer_is_rejected(self):
        self.assert_config_error("issuer is required", issuer=None)

    def test_empty_issuer_is_rejected(self):
        self.assert_config_error("issuer is required", issuer="")

    def test_non_string_audience_is_rejected(self):
        self.assert_config_error("audience is required", audience=123)

    def test_empty_audience_is_rejected(self):
        self.assert_config_error("audience is required", audience="")

    def test_non_string_realm_namespace_is_rejected(self):
        self.assert_config_error(
            "valid realm_namespace is required", realm_namespace=None
        )

    def test_malformed_realm_namespace_is_rejected(self):
        self.assert_config_error(
            "valid realm_namespace is required", realm_namespace="-bad"
        )

    def test_non_string_tenant_claim_is_rejected(self):
        self.assert_config_error("tenant_claim is required", tenant_claim=5)

    def test_empty_tenant_claim_is_rejected(self):
        self.assert_config_error("tenant_claim is required", tenant_claim="")

    def test_provider_and_discovery_url_together_is_rejected(self):
        self.assert_config_error(
            "provide jwks_provider or discovery_url, not both",
            discovery_url=ISSUER + "/.well-known/openid-configuration",
        )

    def test_negative_clock_skew_is_rejected(self):
        self.assert_config_error(
            "clock skew must not be negative", clock_skew_seconds=-1.0
        )

    def test_provider_issuer_mismatch_is_rejected(self):
        self.assert_config_error(
            "JWKS provider issuer does not match verifier issuer",
            jwks_provider=self.make_provider(OTHER_ISSUER),
        )

    def test_non_https_issuer_is_rejected(self):
        # The raise lives in _validate_https_url (owned by another group); this
        # case only pins that __init__ reaches it before building a provider.
        with self.assertRaises(AuthenticationError) as caught:
            self.build(issuer="http://issuer.example/realms/session-ops")
        self.assertEqual(str(caught.exception), "issuer URL must use HTTPS")


class JWTVerifierVerifyHeaderTests(unittest.TestCase):
    """``JWTVerifier.verify`` -- the four header guards at 1045/1047/1049/1053.

    Each case proves ordering, not just rejection: the provider's get_jwk is a
    Mock that raises AssertionError if touched, so a guard that ran after the
    key lookup would surface as an AssertionError instead of AuthenticationError.
    """

    def setUp(self):
        self.provider = Mock()
        self.provider.issuer = ISSUER
        self.provider.get_jwk = Mock(
            side_effect=AssertionError("key lookup must not run for this header")
        )
        self.verifier = JWTVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            realm_namespace=REALM_NAMESPACE,
            jwks_provider=self.provider,
        )

    def assert_rejected_before_key_lookup(self, token, message):
        with self.assertRaises(AuthenticationError) as caught:
            self.verifier.verify(token, now=NOW)
        self.assertEqual(str(caught.exception), message)
        self.provider.get_jwk.assert_not_called()

    def test_alg_none_is_rejected(self):
        self.assert_rejected_before_key_lookup(
            make_token({"alg": "none", "kid": KID}),
            "only RS256 JWTs are accepted",
        )

    def test_alg_hs256_confusion_is_rejected(self):
        self.assert_rejected_before_key_lookup(
            make_token({"alg": "HS256", "kid": KID}),
            "only RS256 JWTs are accepted",
        )

    def test_unexpected_typ_is_rejected(self):
        self.assert_rejected_before_key_lookup(
            make_token({"alg": "RS256", "kid": KID, "typ": "at+jwt"}),
            "unexpected JWT typ header",
        )

    def test_crit_header_is_rejected(self):
        self.assert_rejected_before_key_lookup(
            make_token({"alg": "RS256", "kid": KID, "crit": ["exp"]}),
            "critical JWT headers are unsupported",
        )

    def test_missing_kid_is_rejected(self):
        self.assert_rejected_before_key_lookup(
            make_token({"alg": "RS256"}),
            "JWT kid header is required",
        )

    def test_non_string_kid_is_rejected(self):
        self.assert_rejected_before_key_lookup(
            make_token({"alg": "RS256", "kid": 7}),
            "JWT kid header is required",
        )

    def test_empty_kid_is_rejected(self):
        self.assert_rejected_before_key_lookup(
            make_token({"alg": "RS256", "kid": ""}),
            "JWT kid header is required",
        )

    def test_valid_rs256_token_returns_auth_context(self):
        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        jwk = make_jwk(private_key.public_key())
        provider = provider_with_jwk(jwk)
        verifier = JWTVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            realm_namespace=REALM_NAMESPACE,
            jwks_provider=provider,
        )

        header = {"alg": "RS256", "kid": KID, "typ": "JWT"}
        signing_input = (
            encode_segment(header)
            + "."
            + encode_segment(
                claims_dict(scope="session_ops.work_item.read session_ops.session.read")
            )
        )
        signature = private_key.sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        token = signing_input + "." + b64url(signature)

        auth = verifier.verify(token, now=NOW)

        self.assertEqual(auth.principal.principal_id, "test:alice")
        self.assertEqual(auth.principal.tenant_id, "tenant1")
        self.assertEqual(
            auth.scopes,
            frozenset(
                {"session_ops.work_item.read", "session_ops.session.read"}
            ),
        )
        self.assertEqual(auth.issuer, ISSUER)
        self.assertEqual(auth.subject, "alice")
        self.assertEqual(auth.realm_namespace, REALM_NAMESPACE)


if __name__ == "__main__":
    unittest.main()
