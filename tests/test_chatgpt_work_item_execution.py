import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import chatgpt_send as runtime
from src.session_ops.authority import Principal, SecureWorkItemStore


class ChatGPTWorkItemExecutionTests(unittest.TestCase):
    def test_rotating_create_uses_trusted_work_item_send_and_supplied_attempt_id(self):
        store = object()
        principal = object()
        link = SimpleNamespace(link_id="link-1", session_id="00000000-0000-4000-8000-000000000001")
        api = SimpleNamespace(account="acct2")
        attempt_id = "att_00000000-0000-4000-8000-000000000001"
        context = {
            "store": store,
            "principal": principal,
            "work_item_id": "wi_00000000-0000-4000-8000-000000000001",
            "attempt_id": attempt_id,
        }
        attempts = []
        with (
            mock.patch.object(runtime, "ChatGPTAPI", return_value=api),
            mock.patch.object(
                runtime,
                "send_prompt_for_work_item",
                return_value=((200, "/tmp/reply", link.session_id), link, None),
            ) as send,
        ):
            status, conversation_id = runtime._attempt_create(
                {}, "acct2", "/tmp/prompt", attempts,
                work_item_context=context,
            )

        self.assertEqual(status, 200)
        self.assertEqual(conversation_id, link.session_id)
        self.assertEqual(context["status"], 200)
        self.assertIs(context["api"], api)
        self.assertIs(context["link"], link)
        self.assertIsNone(context["link_error"])
        send.assert_called_once_with(
            api, store, principal, context["work_item_id"], None,
            "/tmp/prompt", new_chat=True, thinking=None,
            attempt_id=attempt_id,
        )

    def test_work_item_send_rejects_anonymous_api_before_any_send(self):
        store = SecureWorkItemStore.__new__(SecureWorkItemStore)
        principal = Principal("tenant-a", "principal-a")
        context = {
            "store": store,
            "principal": principal,
            "work_item_id": "wi_00000000-0000-4000-8000-000000000001",
            "attempt_id": "att_00000000-0000-4000-8000-000000000001",
        }
        api = SimpleNamespace(is_anon=True)
        with mock.patch.object(runtime, "_send_prompt_impl") as send:
            with self.assertRaises(ValueError):
                runtime.send_prompt_for_work_item(
                    api, store, principal, context["work_item_id"], None,
                    "/tmp/prompt", new_chat=True,
                    attempt_id=context["attempt_id"],
                )
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
