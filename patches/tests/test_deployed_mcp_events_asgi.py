import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.session_ops.authority import Principal
from src.session_ops.mcp_auth import AuthContext
from src.session_ops.mcp_events import MCPEventsStore
from src.session_ops.mcp_event_collector import MCPEventCollector
from src.session_ops.mcp_event_dispatcher import MCPEventDispatcher
from src.session_ops.mcp_server import MCPToolService, build_mcp_server
from scripts.run_mcp_server import DeploymentASGI, _load_events_encryption_key, create_application


class TokenProvider:
    audience = "https://resource.example.test/mcp"

    def verify(self, token):
        if token != "test-token":
            raise AssertionError("unexpected token")
        return AuthContext(
            Principal("tenant-a", "user-a"),
            ["session_ops.chatgpt.session.read", "session_ops.chatgpt.session.write"],
            "https://issuer.example.test/realms/session-ops",
            "user-a",
            "session-ops",
        )


async def _asgi_rpc(app, payload, session_id=None, *, disconnect_after=0.05):
    body = json.dumps(payload).encode()
    headers = [
        (b"authorization", b"Bearer test-token"),
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
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "https", "path": "/mcp", "raw_path": b"/mcp",
        "query_string": b"", "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 12345), "server": ("resource.example.test", 443),
    }

    async def disconnect_later():
        await asyncio.sleep(disconnect_after)
        disconnected.set()

    stopper = asyncio.create_task(disconnect_later())
    await app(scope, receive, send)
    await stopper
    start = next(item for item in messages if item["type"] == "http.response.start")
    response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    response_body = b"".join(
        item.get("body", b"") for item in messages if item["type"] == "http.response.body"
    ).decode()
    data = next(line[6:] for line in response_body.splitlines() if line.startswith("data: "))
    return start["status"], response_headers, json.loads(data)


class DeployedMCPEventsTests(unittest.TestCase):
    def test_collector_isolates_bad_rows_and_still_acks_good_rows(self):
        good = {
            "event_id": "event-good", "job_id": "job-good", "tenant_id": "tenant-a",
            "principal_id": "user-a", "name": "chatgpt.job.completed", "lease_token": "lease-good",
            "payload": {"job_id": "job-good", "status": "succeeded", "summary": "done", "completed_at": "2026-11-28T12:00:00Z"},
        }

        class Sender:
            def __init__(self):
                self.acked = []
            def claim_events(self, **_kwargs):
                return {"items": [{"event_id": "event-bad"}, good]}
            def ack_event(self, *, event_id, lease_token):
                self.acked.append((event_id, lease_token))
                return {"acknowledged": True}

        class Store:
            def __init__(self):
                self.events = []
            def publish_job_completed(self, *args, **kwargs):
                self.events.append((args, kwargs))

        sender, store = Sender(), Store()
        collector = MCPEventCollector(sender, store, worker_id="test-dispatcher")
        with patch("src.session_ops.mcp_event_collector.logging.exception") as log:
            self.assertEqual(collector.run_once(), 1)
        log.assert_called_once()
        self.assertEqual(sender.acked, [("event-good", "lease-good")])
        self.assertEqual(len(store.events), 1)

    def test_dispatcher_runs_collection_delivery_and_cleanup_then_stops(self):
        import threading

        stop = threading.Event()
        class Collector:
            def __init__(self):
                self.calls = 0
            def run_once(self):
                self.calls += 1
                if self.calls == 2:
                    stop.set()
                return 0
        class Store:
            def __init__(self):
                self.cleaned = 0
                self.deliveries = 0
            def cleanup_expired(self):
                self.cleaned += 1
            def deliver_pending(self, **kwargs):
                self.deliveries += 1
                self.asserted_limit = kwargs["limit"]
                return 0

        collector, store = Collector(), Store()
        dispatcher = MCPEventDispatcher(collector, store, poll_seconds=0.05)
        dispatcher._run(stop)
        self.assertEqual(collector.calls, 2)
        self.assertEqual(store.cleaned, 1)
        self.assertEqual(store.deliveries, 2)
        self.assertEqual(store.asserted_limit, 1)

    def test_events_key_file_requires_private_service_owned_raw_key(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            key_file = Path(tmp) / "events.key"
            key_file.write_bytes(b"k" * 32)
            key_file.chmod(0o400)
            self.assertEqual(_load_events_encryption_key(str(key_file)), b"k" * 32)
            key_file.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "mode 0400/0600"):
                _load_events_encryption_key(str(key_file))
            with self.assertRaisesRegex(RuntimeError, "absolute path"):
                _load_events_encryption_key("relative/events.key")

    def test_deployment_lifespan_starts_and_stops_background_tasks(self):
        class Worker:
            def __init__(self):
                self.started = 0
                self.stopped = 0
            def start(self):
                self.started += 1
            def stop(self):
                self.stopped += 1

        worker = Worker()
        async def inner(scope, receive, send):
            self.assertEqual(scope["type"], "lifespan")
            startup = await receive()
            self.assertEqual(startup["type"], "lifespan.startup")
            await send({"type": "lifespan.startup.complete"})
            shutdown = await receive()
            self.assertEqual(shutdown["type"], "lifespan.shutdown")
            await send({"type": "lifespan.shutdown.complete"})

        app = DeploymentASGI(
            inner,
            issuer="https://issuer.example.test/realms/session-ops",
            audience=TokenProvider.audience,
            background_tasks=[worker],
        )
        messages = iter([
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ])
        sent = []
        async def receive():
            return next(messages)
        async def send(message):
            sent.append(message)
        asyncio.run(app({"type": "lifespan"}, receive, send))
        self.assertEqual(worker.started, 1)
        self.assertEqual(worker.stopped, 1)
        self.assertEqual([item["type"] for item in sent], ["lifespan.startup.complete", "lifespan.shutdown.complete"])

    def test_create_application_enables_events_only_with_private_key_file(self):
        class Store:
            def initialize(self):
                self.initialized = True

        class Sender:
            def redacted_target(self):
                return "https://sender-gateway.test"
            def claim_events(self, **_kwargs):
                return {"items": []}
            def ack_event(self, **_kwargs):
                return {"acknowledged": True}
            def get_job(self, **kwargs):
                if kwargs.get("job_id") == "job-not-owned":
                    class Denied(Exception):
                        code = "NOT_FOUND"
                    raise Denied("not found")
                return {"job_id": kwargs["job_id"]}

        async def inner_app(_scope, _receive, _send):
            return None

        config = {
            "database_path": "/var/lib/so-mcp/session-ops.sqlite3",
            "principal_directory": None,
            "issuer": "https://issuer.example.test/realms/session-ops",
            "audience": "https://resource.example.test/mcp",
            "realm_namespace": "session-ops",
            "tenant_claim": "tenant_id",
            "discovery_url": None,
            "memory_adapter_command": None,
        }
        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            key_file = Path(tmp) / "events.key"
            key_file.write_bytes(b"k" * 32)
            key_file.chmod(0o400)
            db_path = Path(tmp) / "events" / "events.sqlite3"
            with patch.dict("os.environ", {
                "SO_MCP_EVENTS_KEY_FILE": str(key_file),
                "SO_MCP_EVENTS_DB": str(db_path),
            }, clear=False), patch("scripts.run_mcp_server.SecureWorkItemStore", return_value=Store()), patch(
                "scripts.run_mcp_server.JWTVerifier", return_value=object()
            ), patch("scripts.run_mcp_server.MCPToolService", return_value=object()), patch(
                "scripts.run_mcp_server.build_mcp_server", return_value=inner_app
            ) as build:
                app = create_application(config, sender_gateway=Sender())
            self.assertEqual(len(app.background_tasks), 1)
            self.assertIs(build.call_args.kwargs["sender_gateway"], build.call_args.kwargs["sender_gateway"])
            dispatcher = app.background_tasks[0]
            self.assertTrue(callable(dispatcher.authorize))
            self.assertTrue(dispatcher.authorize(
                tenant_id="tenant-a", principal_id="user-a", job_id="job-1"
            ))
            dispatcher.start()
            self.assertEqual(dispatcher._thread.name, "mcp-events")
            asyncio.run(asyncio.sleep(0.05))
            dispatcher.stop()
            self.assertIsNone(dispatcher._thread)
            self.assertTrue(db_path.exists())
            self.assertEqual(build.call_args.kwargs["events_store"].database, str(db_path))

        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("SO_MCP_EVENTS_KEY_FILE", None)
            os.environ.pop("SO_MCP_EVENTS_DB", None)
            with patch("scripts.run_mcp_server.SecureWorkItemStore", return_value=Store()), patch(
                "scripts.run_mcp_server.JWTVerifier", return_value=object()
            ), patch("scripts.run_mcp_server.MCPToolService", return_value=object()), patch(
                "scripts.run_mcp_server.build_mcp_server", return_value=inner_app
            ) as build:
                app = create_application(config, sender_gateway=Sender())
            self.assertEqual(app.background_tasks, ())
            self.assertIsNone(build.call_args.kwargs["events_store"])

    def test_real_deployed_mcp_server_events_protocol_lifecycle(self):
        async def exercise(app):
            async with app.inner.inner.router.lifespan_context(app.inner.inner):
                status, headers, initialized = await _asgi_rpc(app, {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "deployed-events-poc", "version": "1"},
                    },
                })
                self.assertEqual(status, 200)
                self.assertEqual(initialized["result"]["capabilities"]["events"], {})
                session_id = headers["mcp-session-id"]

                status, _headers, listed = await _asgi_rpc(app, {
                    "jsonrpc": "2.0", "id": 2, "method": "events/list", "params": {},
                }, session_id)
                self.assertEqual(status, 200)
                self.assertEqual(listed["result"]["events"][0]["name"], "chatgpt.job.completed")

                secret = "whsec_" + base64.b64encode(b"w" * 32).decode()
                with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
                    async def heartbeat_tick():
                        await asyncio.sleep(0.01)
                        return time.monotonic()

                    heartbeat_started = time.monotonic()
                    heartbeat = asyncio.create_task(heartbeat_tick())
                    status, _headers, subscribed = await _asgi_rpc(app, {
                        "jsonrpc": "2.0", "id": 3, "method": "events/subscribe",
                        "params": {
                            "name": "chatgpt.job.completed", "arguments": {"job_id": "job-1"},
                            "delivery": {"mode": "webhook", "url": "https://events.example.test/callback", "secret": secret},
                            "ttlMs": None,
                        },
                    }, session_id, disconnect_after=1.0)
                    heartbeat_at = await heartbeat
                    self.assertLess(heartbeat_at - heartbeat_started, 0.15)
                self.assertEqual(status, 200)
                self.assertTrue(subscribed["result"]["id"].startswith("sub_"))
                self.assertIsNone(subscribed["result"]["refreshBefore"])

                status, _headers, denied = await _asgi_rpc(app, {
                    "jsonrpc": "2.0", "id": 33, "method": "events/subscribe",
                    "params": {
                        "name": "chatgpt.job.completed", "arguments": {"job_id": "job-not-owned"},
                        "delivery": {"mode": "webhook", "url": "https://events.example.test/callback", "secret": secret},
                    },
                }, session_id, disconnect_after=1.0)
                self.assertEqual(status, 200)
                self.assertEqual(denied["error"]["code"], -32602)
                self.assertEqual(denied["error"]["data"]["reason"], "job_not_accessible")

                status, _headers, unsubscribed = await _asgi_rpc(app, {
                    "jsonrpc": "2.0", "id": 4, "method": "events/unsubscribe",
                    "params": {
                        "name": "chatgpt.job.completed", "arguments": {"job_id": "job-1"},
                        "delivery": {"mode": "webhook", "url": "https://events.example.test/callback"},
                    },
                }, session_id)
                self.assertEqual(status, 200)
                self.assertEqual(unsubscribed["result"], {})

        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            def slow_post(_url, body, _headers):
                time.sleep(0.2)
                return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()

            store = MCPEventsStore(
                str(Path(tmp) / "events.sqlite3"),
                encryption_key=b"d" * 32,
                post_json=slow_post,
            )
            class Sender:
                def enqueue(self, **_kwargs):
                    return {}
                def list_sessions(self, **_kwargs):
                    return {"items": []}
                def read_session(self, **_kwargs):
                    return {"messages": []}
                def get_job(self, **kwargs):
                    time.sleep(0.2)
                    if kwargs.get("job_id") == "job-not-owned":
                        class Denied(Exception):
                            code = "NOT_FOUND"
                        raise Denied("not found")
                    return {"job_id": kwargs["job_id"]}

            sender = Sender()
            mcp_inner = build_mcp_server(
                MCPToolService(object(), sender_gateway=sender),
                TokenProvider(),
                events_store=store,
                sender_gateway=sender,
            )
            app = DeploymentASGI(
                mcp_inner,
                issuer="https://issuer.example.test/realms/session-ops",
                audience=TokenProvider.audience,
            )
            asyncio.run(exercise(app))


if __name__ == "__main__":
    unittest.main()
