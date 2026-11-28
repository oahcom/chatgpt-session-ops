from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.session_ops.authority import Principal, PrincipalRecord, SecureWorkItemStore, StaticPrincipalDirectory
from src.session_ops.mcp_events import MCPEventsStore


class Callback:
    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []

    def post(self, url: str, body: bytes, headers: dict[str, str]):
        self.requests.append((url, body, dict(headers)))
        payload = json.loads(body)
        if payload.get("type") == "verification":
            return 200, json.dumps({"challenge": payload["challenge"]}).encode()
        return 204, b""


class WorkItemCompletionEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "work-items.sqlite3"
        self.callback = Callback()
        self.events = MCPEventsStore(
            str(self.database),
            encryption_key=b"k" * 32,
            post_json=self.callback.post,
        )
        self.principal = Principal("tenant-a", "owner-a")
        self.delegate = Principal("tenant-a", "delegate-a")
        self.store = SecureWorkItemStore(
            self.database,
            principal_directory=StaticPrincipalDirectory([
                PrincipalRecord("delegate-a", "tenant-a"),
            ]),
            completion_event_publisher=self.events.publish_job_completed_in_transaction,
        )
        self.store.initialize()

    def _subscribe(
        self,
        callback_url: str = "https://events.example.test/work-item-completed",
        principal: Principal | None = None,
        job_id: str | None = None,
    ) -> None:
        principal = principal or self.principal
        auth = SimpleNamespace(principal=SimpleNamespace(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
        ))
        delivery = {
            "mode": "webhook",
            "url": callback_url,
            "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
        }
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.events.subscribe(auth, {
                "name": "chatgpt.job.completed",
                "arguments": ({"job_id": job_id} if job_id else {}),
                "delivery": delivery,
            })

    def _create_linked_item(self):
        item = self.store.create_work_item(self.principal)
        session_id = "12345678-1234-4234-8234-123456789abc"
        attempt_id = "att_12345678-1234-4234-8234-123456789abc"
        self.store._record_trusted_landing(
            session_id,
            match="confirmed",
            evidence_complete=True,
            work_item_id=item.work_item_id,
            attempt_id=attempt_id,
        )
        self.store.link_session(self.principal, session_id, item.work_item_id, attempt_id)
        return item, session_id

    def test_terminal_transition_atomically_enqueues_one_event_for_work_item(self):
        self._subscribe()
        item, source_session = self._create_linked_item()
        result = self.store.finish_work_item(self.principal, item.work_item_id, "completed")
        self.assertEqual(result.status, "completed")
        historical_links = self.store.list_sessions(self.principal, item.work_item_id, include_unlinked=True)
        self.assertEqual(len(historical_links), 1)
        self.assertIsNotNone(historical_links[0].unlinked_at)
        self.assertIsNone(self.store.get_active_link(self.principal, source_session))
        with self.events._db() as db:
            event = db.execute("SELECT * FROM event_outbox").fetchone()
            delivery = db.execute("SELECT status FROM event_deliveries").fetchone()
        payload = json.loads(event["data_json"])
        self.assertEqual(event["tenant_id"], self.principal.tenant_id)
        self.assertEqual(event["principal_id"], self.principal.principal_id)
        self.assertEqual(payload["job_id"], item.work_item_id)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["source_conversation_ids"], [source_session])
        self.assertNotIn("conversation_id", payload)
        self.assertEqual(delivery["status"], "pending")
        self.assertEqual(self.events.deliver_pending(), 1)
        with self.events._db() as db:
            delivered = db.execute("SELECT status FROM event_deliveries").fetchone()["status"]
        self.assertEqual(delivered, "delivered")
        callback_event = json.loads(self.callback.requests[-1][1])
        self.assertEqual(callback_event["data"]["job_id"], item.work_item_id)
        self.assertEqual(self.callback.requests[-1][0], "https://events.example.test/work-item-completed")
    def test_multiple_linked_sessions_are_payload_context_not_delivery_routes(self):
        first_session = "12345678-1234-4234-8234-123456789abc"
        second_session = "22345678-1234-4234-8234-123456789abc"
        self._subscribe("https://events.example.test/chat-one")
        self._subscribe("https://events.example.test/chat-two")
        item, _ = self._create_linked_item()
        second_attempt = "att_22345678-1234-4234-8234-123456789abc"
        self.store._record_trusted_landing(
            second_session,
            match="confirmed",
            evidence_complete=True,
            work_item_id=item.work_item_id,
            attempt_id=second_attempt,
        )
        self.store.link_session(self.principal, second_session, item.work_item_id, second_attempt)
        self.store.finish_work_item(self.principal, item.work_item_id, "completed")
        with self.events._db() as db:
            event_count = db.execute("SELECT count(*) FROM event_outbox").fetchone()[0]
            delivery_count = db.execute("SELECT count(*) FROM event_deliveries").fetchone()[0]
            payload = json.loads(db.execute("SELECT data_json FROM event_outbox").fetchone()[0])
        self.assertEqual(event_count, 1)
        self.assertEqual(delivery_count, 2)
        self.assertEqual(set(payload["source_conversation_ids"]), {first_session, second_session})
        self.assertEqual(self.events.deliver_pending(), 2)
        delivered = [(url, json.loads(body)["data"]) for url, body, _headers in self.callback.requests
                     if json.loads(body).get("type") != "verification"]
        self.assertEqual({url for url, _data in delivered}, {
            "https://events.example.test/chat-one", "https://events.example.test/chat-two"})
        self.assertTrue(all(set(data["source_conversation_ids"]) == {first_session, second_session}
                            for _url, data in delivered))
    def test_each_subscribed_work_chat_receives_owner_events_via_its_callback(self):
        self._subscribe("https://events.example.test/first")
        self._subscribe("https://events.example.test/other")
        item, _ = self._create_linked_item()
        self.store.finish_work_item(self.principal, item.work_item_id, "completed")
        self.assertEqual(self.events.deliver_pending(), 2)
        delivered = [(url, json.loads(body)["data"]) for url, body, _headers in self.callback.requests
                     if json.loads(body).get("type") != "verification"]
        self.assertEqual({url for url, _data in delivered}, {
            "https://events.example.test/first", "https://events.example.test/other"})
        self.assertTrue(all(data["job_id"] == item.work_item_id for _url, data in delivered))
        self.assertTrue(all("conversation_id" not in data for _url, data in delivered))
    def test_replay_backfill_targets_subscribed_work_chats_not_source_sessions(self):
        first_session = "12345678-1234-4234-8234-123456789abc"
        self.events.clock = lambda: 1000.0
        self.events.publish_job_completed(self.principal.tenant_id, self.principal.principal_id,
            "wi_replay", "replay event", event_id="evt_replay_scoped",
            completed_at="1970-01-01T00:16:41Z", source_conversation_ids=[first_session])
        self._subscribe("https://events.example.test/replay-first")
        self._subscribe("https://events.example.test/replay-other")
        with self.events._db() as db:
            rows = db.execute("SELECT s.callback_url FROM event_deliveries d JOIN event_subscriptions s "
                "ON s.id=d.subscription_id WHERE d.event_id='evt_replay_scoped'").fetchall()
        self.assertEqual({row["callback_url"] for row in rows}, {
            "https://events.example.test/replay-first", "https://events.example.test/replay-other"})
    def test_empty_filter_subscription_uses_callback_as_destination(self):
        self._subscribe("https://events.example.test/subscribed-chat")
        item = self.store.create_work_item(self.principal)
        self.store.finish_work_item(self.principal, item.work_item_id, "completed")
        self.assertEqual(self.events.deliver_pending(), 1)
        delivered = [(url, json.loads(body)) for url, body, _headers in self.callback.requests
                     if json.loads(body).get("type") != "verification"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], "https://events.example.test/subscribed-chat")
        self.assertNotIn("conversation_id", delivered[0][1]["data"])
    def test_one_owner_callback_subscription_aggregates_multiple_distinct_workitems(self):
        self._subscribe()
        first, _ = self._create_linked_item()
        second = self.store.create_work_item(self.principal)
        self.store.finish_work_item(self.principal, first.work_item_id, "completed")
        self.store.finish_work_item(self.principal, second.work_item_id, "completed")
        self.assertEqual(self.events.deliver_pending(), 2)
        payloads = [json.loads(body)["data"] for _url, body, _headers in self.callback.requests
                    if json.loads(body).get("type") != "verification"]
        self.assertEqual({payload["job_id"] for payload in payloads}, {first.work_item_id, second.work_item_id})
    def test_owner_callback_does_not_receive_another_principals_workitems(self):
        self._subscribe()
        other = Principal("tenant-a", "another-owner")
        foreign = self.store.create_work_item(other)
        self.store.finish_work_item(other, foreign.work_item_id, "completed")
        self.assertEqual(self.events.deliver_pending(), 0)
    def test_multiple_owners_receive_only_their_events_at_their_callback_urls(self):
        other = Principal("tenant-a", "another-owner")
        self._subscribe("https://events.example.test/owner-a", principal=self.principal)
        self._subscribe("https://events.example.test/owner-b", principal=other)
        first = self.store.create_work_item(self.principal)
        second = self.store.create_work_item(other)
        self.store.finish_work_item(self.principal, first.work_item_id, "completed")
        self.store.finish_work_item(other, second.work_item_id, "completed")
        self.assertEqual(self.events.deliver_pending(), 2)
        delivered = [(url, json.loads(body)["data"]["job_id"]) for url, body, _headers in self.callback.requests
                     if json.loads(body).get("type") != "verification"]
        self.assertEqual(dict(delivered), {
            "https://events.example.test/owner-a": first.work_item_id,
            "https://events.example.test/owner-b": second.work_item_id})
    def test_subscribed_chat_receives_completion_without_source_session_link(self):
        self._subscribe()
        item = self.store.create_work_item(self.principal)
        self.store.finish_work_item(self.principal, item.work_item_id, "completed")
        with self.events._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_outbox").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM event_deliveries").fetchone()[0], 1)
        self.assertEqual(self.events.deliver_pending(), 1)
        delivered = [json.loads(body)["data"] for _url, body, _headers in self.callback.requests
                     if json.loads(body).get("type") != "verification"]
        self.assertEqual(len(delivered), 1)
        self.assertNotIn("source_conversation_ids", delivered[0])
    def test_legacy_destination_subscriptions_are_disabled_on_schema_upgrade(self):
        self._subscribe()
        item = self.store.create_work_item(self.principal)
        self.store.finish_work_item(self.principal, item.work_item_id, "completed")
        with self.events._db() as db:
            db.execute("DROP TABLE event_schema_metadata")
        reopened = MCPEventsStore(str(self.database), encryption_key=b"k" * 32,
                                  post_json=self.callback.post)
        with reopened._db() as db:
            row = db.execute("SELECT active FROM event_subscriptions").fetchone()
            delivery = db.execute("SELECT status FROM event_deliveries").fetchone()
        self.assertEqual(row["active"], 0)
        self.assertEqual(delivery["status"], "failed")
    def test_delegated_completion_notifies_work_item_owner(self):
        self._subscribe(principal=self.principal)
        self._subscribe(
            "https://events.example.test/delegate",
            principal=self.delegate,
        )
        item, _session_id = self._create_linked_item()
        self.store.grant(self.principal, item.work_item_id, self.delegate.principal_id, "write")

        from src.session_ops.authority import NotAuthorized
        with self.assertRaises(NotAuthorized):
            self.store.finish_work_item(self.delegate, item.work_item_id, "completed")
        self.assertEqual(self.store.get_work_item(self.principal, item.work_item_id).status, "active")
        self.store.finish_work_item(self.principal, item.work_item_id, "completed")
        with self.events._db() as db:
            event = db.execute("SELECT tenant_id, principal_id FROM event_outbox").fetchone()
            deliveries = db.execute(
                "SELECT s.principal_id FROM event_deliveries d "
                "JOIN event_subscriptions s ON s.id=d.subscription_id"
            ).fetchall()
        self.assertEqual(event["tenant_id"], self.principal.tenant_id)
        self.assertEqual(event["principal_id"], self.principal.principal_id)
        self.assertEqual([row["principal_id"] for row in deliveries], [self.principal.principal_id])


    def test_cancelled_work_item_emits_failed_completion_event(self):
        self._subscribe()
        item, _session_id = self._create_linked_item()
        self.store.finish_work_item(self.principal, item.work_item_id, "cancelled")
        with self.events._db() as db:
            event = db.execute("SELECT data_json FROM event_outbox").fetchone()
        payload = json.loads(event["data_json"])
        self.assertEqual(payload["job_id"], item.work_item_id)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["summary"], "Work item cancelled")

    def test_repeated_terminal_status_does_not_duplicate_completion_event(self):
        item, _session_id = self._create_linked_item()
        self.store.finish_work_item(self.principal, item.work_item_id, "completed", request_id="finish-once")
        self.store.finish_work_item(self.principal, item.work_item_id, "completed", request_id="finish-once")
        with self.events._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_outbox").fetchone()[0], 1)

    def test_event_insert_failure_rolls_back_terminal_transition(self):
        item, _session_id = self._create_linked_item()

        def fail_after_insert(db, **kwargs):
            self.events.publish_job_completed_in_transaction(db, **kwargs)
            raise RuntimeError("injected outbox failure")

        store = SecureWorkItemStore(
            self.database,
            completion_event_publisher=fail_after_insert,
        )
        with self.assertRaisesRegex(RuntimeError, "injected outbox failure"):
            store.finish_work_item(self.principal, item.work_item_id, "completed")

        self.assertEqual(store.get_work_item(self.principal, item.work_item_id).status, "active")
        self.assertEqual(len(store.list_sessions(self.principal, item.work_item_id)), 1)
        with self.events._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_outbox").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM event_deliveries").fetchone()[0], 0)


    def test_delegate_cannot_subscribe_to_owner_work_item_filter(self):
        item = self.store.create_work_item(self.principal)
        delegate = Principal("tenant-a", "delegate-a")
        conn = self.store._connect()
        try:
            conn.execute(
                "INSERT INTO work_item_grants(work_item_id, principal_id, scope, granted_at) VALUES (?, ?, 'read', ?)",
                (item.work_item_id, delegate.principal_id, "2026-11-28T12:00:00Z"),
            )
        finally:
            conn.close()
        # The owner can target its own WorkItem explicitly.
        owner_delivery = {
            "mode": "webhook",
            "url": "https://events.example.test/owner-job",
            "secret": "whsec_" + base64.b64encode(b"o" * 32).decode(),
        }
        owner_auth = SimpleNamespace(principal=SimpleNamespace(
            tenant_id=self.principal.tenant_id,
            principal_id=self.principal.principal_id,
        ))
        with (
            patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"),
            patch.object(self.events, "_verify_callback") as owner_verify,
        ):
            self.events.subscribe(owner_auth, {
                "name": "chatgpt.job.completed",
                "arguments": {"job_id": item.work_item_id},
                "delivery": owner_delivery,
            })
        owner_verify.assert_called_once()

        delegate_auth = SimpleNamespace(principal=SimpleNamespace(
            tenant_id=delegate.tenant_id,
            principal_id=delegate.principal_id,
        ))
        delivery = {
            "mode": "webhook",
            "url": "https://events.example.test/delegate-job",
            "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
        }
        with patch.object(self.events, "_verify_callback") as verify:
            with self.assertRaisesRegex(ValueError, "owned by the subscriber"):
                self.events.subscribe(delegate_auth, {
                    "name": "chatgpt.job.completed",
                    "arguments": {"job_id": item.work_item_id},
                    "delivery": delivery,
                })
        verify.assert_not_called()

    def test_require_owner_rejects_delegated_writer_even_with_write_grant(self):
        from src.session_ops.authority import NotAuthorized

        item = self.store.create_work_item(self.principal)
        self.store.grant(self.principal, item.work_item_id, self.delegate.principal_id, "write")
        self.store.require_owner(self.principal, item.work_item_id)
        with self.assertRaises(NotAuthorized):
            self.store.require_owner(self.delegate, item.work_item_id)

    def test_events_store_rejects_in_memory_database(self):
        with self.assertRaisesRegex(ValueError, "file-backed SQLite database"):
            MCPEventsStore(":memory:", encryption_key=b"k" * 32)

    def test_completion_event_belongs_to_owner_not_delegated_writer(self):
        from src.session_ops.authority import NotAuthorized

        def authorize_resource(*, tenant_id, principal_id, job_id):
            principal = Principal(tenant_id, principal_id)
            for scope in ("read", "write"):
                try:
                    self.store.get_work_item(principal, job_id)
                    return True
                except NotAuthorized:
                    continue
            return False

        grantee = Principal("tenant-a", "writer-a")
        item, session_id = self._create_linked_item()
        conn = self.store._connect()
        try:
            conn.execute(
                "INSERT INTO work_item_grants(work_item_id, principal_id, scope, granted_at) VALUES (?, ?, 'write', ?)",
                (item.work_item_id, grantee.principal_id, "2026-11-28T12:00:00Z"),
            )
        finally:
            conn.close()

        # The WorkItem owner and delegated writer each have a subscription.
        self._subscribe()
        writer_auth = SimpleNamespace(principal=SimpleNamespace(
            tenant_id=grantee.tenant_id,
            principal_id=grantee.principal_id,
        ))
        writer_delivery = {
            "mode": "webhook",
            "url": "https://events.example.test/writer",
            "secret": "whsec_" + base64.b64encode(b"r" * 32).decode(),
        }
        with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
            self.events.subscribe(writer_auth, {
                "name": "chatgpt.job.completed",
                "arguments": {},
                "delivery": writer_delivery,
            })

        self.store.finish_work_item(self.principal, item.work_item_id, "completed")

        # Revoking the delegated write grant must not redirect the owner's
        # event to the delegate or leak it to the delegate's subscription.
        conn = self.store._connect()
        try:
            conn.execute(
                "DELETE FROM work_item_grants WHERE work_item_id=? AND principal_id=? AND scope='write'",
                (item.work_item_id, grantee.principal_id),
            )
        finally:
            conn.close()

        checked_principals = []
        def authorize(**kwargs):
            checked_principals.append(kwargs["principal_id"])
            return authorize_resource(**kwargs)

        self.assertEqual(self.events.deliver_pending(authorize=authorize), 1)
        self.assertEqual(checked_principals, [self.principal.principal_id])
        event_posts = [
            json.loads(body)
            for _url, body, _headers in self.callback.requests
            if "eventId" in json.loads(body)
        ]
        self.assertEqual(len(event_posts), 1)
        self.assertEqual(event_posts[0]["data"]["source_conversation_ids"], [session_id])
        with self.events._db() as db:
            rows = db.execute(
                """SELECT d.status, s.principal_id FROM event_deliveries d
                   JOIN event_subscriptions s ON s.id=d.subscription_id"""
            ).fetchall()
        self.assertEqual([(row["status"], row["principal_id"]) for row in rows], [
            ("delivered", self.principal.principal_id)
        ])

    def test_transactional_publisher_rejects_a_different_database(self):
        other_db = Path(self.tmp.name) / "other.sqlite3"
        other_events = MCPEventsStore(str(other_db), encryption_key=b"z" * 32)
        conn = self.store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "same SQLite database"):
                other_events.publish_job_completed_in_transaction(
                    conn,
                    tenant_id=self.principal.tenant_id,
                    principal_id=self.principal.principal_id,
                    job_id="wi_12345678-1234-4234-8234-123456789abc",
                    summary="done",
                    event_id="evt_1234567890abcdef1234567890abcdef",
                    completed_at="2026-11-28T12:00:00Z",
                )
        finally:
            conn.rollback()
            conn.close()
