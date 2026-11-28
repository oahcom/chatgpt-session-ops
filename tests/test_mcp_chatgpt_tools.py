import unittest
from types import SimpleNamespace
from unittest import mock

from src.session_ops.authority import Principal
from src.session_ops.mcp_auth import AuthContext
from src.session_ops.mcp_server import MCPToolService


WORK_ITEM_ID = "wi_00000000-0000-4000-8000-000000000001"
SESSION_ID = "00000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "att_00000000-0000-4000-8000-000000000001"


class FakeStore:
    def __init__(self, *, link=None, deny_write=False):
        self.link = link
        self.deny_write = deny_write
        self.access_checks = []
        self.attempts = {}

    def reserve_send_attempt(self, principal, work_item_id, attempt_id, *, request_hash):
        self.access_checks.append((principal, work_item_id, "write"))
        if self.deny_write:
            from src.session_ops.authority import NotAuthorized
            raise NotAuthorized()
        existing = self.attempts.get(attempt_id)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                from src.session_ops.authority import InvalidIdentifier
                raise InvalidIdentifier("attempt_id was reused with different request parameters")
            return False, dict(existing)
        record = {
            "attempt_id": attempt_id,
            "work_item_id": work_item_id,
            "request_hash": request_hash,
            "status": "running",
            "conversation_id": None,
            "account": None,
            "http_status": None,
            "error": None,
        }
        self.attempts[attempt_id] = record
        return True, dict(record)

    def complete_send_attempt(self, principal, work_item_id, attempt_id, **values):
        record = self.attempts[attempt_id]
        record.update(values)
        return dict(record)

    def get_active_link(self, principal, session_id):
        return self.link

    def get_session_link_for_work_item(self, principal, work_item_id, session_id):
        self.access_checks.append((principal, work_item_id, "read"))
        if self.link and self.link.work_item_id == work_item_id and self.link.session_id == session_id:
            return self.link
        return None


def make_auth(*scopes):
    principal = Principal("tenant-a", "principal-a")
    return AuthContext(
        principal,
        scopes,
        issuer="https://issuer.example.test/realm",
        subject="subject-a",
        realm_namespace="realm",
    )


class MCPChatGPTToolTests(unittest.TestCase):
    def test_send_existing_session_uses_repository_runtime_and_attempt_id(self):
        link = SimpleNamespace(
            link_id="link-a",
            session_id=SESSION_ID,
            work_item_id=WORK_ITEM_ID,
            linked_at="2026-11-28T00:00:00Z",
            unlinked_at=None,
        )
        store = FakeStore(link=link)
        service = MCPToolService(store)
        api = SimpleNamespace(account="acct2")
        with (
            mock.patch("scripts.chatgpt_send.resolve_conversation_account", return_value={"status": "known", "account": "acct2"}),
            mock.patch("scripts.chatgpt_send.ChatGPTAPI", return_value=api),
            mock.patch("scripts.chatgpt_send.send_prompt", return_value=(200, "/tmp/reply", SESSION_ID)) as send,
        ):
            result = service.dispatch(
                "chatgpt.session.send",
                {
                    "work_item_id": WORK_ITEM_ID,
                    "conversation_id": SESSION_ID,
                    "prompt": "Run the requested task",
                    "attempt_id": ATTEMPT_ID,
                },
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "sent")
        self.assertEqual(result["data"]["attempt_id"], ATTEMPT_ID)
        self.assertEqual(result["data"]["conversation_id"], SESSION_ID)
        self.assertEqual(store.access_checks[0][2], "write")
        self.assertEqual(send.call_args.args[1], SESSION_ID)
        prompt_path = send.call_args.args[2]
        self.assertFalse(__import__("os").path.exists(prompt_path))

        replay = service.dispatch(
            "chatgpt.session.send",
            {
                "work_item_id": WORK_ITEM_ID,
                "conversation_id": SESSION_ID,
                "prompt": "Run the requested task",
                "attempt_id": ATTEMPT_ID,
            },
            make_auth("session_ops.session.write", "session_ops.work_item.write"),
        )
        self.assertTrue(replay["ok"])
        self.assertEqual(replay["data"]["status"], "sent")
        self.assertTrue(replay["data"]["replayed"])
        send.assert_called_once()

    def test_existing_link_does_not_make_failed_send_look_successful(self):
        link = SimpleNamespace(link_id="link-existing", session_id=SESSION_ID,
                               work_item_id=WORK_ITEM_ID, linked_at="now", unlinked_at=None)
        service = MCPToolService(FakeStore(link=link))
        with (
            mock.patch("scripts.chatgpt_send.resolve_conversation_account", return_value={"status": "known", "account": "acct2"}),
            mock.patch("scripts.chatgpt_send.ChatGPTAPI", return_value=SimpleNamespace(account="acct2")),
            mock.patch("scripts.chatgpt_send.send_prompt", return_value=(503, "/tmp/reply", SESSION_ID)),
        ):
            result = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "try again", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "outcome_unknown")
        self.assertEqual(result["data"]["http_status"], 503)

    def test_existing_conversation_identity_mismatch_is_outcome_unknown_and_not_replayed(self):
        link = SimpleNamespace(link_id="link-mismatch", session_id=SESSION_ID,
                               work_item_id=WORK_ITEM_ID, linked_at="now", unlinked_at=None)
        store = FakeStore(link=link)
        service = MCPToolService(store)
        conv_ref = SimpleNamespace(
            mismatch=True,
            actual_conv_id="11111111-1111-4111-8111-111111111111",
            reason="ambiguous conversation identity",
        )
        with (
            mock.patch("scripts.chatgpt_send.resolve_conversation_account", return_value={"status": "known", "account": "acct2"}),
            mock.patch("scripts.chatgpt_send.ChatGPTAPI", return_value=SimpleNamespace(account="acct2")),
            mock.patch("scripts.chatgpt_send.send_prompt", return_value=(1, None, conv_ref)) as send,
        ):
            first = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "send once", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
            replay = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "send once", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertTrue(first["ok"])
        self.assertEqual(first["data"]["status"], "outcome_unknown")
        self.assertEqual(first["data"]["http_status"], 200)
        self.assertEqual(first["data"]["error"], "session_link_failed")
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual(replay["data"]["status"], "outcome_unknown")
        send.assert_called_once()

    def test_existing_session_transport_failure_after_post_is_outcome_unknown(self):
        link = SimpleNamespace(link_id="link-timeout", session_id=SESSION_ID,
                               work_item_id=WORK_ITEM_ID, linked_at="now", unlinked_at=None)
        store = FakeStore(link=link)
        service = MCPToolService(store)
        api = SimpleNamespace(account="acct2", last_send_started=True)
        with (
            mock.patch("scripts.chatgpt_send.resolve_conversation_account", return_value={"status": "known", "account": "acct2"}),
            mock.patch("scripts.chatgpt_send.ChatGPTAPI", return_value=api),
            mock.patch("scripts.chatgpt_send.send_prompt", return_value=(0, None, None)) as send,
        ):
            result = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "send once", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "outcome_unknown")
        self.assertIn("POST transport failure", result["data"]["error"])
        send.assert_called_once()

    def test_reusing_attempt_id_with_different_prompt_is_rejected(self):
        link = SimpleNamespace(link_id="link-existing", session_id=SESSION_ID,
                               work_item_id=WORK_ITEM_ID, linked_at="now", unlinked_at=None)
        store = FakeStore(link=link)
        service = MCPToolService(store)
        with (
            mock.patch("scripts.chatgpt_send.resolve_conversation_account", return_value={"status": "known", "account": "acct2"}),
            mock.patch("scripts.chatgpt_send.ChatGPTAPI", return_value=SimpleNamespace(account="acct2")),
            mock.patch("scripts.chatgpt_send.send_prompt", return_value=(200, "/tmp/reply", SESSION_ID)) as send,
        ):
            first = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "first prompt", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
            second = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "different prompt", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"]["code"], "INVALID_ARGUMENT")
        send.assert_called_once()


    def test_success_without_conversation_id_is_outcome_unknown(self):
        service = MCPToolService(FakeStore())

        def create(_prompt_path, *, work_item_context, account):
            work_item_context.update({
                "status": 200,
                "account": "acct3",
                "conversation_id": None,
                "link": None,
                "link_error": None,
            })
            return 0

        with mock.patch("scripts.chatgpt_send.create_chat_rotating", side_effect=create):
            result = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "prompt": "start", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "outcome_unknown")

    def test_send_existing_conversation_requires_active_work_item_link(self):
        service = MCPToolService(FakeStore())
        with mock.patch("scripts.chatgpt_send.send_prompt") as send:
            result = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "must not cross-link", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")
        send.assert_not_called()

    def test_send_without_conversation_uses_existing_account_rotation(self):
        store = FakeStore()
        service = MCPToolService(store)
        link = SimpleNamespace(
            link_id="link-b",
            session_id=SESSION_ID,
            work_item_id=WORK_ITEM_ID,
            linked_at="2026-11-28T00:00:00Z",
            unlinked_at=None,
        )

        def create(_prompt_path, *, work_item_context, account):
            self.assertIs(work_item_context["store"], store)
            self.assertEqual(work_item_context["work_item_id"], WORK_ITEM_ID)
            self.assertEqual(work_item_context["attempt_id"], ATTEMPT_ID)
            self.assertIsNone(account)
            work_item_context.update({
                "status": 200,
                "account": "acct3",
                "conversation_id": SESSION_ID,
                "link": link,
                "link_error": None,
            })
            return 0

        with mock.patch("scripts.chatgpt_send.create_chat_rotating", side_effect=create) as rotate:
            result = service.dispatch(
                "chatgpt.session.send",
                {
                    "work_item_id": WORK_ITEM_ID,
                    "prompt": "Start a new task session",
                    "attempt_id": ATTEMPT_ID,
                },
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "sent")
        self.assertEqual(result["data"]["account"], "acct3")
        rotate.assert_called_once()


    def test_running_attempt_is_reported_without_starting_a_second_send(self):
        import hashlib
        import json

        link = SimpleNamespace(link_id="link-running", session_id=SESSION_ID,
                               work_item_id=WORK_ITEM_ID, linked_at="now", unlinked_at=None)
        store = FakeStore(link=link)
        request_hash = hashlib.sha256(json.dumps({
            "work_item_id": WORK_ITEM_ID,
            "conversation_id": SESSION_ID,
            "prompt": "pending prompt",
            "account": None,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        store.attempts[ATTEMPT_ID] = {
            "attempt_id": ATTEMPT_ID,
            "work_item_id": WORK_ITEM_ID,
            "request_hash": request_hash,
            "status": "running",
            "conversation_id": None,
            "account": None,
            "http_status": None,
            "error": None,
        }
        service = MCPToolService(store)
        with mock.patch("scripts.chatgpt_send.send_prompt") as send:
            result = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "pending prompt", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "in_progress")
        self.assertTrue(result["data"]["replayed"])
        send.assert_not_called()

    def test_read_allows_completed_work_item_historical_session_link(self):
        link = SimpleNamespace(
            link_id="link-c",
            session_id=SESSION_ID,
            work_item_id=WORK_ITEM_ID,
            linked_at="2026-11-28T00:00:00Z",
            unlinked_at="2026-11-28T01:00:00Z",
        )
        store = FakeStore(link=link)
        service = MCPToolService(store)
        api = SimpleNamespace(live_tail=mock.Mock(return_value=("assistant: done", {"mapping": {}})), last_status=200)
        with (
            mock.patch("scripts.chatgpt_send.resolve_conversation_account", return_value={"status": "known", "account": "acct2"}),
            mock.patch("scripts.chatgpt_send.ChatGPTAPI", return_value=api),
        ):
            result = service.dispatch(
                "chatgpt.session.read",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID},
                make_auth("session_ops.session.read", "session_ops.work_item.read"),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["text"], "assistant: done")
        self.assertEqual(result["data"]["status"], "ok")
        self.assertEqual(store.access_checks[0][2], "read")

    def test_explicit_account_conflict_rejects_send_before_remote_call(self):
        link = SimpleNamespace(link_id="link-conflict", session_id=SESSION_ID,
                               work_item_id=WORK_ITEM_ID, linked_at="now", unlinked_at=None)
        store = FakeStore(link=link)
        service = MCPToolService(store)
        with (
            mock.patch("scripts.chatgpt_send.resolve_conversation_account", return_value={"status": "conflict", "account": "acct2"}),
            mock.patch("scripts.chatgpt_send.ChatGPTAPI") as api,
            mock.patch("scripts.chatgpt_send.send_prompt") as send,
        ):
            result = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID,
                 "prompt": "do not send", "account": "acct3", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")
        api.assert_not_called()
        send.assert_not_called()

    def test_explicit_account_conflict_rejects_read(self):
        link = SimpleNamespace(link_id="link-d", session_id=SESSION_ID,
                               work_item_id=WORK_ITEM_ID, linked_at="now", unlinked_at=None)
        service = MCPToolService(FakeStore(link=link))
        with (
            mock.patch("scripts.chatgpt_send.resolve_conversation_account", return_value={"status": "conflict", "account": "acct2"}),
            mock.patch("scripts.chatgpt_send.ChatGPTAPI") as api,
        ):
            result = service.dispatch(
                "chatgpt.session.read",
                {"work_item_id": WORK_ITEM_ID, "conversation_id": SESSION_ID, "account": "acct3"},
                make_auth("session_ops.session.read", "session_ops.work_item.read"),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")
        api.assert_not_called()

    def test_send_requires_work_item_write_access_before_remote_send(self):
        store = FakeStore(deny_write=True)
        service = MCPToolService(store)
        with mock.patch("scripts.chatgpt_send.create_chat_rotating") as send:
            result = service.dispatch(
                "chatgpt.session.send",
                {"work_item_id": WORK_ITEM_ID, "prompt": "do it", "attempt_id": ATTEMPT_ID},
                make_auth("session_ops.session.write", "session_ops.work_item.write"),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
