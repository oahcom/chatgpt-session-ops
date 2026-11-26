"""SDK-independent contract tests for the Session Ops MCP service.

Run in a bare environment:
    python -m unittest discover -s tests

These tests deliberately do not import or require the MCP SDK.
Authentication is injected as a verified AuthContext. No JWKS network access
or real Memory Adapter subprocess is used.
"""

import importlib
import tempfile
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.session_ops.authority import (
    Page,
    Principal,
    SecureWorkItemStore,
)
from src.session_ops.mcp_auth import (
    AuthContext,
    principal_from_claims,
)
from src.session_ops.mcp_server import (
    MAX_CONTEXT_CONTENT_CHARS,
    MAX_CONTEXT_ENTRIES,
    MAX_CURSOR_CHARS,
    MAX_PAGE_SIZE,
    MAX_QUERY_CHARS,
    MAX_STRUCTURED_CONTENT_BYTES,
    MCPToolService,
    _validate_arguments,
)


TENANT = "tenant-a"
OWNER = "realm-a:owner"
OTHER = "realm-b:other"

WORK_ITEM_ID = "wi_12345678-1234-4234-8234-123456789abc"
SESSION_ID = "12345678-1234-4234-8234-123456789abc"
LINK_ID = "sl_12345678-1234-4234-8234-123456789abc"

ALL_SCOPES = (
    "session_ops.work_item.read",
    "session_ops.work_item.write",
    "session_ops.access.manage",
    "session_ops.session.read",
    "session_ops.session.write",
    "session_ops.context.read",
)


def make_auth(
    tenant=TENANT,
    principal_id=OWNER,
    scopes=ALL_SCOPES,
):
    """Inject an already-verified identity; never perform network auth."""
    return AuthContext(
        principal=Principal(
            tenant_id=tenant,
            principal_id=principal_id,
        ),
        scopes=scopes,
        issuer="https://identity.example.test/realms/test",
        subject=principal_id,
        realm_namespace="test",
    )


class FakeStore:
    """Deterministic authority double for dispatch contract tests."""

    def __init__(self):
        self.calls = []
        self.created = []

    def create_work_item(self, principal):
        self.calls.append(("create", principal))
        item = {
            "work_item_id": WORK_ITEM_ID,
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        self.created.append(item)
        return item

    def get_work_item(self, principal, work_item_id):
        self.calls.append(("get", principal, work_item_id))
        return {
            "work_item_id": work_item_id,
            "status": "active",
        }

    def list_work_items(
        self, principal, *, status=None, page_size=25, cursor=None
    ):
        self.calls.append(
            ("list_work_items", principal, status, page_size, cursor)
        )
        return Page(
            [{"work_item_id": WORK_ITEM_ID}],
            next_cursor="work-items-next" if cursor is None else None,
        )

    def set_status(
        self, principal, work_item_id, status,
        expected_updated_at=None, request_id=None,
    ):
        self.calls.append(
            ("update", principal, work_item_id, status)
        )
        return {
            "work_item_id": work_item_id,
            "status": status,
        }

    def grant(
        self, principal, work_item_id, target_principal_id,
        scope, request_id=None,
    ):
        self.calls.append(("grant", principal, target_principal_id, scope))

    def revoke(
        self, principal, work_item_id, target_principal_id,
        scope, request_id=None,
    ):
        self.calls.append(("revoke", principal, target_principal_id, scope))

    def link_session(
        self,
        principal,
        session_id,
        work_item_id,
        attempt_id,
        evidence=None,
        request_id=None,
    ):
        self.calls.append(
            ("link", principal, session_id, work_item_id, attempt_id)
        )
        return {
            "link_id": LINK_ID,
            "session_id": session_id,
            "work_item_id": work_item_id,
        }

    def unlink_session(
        self, principal, session_id,
        expected_link_id=None, request_id=None,
    ):
        self.calls.append(("unlink", principal, session_id))
        return SimpleNamespace(
            link_id=LINK_ID,
            session_id=session_id,
            work_item_id=WORK_ITEM_ID,
            linked_at="2026-01-01T00:00:00Z",
            unlinked_at="2026-01-02T00:00:00Z",
        )

    def list_sessions(
        self, principal, work_item_id, *,
        include_unlinked=False, page_size=25, cursor=None,
    ):
        self.calls.append(
            ("list_sessions", principal, work_item_id,
             page_size, cursor)
        )
        return Page(
            [{
                "link_id": LINK_ID,
                "session_id": SESSION_ID,
                "work_item_id": work_item_id,
            }],
            next_cursor="sessions-next" if cursor is None else None,
        )

    def get_active_link(self, principal, session_id):
        self.calls.append(("get_active_link", principal, session_id))
        return SimpleNamespace(
            link_id=LINK_ID,
            session_id=session_id,
            work_item_id=WORK_ITEM_ID,
        )


class FakeMemoryAdapterClient:
    """In-process replacement: no executable or subprocess is started."""

    instances = []

    def __init__(self, command, timeout_seconds):
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.instances.append(self)

    def retrieve(self, *, work_item_id, session_id, query, limit):
        return SimpleNamespace(
            entries=[
                SimpleNamespace(
                    memory_id="memory-1",
                    session_id=session_id,
                    content="test memory",
                    provenance={"source": "test"},
                    created_at="2026-01-01T00:00:00Z",
                    score=0.9,
                )
            ][:limit]
        )


class MCPContractTests(unittest.TestCase):

    def setUp(self):
        self.store = FakeStore()
        self.service = MCPToolService(self.store)
        self.auth = make_auth()

    def dispatch(self, name, arguments=None, service=None):
        return (service or self.service).dispatch(
            name,
            arguments or {},
            self.auth,
            request_id="test-request",
        )

    def assert_success(self, result):
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["meta"]["contract_version"], "0.1")
        self.assertEqual(result["meta"]["request_id"], "test-request")
        self.assertIn("data", result)

    def assert_error(self, result, code):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], code)
        self.assertIn("message", result["error"])
        self.assertIn("retryable", result["error"])
        self.assertIn("details", result["error"])

    # --------------------------------------------------------------
    # Ten tools: one successful dispatch each
    # --------------------------------------------------------------

    def test_work_item_create(self):
        result = self.dispatch("work_item.create")
        self.assert_success(result)
        self.assertEqual(result["data"]["work_item_id"], WORK_ITEM_ID)

    def test_work_item_get(self):
        result = self.dispatch(
            "work_item.get", {"work_item_id": WORK_ITEM_ID}
        )
        self.assert_success(result)
        self.assertEqual(result["data"]["status"], "active")

    def test_work_item_list(self):
        result = self.dispatch("work_item.list", {"page_size": 10})
        self.assert_success(result)
        self.assertEqual(len(result["data"]["items"]), 1)

    def test_work_item_update(self):
        result = self.dispatch(
            "work_item.update",
            {"work_item_id": WORK_ITEM_ID, "status": "paused"},
        )
        self.assert_success(result)
        self.assertEqual(result["data"]["status"], "paused")

    def test_work_item_grant(self):
        result = self.dispatch(
            "work_item.grant",
            {
                "work_item_id": WORK_ITEM_ID,
                "target_principal_id": "realm-a:reader",
                "scope": "read",
            },
        )
        self.assert_success(result)
        self.assertTrue(result["data"]["granted"])

    def test_work_item_revoke(self):
        result = self.dispatch(
            "work_item.revoke",
            {
                "work_item_id": WORK_ITEM_ID,
                "target_principal_id": "realm-a:reader",
                "scope": "read",
            },
        )
        self.assert_success(result)
        self.assertTrue(result["data"]["revoked"])

    def test_work_item_grant_requires_access_manage(self):
        """work_item.write alone must not authorize a grant."""
        auth = make_auth(
            scopes=("session_ops.work_item.write",)
        )

        result = self.service.dispatch(
            "work_item.grant",
            {
                "work_item_id": WORK_ITEM_ID,
                "target_principal_id": "realm-a:reader",
                "scope": "read",
            },
            auth,
            request_id="test-request",
        )

        self.assert_error(result, "PERMISSION_DENIED")
        self.assertEqual(
            result["error"]["details"]["required_scopes"],
            [
                "session_ops.work_item.write",
                "session_ops.access.manage",
            ],
        )
        # Authorization must fail before any authority-store operation.
        self.assertEqual(self.store.calls, [])

    def test_work_item_revoke_requires_access_manage(self):
        """work_item.write alone must not authorize a revoke."""
        auth = make_auth(scopes=("session_ops.work_item.write",))

        result = self.service.dispatch(
            "work_item.revoke",
            {
                "work_item_id": WORK_ITEM_ID,
                "target_principal_id": "realm-a:reader",
                "scope": "read",
            },
            auth,
            request_id="test-request",
        )

        self.assert_error(result, "PERMISSION_DENIED")
        self.assertEqual(
            result["error"]["details"]["required_scopes"],
            [
                "session_ops.work_item.write",
                "session_ops.access.manage",
            ],
        )
        # Authorization must fail before any authority-store operation.
        self.assertEqual(self.store.calls, [])

    def test_session_link(self):
        result = self.dispatch(
            "session.link",
            {
                "session_id": SESSION_ID,
                "work_item_id": WORK_ITEM_ID,
            },
        )
        self.assert_success(result)
        self.assertEqual(result["data"]["session_id"], SESSION_ID)

    def test_session_unlink(self):
        result = self.dispatch(
            "session.unlink", {"session_id": SESSION_ID}
        )
        self.assert_success(result)
        self.assertTrue(result["data"]["unlinked"])

    def test_session_list(self):
        result = self.dispatch(
            "session.list", {"work_item_id": WORK_ITEM_ID}
        )
        self.assert_success(result)
        self.assertEqual(len(result["data"]["items"]), 1)

    def test_session_context_with_injected_adapter(self):
        adapter_module = importlib.import_module(
            "src.session_ops.memory_adapter_client"
        )
        FakeMemoryAdapterClient.instances.clear()

        with patch.object(
            adapter_module,
            "MemoryAdapterClient",
            FakeMemoryAdapterClient,
        ):
            configured = MCPToolService(
                self.store,
                memory_adapter=["python", "-m", "memory_adapter"],
            )
            result = self.dispatch(
                "session.context",
                {
                    "session_id": SESSION_ID,
                    "query": "test query",
                    "limit": 1,
                },
                service=configured,
            )

        self.assert_success(result)
        self.assertEqual(len(result["data"]["entries"]), 1)
        self.assertEqual(
            result["data"]["entries"][0]["trust"],
            "untrusted_historical_context",
        )
        self.assertEqual(len(FakeMemoryAdapterClient.instances), 1)

    def test_oversized_session_context_becomes_resource_exhausted(self):
        adapter_module = importlib.import_module(
            "src.session_ops.memory_adapter_client"
        )

        class OversizedMemoryAdapterClient(FakeMemoryAdapterClient):
            def retrieve(self, *, work_item_id, session_id, query, limit):
                retrieved = super().retrieve(
                    work_item_id=work_item_id,
                    session_id=session_id,
                    query=query,
                    limit=limit,
                )
                retrieved.entries[0].content = (
                    "x" * (MAX_CONTEXT_CONTENT_CHARS + 1)
                )
                return retrieved

        with patch.object(
            adapter_module,
            "MemoryAdapterClient",
            OversizedMemoryAdapterClient,
        ):
            configured = MCPToolService(
                self.store,
                memory_adapter=["python", "-m", "memory_adapter"],
            )
            result = self.dispatch(
                "session.context",
                {
                    "session_id": SESSION_ID,
                    "query": "oversized content test",
                    "limit": 1,
                },
                service=configured,
            )

        self.assert_error(result, "RESOURCE_EXHAUSTED")

    # --------------------------------------------------------------
    # Authentication and identity isolation
    # --------------------------------------------------------------

    def test_identity_cannot_be_injected_through_arguments(self):
        for identity_field in ("principal_id", "tenant_id"):
            with self.subTest(field=identity_field):
                result = self.dispatch(
                    "work_item.create",
                    {identity_field: "attacker-controlled"},
                )
                self.assert_error(result, "INVALID_ARGUMENT")

        self.assertEqual(
            [call[0] for call in self.store.calls],
            [],
        )

    def test_missing_auth_context_is_unauthenticated(self):
        result = self.service.dispatch(
            "work_item.create",
            {},
            None,
            request_id="test-request",
        )
        self.assert_error(result, "UNAUTHENTICATED")

    def test_insufficient_scope_is_permission_denied(self):
        auth = make_auth(scopes=("session_ops.work_item.read",))
        result = self.service.dispatch(
            "work_item.create", {}, auth,
            request_id="test-request",
        )
        self.assert_error(result, "PERMISSION_DENIED")

    # --------------------------------------------------------------
    # Pagination and bounds
    # --------------------------------------------------------------

    def test_work_item_list_pagination(self):
        first = self.dispatch(
            "work_item.list", {"page_size": 10}
        )
        self.assert_success(first)
        cursor = first["data"]["next_cursor"]
        self.assertEqual(cursor, "work-items-next")

        second = self.dispatch(
            "work_item.list",
            {"page_size": 10, "cursor": cursor},
        )
        self.assert_success(second)
        self.assertIsNone(second["data"]["next_cursor"])

        calls = [
            call for call in self.store.calls
            if call[0] == "list_work_items"
        ]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][4], cursor)

    def test_session_list_pagination(self):
        first = self.dispatch(
            "session.list",
            {"work_item_id": WORK_ITEM_ID, "page_size": 10},
        )
        self.assert_success(first)
        cursor = first["data"]["next_cursor"]
        self.assertEqual(cursor, "sessions-next")

        second = self.dispatch(
            "session.list",
            {
                "work_item_id": WORK_ITEM_ID,
                "page_size": 10,
                "cursor": cursor,
            },
        )
        self.assert_success(second)
        self.assertIsNone(second["data"]["next_cursor"])

        calls = [
            call for call in self.store.calls
            if call[0] == "list_sessions"
        ]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][4], cursor)

    def test_page_size_upper_bound(self):
        self.assertEqual(MAX_PAGE_SIZE, 50)

        for tool, arguments in (
            ("work_item.list", {"page_size": MAX_PAGE_SIZE + 1}),
            (
                "session.list",
                {
                    "work_item_id": WORK_ITEM_ID,
                    "page_size": MAX_PAGE_SIZE + 1,
                },
            ),
        ):
            with self.subTest(tool=tool):
                result = self.dispatch(tool, arguments)
                self.assert_error(result, "INVALID_ARGUMENT")

    def test_cursor_upper_bound(self):
        self.assertEqual(MAX_CURSOR_CHARS, 2048)

        for tool, arguments in (
            ("work_item.list", {"cursor": "x" * 2049}),
            (
                "session.list",
                {
                    "work_item_id": WORK_ITEM_ID,
                    "cursor": "x" * 2049,
                },
            ),
        ):
            with self.subTest(tool=tool):
                result = self.dispatch(tool, arguments)
                self.assert_error(result, "INVALID_ARGUMENT")

    def test_context_limit_upper_bound(self):
        result = self.dispatch(
            "session.context",
            {
                "session_id": SESSION_ID,
                "query": "query",
                "limit": MAX_CONTEXT_ENTRIES + 1,
            },
        )
        self.assert_error(result, "INVALID_ARGUMENT")

    # --------------------------------------------------------------
    # Argument validation branches
    # --------------------------------------------------------------

    def assert_argument_rejected(self, tool, arguments, fragment):
        with self.assertRaises(ValueError) as caught:
            _validate_arguments(tool, arguments)
        self.assertIn(fragment, str(caught.exception))

    def test_required_argument_missing_is_rejected(self):
        self.assert_argument_rejected(
            "work_item.get", {}, "required argument missing"
        )

    def test_string_argument_required_is_rejected(self):
        self.assert_argument_rejected(
            "work_item.get",
            {"work_item_id": 123},
            "string argument required",
        )

    def test_string_argument_too_short_is_rejected(self):
        self.assert_argument_rejected(
            "work_item.grant",
            {
                "work_item_id": WORK_ITEM_ID,
                "target_principal_id": "",
                "scope": "read",
            },
            "string argument too short",
        )

    def test_string_argument_too_long_is_rejected(self):
        # The schema maxLength fires before the dedicated cursor bound,
        # which makes the later "cursor is too long" branch unreachable.
        self.assert_argument_rejected(
            "work_item.list",
            {"cursor": "x" * (MAX_CURSOR_CHARS + 1)},
            "string argument too long",
        )

    def test_string_argument_not_in_enum_is_rejected(self):
        self.assert_argument_rejected(
            "work_item.list",
            {"status": "definitely-not-in-enum"},
            "string argument is not an allowed value",
        )

    def test_string_argument_invalid_format_is_rejected(self):
        self.assert_argument_rejected(
            "session.link",
            {
                "session_id": SESSION_ID,
                "work_item_id": WORK_ITEM_ID,
                "attempt_id": "att_not-a-uuid",
            },
            "string argument has invalid format",
        )

    def test_integer_argument_required_is_rejected(self):
        for value in ("10", True):
            with self.subTest(value=value):
                self.assert_argument_rejected(
                    "work_item.list",
                    {"page_size": value},
                    "integer argument required",
                )

    def test_integer_argument_below_minimum_is_rejected(self):
        self.assert_argument_rejected(
            "work_item.list",
            {"page_size": 0},
            "integer argument below minimum",
        )

    def test_boolean_argument_required_is_rejected(self):
        for value in (0, 1, "true"):
            with self.subTest(value=value):
                self.assert_argument_rejected(
                    "session.list",
                    {
                        "work_item_id": WORK_ITEM_ID,
                        "include_unlinked": value,
                    },
                    "boolean argument required",
                )

    # --------------------------------------------------------------
    # Non-disclosure and trusted evidence
    # --------------------------------------------------------------

    def test_cross_tenant_and_missing_resource_are_indistinguishable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SecureWorkItemStore(
                Path(directory) / "authority.sqlite3"
            )
            store.initialize()

            owner = Principal(TENANT, OWNER)
            foreign = Principal("tenant-b", "realm-b:other")
            created = store.create_work_item(owner)

            service = MCPToolService(store)
            auth = make_auth(
                tenant=foreign.tenant_id,
                principal_id=foreign.principal_id,
            )

            cross_tenant = service.dispatch(
                "work_item.get",
                {"work_item_id": created.work_item_id},
                auth,
                request_id="same-request",
            )

            nonexistent = service.dispatch(
                "work_item.get",
                {"work_item_id": WORK_ITEM_ID},
                auth,
                request_id="same-request",
            )

            self.assert_error(cross_tenant, "NOT_FOUND")
            self.assert_error(nonexistent, "NOT_FOUND")
            self.assertEqual(
                cross_tenant["error"],
                nonexistent["error"],
            )

    def test_unconfirmed_f13_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SecureWorkItemStore(
                Path(directory) / "authority.sqlite3"
            )
            store.initialize()
            principal = Principal(TENANT, OWNER)
            item = store.create_work_item(principal)

            service = MCPToolService(store)
            result = service.dispatch(
                "session.link",
                {
                    "session_id": SESSION_ID,
                    "work_item_id": item.work_item_id,
                    "attempt_id": "att_" + str(uuid.uuid4()),
                },
                self.auth,
                request_id="test-request",
            )
            self.assert_error(result, "QUARANTINED_SESSION")

    def test_caller_supplied_evidence_is_rejected_by_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SecureWorkItemStore(
                Path(directory) / "authority.sqlite3"
            )
            store.initialize()
            principal = Principal(TENANT, OWNER)
            item = store.create_work_item(principal)

            with self.assertRaises(Exception) as caught:
                store.link_session(
                    principal,
                    SESSION_ID,
                    item.work_item_id,
                    "att_" + str(uuid.uuid4()),
                    evidence={
                        "confirmed": True,
                        "evidence_complete": True,
                    },
                )

            self.assertEqual(
                caught.exception.__class__.__name__,
                "QuarantineNotCleared",
            )

    # --------------------------------------------------------------
    # Dependency and response limits
    # --------------------------------------------------------------

    def test_context_without_adapter_is_dependency_unavailable(self):
        service = MCPToolService(self.store)
        result = service.dispatch(
            "session.context",
            {
                "session_id": SESSION_ID,
                "query": "test",
            },
            self.auth,
            request_id="test-request",
        )
        self.assert_error(result, "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(self.store.calls, [])

    # --------------------------------------------------------------
    # MemoryAdapterError code -> MCP error code mapping (#130)
    # --------------------------------------------------------------

    def dispatch_with_adapter_error(self, adapter_code):
        """Send a MemoryAdapterError(code) through the real MCP boundary."""
        from src.session_ops.memory_adapter_client import MemoryAdapterError

        adapter_module = importlib.import_module(
            "src.session_ops.memory_adapter_client"
        )

        class RaisingMemoryAdapterClient(FakeMemoryAdapterClient):
            def retrieve(self, *, work_item_id, session_id, query, limit):
                raise MemoryAdapterError(
                    adapter_code,
                    "adapter-request",
                    f"adapter error: {adapter_code}",
                )

        with patch.object(
            adapter_module,
            "MemoryAdapterClient",
            RaisingMemoryAdapterClient,
        ):
            configured = MCPToolService(
                self.store,
                memory_adapter=["python", "-m", "memory_adapter"],
            )
            return self.dispatch(
                "session.context",
                {
                    "session_id": SESSION_ID,
                    "query": "mapping test",
                    "limit": 1,
                },
                service=configured,
            )

    def test_adapter_error_codes_map_to_exact_mcp_codes(self):
        for adapter_code, expected_code in (
            ("ADAPTER_TIMEOUT", "TIMEOUT"),
            ("ADAPTER_UNAVAILABLE", "DEPENDENCY_UNAVAILABLE"),
            ("ADAPTER_CRASHED", "DEPENDENCY_UNAVAILABLE"),
            ("SCOPE_VIOLATION", "INTERNAL"),
        ):
            with self.subTest(adapter_code=adapter_code):
                result = self.dispatch_with_adapter_error(adapter_code)
                self.assert_error(result, expected_code)

    def test_unknown_adapter_error_code_falls_through_to_internal(self):
        result = self.dispatch_with_adapter_error("ADAPTER_CODE_UNSPECIFIED")
        self.assert_error(result, "INTERNAL")

    def test_oversized_response_becomes_resource_exhausted(self):
        class OversizedStore(FakeStore):
            def create_work_item(self, principal):
                return {"content": "x" * MAX_STRUCTURED_CONTENT_BYTES}

        service = MCPToolService(OversizedStore())
        result = service.dispatch(
            "work_item.create",
            {},
            self.auth,
            request_id="test-request",
        )
        self.assert_error(result, "RESOURCE_EXHAUSTED")

    # --------------------------------------------------------------
    # Pure mcp_auth claim-to-principal mapping
    # --------------------------------------------------------------

    def test_verified_claims_map_to_namespaced_principal(self):
        claims = {
            "iss": "https://identity.example.test/realms/test",
            "aud": "https://mcp.example.test/mcp",
            "sub": "subject-123",
            "tenant_id": TENANT,
            "scope": "session_ops.work_item.read session_ops.session.read",
        }

        auth = principal_from_claims(
            claims,
            expected_issuer=claims["iss"],
            expected_audience=claims["aud"],
            realm_namespace="realm-a",
        )

        self.assertEqual(auth.principal.tenant_id, TENANT)
        self.assertEqual(
            auth.principal.principal_id,
            "realm-a:subject-123",
        )
        self.assertIn("session_ops.work_item.read", auth.scopes)
        self.assertNotIn("session_ops.work_item.write", auth.scopes)

    def test_claim_mapping_rejects_wrong_audience(self):
        claims = {
            "iss": "https://identity.example.test/realms/test",
            "aud": "https://wrong.example.test/mcp",
            "sub": "subject-123",
            "tenant_id": TENANT,
            "scope": "session_ops.work_item.read",
        }

        from src.session_ops.mcp_auth import AuthenticationError

        with self.assertRaises(AuthenticationError):
            principal_from_claims(
                claims,
                expected_issuer=claims["iss"],
                expected_audience="https://mcp.example.test/mcp",
                realm_namespace="realm-a",
            )


class ArgumentValidationTests(unittest.TestCase):
    """One case per rejection branch in _validate_arguments (#76).

    Every branch raises ValueError, and _error_code_for maps all of them to
    the same INVALID_ARGUMENT with empty details, so the caller cannot tell
    them apart. That is deliberate non-disclosure, and it is also why a
    dispatch-level test cannot watch these branches: deleting one leaves
    another raising, and the response looks identical. The message is the
    only observable, so these cases assert it directly.
    """

    def assert_rejected(self, tool, arguments, message):
        with self.assertRaises(ValueError) as ctx:
            _validate_arguments(tool, arguments)
        self.assertEqual(str(ctx.exception), message)

    def test_required_argument_missing(self):
        self.assert_rejected("work_item.get", {}, "required argument missing")

    def test_string_argument_required(self):
        self.assert_rejected(
            "work_item.get", {"work_item_id": 5}, "string argument required"
        )

    def test_string_argument_too_short(self):
        self.assert_rejected(
            "session.context",
            {"session_id": SESSION_ID, "query": ""},
            "string argument too short",
        )

    def test_string_argument_too_long(self):
        self.assert_rejected(
            "session.context",
            {"session_id": SESSION_ID, "query": "a" * 2049},
            "string argument too long",
        )

    def test_string_argument_is_not_an_allowed_value(self):
        self.assert_rejected(
            "work_item.list",
            {"status": "definitely-not-in-enum"},
            "string argument is not an allowed value",
        )

    def test_string_argument_has_invalid_format(self):
        self.assert_rejected(
            "work_item.get",
            {"work_item_id": "wi_not-a-uuid"},
            "string argument has invalid format",
        )

    def test_integer_argument_required(self):
        for value in ("5", 5.0, True):
            with self.subTest(value=value):
                self.assert_rejected(
                    "work_item.list",
                    {"page_size": value},
                    "integer argument required",
                )

    def test_integer_argument_below_minimum(self):
        self.assert_rejected(
            "session.context",
            {"session_id": SESSION_ID, "query": "x", "limit": 0},
            "integer argument below minimum",
        )

    def test_boolean_argument_required(self):
        self.assert_rejected(
            "session.list",
            {"work_item_id": WORK_ITEM_ID, "include_unlinked": "yes"},
            "boolean argument required",
        )

    def test_cursor_length_guard_cannot_be_reached(self):
        # "cursor is too long" is dead code: the schema declares
        # maxLength == MAX_CURSOR_CHARS, so the string branch rejects a
        # longer cursor first and an equal one passes this guard. Pinned so
        # that the branch is either removed or made reachable on purpose.
        self.assertEqual(MAX_CURSOR_CHARS, 2048)

        self.assert_rejected(
            "work_item.list",
            {"cursor": "x" * (MAX_CURSOR_CHARS + 1)},
            "string argument too long",
        )
        accepted = _validate_arguments(
            "work_item.list", {"cursor": "x" * MAX_CURSOR_CHARS}
        )
        self.assertEqual(len(accepted["cursor"]), MAX_CURSOR_CHARS)


if __name__ == "__main__":
    unittest.main()
