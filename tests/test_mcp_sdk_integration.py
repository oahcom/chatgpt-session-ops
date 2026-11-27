"""Integration tests against the real MCP SDK.

These tests deliberately use the installed SDK rather than mocked handlers.
The SDK is optional for the core package, so this entire test class is skipped
only when importing the top-level ``mcp`` package raises ImportError.
"""

import asyncio
import unittest
from unittest.mock import patch
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

    def test_events_handlers_register_and_advertise_capability(self):
        import base64
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from src.session_ops.authority import Principal
        from src.session_ops.mcp_auth import AuthContext
        from src.session_ops.mcp_events import MCPEventsStore, build_event_request_models

        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            store = MCPEventsStore(
                str(Path(tmp) / "events.sqlite3"),
                post_json=lambda _url, body, _headers: (
                    200,
                    __import__("json").dumps({"challenge": __import__("json").loads(body)["challenge"]}).encode(),
                ),
                encryption_key=b"e" * 32,
            )
            app = build_mcp_server(self.service, self.verifier, events_store=store)
            previous_app = type(self).app
            try:
                type(self).app = app
                server = self._find_server()
            finally:
                type(self).app = previous_app

            handlers = [server.get_request_handler(method) for method in (
                "events/list", "events/subscribe", "events/unsubscribe"
            )]
            self.assertTrue(all(handler is not None for handler in handlers))

            middleware = next(
                item for item in server.middleware
                if getattr(item, "__name__", "") == "advertise_events_capability"
            )

            async def next_initialize(_ctx):
                return {"capabilities": {"tools": {}}, "protocolVersion": "2025-03-26"}

            advertised = asyncio.run(middleware(self._request_context("initialize"), next_initialize))
            self.assertEqual(advertised["capabilities"]["events"], {})

            async def next_discover(_ctx):
                return {"capabilities": {"tools": {}}, "supportedVersions": ["2026-07-28"], "resultType": "complete"}

            discovered = asyncio.run(middleware(self._request_context("server/discover"), next_discover))
            self.assertEqual(discovered["capabilities"]["events"], {})

            auth = AuthContext(
                Principal(tenant_id="tenant-a", principal_id="session_ops:user-a"),
                ["session_ops.chatgpt.session.read", "session_ops.chatgpt.session.write"],
                "https://issuer.example.test/realm",
                "user-a",
                "session_ops",
            )
            context = self._request_context("events/list")
            context.request = SimpleNamespace(scope={"state": {"session_ops_auth": auth}})
            EventsListParams, EventsSubscribeParams, EventsUnsubscribeParams = build_event_request_models()
            listed = asyncio.run(handlers[0].handler(context, EventsListParams()))
            self.assertEqual(listed["events"][0]["name"], "chatgpt.job.completed")

            with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
                subscribed = asyncio.run(handlers[1].handler(
                    context,
                    EventsSubscribeParams.model_validate({
                        "name": "chatgpt.job.completed",
                        "arguments": {},
                        "delivery": {
                            "mode": "webhook",
                            "url": "https://events.example.test/callback",
                            "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
                        },
                    }),
                ))
            self.assertTrue(subscribed["id"].startswith("sub_"))

            from mcp.shared.exceptions import MCPError
            store.post_json = lambda _url, _body, _headers: (200, b'{"challenge":"wrong"}')
            with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
                with self.assertRaises(MCPError) as callback_error:
                    asyncio.run(handlers[1].handler(
                        context,
                        EventsSubscribeParams.model_validate({
                            "name": "chatgpt.job.completed",
                            "arguments": {},
                            "delivery": {
                                "mode": "webhook",
                                "url": "https://events.example.test/second-callback",
                                "secret": "whsec_" + base64.b64encode(b"z" * 32).decode(),
                            },
                        }),
                    ))
            self.assertEqual(callback_error.exception.code, -32015)
            self.assertEqual(callback_error.exception.data, {"reason": "challenge_failed"})

    def test_streamable_http_events_handshake_and_lifecycle(self):
        import base64
        import json
        import tempfile
        from pathlib import Path
        from src.session_ops.authority import Principal
        from src.session_ops.mcp_auth import AuthContext
        from src.session_ops.mcp_events import MCPEventsStore

        class TokenProvider:
            audience = "https://resource.example.test/mcp"

            def verify(self, token):
                self_token = token
                if self_token != "probe-token":
                    raise AssertionError("unexpected token")
                return AuthContext(
                    Principal("tenant-a", "session_ops:user-a"),
                    ["session_ops.chatgpt.session.read", "session_ops.chatgpt.session.write"],
                    "https://issuer.example.test/realm",
                    "user-a",
                    "session_ops",
                )

        async def run_protocol(app, store):
            async def send_rpc(payload, session_id=None):
                body = __import__("json").dumps(payload).encode()
                headers = [
                    (b"authorization", b"Bearer probe-token"),
                    (b"accept", b"application/json, text/event-stream"),
                    (b"content-type", b"application/json"),
                    (b"host", b"resource.example.test"),
                ]
                if session_id:
                    headers.append((b"mcp-session-id", session_id.encode()))
                messages = []
                first = True
                disconnected = asyncio.Event()

                async def receive():
                    nonlocal first
                    if first:
                        first = False
                        return {"type": "http.request", "body": body, "more_body": False}
                    await disconnected.wait()
                    return {"type": "http.disconnect"}

                async def send(message):
                    messages.append(message)

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "https",
                    "path": "/mcp",
                    "raw_path": b"/mcp",
                    "query_string": b"",
                    "root_path": "",
                    "headers": headers,
                    "client": ("127.0.0.1", 12345),
                    "server": ("resource.example.test", 443),
                }

                async def disconnect_later():
                    await asyncio.sleep(0.05)
                    disconnected.set()

                stopper = asyncio.create_task(disconnect_later())
                await app(scope, receive, send)
                await stopper
                start = next(item for item in messages if item["type"] == "http.response.start")
                response_headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
                response_body = b"".join(
                    item.get("body", b"") for item in messages if item["type"] == "http.response.body"
                ).decode()
                data_line = next(line[6:] for line in response_body.splitlines() if line.startswith("data: "))
                return start["status"], response_headers, json.loads(data_line)

            inner = app.inner
            async with inner.router.lifespan_context(inner):
                status, headers, initialized = await send_rpc({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "events-poc", "version": "1"},
                    },
                })
                self.assertEqual(status, 200)
                self.assertEqual(initialized["result"]["capabilities"]["events"], {})
                session_id = headers["mcp-session-id"]

                status, _headers, listed = await send_rpc(
                    {"jsonrpc": "2.0", "id": 2, "method": "events/list", "params": {}},
                    session_id,
                )
                self.assertEqual(status, 200)
                self.assertEqual(listed["result"]["events"][0]["name"], "chatgpt.job.completed")

                secret = "whsec_" + base64.b64encode(b"w" * 32).decode()
                with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
                    status, _headers, subscribed = await send_rpc({
                        "jsonrpc": "2.0", "id": 3, "method": "events/subscribe",
                        "params": {
                            "name": "chatgpt.job.completed",
                            "arguments": {"job_id": "job-1"},
                            "delivery": {"mode": "webhook", "url": "https://events.example.test/callback", "secret": secret},
                            "ttlMs": None,
                        },
                    }, session_id)
                self.assertEqual(status, 200)
                self.assertTrue(subscribed["result"]["id"].startswith("sub_"))
                self.assertIsNone(subscribed["result"]["refreshBefore"])

                status, _headers, unsubscribed = await send_rpc({
                    "jsonrpc": "2.0", "id": 4, "method": "events/unsubscribe",
                    "params": {
                        "name": "chatgpt.job.completed",
                        "arguments": {"job_id": "job-1"},
                        "delivery": {"mode": "webhook", "url": "https://events.example.test/callback"},
                    },
                }, session_id)
                self.assertEqual(status, 200)
                self.assertEqual(unsubscribed["result"], {})

        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            store = MCPEventsStore(
                str(Path(tmp) / "events.sqlite3"),
                encryption_key=b"h" * 32,
                post_json=lambda _url, body, _headers: (
                    200,
                    json.dumps({"challenge": json.loads(body)["challenge"]}).encode(),
                ),
            )
            class Sender:
                def get_job(self, **kwargs):
                    return {"job_id": kwargs["job_id"]}

            sender = Sender()
            app = build_mcp_server(
                self.service,
                TokenProvider(),
                events_store=store,
                sender_gateway=sender,
            )
            asyncio.run(run_protocol(app, store))

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
