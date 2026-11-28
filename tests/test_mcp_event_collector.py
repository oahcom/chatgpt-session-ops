import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.session_ops.mcp_event_collector import MCPEventCollector
from src.session_ops.mcp_events import MCPEventsStore


class FakeSenderClient:
    def __init__(self, items):
        self.items = list(items)
        self.claims = []
        self.acks = []
        self.fail_ack_once = False

    def claim_events(self, *, worker_id, limit):
        self.claims.append((worker_id, limit))
        return {"items": list(self.items[:limit])}

    def ack_event(self, *, event_id, lease_token):
        self.acks.append((event_id, lease_token))
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise TimeoutError("simulated lost ack")
        self.items = [item for item in self.items if item["event_id"] != event_id]
        return {"acknowledged": True}


class MCPEventCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.store = MCPEventsStore(
            str(Path(self.tmp.name) / "events.sqlite3"),
            encryption_key=b"c" * 32,
            post_json=lambda _url, body, _headers: (
                200,
                json.dumps({"challenge": json.loads(body)["challenge"]}).encode(),
            ),
        )
        self.item = {
            "event_id": "evt_sender-collector-1",
            "job_id": "cgj_12345678-1234-4234-8234-123456789abc",
            "tenant_id": "tenant-a",
            "principal_id": "user-a",
            "name": "chatgpt.job.completed",
            "payload": {
                "job_id": "cgj_12345678-1234-4234-8234-123456789abc",
                "status": "succeeded",
                "summary": "Job completed; fetch the result with chatgpt.job.get.",
                "completed_at": "2026-11-27T12:00:00Z",
            },
            "lease_token": "lease-1",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_persists_event_before_acknowledging_sender(self):
        client = FakeSenderClient([self.item])
        collector = MCPEventCollector(client, self.store, worker_id="mcp-1", batch_size=2)
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.store.subscribe(
                type("Auth", (), {"principal": type("P", (), {"tenant_id": "tenant-a", "principal_id": "user-a"})()})(),
                {
                    "name": "chatgpt.job.completed",
                    "arguments": {"job_id": self.item["job_id"]},
                    "delivery": {
                        "mode": "webhook",
                        "url": "https://events.example.test/callback",
                        "secret": "whsec_" + __import__("base64").b64encode(b"s" * 32).decode(),
                    },
                },
            )
        self.assertEqual(collector.run_once(), 1)
        self.assertEqual(client.claims, [("mcp-1", 2)])
        self.assertEqual(client.acks, [(self.item["event_id"], "lease-1")])
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_outbox WHERE event_id=?", (self.item["event_id"],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM event_deliveries WHERE event_id=?", (self.item["event_id"],)).fetchone()[0], 1)

    def test_lost_ack_replay_is_deduplicated(self):
        client = FakeSenderClient([self.item])
        client.fail_ack_once = True
        collector = MCPEventCollector(client, self.store, worker_id="mcp-1")
        # Ack loss is isolated to this row; a later claim/redelivery is safe.
        self.assertEqual(collector.run_once(), 0)
        self.assertEqual(collector.run_once(), 1)
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_outbox WHERE event_id=?", (self.item["event_id"],)).fetchone()[0], 1)

    def test_failed_terminal_event_is_persisted_before_sender_ack(self):
        failed = {
            **self.item,
            "event_id": "evt_failed_from_sender",
            "job_id": "job-failed",
            "payload": {
                "job_id": "job-failed",
                "status": "failed",
                "summary": "Job failed with error code OUTCOME_UNKNOWN.",
                "completed_at": "2026-11-27T12:00:00Z",
                "error_code": "OUTCOME_UNKNOWN",
                "outcome_unknown": True,
            },
            "lease_token": "lease-failed",
        }
        client = FakeSenderClient([failed])
        collector = MCPEventCollector(client, self.store, worker_id="mcp-failed")
        self.assertEqual(collector.run_once(), 1)
        self.assertEqual(client.acks, [("evt_failed_from_sender", "lease-failed")])
        with self.store._db() as db:
            row = db.execute(
                "SELECT data_json FROM event_outbox WHERE event_id=?",
                ("evt_failed_from_sender",),
            ).fetchone()
        payload = json.loads(row["data_json"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "OUTCOME_UNKNOWN")
        self.assertTrue(payload["outcome_unknown"])

    def test_payload_job_id_mismatch_is_rejected_before_ack(self):
        bad = {**self.item, "payload": {**self.item["payload"], "job_id": "another-job"}}
        client = FakeSenderClient([bad])
        collector = MCPEventCollector(client, self.store, worker_id="mcp-1")
        self.assertEqual(collector.run_once(), 0)
        self.assertEqual(client.acks, [])


    def test_poison_event_does_not_block_later_valid_event(self):
        bad = {**self.item, "payload": {**self.item["payload"], "job_id": "wrong-job"}}
        good = {**self.item, "event_id": "evt_valid_after_poison"}
        client = FakeSenderClient([bad, good])
        collector = MCPEventCollector(client, self.store, worker_id="mcp-isolated")
        self.assertEqual(collector.run_once(), 1)
        self.assertEqual(client.acks, [(good["event_id"], good["lease_token"])])
        with self.store._db() as db:
            self.assertIsNone(db.execute("SELECT event_id FROM event_outbox WHERE event_id=?", (bad["event_id"],)).fetchone())
            self.assertIsNotNone(db.execute("SELECT event_id FROM event_outbox WHERE event_id=?", (good["event_id"],)).fetchone())


    def test_default_claim_is_bounded_to_two_events(self):
        client = FakeSenderClient([self.item])
        collector = MCPEventCollector(client, self.store, worker_id="mcp-default")
        self.assertEqual(collector.run_once(), 1)
        self.assertEqual(client.claims, [("mcp-default", 2)])

    def test_claim_batch_above_lease_budget_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "1..2"):
            MCPEventCollector(FakeSenderClient([]), self.store, worker_id="mcp-large", batch_size=3)


if __name__ == "__main__":
    unittest.main()
