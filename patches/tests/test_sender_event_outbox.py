import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from http.server import ThreadingHTTPServer

from sender_gateway.app import make_handler
from sender_gateway.job_store import JobStore
from sender_gateway.models import add_seconds
from session_ops.sender_gateway_client import SenderGatewayClient, EVENT_DISPATCHER_IDENTITY

FIXED = "2026-11-27T12:00:00.000000Z"


class SenderOutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.db_path = Path(self.tmp.name) / "sender.sqlite3"
        self.store = JobStore(self.db_path, now=lambda: FIXED)
        job = self.store.enqueue(
            principal_id="user-a",
            tenant_id="tenant-a",
            operation="session.send",
            idempotency_key=str(uuid.uuid4()),
            payload={"conversation_id": "a" * 36, "prompt": "do work", "account": "acct1"},
        )
        self.job_id = job["job_id"]
        self.store.claim_next("worker-1", now=FIXED)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def complete(self):
        self.store.complete(self.job_id, {"summary": "Finished"}, now=FIXED)

    def create_job(self, *, now):
        store = JobStore(self.db_path, now=lambda: now)
        job = store.enqueue(
            principal_id="user-a",
            tenant_id="tenant-a",
            operation="session.send",
            idempotency_key=str(uuid.uuid4()),
            payload={"conversation_id": str(uuid.uuid4()), "prompt": "do work", "account": "acct1"},
        )
        store.claim_next("worker-retention", now=now)
        store.close()
        return job["job_id"]

    def event_rows(self):
        return self.store._connection().execute(
            "SELECT * FROM job_event_outbox ORDER BY created_at"
        ).fetchall()

    def test_completion_and_outbox_are_atomic_and_idempotent(self):
        conn = self.store._connection()
        conn.execute("""CREATE TRIGGER reject_event BEFORE INSERT ON job_event_outbox
                        BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete()
        job = self.store.get("user-a", "tenant-a", self.job_id)
        self.assertEqual(job["status"], "running")
        self.assertEqual(len(self.event_rows()), 0)

        conn.execute("DROP TRIGGER reject_event")
        self.complete()
        self.complete()  # terminal transition is idempotent
        job = self.store.get("user-a", "tenant-a", self.job_id)
        self.assertEqual(job["status"], "succeeded")
        rows = self.event_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tenant_id"], "tenant-a")
        self.assertEqual(rows[0]["principal_id"], "user-a")
        self.assertEqual(rows[0]["event_name"], "chatgpt.job.completed")
        self.assertEqual(json.loads(rows[0]["payload_json"])["summary"], "Finished")
        expected_id = "evt_" + hashlib.sha256(self.job_id.encode()).hexdigest()[:32]
        self.assertEqual(rows[0]["event_id"], expected_id)

    def test_failure_and_outbox_are_atomic_and_include_outcome_unknown(self):
        conn = self.store._connection()
        conn.execute("""CREATE TRIGGER reject_failed_event BEFORE INSERT ON job_event_outbox
                        BEGIN SELECT RAISE(ABORT, 'injected failed outbox failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.fail(self.job_id, "OUTCOME_UNKNOWN", now=FIXED)
        job = self.store.get("user-a", "tenant-a", self.job_id)
        self.assertEqual(job["status"], "running")
        self.assertEqual(len(self.event_rows()), 0)

        conn.execute("DROP TRIGGER reject_failed_event")
        self.store.fail(self.job_id, "OUTCOME_UNKNOWN", now=FIXED)
        self.store.fail(self.job_id, "OUTCOME_UNKNOWN", now=FIXED)  # idempotent
        job = self.store.get("user-a", "tenant-a", self.job_id)
        self.assertEqual(job["status"], "failed")
        rows = self.event_rows()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "OUTCOME_UNKNOWN")
        self.assertTrue(payload["outcome_unknown"])
        self.assertEqual(rows[0]["tenant_id"], "tenant-a")
        self.assertEqual(rows[0]["principal_id"], "user-a")

    def test_event_outbox_retention_prunes_only_terminal_rows_and_keeps_pending_or_active_lease(self):
        old = add_seconds(FIXED, -31 * 24 * 60 * 60)
        with self.store._connection() as conn:
            for event_id, job_id, status, created_at, delivered_at, lease_until, lease_token in (
                ("evt_old_pending", "job_old_pending", "pending", old, None, None, None),
                ("evt_old_sending", "job_old_sending", "sending", old, None, add_seconds(FIXED, 60), "lease"),
                ("evt_old_delivered", "job_old_delivered", "delivered", old, old, None, None),
                ("evt_current_pending", "job_current_pending", "pending", FIXED, None, None, None),
            ):
                conn.execute(
                    """INSERT INTO job_event_outbox
                       (event_id, job_id, tenant_id, principal_id, event_name, payload_json,
                        created_at, status, attempts, next_attempt_at, delivered_at,
                        lease_until, lease_token, worker_id)
                       VALUES (?, ?, 'tenant-a', 'user-a', 'chatgpt.job.completed',
                               '{"job_id":"x"}', ?, ?, 0, ?, ?, ?, ?, NULL)""",
                    (event_id, job_id, created_at, status, created_at, delivered_at, lease_until, lease_token),
                )
        with self.store.transaction() as txn:
            deleted = self.store._cleanup_event_outbox_txn(txn, FIXED)
        self.assertEqual(deleted, 1)  # only terminal history is pruned
        with self.store._connection() as conn:
            rows = conn.execute(
                "SELECT job_id, status FROM job_event_outbox ORDER BY job_id"
            ).fetchall()
        self.assertEqual(
            {row["job_id"]: row["status"] for row in rows},
            {
                "job_old_pending": "pending",
                "job_old_sending": "sending",
                "job_current_pending": "pending",
            },
        )

    def test_claim_lease_expiry_stale_ack_and_idempotent_ack(self):
        self.complete()
        first = self.store.claim_events("mcp-a", limit=10, lease_seconds=10, now=FIXED)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["tenant_id"], "tenant-a")
        self.assertEqual(first[0]["principal_id"], "user-a")
        self.assertEqual(first[0]["payload"]["job_id"], self.job_id)
        self.assertEqual(self.store.claim_events("mcp-b", now="2026-11-27T12:00:05Z"), [])

        second = self.store.claim_events("mcp-b", now="2026-11-27T12:00:11Z")
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["lease_token"], second[0]["lease_token"])
        self.assertFalse(self.store.ack_event(first[0]["event_id"], first[0]["lease_token"], now="2026-11-27T12:00:12Z"))
        self.assertTrue(self.store.ack_event(second[0]["event_id"], second[0]["lease_token"], now="2026-11-27T12:00:12Z"))
        self.assertTrue(self.store.ack_event(second[0]["event_id"], "repeated-ack", now="2026-11-27T12:00:13Z"))
        self.assertEqual(self.store.claim_events("mcp-c", now="2026-11-27T12:00:20Z"), [])

    def test_claim_and_ack_routes_require_the_dedicated_dispatcher_identity(self):
        self.complete()

        class FakeVerifier:
            def verify(self, handler):
                return handler.headers.get("X-Test-Principal", ""), handler.headers.get("X-Test-Tenant", "")

        app = SimpleNamespace(store=self.store, verifier=FakeVerifier())
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post(path, body, principal, tenant):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                raw = json.dumps(body).encode()
                conn.request("POST", path, body=raw, headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw)),
                    "X-Test-Principal": principal,
                    "X-Test-Tenant": tenant,
                })
                response = conn.getresponse()
                payload = json.loads(response.read())
                conn.close()
                return response.status, payload

            status, _ = post("/internal/v1/events/claim", {"worker_id": "x"}, "user-a", "tenant-a")
            self.assertEqual(status, 403)

            status, claimed = post(
                "/internal/v1/events/claim", {"worker_id": "mcp-1", "limit": 10},
                EVENT_DISPATCHER_IDENTITY["principal_id"], EVENT_DISPATCHER_IDENTITY["tenant_id"],
            )
            self.assertEqual(status, 200)
            event = claimed["items"][0]
            status, acked = post(
                f"/internal/v1/events/{event['event_id']}/ack", {"lease_token": event["lease_token"]},
                EVENT_DISPATCHER_IDENTITY["principal_id"], EVENT_DISPATCHER_IDENTITY["tenant_id"],
            )
            self.assertEqual(status, 200)
            self.assertTrue(acked["acknowledged"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_handler_closes_thread_local_sqlite_connection_after_each_request(self):
        class FakeVerifier:
            def verify(self, handler):
                return "user-a", "tenant-a"

        app = SimpleNamespace(store=self.store, verifier=FakeVerifier())
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        baseline = len(self.store._connections)
        try:
            for _ in range(40):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                conn.request("GET", f"/internal/v1/jobs/{self.job_id}")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                conn.close()
                deadline = time.monotonic() + 1.0
                while len(self.store._connections) > baseline and time.monotonic() < deadline:
                    time.sleep(0.005)
                # Wait after every request so an unfinished handler cannot
                # hide a per-request connection leak behind the next request.
                self.assertEqual(len(self.store._connections), baseline)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_mcp_client_uses_service_identity_for_claim_and_ack(self):
        client = object.__new__(SenderGatewayClient)
        client._base_path = "/gateway"
        client.read_timeout_seconds = 8.0
        client.timeout_seconds = 2.0
        client._request = Mock(return_value={"items": []})

        client.claim_events(worker_id="mcp-1", limit=7)
        args, kwargs = client._request.call_args
        self.assertEqual(args[0:2], ("POST", "/gateway/internal/v1/events/claim"))
        self.assertEqual(args[2], {"worker_id": "mcp-1", "limit": 7})
        self.assertEqual(args[3], EVENT_DISPATCHER_IDENTITY)

        client.ack_event(event_id="evt_" + "a" * 32, lease_token="lease-token")
        args, kwargs = client._request.call_args
        self.assertEqual(args[0:2], ("POST", "/gateway/internal/v1/events/evt_" + "a" * 32 + "/ack"))
        self.assertEqual(args[2], {"lease_token": "lease-token"})
        self.assertEqual(args[3], EVENT_DISPATCHER_IDENTITY)


if __name__ == "__main__":
    unittest.main()
