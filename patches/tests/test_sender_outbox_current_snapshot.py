from __future__ import annotations

import http.client
import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from sender_gateway.app import make_handler
from sender_gateway.job_store import JobStore
from sender_gateway.models import ErrorCode, add_seconds
from session_ops.sender_gateway_client import SenderGatewayClient, EVENT_DISPATCHER_IDENTITY


class TestSenderOutboxCurrentSnapshot:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sender.sqlite3"
        self.store = JobStore(self.db)

    def teardown_method(self):
        self.store.close()
        self.tmp.cleanup()

    def enqueue_running(self, key="k1"):
        job = self.store.enqueue(principal_id="p1", tenant_id="t1",
            operation="session.create", idempotency_key=key, payload={"prompt": "x"})
        assert self.store.claim_next("worker") is not None
        return job["job_id"]

    def test_success_and_failure_terminal_events_are_atomic_and_deduplicated(self):
        succeeded = self.enqueue_running("success")
        self.store.complete(succeeded, {"private_result": "not included"})
        with self.store.transaction() as tx:
            row = tx.fetchone("SELECT event_id, payload_json FROM job_event_outbox WHERE job_id=?", (succeeded,))
            assert row is not None
            assert "private_result" not in row["payload_json"]
            assert json.loads(row["payload_json"])["status"] == "succeeded"
        self.store.complete(succeeded, {"ignored": True})
        with self.store.transaction() as tx:
            assert tx.fetchone("SELECT COUNT(*) AS n FROM job_event_outbox WHERE job_id=?", (succeeded,))["n"] == 1

        failed = self.enqueue_running("failed")
        self.store.fail(failed, ErrorCode.DEPENDENCY_UNAVAILABLE.value, outcome_unknown=True)
        with self.store.transaction() as tx:
            row = tx.fetchone("SELECT payload_json FROM job_event_outbox WHERE job_id=?", (failed,))
            payload = json.loads(row["payload_json"])
            assert payload["status"] == "failed" and payload["outcome_unknown"] is True

    def test_event_insert_failure_rolls_back_terminal_job_transition(self):
        job_id = self.enqueue_running()
        with self.store.transaction() as tx:
            tx.execute("CREATE TRIGGER reject_event BEFORE INSERT ON job_event_outbox BEGIN SELECT RAISE(ABORT, 'test'); END")
        try:
            self.store.complete(job_id, {"ok": True})
        except Exception:
            pass
        else:
            raise AssertionError("expected outbox insert to abort completion")
        with self.store.transaction() as tx:
            assert tx.fetchone("SELECT status FROM jobs WHERE job_id=?", (job_id,))["status"] == "running"

    def test_claim_ack_and_dispatcher_identity_gate(self):
        job_id = self.enqueue_running()
        self.store.complete(job_id, {"ok": True})
        class Verifier:
            def verify(self, handler):
                return handler.headers.get("X-P", ""), handler.headers.get("X-T", "")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(SimpleNamespace(store=self.store, verifier=Verifier())))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            def post(path, body, identity):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                raw = json.dumps(body).encode()
                conn.request("POST", path, raw, {"Content-Type":"application/json", "Content-Length":str(len(raw)), "X-P":identity["principal_id"], "X-T":identity["tenant_id"]})
                resp=conn.getresponse(); data=json.loads(resp.read()); conn.close(); return resp.status,data
            assert post("/internal/v1/events/claim", {"worker_id":"x"}, {"principal_id":"user","tenant_id":"t1"})[0] == 403
            status, data = post("/internal/v1/events/claim", {"worker_id":"mcp","limit":1}, EVENT_DISPATCHER_IDENTITY)
            assert status == 200 and len(data["items"]) == 1
            item=data["items"][0]
            status, data = post(f"/internal/v1/events/{item['event_id']}/ack", {"lease_token":item["lease_token"]}, EVENT_DISPATCHER_IDENTITY)
            assert status == 200 and data["acknowledged"] is True
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_mcp_client_uses_per_event_ack_path_and_dispatcher_identity(self):
        client=object.__new__(SenderGatewayClient); client._base_path="/gateway"; client.read_timeout_seconds=8.0; client._request=Mock(return_value={"acknowledged":True})
        client.claim_events(worker_id="mcp", limit=1)
        args=client._request.call_args.args
        assert args[1] == "/gateway/internal/v1/events/claim" and args[3] == EVENT_DISPATCHER_IDENTITY
        event_id="evt_"+"a"*32
        client.ack_event(event_id=event_id, lease_token="lease")
        args=client._request.call_args.args
        assert args[1] == f"/gateway/internal/v1/events/{event_id}/ack" and args[3] == EVENT_DISPATCHER_IDENTITY

    def test_expired_and_reclaimed_event_leases_reject_stale_ack(self):
        job_id = self.enqueue_running()
        self.store.complete(job_id, {"ok": True})
        first = self.store.claim_events("collector-a", limit=1)[0]
        expired_at = add_seconds(self.store.now(), 31)
        assert self.store.ack_event(first["event_id"], first["lease_token"], now=expired_at) is False
        second = self.store.claim_events("collector-b", limit=1, now=expired_at)[0]
        assert second["lease_token"] != first["lease_token"]
        assert self.store.ack_event(first["event_id"], first["lease_token"], now=add_seconds(expired_at, 1)) is False
        assert self.store.ack_event(second["event_id"], second["lease_token"], now=add_seconds(expired_at, 1)) is True
