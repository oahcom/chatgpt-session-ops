"""Protocol and storage tests for the isolated ChatGPT MCP Events PoC."""
import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.session_ops.mcp_events import CallbackEndpointError, MCPEventsStore, _secret_key


class FakeCallback:
    def __init__(self):
        self.requests = []
        self.fail_statuses = []

    def post(self, url, body, headers):
        self.requests.append((url, body, dict(headers)))
        if self.fail_statuses:
            return self.fail_statuses.pop(0), b"{}"
        payload = json.loads(body)
        if payload.get("type") == "verification":
            return 200, json.dumps({"challenge": payload["challenge"]}).encode()
        return 204, b""


class MCPEventsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.db = str(Path(self.tmp.name) / "events.sqlite3")
        self.callback = FakeCallback()
        self.store = MCPEventsStore(self.db, post_json=self.callback.post, encryption_key=b"k" * 32)
        self.auth = SimpleNamespace(
            principal=SimpleNamespace(tenant_id="tenant-a", principal_id="issuer:user-a")
        )
        self.delivery = {
            "mode": "webhook",
            "url": "https://events.example.test/callback/abc",
            "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def subscribe(self, **kwargs):
        kwargs.setdefault("conversation_id", "12345678-1234-4234-8234-123456789abc")
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            return self.store.subscribe(self.auth, {
                "name": "chatgpt.job.completed",
                "arguments": kwargs,
                "delivery": self.delivery,
            })

    def publish_job_completed(self, *args, **kwargs):
        kwargs.setdefault("conversation_id", "12345678-1234-4234-8234-123456789abc")
        return self.store.publish_job_completed(*args, **kwargs)

    def test_event_catalog_requires_destination_conversation_and_optional_job_filter(self):
        definition = self.store.event_catalog()[0]
        properties = definition["payloadSchema"]["properties"]
        required = definition["payloadSchema"]["required"]
        self.assertEqual(properties["conversation_id"], {"type": "string"})
        input_property = definition["inputSchema"]["properties"]["conversation_id"]
        self.assertEqual(input_property["minLength"], 36)
        self.assertEqual(input_property["maxLength"], 36)
        self.assertEqual(
            input_property["pattern"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        self.assertIn("conversation_id", required)
        input_required = definition["inputSchema"]["required"]
        self.assertNotIn("job_id", input_required)
        self.assertNotIn("conversation_ids", properties)
        self.assertIn("owner", input_property["description"])

    def test_subscribe_rejects_noncanonical_conversation_id_before_callback(self):
        with self.assertRaisesRegex(ValueError, "canonical lowercase UUID"):
            self.subscribe(conversation_id="not-a-session-id")
        self.assertEqual(self.callback.requests, [])

    def test_subscribe_verifies_challenge_and_persists_subscription(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            result = self.subscribe(job_id="task-1")
        self.assertTrue(result["id"].startswith("sub_"))
        self.assertTrue(result["refreshBefore"].endswith("Z"))
        self.assertEqual(len(self.callback.requests), 1)
        _, body, headers = self.callback.requests[0]
        self.assertEqual(json.loads(body)["type"], "verification")
        self.assertTrue(headers["webhook-id"].startswith("msg_verification_"))
        self.assertTrue(headers["webhook-signature"].startswith("v1,"))
        self.assertEqual(headers["X-MCP-Subscription-Id"], result["id"])
        with self.store._db() as db:
            stored = db.execute("SELECT secret_enc FROM event_subscriptions").fetchone()[0]
        self.assertNotIn(self.delivery["secret"].encode(), bytes(stored))

    def test_database_rejects_a_replacement_encryption_key(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            subscription = self.subscribe()
        self.assertTrue(subscription["id"].startswith("sub_"))
        with self.store._db() as db:
            before = db.execute(
                "SELECT active, secret_enc FROM event_subscriptions WHERE id=?",
                (subscription["id"],),
            ).fetchone()
            key_check_before = db.execute(
                "SELECT key_check FROM event_key_metadata WHERE singleton=1"
            ).fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "encryption key does not match the database"):
            MCPEventsStore(self.db, post_json=self.callback.post, encryption_key=b"x" * 32)
        with self.store._db() as db:
            after = db.execute(
                "SELECT active, secret_enc FROM event_subscriptions WHERE id=?",
                (subscription["id"],),
            ).fetchone()
            key_check_after = db.execute(
                "SELECT key_check FROM event_key_metadata WHERE singleton=1"
            ).fetchone()[0]
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(key_check_after, key_check_before)

    def test_legacy_database_checks_existing_secrets_before_recording_key(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        with self.store._db() as db:
            db.execute("DROP TABLE event_key_metadata")

        with self.assertRaisesRegex(RuntimeError, "does not decrypt existing subscriptions"):
            MCPEventsStore(self.db, post_json=self.callback.post, encryption_key=b"x" * 32)

        restored = MCPEventsStore(self.db, post_json=self.callback.post, encryption_key=b"k" * 32)
        with restored._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_key_metadata").fetchone()[0], 1)

    def test_sqlite_backup_restores_subscriptions_and_callback_secrets_with_same_key(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        self.store.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-backup", "Finished",
            event_id="evt_backup_restore",
        )

        backup_path = str(Path(self.tmp.name) / "restored" / "events.sqlite3")
        Path(backup_path).parent.mkdir(mode=0o700)
        source = sqlite3.connect(self.db)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.chmod(backup_path, 0o600)

        restored_callback = FakeCallback()
        restored = MCPEventsStore(
            backup_path,
            post_json=restored_callback.post,
            encryption_key=b"k" * 32,
        )
        self.assertEqual(restored.deliver_pending(), 1)
        event_requests = [
            (body, headers)
            for _url, body, headers in restored_callback.requests
            if json.loads(body).get("type") != "verification"
        ]
        self.assertEqual(len(event_requests), 1)
        body, headers = event_requests[0]
        self.assertEqual(json.loads(body)["data"]["job_id"], "job-backup")
        self.assertTrue(headers["webhook-signature"].startswith("v1,"))

    def test_lost_key_recovery_discards_subscriptions_but_preserves_workitem_events(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        self.store.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-key-loss", "Finished",
            event_id="evt_key_loss",
        )
        with self.store._db() as db:
            db.execute("DELETE FROM event_deliveries")
            db.execute("DELETE FROM event_subscriptions")
            db.execute("DELETE FROM callback_verifications")
            db.execute("DELETE FROM event_key_metadata")

        recovered = MCPEventsStore(self.db, post_json=self.callback.post, encryption_key=b"n" * 32)
        with recovered._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_subscriptions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM event_deliveries").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM callback_verifications").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM event_key_metadata").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM event_outbox WHERE event_id='evt_key_loss'").fetchone()[0], 1)

    def test_health_snapshot_reports_aggregate_delivery_metrics_without_identifiers(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-health", "Finished",
            event_id="evt_health_private",
        )
        self.assertEqual(self.store.deliver_pending(), 1)

        snapshot = self.store.health_snapshot()
        self.assertEqual(snapshot["outbox"]["delivered"], 1)
        self.assertEqual(snapshot["outbox"]["pending"], 0)
        self.assertEqual(snapshot["subscriptions"]["active"], 1)
        self.assertEqual(snapshot["callbacks"]["callback_count"], 2)
        self.assertEqual(snapshot["callbacks"]["callback_status_2xx"], 2)
        self.assertGreaterEqual(snapshot["callbacks"]["callback_latency_ms_total"], 0)
        self.assertGreater(snapshot["storage"]["database_bytes"], 0)
        serialized = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn("evt_health_private", serialized)
        self.assertNotIn("job-health", serialized)
        self.assertNotIn("callback_url", serialized)
        self.assertNotIn(self.delivery["url"], serialized)

    def test_callback_challenge_mismatch_rejects_subscription(self):
        self.store.post_json = lambda url, body, headers: (200, b'{"challenge":"wrong"}')
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            with self.assertRaises(CallbackEndpointError) as caught:
                self.subscribe()
        self.assertEqual(caught.exception.reason, "challenge_failed")

    def test_cleanup_expires_subscriptions_and_prunes_old_history(self):
        now = [1_800_000_000]
        tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        try:
            callback = FakeCallback()
            store = MCPEventsStore(
                str(Path(tmp.name) / "events.sqlite3"),
                post_json=callback.post,
                encryption_key=b"c" * 32,
                clock=lambda: now[0],
            )
            auth = SimpleNamespace(principal=SimpleNamespace(
                tenant_id="tenant-a", principal_id="issuer:user-a"
            ))
            delivery = {
                "mode": "webhook",
                "url": "https://events.example.test/cleanup",
                "secret": "whsec_" + base64.b64encode(b"z" * 32).decode(),
            }
            with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
                finite = store.subscribe(auth, {
                    "name": "chatgpt.job.completed",
                    "arguments": {"job_id": "job-cleanup", "conversation_id": "12345678-1234-4234-8234-123456789abc"},
                    "delivery": delivery,
                    "ttlMs": 60_000,
                })
                permanent = store.subscribe(auth, {
                    "name": "chatgpt.job.completed",
                    "arguments": {"conversation_id": "12345678-1234-4234-8234-123456789abc"},
                    "delivery": {
                        **delivery,
                        "url": "https://events.example.test/permanent",
                    },
                    "ttlMs": None,
                })
            event_id = store.publish_job_completed(
                "tenant-a", "issuer:user-a", "job-cleanup", "Finished",
                event_id="evt_cleanup_old",
                completed_at="2027-01-01T00:00:00Z",
                conversation_id="12345678-1234-4234-8234-123456789abc",
            )
            self.assertEqual(store.deliver_pending(), 2)
            now[0] += 31 * 24 * 60 * 60
            result = store.cleanup_expired()
            self.assertEqual(result["expired_subscriptions"], 1)
            with store._db() as db:
                self.assertIsNone(db.execute(
                    "SELECT id FROM event_subscriptions WHERE id=?", (finite["id"],)
                ).fetchone())
                self.assertIsNotNone(db.execute(
                    "SELECT id FROM event_subscriptions WHERE id=?", (permanent["id"],)
                ).fetchone())
                self.assertIsNone(db.execute(
                    "SELECT event_id FROM event_outbox WHERE event_id=?", (event_id,)
                ).fetchone())
                self.assertEqual(db.execute("SELECT count(*) FROM callback_verifications").fetchone()[0], 0)
        finally:
            tmp.cleanup()

    def test_delivery_is_blocked_when_job_access_has_been_revoked(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-revoked")
        event_id = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-revoked", "Finished",
            event_id="evt_revoked_job",
        )
        checks = []
        def authorize(**kwargs):
            checks.append(kwargs)
            return False
        self.assertEqual(self.store.deliver_pending(authorize=authorize), 0)
        self.assertEqual(checks, [{
            "tenant_id": "tenant-a",
            "principal_id": "issuer:user-a",
            "job_id": "job-revoked",
        }])
        self.assertEqual(len(self.callback.requests), 1)  # challenge only; event never sent
        with self.store._db() as db:
            row = db.execute(
                "SELECT status, last_status FROM event_deliveries WHERE event_id=?",
                (event_id,),
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_status"], 403)

    def test_subscription_expiry_during_authorization_prevents_callback(self):
        now = [1_800_000_000]
        self.store.clock = lambda: now[0]
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            subscription = self.store.subscribe(self.auth, {
                "name": "chatgpt.job.completed",
                "arguments": {"job_id": "job-expires-during-auth", "conversation_id": "12345678-1234-4234-8234-123456789abc"},
                "delivery": self.delivery,
                "ttlMs": 60_000,
            })
        event_id = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-expires-during-auth", "Finished",
            event_id="evt_expiry_during_auth",
        )
        def authorize(**_kwargs):
            now[0] += 61
            return True
        self.assertEqual(self.store.deliver_pending(authorize=authorize), 0)
        self.assertEqual(len(self.callback.requests), 1)  # challenge only; no event POST
        with self.store._db() as db:
            row = db.execute(
                "SELECT status, last_status FROM event_deliveries WHERE event_id=? AND subscription_id=?",
                (event_id, subscription["id"]),
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_status"], 410)

    def test_transient_authorization_check_failure_retries_without_delivery(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-auth-transient")
        event_id = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-auth-transient", "Finished",
            event_id="evt_auth_transient",
        )
        def authorize(**_kwargs):
            raise RuntimeError("sender gateway unavailable")
        self.assertEqual(self.store.deliver_pending(authorize=authorize), 0)
        self.assertEqual(len(self.callback.requests), 1)
        with self.store._db() as db:
            row = db.execute(
                "SELECT status, attempts, last_status, next_attempt_at FROM event_deliveries WHERE event_id=?",
                (event_id,),
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_status"], 599)
        self.assertGreater(row["next_attempt_at"], 0)

    def test_failed_terminal_event_is_delivered_with_error_metadata(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-failed")
        event_id = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-failed", "Job failed with error code OUTCOME_UNKNOWN.",
            event_id="evt_failed_terminal", completed_at="2026-11-27T12:00:00Z",
            status="failed", error_code="OUTCOME_UNKNOWN", outcome_unknown=True,
        )
        self.assertEqual(self.store.deliver_pending(), 1)
        event = json.loads(self.callback.requests[-1][1])
        self.assertEqual(event["eventId"], event_id)
        self.assertEqual(event["data"]["status"], "failed")
        self.assertEqual(event["data"]["error_code"], "OUTCOME_UNKNOWN")
        self.assertTrue(event["data"]["outcome_unknown"])

    def test_subscribe_backfills_completion_that_arrived_before_subscription(self):
        # Simulate the fast-job race: Sender event has already reached the MCP
        # event store before ChatGPT's events/subscribe request is persisted.
        event_ids = []
        original_verify = self.store._verify_callback
        def complete_during_subscribe(*args):
            original_verify(*args)
            event_ids.append(self.publish_job_completed(
                "tenant-a", "issuer:user-a", "task-fast", "Finished",
                event_id="evt_fast_job_before_subscribe",
            ))
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            with patch.object(self.store, "_verify_callback", side_effect=complete_during_subscribe):
                self.subscribe(job_id="task-fast")
        event_id = event_ids[0]
        with self.store._db() as db:
            row = db.execute(
                "SELECT status FROM event_deliveries WHERE event_id=?",
                (event_id,),
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.store.deliver_pending(), 1)

    def test_subscribe_backfill_is_owner_and_job_filter_scoped(self):
        event_ids = []
        original_verify = self.store._verify_callback
        def publish_racing_events(*args):
            original_verify(*args)
            event_ids.append(self.publish_job_completed(
                "tenant-a", "issuer:user-a", "task-match", "Finished", event_id="evt_own_match"
            ))
            self.publish_job_completed(
                "tenant-a", "issuer:user-a", "task-other", "Finished", event_id="evt_own_other"
            )
            self.publish_job_completed(
                "tenant-b", "issuer:user-a", "task-match", "Finished", event_id="evt_other_tenant"
            )
            self.publish_job_completed(
                "tenant-a", "issuer:other-user", "task-match", "Finished", event_id="evt_other_principal"
            )
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            with patch.object(self.store, "_verify_callback", side_effect=publish_racing_events):
                self.subscribe(job_id="task-match")
        with self.store._db() as db:
            ids = [row[0] for row in db.execute("SELECT event_id FROM event_deliveries ORDER BY event_id")]
        self.assertEqual(ids, event_ids)

    def test_subscribe_backfill_does_not_replay_events_completed_before_request(self):
        now = int(self.store.clock())
        old_event = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "task-old", "Finished",
            event_id="evt_old_completion",
            completed_at="2026-11-25T00:00:00Z",
        )
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="task-old")
        with self.store._db() as db:
            count = db.execute(
                "SELECT count(*) FROM event_deliveries WHERE event_id=?", (old_event,)
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_event_is_persisted_then_delivered_with_standard_webhook_signature(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="task-1")
        event_id = self.publish_job_completed("tenant-a", "issuer:user-a", "task-1", "Finished")
        self.assertEqual(self.store.deliver_pending(), 1)
        _, body, headers = self.callback.requests[-1]
        event = json.loads(body)
        self.assertEqual(event["eventId"], event_id)
        self.assertEqual(event["name"], "chatgpt.job.completed")
        self.assertEqual(event["data"]["job_id"], "task-1")
        self.assertEqual(headers["webhook-id"], event_id)
        self.assertEqual(headers["X-MCP-Subscription-Id"], self.subscribe_id())
        self.assertTrue(headers["webhook-signature"].startswith("v1,"))

    def subscribe_id(self):
        with self.store._db() as db:
            return db.execute("SELECT id FROM event_subscriptions").fetchone()[0]

    def test_retry_preserves_event_id_and_terminal_statuses_are_not_retried(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        event_id = self.publish_job_completed("tenant-a", "issuer:user-a", "task-2", "Finished")
        self.callback.fail_statuses = [503, 204]
        self.assertEqual(self.store.deliver_pending(), 0)
        with self.store._db() as db:
            db.execute("UPDATE event_deliveries SET next_attempt_at=0 WHERE event_id=?", (event_id,))
        self.assertEqual(self.store.deliver_pending(), 1)
        delivered_event_ids = [json.loads(item[1])["eventId"] for item in self.callback.requests if b'"eventId"' in item[1]]
        self.assertEqual(delivered_event_ids, [event_id, event_id])

    def test_secret_refresh_uses_dual_signature_during_rotation_window(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            first = self.subscribe(job_id="rotation-task")
            old_secret = self.delivery["secret"]
            new_secret = "whsec_" + base64.b64encode(b"n" * 32).decode()
            refreshed = self.store.subscribe(self.auth, {
                "name": "chatgpt.job.completed",
                "arguments": {"job_id": "rotation-task", "conversation_id": "12345678-1234-4234-8234-123456789abc"},
                "delivery": {**self.delivery, "secret": new_secret},
            })
        self.assertEqual(first["id"], refreshed["id"])
        with self.store._db() as db:
            row = db.execute("SELECT secret_enc, secret_prev_enc, secret_prev_until FROM event_subscriptions").fetchone()
        self.assertEqual(self.store._decrypt_secret(refreshed["id"], row["secret_enc"]), new_secret)
        self.assertEqual(self.store._decrypt_secret(refreshed["id"], row["secret_prev_enc"]), old_secret)
        self.assertGreater(row["secret_prev_until"], int(self.store.clock()))
        self.publish_job_completed("tenant-a", "issuer:user-a", "rotation-task", "Finished")
        self.assertEqual(self.store.deliver_pending(), 1)
        signatures = self.callback.requests[-1][2]["webhook-signature"].split()
        self.assertEqual(len(signatures), 2)
        self.assertTrue(all(item.startswith("v1,") for item in signatures))

    def test_callback_timeout_has_protocol_reason(self):
        self.store.post_json = lambda *_args: (_ for _ in ()).throw(TimeoutError("timeout"))
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            with self.assertRaises(CallbackEndpointError) as caught:
                self.subscribe()
        self.assertEqual(caught.exception.reason, "timeout")

    def test_task_filter_only_delivers_matching_task(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="only-this-task")
        self.publish_job_completed("tenant-a", "issuer:user-a", "another-task", "Finished")
        self.assertEqual(self.store.deliver_pending(), 0)
        self.publish_job_completed("tenant-a", "issuer:user-a", "only-this-task", "Finished")
        self.assertEqual(self.store.deliver_pending(), 1)

    def test_replayed_sender_event_is_idempotent_and_owner_bound(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-replay")
        args = ("tenant-a", "issuer:user-a", "job-replay", "Finished")
        event_id = self.publish_job_completed(
            *args, event_id="evt_sender-stable-1", completed_at="2026-11-27T12:00:00Z"
        )
        self.assertEqual(
            self.publish_job_completed(
                *args, event_id="evt_sender-stable-1", completed_at="2026-11-27T12:00:00Z"
            ),
            event_id,
        )
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_outbox WHERE event_id=?", (event_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM event_deliveries WHERE event_id=?", (event_id,)).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "different event"):
            self.publish_job_completed(
                "tenant-b", "issuer:user-b", "job-replay", "Finished",
                event_id=event_id, completed_at="2026-11-27T12:00:00Z",
            )

    def test_concurrent_delivery_workers_claim_each_event_once(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        self.publish_job_completed("tenant-a", "issuer:user-a", "task-concurrent", "Finished")
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_post(url, body, headers):
            if b'"eventId"' in body:
                calls.append((url, body, headers))
                entered.set()
                release.wait(timeout=2)
                return 204, b""
            payload = json.loads(body)
            return 200, json.dumps({"challenge": payload["challenge"]}).encode()

        self.store.post_json = blocking_post
        first_result = []
        first = threading.Thread(target=lambda: first_result.append(self.store.deliver_pending()))
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        self.assertEqual(self.store.deliver_pending(), 0)
        release.set()
        first.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_result, [1])
        self.assertEqual(len(calls), 1)

    def test_lease_reclaim_between_validation_and_renewal_prevents_callback(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-lease-race")
        self.publish_job_completed("tenant-a", "issuer:user-a", "job-lease-race", "Finished")
        self.callback.requests.clear()
        original_db = self.store._db

        class InterleavingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, params=()):
                if sql.lstrip().startswith("UPDATE event_deliveries SET lease_until=?"):
                    # Simulate another worker reclaiming the lease after this
                    # worker's old SELECT snapshot but before its renewal.
                    with original_db() as competing:
                        competing.execute(
                            "UPDATE event_deliveries SET lease_token='reclaimed' WHERE event_id=?",
                            (params[1],),
                        )
                return self.connection.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def interleaving_db():
            with original_db() as connection:
                yield InterleavingConnection(connection)

        self.store._db = interleaving_db
        self.assertEqual(self.store.deliver_pending(limit=1), 0)
        self.assertEqual(self.callback.requests, [])
        with original_db() as db:
            row = db.execute("SELECT status, lease_token FROM event_deliveries").fetchone()
        self.assertEqual(row["status"], "sending")
        self.assertEqual(row["lease_token"], "reclaimed")

    def test_null_ttl_means_no_expiration(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            result = self.store.subscribe(self.auth, {"name": "chatgpt.job.completed", "arguments": {"conversation_id": "12345678-1234-4234-8234-123456789abc"}, "delivery": self.delivery, "ttlMs": None})
        self.assertIsNone(result["refreshBefore"])

    def test_new_database_parent_is_created_private(self):
        state_dir = Path(self.tmp.name) / "private-events-state"
        store = MCPEventsStore(
            str(state_dir / "events.sqlite3"),
            encryption_key=b"p" * 32,
            post_json=self.callback.post,
        )
        self.assertEqual(state_dir.stat().st_mode & 0o077, 0)

    def test_existing_insecure_database_parent_is_rejected(self):
        state_dir = Path(self.tmp.name) / "insecure-events-state"
        state_dir.mkdir(mode=0o755)
        state_dir.chmod(0o755)
        with self.assertRaises(PermissionError):
            MCPEventsStore(
                str(state_dir / "events.sqlite3"),
                encryption_key=b"p" * 32,
                post_json=self.callback.post,
            )

    def test_secret_length_is_enforced(self):
        with self.assertRaises(ValueError):
            _secret_key("whsec_" + base64.b64encode(b"short").decode())

    def test_unsubscribe_marks_pending_deliveries_terminal(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-unsubscribe-pending")
        event_id = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-unsubscribe-pending", "Finished"
        )
        self.store.unsubscribe(self.auth, {
            "name": "chatgpt.job.completed",
            "arguments": {"job_id": "job-unsubscribe-pending", "conversation_id": "12345678-1234-4234-8234-123456789abc"},
            "delivery": {"url": self.delivery["url"]},
        })
        self.assertEqual(self.store.deliver_pending(), 0)
        with self.store._db() as db:
            row = db.execute(
                "SELECT status, last_status FROM event_deliveries WHERE event_id=?",
                (event_id,),
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_status"], 410)
        self.assertEqual(len(self.callback.requests), 1)  # challenge only; no event callback

    def test_gone_callback_deactivates_subscription(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        first_event = self.publish_job_completed("tenant-a", "issuer:user-a", "job-gone", "Finished")
        self.callback.fail_statuses = [410]
        self.assertEqual(self.store.deliver_pending(), 0)
        with self.store._db() as db:
            row = db.execute("SELECT active FROM event_subscriptions").fetchone()
            delivery = db.execute(
                "SELECT status, last_status FROM event_deliveries WHERE event_id=?",
                (first_event,),
            ).fetchone()
        self.assertEqual(row["active"], 0)
        self.assertEqual(delivery["status"], "failed")
        self.assertEqual(delivery["last_status"], 410)
        self.publish_job_completed("tenant-a", "issuer:user-a", "job-gone-2", "Finished")
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_deliveries").fetchone()[0], 1)

    def test_unsubscribe_waits_for_inflight_callback_before_returning(self):
        self.subscribe(job_id="job-unsubscribe-race")
        self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-unsubscribe-race", "Finished"
        )
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        requests = []

        def blocked_callback(url, body, headers):
            requests.append((url, body, headers))
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release in-flight callback")
            return 204, b""

        self.store.post_json = blocked_callback
        delivery_thread = threading.Thread(target=self.store.deliver_pending)
        delivery_thread.start()
        self.assertTrue(entered.wait(1), "delivery did not reach callback I/O")

        def unsubscribe():
            self.store.unsubscribe(self.auth, {
                "name": "chatgpt.job.completed",
                "arguments": {
                    "job_id": "job-unsubscribe-race",
                    "conversation_id": "12345678-1234-4234-8234-123456789abc",
                },
                "delivery": {"url": self.delivery["url"]},
            })
            finished.set()

        unsubscribe_thread = threading.Thread(target=unsubscribe)
        unsubscribe_thread.start()
        self.assertFalse(
            finished.wait(0.05),
            "unsubscribe returned while callback was still in flight",
        )
        release.set()
        delivery_thread.join(timeout=2)
        unsubscribe_thread.join(timeout=2)
        self.assertFalse(delivery_thread.is_alive())
        self.assertFalse(unsubscribe_thread.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.store.deliver_pending(), 0)

    def test_unsubscribe_is_owner_scoped_and_idempotent(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        self.store.unsubscribe(self.auth, {"name": "chatgpt.job.completed", "arguments": {"conversation_id": "12345678-1234-4234-8234-123456789abc"}, "delivery": {"url": self.delivery["url"]}})
        self.store.unsubscribe(self.auth, {"name": "chatgpt.job.completed", "arguments": {"conversation_id": "12345678-1234-4234-8234-123456789abc"}, "delivery": {"url": self.delivery["url"]}})
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_subscriptions WHERE active=1").fetchone()[0], 0)


    def test_unsubscribe_then_resubscribe_does_not_revive_old_generation(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            first = self.subscribe(job_id="job-generation")
        old_event = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-generation", "old",
            event_id="evt_old_generation",
        )
        self.store.unsubscribe(self.auth, {
            "name": "chatgpt.job.completed",
            "arguments": {"job_id": "job-generation", "conversation_id": "12345678-1234-4234-8234-123456789abc"},
            "delivery": {"url": self.delivery["url"]},
        })
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            second = self.subscribe(job_id="job-generation")
        self.assertEqual(first["id"], second["id"])
        new_event = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-generation", "new",
            event_id="evt_new_generation",
        )
        self.assertEqual(self.store.deliver_pending(limit=10), 1)
        with self.store._db() as db:
            old = db.execute("SELECT status, generation FROM event_deliveries WHERE event_id=?", (old_event,)).fetchone()
            new = db.execute("SELECT status, generation FROM event_deliveries WHERE event_id=?", (new_event,)).fetchone()
            sub = db.execute("SELECT generation FROM event_subscriptions WHERE id=?", (second["id"],)).fetchone()
        self.assertEqual(old["status"], "failed")
        self.assertEqual(new["status"], "delivered")
        self.assertEqual(old["generation"] + 1, new["generation"])
        self.assertEqual(new["generation"], sub["generation"])

    def test_stale_410_response_cannot_cancel_other_deliveries(self):
        now = [1_800_000_000]
        self.store.clock = lambda: now[0]
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe()
        first_event = self.publish_job_completed("tenant-a", "issuer:user-a", "job-stale-410-a", "A")
        second_event = self.publish_job_completed("tenant-a", "issuer:user-a", "job-stale-410-b", "B")
        entered = threading.Event()
        release = threading.Event()
        calls = []
        def callback(_url, body, _headers):
            if b'"eventId"' in body:
                calls.append(json.loads(body)["eventId"])
                if len(calls) == 1:
                    entered.set()
                    release.wait(timeout=2)
                    return 410, b""
                return 204, b""
            payload = json.loads(body)
            return 200, json.dumps({"challenge": payload["challenge"]}).encode()
        self.store.post_json = callback
        # A second store instance models another process: the per-instance
        # lifecycle lock must not weaken SQLite lease fencing across workers.
        second_store = MCPEventsStore(
            self.db,
            post_json=lambda _url, _body, _headers: (204, b""),
            encryption_key=b"k" * 32,
            clock=lambda: now[0],
        )
        first_result = []
        worker = threading.Thread(target=lambda: first_result.append(self.store.deliver_pending(limit=1)))
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        now[0] += 31
        self.assertEqual(second_store.deliver_pending(limit=1), 1)
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        with self.store._db() as db:
            sub = db.execute("SELECT active FROM event_subscriptions").fetchone()
            stale = db.execute("SELECT status FROM event_deliveries WHERE event_id=?", (first_event,)).fetchone()
            untouched = db.execute("SELECT status FROM event_deliveries WHERE event_id=?", (second_event,)).fetchone()
        self.assertEqual(sub["active"], 1)
        self.assertEqual(stale["status"], "delivered")
        self.assertEqual(untouched["status"], "pending")

    def test_retry_backoff_starts_at_callback_failure_time(self):
        now = [1_800_000_000]
        self.store.clock = lambda: now[0]
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-backoff-time")
        event_id = self.publish_job_completed("tenant-a", "issuer:user-a", "job-backoff-time", "Finished")
        def slow_failure(_url, _body, _headers):
            now[0] += 10
            return 503, b""
        self.store.post_json = slow_failure
        self.assertEqual(self.store.deliver_pending(), 0)
        with self.store._db() as db:
            row = db.execute("SELECT next_attempt_at, attempts FROM event_deliveries WHERE event_id=?", (event_id,)).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["next_attempt_at"], now[0] + 2)

    def test_expired_crash_lease_at_attempt_ceiling_is_terminal(self):
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-crash-ceiling")
        event_id = self.publish_job_completed("tenant-a", "issuer:user-a", "job-crash-ceiling", "Finished")
        now = int(self.store.clock())
        with self.store._db() as db:
            db.execute(
                """UPDATE event_deliveries SET status='sending', attempts=5,
                   lease_until=?, lease_token='abandoned' WHERE event_id=?""",
                (now - 1, event_id),
            )
        self.assertEqual(self.store.deliver_pending(), 0)
        with self.store._db() as db:
            row = db.execute("SELECT status, attempts, last_status FROM event_deliveries WHERE event_id=?", (event_id,)).fetchone()
        self.assertEqual((row["status"], row["attempts"], row["last_status"]), ("failed", 5, 599))

    def test_recent_event_completed_before_subscribe_request_is_not_backfilled(self):
        now = [1_800_000_000.5]
        self.store.clock = lambda: now[0]
        old_event = self.publish_job_completed(
            "tenant-a", "issuer:user-a", "job-before-request", "Finished",
            event_id="evt_before_request",
            completed_at="2027-01-15T07:59:30Z",
        )
        now[0] = float(__import__("datetime").datetime.fromisoformat("2027-01-15T08:00:00+00:00").timestamp()) + 0.5
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.subscribe(job_id="job-before-request")
        with self.store._db() as db:
            count = db.execute("SELECT count(*) FROM event_deliveries WHERE event_id=?", (old_event,)).fetchone()[0]
        self.assertEqual(count, 0)



if __name__ == "__main__":
    unittest.main()
