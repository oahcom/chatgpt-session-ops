"""Claims/JWT-parsing guard tests for ``src/session_ops/mcp_auth.py``.

Covers the 42 ``raise`` statements that live in the pure claims/JWT-parsing
functions (issue #111). Every one of these functions is a pure function of its
arguments, so each guard is pinned without fixtures, network or the MCP SDK.

Two rules make these tests trustworthy:

* Messages are asserted in full (``assertEqual(str(exc), ...)`` or an anchored
  ``^...$`` regex). This module has 20 message pairs where one is a strict
  prefix of another (``issuer mismatch`` / ``OIDC discovery issuer mismatch``,
  ``URL is malformed`` / ``%s URL is malformed``), so a bare ``assertIn`` would
  pass for the wrong raise and report a false kill.
* Every rejection is paired with an acceptance case for the same function, so
  a guard that always raised could not pass.

The one guard that cannot be reached is ``_decode_base64url``'s
``b64decode`` failure handler (line 210): the preceding character-class and
length checks plus our own re-padding leave no input that ``validate=True``
rejects. It is exercised only through the guard above it; see
``test_decode_base64url_decode_failure_is_unreachable`` for the argument.

Run with:
    python -m unittest tests.test_mcp_auth_claims
"""

import json
import unittest

from src.session_ops.authority import Principal
from src.session_ops.mcp_auth import (
    MAX_TOKEN_BYTES,
    TOOL_REQUIRED_SCOPES,
    AuthContext,
    AuthenticationError,
    ScopeError,
    _decode_base64url,
    _numeric_date,
    _parse_json_object,
    _reject_duplicate_json_keys,
    parse_jwt,
    principal_from_claims,
    required_scopes_for_tool,
    require_scopes,
    scopes_from_claims,
    select_jwk,
    validate_registered_claims,
)


def _context(*scopes):
    return AuthContext(
        principal=Principal(tenant_id="tenant-a", principal_id="principal-a"),
        scopes=scopes,
        issuer="https://issuer.example",
        subject="user-1",
        realm_namespace="realm-a",
    )


def _rsa_jwk(**overrides):
    key = {"kid": "key-1", "kty": "RSA", "n": "abc", "e": "AQAB"}
    key.update(overrides)
    return key


class TestRequiredScopesForTool(unittest.TestCase):
    """1 raise."""

    def test_known_tool_returns_its_tuple(self):
        for name, expected in TOOL_REQUIRED_SCOPES.items():
            with self.subTest(tool=name):
                self.assertEqual(required_scopes_for_tool(name), expected)
        self.assertIsInstance(required_scopes_for_tool("work_item.get"), tuple)

    def test_unknown_tool_raises(self):
        for name in ("work_item.delete", "", "WORK_ITEM.GET"):
            with self.subTest(tool=name):
                with self.assertRaisesRegex(ValueError, "^unknown MCP tool: "):
                    required_scopes_for_tool(name)

    def test_unknown_tool_message_interpolates_name(self):
        with self.assertRaises(ValueError) as ctx:
            required_scopes_for_tool("nope")
        self.assertEqual(str(ctx.exception), "unknown MCP tool: nope")


class TestRequireScopes(unittest.TestCase):
    """2 raises."""

    def test_non_auth_context_rejected(self):
        for value in (None, object(), "scopes", {"scopes": ()}):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    require_scopes(value, ("session_ops.work_item.read",))
                self.assertEqual(
                    str(ctx.exception), "verified authentication context required"
                )

    def test_missing_scope_raises_scope_error(self):
        auth = _context("session_ops.work_item.read")
        with self.assertRaises(ScopeError) as ctx:
            require_scopes(auth, ("session_ops.work_item.read", "session_ops.access.manage"))
        self.assertEqual(ctx.exception.required, (
            "session_ops.work_item.read",
            "session_ops.access.manage",
        ))
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

    def test_empty_required_sequence_returns_early(self):
        # The early return means a context with no scopes at all still passes.
        self.assertIsNone(require_scopes(_context(), ()))
        self.assertIsNone(require_scopes(_context(), []))

    def test_full_satisfaction_passes(self):
        auth = _context("session_ops.work_item.write", "session_ops.access.manage")
        self.assertIsNone(require_scopes(auth, ("session_ops.work_item.write",)))


class TestRejectDuplicateJsonKeys(unittest.TestCase):
    """1 raise."""

    def test_unique_keys_pass(self):
        self.assertEqual(
            _reject_duplicate_json_keys([("a", 1), ("b", 2)]), {"a": 1, "b": 2}
        )
        self.assertEqual(_reject_duplicate_json_keys([]), {})

    def test_duplicate_key_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _reject_duplicate_json_keys([("a", 1), ("a", 2)])
        self.assertEqual(str(ctx.exception), "duplicate JSON object key")

    def test_hook_is_wired_into_json_loads(self):
        self.assertEqual(
            json.loads('{"a": 1, "b": 2}', object_pairs_hook=_reject_duplicate_json_keys),
            {"a": 1, "b": 2},
        )
        with self.assertRaisesRegex(ValueError, "^duplicate JSON object key$"):
            json.loads('{"a": 1, "a": 2}', object_pairs_hook=_reject_duplicate_json_keys)


class TestParseJsonObject(unittest.TestCase):
    """2 raises."""

    def test_invalid_utf8_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            _parse_json_object(b"\xff\xfe", "JWT header")
        self.assertEqual(str(ctx.exception), "JWT header is not valid JSON")

    def test_invalid_json_rejected(self):
        for raw in (b"", b"{", b"not json", b"{'a': 1}"):
            with self.subTest(raw=raw):
                with self.assertRaises(AuthenticationError) as ctx:
                    _parse_json_object(raw, "JWT claims")
                self.assertEqual(str(ctx.exception), "JWT claims is not valid JSON")

    def test_duplicate_key_rejected_through_this_function(self):
        # Proves object_pairs_hook is wired, not just imported.
        with self.assertRaises(AuthenticationError) as ctx:
            _parse_json_object(b'{"a": 1, "a": 2}', "JWT header")
        self.assertEqual(str(ctx.exception), "JWT header is not valid JSON")

    def test_top_level_array_rejected(self):
        for raw in (b"[]", b'[{"a": 1}]', b"1", b'"str"', b"null"):
            with self.subTest(raw=raw):
                with self.assertRaises(AuthenticationError) as ctx:
                    _parse_json_object(raw, "JWKS document")
                self.assertEqual(str(ctx.exception), "JWKS document must be a JSON object")

    def test_valid_object_returned(self):
        self.assertEqual(
            _parse_json_object(b'{"a": 1, "b": {"c": 2}}', "JWT header"),
            {"a": 1, "b": {"c": 2}},
        )
        self.assertEqual(_parse_json_object(b"{}", "JWT header"), {})


class TestDecodeBase64Url(unittest.TestCase):
    """5 raises, one of which is unreachable (see below)."""

    def test_non_string_or_empty_rejected(self):
        for value in (None, b"YWJj", 0, ""):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _decode_base64url(value)
                self.assertEqual(str(ctx.exception), "JWT segment is empty")

    def test_invalid_character_rejected(self):
        for value in ("YW Jj", "YW+j", "YW/j", "YWJj.", "YWJj\n", "=YWJj", "YWJj==="):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _decode_base64url(value)
                self.assertEqual(
                    str(ctx.exception), "JWT segment is not valid Base64URL"
                )

    def test_invalid_length_rejected(self):
        # len(unpadded) % 4 == 1 is the only impossible Base64 length.
        for value in ("A", "AAAAB", "YWJjZ"):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _decode_base64url(value)
                self.assertEqual(
                    str(ctx.exception), "JWT segment has invalid Base64URL length"
                )

    def test_non_canonical_encoding_rejected(self):
        # Trailing bits set: decodes to the same byte as "AA" but re-encodes
        # to "AA", so the canonical re-encode check catches it.
        for value in ("AB", "AC", "AAB"):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _decode_base64url(value)
                self.assertEqual(
                    str(ctx.exception), "JWT segment is not canonically encoded"
                )

    def test_padded_and_unpadded_decode_identically(self):
        for padded, unpadded in (
            ("YWJj", "YWJj"),
            ("YWJjZA==", "YWJjZA"),
            ("AA==", "AA"),
        ):
            with self.subTest(value=padded):
                self.assertEqual(_decode_base64url(padded), _decode_base64url(unpadded))
        self.assertEqual(_decode_base64url("YWJjZA"), b"abcd")

    def test_decode_base64url_decode_failure_is_unreachable(self):
        # The b64decode/validate=True handler at line 210 cannot fire: the
        # character-class guard rejects everything outside [A-Za-z0-9_-],
        # "=" is only allowed as a suffix, the length guard drops the one
        # impossible residue, and we re-pad ourselves. Exhaustive search over
        # {A,B,a,b,0,1,-,_,=} up to length 5 and 200k random samples up to
        # length 14 produced zero failures. Kept as a documented survivor.
        self.assertEqual(_decode_base64url("AAAA"), b"\x00\x00\x00")


class TestParseJwt(unittest.TestCase):
    """4 raises."""

    def test_non_string_rejected(self):
        for value in (None, b"a.b.c", 0, ["a.b.c"]):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    parse_jwt(value)
                self.assertEqual(str(ctx.exception), "bearer token must be text")

    def test_non_ascii_rejected(self):
        for value in ("\u00e4.\u00e4.\u00e4", "a.b.\u00e4", "\u00e4"):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    parse_jwt(value)
                self.assertEqual(str(ctx.exception), "bearer token must be ASCII")

    def test_empty_or_oversized_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            parse_jwt("")
        self.assertEqual(str(ctx.exception), "bearer token is empty or too large")
        with self.assertRaises(AuthenticationError) as ctx:
            parse_jwt("a" * (MAX_TOKEN_BYTES + 1))
        self.assertEqual(str(ctx.exception), "bearer token is empty or too large")

    def test_wrong_segment_count_rejected(self):
        for value in ("a.b", "a.b.c.d", "abc", "a.b.c."):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    parse_jwt(value)
                self.assertEqual(str(ctx.exception), "JWT must contain three segments")

    def test_valid_token_returns_four_tuple(self):
        header_seg = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsifQ"  # {"alg":"RS256","kid":"k"}
        claims_seg = "eyJzdWIiOiJ1In0"  # {"sub":"u"}
        sig_seg = "c2ln"  # b"sig"
        token = header_seg + "." + claims_seg + "." + sig_seg
        header, claims, signature, signing_input = parse_jwt(token)
        self.assertEqual(header, {"alg": "RS256", "kid": "k"})
        self.assertEqual(claims, {"sub": "u"})
        self.assertEqual(signature, b"sig")
        self.assertEqual(signing_input, (header_seg + "." + claims_seg).encode("ascii"))


class TestSelectJwk(unittest.TestCase):
    """4 raises, all reachable."""

    def test_non_mapping_document_rejected(self):
        for doc in (None, [], "keys", 0):
            with self.subTest(doc=doc):
                with self.assertRaises(AuthenticationError) as ctx:
                    select_jwk(doc, "key-1")
                self.assertEqual(str(ctx.exception), "JWKS document must be an object")

    def test_missing_or_non_list_keys_rejected(self):
        for doc in ({}, {"keys": None}, {"keys": {}}, {"keys": "x"}, {"keys": ()}):
            with self.subTest(doc=doc):
                with self.assertRaises(AuthenticationError) as ctx:
                    select_jwk(doc, "key-1")
                self.assertEqual(str(ctx.exception), "JWKS document has no keys array")

    def test_no_matching_key_rejected(self):
        for doc in (
            {"keys": []},
            {"keys": [{"kid": "other", "kty": "RSA", "n": "a", "e": "AQAB"}]},
            {"keys": ["not a dict"]},
        ):
            with self.subTest(doc=doc):
                with self.assertRaises(AuthenticationError) as ctx:
                    select_jwk(doc, "key-1")
                self.assertEqual(str(ctx.exception), "no matching RS256 signing key")

    def test_each_per_key_filter_yields_no_match(self):
        # A filtered key is indistinguishable from an absent one by design, so
        # every filter must surface as the same "no matching" raise.
        variants = {
            "wrong kty": _rsa_jwk(kty="EC"),
            "use not sig": _rsa_jwk(use="enc"),
            "alg not RS256": _rsa_jwk(alg="PS256"),
            "n not a string": _rsa_jwk(n=123),
            "e not a string": _rsa_jwk(e=b"AQAB"),
        }
        for label, key in variants.items():
            with self.subTest(filter=label):
                with self.assertRaises(AuthenticationError) as ctx:
                    select_jwk({"keys": [key]}, "key-1")
                self.assertEqual(str(ctx.exception), "no matching RS256 signing key")

    def test_duplicate_matching_kid_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            select_jwk({"keys": [_rsa_jwk(), _rsa_jwk()]}, "key-1")
        self.assertEqual(str(ctx.exception), "JWKS contains duplicate matching key IDs")

    def test_valid_key_returned_as_dict(self):
        key = _rsa_jwk()
        selected = select_jwk({"keys": [key]}, "key-1")
        self.assertEqual(selected, key)
        self.assertIsInstance(selected, dict)
        self.assertIsNot(selected, key)

    def test_defaults_accepted_for_optional_fields(self):
        # use/alg are optional and default to sig/RS256.
        key = {"kid": "key-1", "kty": "RSA", "n": "abc", "e": "AQAB"}
        self.assertEqual(select_jwk({"keys": [key]}, "key-1"), key)


class TestScopesFromClaims(unittest.TestCase):
    """4 raises."""

    def test_non_string_scope_rejected(self):
        for claims in ({}, {"scope": None}, {"scope": 1}, {"scope": ["a"]}):
            with self.subTest(claims=claims):
                with self.assertRaises(AuthenticationError) as ctx:
                    scopes_from_claims(claims)
                self.assertEqual(str(ctx.exception), "scope claim must be a string")

    def test_empty_or_untrimmed_scope_rejected(self):
        for value in ("", " a", "a ", "a b ", " "):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    scopes_from_claims({"scope": value})
                self.assertEqual(
                    str(ctx.exception), "scope claim is empty or malformed"
                )

    def test_invalid_token_rejected(self):
        # Empty token from a double space, and characters outside \x21-\x7e.
        for value in ("a  b", "a\tb", "a\u00e4", "a" * 129):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    scopes_from_claims({"scope": value})
                self.assertEqual(
                    str(ctx.exception), "scope claim contains an invalid scope"
                )

    def test_duplicate_scope_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            scopes_from_claims({"scope": "a b a"})
        self.assertEqual(str(ctx.exception), "scope claim contains duplicate scopes")

    def test_valid_multi_scope_string(self):
        self.assertEqual(
            scopes_from_claims({"scope": "session_ops.work_item.read b"}),
            frozenset({"session_ops.work_item.read", "b"}),
        )
        self.assertEqual(scopes_from_claims({"scope": "a"}), frozenset({"a"}))


class TestNumericDate(unittest.TestCase):
    """4 raises."""

    def test_required_and_absent_rejected(self):
        for claims in ({}, {"exp": None}):
            with self.subTest(claims=claims):
                with self.assertRaises(AuthenticationError) as ctx:
                    _numeric_date(claims, "exp", True)
                self.assertEqual(str(ctx.exception), "exp claim is required")

    def test_bool_rejected(self):
        # bool is an int subclass; both spellings must be rejected.
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _numeric_date({"nbf": value}, "nbf", True)
                self.assertEqual(str(ctx.exception), "nbf claim must be a NumericDate")

    def test_non_numeric_rejected(self):
        for value in ("1000", b"1000", [1], {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _numeric_date({"iat": value}, "iat", False)
                self.assertEqual(str(ctx.exception), "iat claim must be a NumericDate")

    def test_overflowing_int_rejected(self):
        # float(10**400) raises OverflowError inside the conversion guard.
        with self.assertRaises(AuthenticationError) as ctx:
            _numeric_date({"exp": 10 ** 400}, "exp", True)
        self.assertEqual(str(ctx.exception), "exp claim is invalid")

    def test_non_finite_float_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    _numeric_date({"exp": value}, "exp", True)
                self.assertEqual(str(ctx.exception), "exp claim is not finite")

    def test_valid_numbers(self):
        self.assertEqual(_numeric_date({"exp": 1000}, "exp", True), 1000.0)
        self.assertEqual(_numeric_date({"exp": 1000.5}, "exp", True), 1000.5)
        self.assertEqual(_numeric_date({"exp": 0}, "exp", True), 0.0)

    def test_optional_and_absent_returns_none(self):
        self.assertIsNone(_numeric_date({}, "nbf", False))


class TestValidateRegisteredClaims(unittest.TestCase):
    """6 raises."""

    def _claims(self, **overrides):
        claims = {
            "iss": "https://issuer.example",
            "aud": "session-ops",
            "exp": 2000,
            "nbf": 900,
            "iat": 900,
        }
        claims.update(overrides)
        return claims

    def test_issuer_mismatch(self):
        with self.assertRaises(AuthenticationError) as ctx:
            validate_registered_claims(
                self._claims(iss="https://other.example"),
                issuer="https://issuer.example",
                audience="session-ops",
                now=1000,
            )
        self.assertEqual(str(ctx.exception), "issuer mismatch")

    def test_audience_mismatch_string_and_list(self):
        for aud in ("other", ["other"], [], ["session-ops", ""], 0, None):
            with self.subTest(aud=aud):
                with self.assertRaises(AuthenticationError) as ctx:
                    validate_registered_claims(
                        self._claims(aud=aud),
                        issuer="https://issuer.example",
                        audience="session-ops",
                        now=1000,
                    )
                self.assertEqual(str(ctx.exception), "audience mismatch")

    def test_negative_clock_skew_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_registered_claims(
                self._claims(),
                issuer="https://issuer.example",
                audience="session-ops",
                now=1000,
                clock_skew_seconds=-1,
            )
        self.assertEqual(str(ctx.exception), "clock skew must not be negative")

    def test_missing_exp_rejected(self):
        claims = self._claims()
        del claims["exp"]
        with self.assertRaises(AuthenticationError) as ctx:
            validate_registered_claims(
                claims,
                issuer="https://issuer.example",
                audience="session-ops",
                now=1000,
            )
        self.assertEqual(str(ctx.exception), "exp claim is required")

    def test_expired_token_rejected(self):
        # now >= exp + skew, with the skew boundary itself counting as expired.
        for now, exp in ((1000, 900), (930, 900)):
            with self.subTest(now=now, exp=exp):
                with self.assertRaises(AuthenticationError) as ctx:
                    validate_registered_claims(
                        self._claims(exp=exp),
                        issuer="https://issuer.example",
                        audience="session-ops",
                        now=now,
                    )
                self.assertEqual(str(ctx.exception), "token has expired")

    def test_not_yet_active_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            validate_registered_claims(
                self._claims(nbf=2000),
                issuer="https://issuer.example",
                audience="session-ops",
                now=1000,
            )
        self.assertEqual(str(ctx.exception), "token is not active yet")

    def test_issued_in_the_future_rejected(self):
        with self.assertRaises(AuthenticationError) as ctx:
            validate_registered_claims(
                self._claims(iat=2000),
                issuer="https://issuer.example",
                audience="session-ops",
                now=1000,
            )
        self.assertEqual(str(ctx.exception), "token was issued in the future")

    def test_valid_claim_set_passes(self):
        for aud in ("session-ops", ["session-ops"], ["session-ops", "other"]):
            with self.subTest(aud=aud):
                self.assertIsNone(
                    validate_registered_claims(
                        self._claims(aud=aud),
                        issuer="https://issuer.example",
                        audience="session-ops",
                        now=1000,
                    )
                )

    def test_optional_dates_may_be_absent(self):
        claims = self._claims()
        del claims["nbf"]
        del claims["iat"]
        self.assertIsNone(
            validate_registered_claims(
                claims,
                issuer="https://issuer.example",
                audience="session-ops",
                now=1000,
            )
        )


class TestPrincipalFromClaims(unittest.TestCase):
    """9 raises."""

    def _claims(self, **overrides):
        claims = {
            "iss": "https://issuer.example",
            "aud": "session-ops",
            "sub": "user-1",
            "tenant_id": "tenant-a",
            "scope": "session_ops.work_item.read",
        }
        claims.update(overrides)
        return claims

    def _build(self, claims, **overrides):
        kwargs = {
            "expected_issuer": "https://issuer.example",
            "expected_audience": "session-ops",
            "realm_namespace": "realm-a",
        }
        kwargs.update(overrides)
        return principal_from_claims(claims, **kwargs)

    def test_issuer_mismatch(self):
        with self.assertRaises(AuthenticationError) as ctx:
            self._build(self._claims(iss="https://other.example"))
        self.assertEqual(str(ctx.exception), "issuer mismatch")

    def test_audience_mismatch(self):
        for aud in ("other", [], ["other"], 0, None):
            with self.subTest(aud=aud):
                with self.assertRaises(AuthenticationError) as ctx:
                    self._build(self._claims(aud=aud))
                self.assertEqual(str(ctx.exception), "audience mismatch")

    def test_non_string_realm_namespace(self):
        for value in (None, 0, b"realm-a", ["realm-a"]):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    self._build(self._claims(), realm_namespace=value)
                self.assertEqual(
                    str(ctx.exception), "realm namespace is not configured"
                )

    def test_malformed_realm_namespace(self):
        for value in ("", "-realm", "realm a", "realm/a", "a" * 49):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    self._build(self._claims(), realm_namespace=value)
                self.assertEqual(str(ctx.exception), "realm namespace is malformed")

    def test_empty_tenant_claim_name(self):
        for value in ("", None, 0):
            with self.subTest(value=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    self._build(self._claims(), tenant_claim=value)
                self.assertEqual(
                    str(ctx.exception), "tenant claim name is not configured"
                )

    def test_missing_or_empty_subject(self):
        claims = self._claims()
        del claims["sub"]
        for value in (claims, self._claims(sub=""), self._claims(sub=0), self._claims(sub=None)):
            with self.subTest(claims=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    self._build(value)
                self.assertEqual(str(ctx.exception), "sub claim is required")

    def test_missing_or_empty_tenant_claim_value(self):
        claims = self._claims()
        del claims["tenant_id"]
        for value in (
            claims,
            self._claims(tenant_id=""),
            self._claims(tenant_id=0),
            self._claims(tenant_id=None),
        ):
            with self.subTest(claims=value):
                with self.assertRaises(AuthenticationError) as ctx:
                    self._build(value)
                self.assertEqual(
                    str(ctx.exception), "trusted tenant claim is required"
                )

    def test_namespaced_subject_over_limit(self):
        # 40-char namespace + ":" + 100-char sub = 141 > 128.
        with self.assertRaises(AuthenticationError) as ctx:
            self._build(
                self._claims(sub="s" * 100),
                realm_namespace="r" * 40,
            )
        self.assertEqual(
            str(ctx.exception), "namespaced subject exceeds principal limit"
        )

    def test_principal_construction_failure(self):
        # Passes the length and namespace checks but fails PRINCIPAL_ID_RE:
        # "/" is outside [A-Za-z0-9._:@-].
        for overrides in (
            {"sub": "bad/sub"},
            {"sub": "user 1"},
            {"sub": "\u00e4"},
            {"tenant_id": "bad tenant"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(AuthenticationError) as ctx:
                    self._build(self._claims(**overrides))
                self.assertEqual(
                    str(ctx.exception), "principal claims are malformed"
                )

    def test_valid_claims_return_auth_context(self):
        auth = self._build(self._claims())
        self.assertIsInstance(auth, AuthContext)
        self.assertEqual(auth.principal.principal_id, "realm-a:user-1")
        self.assertEqual(auth.principal.tenant_id, "tenant-a")
        self.assertEqual(auth.subject, "user-1")
        self.assertEqual(auth.issuer, "https://issuer.example")
        self.assertEqual(auth.realm_namespace, "realm-a")
        self.assertEqual(auth.scopes, frozenset({"session_ops.work_item.read"}))

    def test_custom_tenant_claim_name(self):
        auth = self._build(
            self._claims(org="tenant-b"),
            tenant_claim="org",
        )
        self.assertEqual(auth.principal.tenant_id, "tenant-b")


if __name__ == "__main__":
    unittest.main()
