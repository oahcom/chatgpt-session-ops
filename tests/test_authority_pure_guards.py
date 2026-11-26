"""Guards in src/session_ops/authority.py that are pure functions of their args.

Each test pins the exception TYPE and the FULL message, and pairs every
rejection with an acceptance case so a guard that always raised would fail.
"""

from __future__ import annotations

import base64
import json
import unittest

from src.session_ops.authority import (
    MAX_CURSOR_LENGTH,
    MAX_PAGE_SIZE,
    AuthorityError,
    InvalidIdentifier,
    InvalidPrincipal,
    Principal,
    UnconfirmedSession,
    _decode_cursor,
    _encode_cursor,
    _validate_page_size,
    _validate_principal_id,
    _validate_scope,
    validate_conversation_id,
    validate_link_id,
    validate_work_item_id,
)

VALID_SESSION = "12345678-1234-4234-8234-123456789abc"
VALID_WORK_ITEM = "wi_12345678-1234-4234-8234-123456789abc"
VALID_LINK = "sl_12345678-1234-4234-8234-123456789abc"


class PrincipalPostInitTests(unittest.TestCase):
    """authority.py:227 (tenant_id) and :231 (principal_id)."""

    def test_accepts_valid_principal(self) -> None:
        principal = Principal(tenant_id="tenant-a", principal_id="owner-a")
        self.assertEqual(principal.tenant_id, "tenant-a")
        self.assertEqual(principal.principal_id, "owner-a")

    def test_accepts_regex_edge_characters(self) -> None:
        principal = Principal(tenant_id="A0._:@-", principal_id="a" * 128)
        self.assertEqual(principal.principal_id, "a" * 128)

    def test_rejects_malformed_tenant_id(self) -> None:
        for bad in ("", "-leading-dash", "a" * 129, "has space", None, 123):
            with self.subTest(tenant_id=bad):
                with self.assertRaises(InvalidPrincipal) as ctx:
                    Principal(tenant_id=bad, principal_id="owner-a")
                self.assertEqual(str(ctx.exception), "tenant_id is malformed")

    def test_rejects_malformed_principal_id(self) -> None:
        for bad in ("", "-leading-dash", "a" * 129, "has space", None, 123):
            with self.subTest(principal_id=bad):
                with self.assertRaises(InvalidPrincipal) as ctx:
                    Principal(tenant_id="tenant-a", principal_id=bad)
                self.assertEqual(str(ctx.exception), "principal_id is malformed")


class ValidatePrincipalIdTests(unittest.TestCase):
    """authority.py:294."""

    def test_accepts_valid_and_returns_it(self) -> None:
        self.assertEqual(_validate_principal_id("owner-a"), "owner-a")

    def test_rejects_with_default_field_name(self) -> None:
        for bad in ("", "-nope", "a" * 129, None, 7):
            with self.subTest(value=bad):
                with self.assertRaises(InvalidPrincipal) as ctx:
                    _validate_principal_id(bad)
                self.assertEqual(str(ctx.exception), "principal_id is malformed")

    def test_rejects_with_custom_field_name(self) -> None:
        with self.assertRaises(InvalidPrincipal) as ctx:
            _validate_principal_id("", field="grantee")
        self.assertEqual(str(ctx.exception), "grantee is malformed")


class ValidateConversationIdTests(unittest.TestCase):
    """authority.py:306 — same CONVERSATION_ID_RE as work_items (issue #109)."""

    def test_accepts_canonical_lowercase_uuid(self) -> None:
        self.assertEqual(validate_conversation_id(VALID_SESSION), VALID_SESSION)

    def test_rejects_non_canonical_forms(self) -> None:
        bad_values = {
            "plain name": "session-a",
            "uppercase": VALID_SESSION.upper(),
            "dash stripped": VALID_SESSION.replace("-", ""),
            "empty": "",
            "None": None,
            "not a uuid": "12345678-1234-4234-8234-123456789ab",
        }
        for label, bad in bad_values.items():
            with self.subTest(case=label):
                with self.assertRaises(UnconfirmedSession) as ctx:
                    validate_conversation_id(bad)
                self.assertEqual(
                    str(ctx.exception),
                    "session_id must be a canonical lowercase UUID",
                )


class ValidateWorkItemIdTests(unittest.TestCase):
    """authority.py:312."""

    def test_accepts_valid_and_returns_it(self) -> None:
        self.assertEqual(validate_work_item_id(VALID_WORK_ITEM), VALID_WORK_ITEM)

    def test_rejects_malformed(self) -> None:
        bad_values = {
            "empty": "",
            "session id": VALID_SESSION,
            "wrong prefix": "xx_" + VALID_WORK_ITEM[3:],
            "not v4": "wi_12345678-1234-1234-8234-123456789abc",
            "uppercase hex": VALID_WORK_ITEM.upper(),
            "None": None,
            "int": 3,
        }
        for label, bad in bad_values.items():
            with self.subTest(case=label):
                with self.assertRaises(InvalidIdentifier) as ctx:
                    validate_work_item_id(bad)
                self.assertEqual(str(ctx.exception), "work_item_id is malformed")


class ValidateLinkIdTests(unittest.TestCase):
    """authority.py:318."""

    def test_accepts_valid_and_returns_it(self) -> None:
        self.assertEqual(validate_link_id(VALID_LINK), VALID_LINK)

    def test_rejects_malformed(self) -> None:
        bad_values = {
            "empty": "",
            "work item id": VALID_WORK_ITEM,
            "wrong prefix": "xx_" + VALID_LINK[3:],
            "not v4": "sl_12345678-1234-1234-8234-123456789abc",
            "uppercase hex": VALID_LINK.upper(),
            "None": None,
            "int": 3,
        }
        for label, bad in bad_values.items():
            with self.subTest(case=label):
                with self.assertRaises(InvalidIdentifier) as ctx:
                    validate_link_id(bad)
                self.assertEqual(str(ctx.exception), "link_id is malformed")


class ValidateScopeTests(unittest.TestCase):
    """authority.py:324."""

    def test_accepts_both_scopes(self) -> None:
        for scope in ("read", "write"):
            with self.subTest(scope=scope):
                self.assertEqual(_validate_scope(scope), scope)

    def test_rejects_anything_else(self) -> None:
        for bad in ("admin", "READ", "", None, "read "):
            with self.subTest(scope=bad):
                with self.assertRaises(AuthorityError) as ctx:
                    _validate_scope(bad)
                self.assertEqual(str(ctx.exception), "scope must be 'read' or 'write'")


class ValidatePageSizeTests(unittest.TestCase):
    """authority.py:330 (bool/non-int) and :332 (range)."""

    def test_accepts_in_range_integers(self) -> None:
        for value in (1, 25, MAX_PAGE_SIZE):
            with self.subTest(limit=value):
                self.assertEqual(_validate_page_size(value), value)

    def test_rejects_bool_before_range(self) -> None:
        # bool is an int subclass: True would paginate by 1, False by 0.
        for value in (True, False):
            with self.subTest(limit=value):
                with self.assertRaises(AuthorityError) as ctx:
                    _validate_page_size(value)
                self.assertEqual(str(ctx.exception), "page size must be an integer")

    def test_rejects_non_integer_types(self) -> None:
        for value in (1.0, "5", None, [1]):
            with self.subTest(limit=value):
                with self.assertRaises(AuthorityError) as ctx:
                    _validate_page_size(value)
                self.assertEqual(str(ctx.exception), "page size must be an integer")

    def test_rejects_out_of_range(self) -> None:
        for value in (0, -1, MAX_PAGE_SIZE + 1):
            with self.subTest(limit=value):
                with self.assertRaises(AuthorityError) as ctx:
                    _validate_page_size(value)
                self.assertEqual(
                    str(ctx.exception), "page size must be between 1 and 50"
                )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class EncodeCursorTests(unittest.TestCase):
    """authority.py:340."""

    def test_encodes_short_payload(self) -> None:
        encoded = _encode_cursor(
            {"kind": "work_items", "updated_at": "2026-01-01", "work_item_id": VALID_WORK_ITEM}
        )
        self.assertLessEqual(len(encoded), MAX_CURSOR_LENGTH)
        self.assertEqual(
            json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))),
            {"kind": "work_items", "updated_at": "2026-01-01", "work_item_id": VALID_WORK_ITEM},
        )

    def test_rejects_payload_that_overflows_after_encoding(self) -> None:
        # 2000 raw chars encode to ~2667 base64 chars > MAX_CURSOR_LENGTH.
        with self.assertRaises(AuthorityError) as ctx:
            _encode_cursor(
                {"kind": "work_items", "updated_at": "x" * 2000, "work_item_id": VALID_WORK_ITEM}
            )
        self.assertEqual(str(ctx.exception), "cursor exceeds 2048 characters")


class DecodeCursorTests(unittest.TestCase):
    """authority.py:348, 354, 356, 360, 362."""

    def test_none_round_trips_to_none(self) -> None:
        self.assertIsNone(_decode_cursor(None, "work_items"))

    def test_round_trips_both_kinds(self) -> None:
        work_items = {
            "kind": "work_items",
            "updated_at": "2026-01-01T00:00:00.000000Z",
            "work_item_id": VALID_WORK_ITEM,
        }
        links = {
            "kind": "session_links",
            "linked_at": "2026-01-01T00:00:00.000000Z",
            "link_id": VALID_LINK,
        }
        self.assertEqual(
            _decode_cursor(_encode_cursor(work_items), "work_items"), work_items
        )
        self.assertEqual(
            _decode_cursor(_encode_cursor(links), "session_links"), links
        )

    def test_rejects_empty_non_string_and_oversized(self) -> None:
        # L348: the type/length gate, reached before any decoding.
        for bad in ("", 123, b"abc", "a" * (MAX_CURSOR_LENGTH + 1)):
            with self.subTest(value=str(bad)[:20]):
                with self.assertRaises(AuthorityError) as ctx:
                    _decode_cursor(bad, "work_items")
                self.assertEqual(
                    str(ctx.exception),
                    "cursor is invalid or exceeds 2048 characters",
                )

    def test_rejects_undecodable_base64_or_non_json(self) -> None:
        # L354: inside the except (ValueError, UnicodeError, TypeError) block.
        for raw in (b"not json at all", b"[]", b'"str"', b"\xff\xfe"):
            with self.subTest(raw=raw):
                with self.assertRaises(AuthorityError) as ctx:
                    _decode_cursor(_b64(raw), "work_items")
                self.assertEqual(str(ctx.exception), "cursor is invalid")

    def test_rejects_wrong_kind(self) -> None:
        # L356: not a dict, or kind mismatch.
        payload = _b64(json.dumps(["not", "a", "dict"]).encode())
        with self.assertRaises(AuthorityError) as ctx:
            _decode_cursor(payload, "work_items")
        self.assertEqual(str(ctx.exception), "cursor is invalid")

        wrong_kind = _encode_cursor(
            {"kind": "session_links", "linked_at": "t", "link_id": VALID_LINK}
        )
        with self.assertRaises(AuthorityError) as ctx:
            _decode_cursor(wrong_kind, "work_items")
        self.assertEqual(str(ctx.exception), "cursor is invalid")

    def test_rejects_wrong_key_set_for_each_kind(self) -> None:
        # L360: the key set differs per kind; the other kind's keys must fail.
        for kind in ("work_items", "session_links"):
            with self.subTest(kind=kind):
                other = (
                    {"kind": "session_links", "linked_at": "t", "link_id": VALID_LINK}
                    if kind == "work_items"
                    else {"kind": "work_items", "updated_at": "t", "work_item_id": VALID_WORK_ITEM}
                )
                with self.assertRaises(AuthorityError) as ctx:
                    _decode_cursor(_encode_cursor(other), kind)
                self.assertEqual(str(ctx.exception), "cursor is invalid")

        extra_key = _encode_cursor(
            {"kind": "work_items", "updated_at": "t", "work_item_id": VALID_WORK_ITEM, "x": "y"}
        )
        with self.assertRaises(AuthorityError) as ctx:
            _decode_cursor(extra_key, "work_items")
        self.assertEqual(str(ctx.exception), "cursor is invalid")

    def test_rejects_non_string_value(self) -> None:
        # L362: shape is right, but a value is not a str.
        for payload in (
            {"kind": "work_items", "updated_at": "t", "work_item_id": 1},
            {"kind": "session_links", "linked_at": None, "link_id": VALID_LINK},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(AuthorityError) as ctx:
                    _decode_cursor(_encode_cursor(payload), payload["kind"])
                self.assertEqual(str(ctx.exception), "cursor is invalid")


if __name__ == "__main__":
    unittest.main()
