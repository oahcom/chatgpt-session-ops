"""Tests for the trusted send-to-WorkItem orchestration path."""

from __future__ import annotations

import inspect
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts import chatgpt_send as sender
from src.session_ops.authority import (
    Principal,
    QuarantineNotCleared,
    SecureWorkItemStore,
    StaticPrincipalDirectory,
    PrincipalRecord,
)


def cid():
    return str(uuid.uuid4())


class FakeAPI:
    is_anon = False
    account = "test"
    last_truncated = False

    def __init__(self, response_ids, complete=True, status=200, root=None):
        self.response_ids = response_ids
        self.complete = complete
        self.status = status
        self.root = Path(root)
        self._last_reply_text = ""

    def requirements(self):
        return "requirements", "proof"

    def live_tail(self, conv_id, tries=2):
        return "parent-message", ""

    def cached_tail(self, conv_id):
        return None

    def send(self, *args, **kwargs):
        events = []
        for value in self.response_ids:
            events.append('data: {"conversation_id":"%s"}' % value)
        events.append("data: [DONE]")
        return self.status, "\n\n".join(events) + "\n\n"

    def extract_reply_sse_checked(self, response):
        return "assistant reply", self.complete, "test"

    def req(self, method, url, timeout=120):
        return 404, ""

    def save_reply(self, real_conv, prompt_file, archived):
        path = self.root / (real_conv + ".md")
        path.write_text(archived, encoding="utf-8")
        return str(path)


class SendForWorkItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "authority.sqlite3"
        directory = StaticPrincipalDirectory(
            [PrincipalRecord("alice", "tenant-1")]
        )
        self.store = SecureWorkItemStore(
            self.db, principal_directory=directory
        )
        self.store.initialize()
        self.principal = Principal("tenant-1", "alice")
        self.item = self.store.create_work_item(self.principal)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("test prompt", encoding="utf-8")

    def call_sender(
        self,
        api,
        conv_id=None,
        new_chat=True,
    ):
        return sender.send_prompt_for_work_item(
            api,
            self.store,
            self.principal,
            self.item.work_item_id,
            conv_id,
            str(self.prompt),
            new_chat=new_chat,
        )

    def event_count(self):
        conn = self.store._connect()
        try:
            return conn.execute(
                "SELECT count(*) FROM authority_landing_events"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_confirmed_complete_archive_records_event_and_links(self):
        session_id = cid()
        api = FakeAPI([session_id], root=self.root)

        result, link, link_error = self.call_sender(api)

        self.assertEqual(result[0], 200)
        self.assertIsNone(link_error)
        self.assertIsNotNone(link)
        self.assertEqual(link.session_id, session_id)
        self.assertEqual(link.work_item_id, self.item.work_item_id)
        self.assertEqual(self.event_count(), 1)

    def test_missing_archive_file_does_not_record_a_landing_event(self):
        """save_reply returning a path is not the same as the file existing.

        The landing event is the proof that a reply was archived, so a path
        pointing at nothing must not become a consumable event. Deleting the
        os.path.isfile conjunct leaves every other case here green.
        """
        session_id = cid()

        class VanishingAPI(FakeAPI):
            def save_reply(self, real_conv, prompt_file, archived):
                return str(self.root / (real_conv + ".md"))  # never written

        api = VanishingAPI([session_id], root=self.root)

        result, link, _ = self.call_sender(api)

        self.assertEqual(result[0], 200)
        self.assertIsNone(link)
        self.assertEqual(self.event_count(), 0)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal, session_id, self.item.work_item_id
            )

    def test_mismatch_409_does_not_record_or_link(self):
        target_id, actual_id = cid(), cid()
        api = FakeAPI([actual_id], root=self.root)
        with patch.object(
            sender,
            "_quarantine_reply",
            return_value=(str(self.root / "quarantine"), True),
        ):
            result, link, link_error = self.call_sender(
                api, conv_id=target_id, new_chat=False
            )

        self.assertEqual(result[0], sender.LANDING_MISMATCH_STATUS)
        self.assertIsNone(link)
        self.assertIsNone(link_error)
        self.assertEqual(self.event_count(), 0)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal, actual_id, self.item.work_item_id
            )

    def test_ambiguous_multiple_ids_do_not_record_or_link(self):
        first, second = cid(), cid()
        api = FakeAPI([first, second], root=self.root)

        result, link, _ = self.call_sender(api)

        self.assertEqual(result[0], 1)
        self.assertIsNone(link)
        self.assertEqual(self.event_count(), 0)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal, first, self.item.work_item_id
            )

    def test_incomplete_reply_does_not_record_or_link(self):
        session_id = cid()
        api = FakeAPI([session_id], complete=False, root=self.root)

        result, link, _ = self.call_sender(api)

        self.assertEqual(result[0], 200)
        self.assertIsNone(link)
        self.assertEqual(self.event_count(), 0)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal, session_id, self.item.work_item_id
            )

    def test_event_replay_cannot_create_a_second_link(self):
        session_id = cid()
        api = FakeAPI([session_id], root=self.root)
        _, link, error = self.call_sender(api)
        self.assertIsNotNone(link)
        self.assertIsNone(error)

        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal, session_id, self.item.work_item_id
            )

    def test_plain_ids_convref_and_booleans_are_not_evidence_inputs(self):
        parameters = inspect.signature(
            sender.send_prompt_for_work_item
        ).parameters
        self.assertNotIn("evidence", parameters)
        self.assertNotIn("evidence_complete", parameters)
        self.assertNotIn("conv_ref", parameters)
        self.assertNotIn("match", parameters)

        session_id = cid()
        fake_ref = sender.ConvRef(
            session_id, reply_complete=True
        )
        self.assertTrue(fake_ref.reply_complete)
        self.assertEqual(self.event_count(), 0)
        with self.assertRaises(QuarantineNotCleared):
            self.store.link_session(
                self.principal, session_id, self.item.work_item_id,
                evidence={
                    "session_id": session_id,
                    "match": "confirmed",
                    "evidence_complete": True,
                },
            )
        self.assertEqual(self.event_count(), 0)


if __name__ == "__main__":
    unittest.main()
