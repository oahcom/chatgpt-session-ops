"""Integration tests against the real MCP SDK.

These tests deliberately use the installed SDK rather than mocked handlers.
The SDK is optional for the core package, so this entire test class is skipped
only when importing the top-level ``mcp`` package raises ImportError.
"""

import asyncio
import unittest
from urllib.parse import urlsplit

try:
    import mcp  # noqa: F401
except ImportError:
    mcp = None

from src.session_ops.mcp_server import (
    MCPToolService,
    TOOL_SCHEMAS,
    build_mcp_server,
)
from src.session_ops.mcp_auth import JWTVerifier


@unittest.skipIf(mcp is None, "MCP SDK is not installed")
class MCPSDKIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from starlette.applications import Starlette
        from mcp import types
        from mcp.server import Server
        from mcp.server.context import ServerRequestContext

        cls.Starlette = Starlette
        cls.types = types
        cls.Server = Server
        cls.ServerRequestContext = ServerRequestContext

        cls.verifier = JWTVerifier(
            issuer="https://issuer.example.test/realm",
            audience="https://session-ops.example.test/mcp",
            realm_namespace="session_ops",
        )
        # build_mcp_server validates the service type and non-None store.
        # The unauthenticated handler path under test never dispatches to it.
        cls.service = MCPToolService(object())
        cls.app = build_mcp_server(cls.service, cls.verifier)

    def _inner_app(self):
        inner = getattr(self.app, "inner", None)
        self.assertIsNotNone(
            inner,
            "build_mcp_server did not return the expected auth wrapper",
        )
        self.assertIsInstance(inner, self.Starlette)
        return inner

    def _route_paths(self):
        inner = self._inner_app()
        return [
            getattr(route, "path", None)
            for route in inner.routes
        ]

    def test_builds_real_starlette_app_at_verifier_audience_path(self):
        expected_path = urlsplit(self.verifier.audience).path or "/mcp"
        route_paths = self._route_paths()

        self.assertIn(
            expected_path,
            route_paths,
            "The real Starlette app does not expose the verifier audience path",
        )

    @classmethod
    def _find_server(cls):
        inner = getattr(cls.app, "inner", None)
        routes = getattr(inner, "routes", ())
        attempts = []

        for index, route in enumerate(routes):
            endpoint = getattr(route, "endpoint", None)
            route_app = getattr(route, "app", None)

            candidates = (
                ("endpoint.session_manager.app", endpoint, "session_manager", "app"),
                ("endpoint.session_manager._app", endpoint, "session_manager", "_app"),
                ("route.app.session_manager.app", route_app, "session_manager", "app"),
                ("route.app.session_manager._app", route_app, "session_manager", "_app"),
            )

            for description, root, first, second in candidates:
                try:
                    manager = getattr(root, first)
                    candidate = getattr(manager, second)
                except (AttributeError, TypeError):
                    attempts.append(
                        "route[%d].%s: unavailable" % (index, description)
                    )
                    continue

                if isinstance(candidate, cls.Server):
                    return candidate

                attempts.append(
                    "route[%d].%s: found %s"
                    % (index, description, type(candidate).__name__)
                )

        route_details = [
            {
                "path": getattr(route, "path", None),
                "endpoint_type": type(
                    getattr(route, "endpoint", None)
                ).__name__,
                "app_type": type(getattr(route, "app", None)).__name__,
            }
            for route in routes
        ]
        raise AssertionError(
            "Could not locate mcp.server.Server in the real SDK app. "
            "Tried: %r. Routes: %r"
            % (attempts, route_details)
        )

    def _handler(self, server, method):
        entry = server.get_request_handler(method)
        self.assertIsNotNone(
            entry,
            "SDK did not register request handler %r" % method,
        )
        handler = getattr(entry, "handler", None)
        self.assertTrue(
            callable(handler),
            "SDK handler entry for %r has no callable handler: %r"
            % (method, entry),
        )
        return handler

    def _request_context(self, method):
        return self.ServerRequestContext(
            session=None,
            lifespan_context=None,
            protocol_version="2025-11-25",
            method=method,
            request_id="probe-1",
        )

    def test_real_sdk_registers_list_and_call_handlers(self):
        server = self._find_server()

        list_handler = self._handler(server, "tools/list")
        call_handler = self._handler(server, "tools/call")

        self.assertTrue(callable(list_handler))
        self.assertTrue(callable(call_handler))

    def test_real_tools_list_returns_the_frozen_tool_surface(self):
        server = self._find_server()
        handler = self._handler(server, "tools/list")
        context = self._request_context("tools/list")

        result = asyncio.run(handler(context, None))

        self.assertIsInstance(result, self.types.ListToolsResult)
        actual_names = {tool.name for tool in result.tools}
        self.assertEqual(actual_names, set(TOOL_SCHEMAS))

    def test_real_tools_call_rejects_missing_request_authentication(self):
        server = self._find_server()
        handler = self._handler(server, "tools/call")
        context = self._request_context("tools/call")
        params = self.types.CallToolRequestParams(
            name="work_item.get",
            arguments={},
        )

        result = asyncio.run(handler(context, params))

        self.assertIsInstance(result, self.types.CallToolResult)
        self.assertIs(result.is_error, True)
        self.assertIsInstance(result.structured_content, dict)
        self.assertEqual(
            result.structured_content["error"]["code"],
            "UNAUTHENTICATED",
        )


if __name__ == "__main__":
    unittest.main()
