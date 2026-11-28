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

    def test_new_chat_mismatch_is_outcome_unknown_and_does_not_rotate(self):
        store = object()
        principal = object()
        attempt_id = "att_00000000-0000-4000-8000-000000000001"
        context = {
            "store": store,
            "principal": principal,
            "work_item_id": "wi_00000000-0000-4000-8000-000000000001",
            "attempt_id": attempt_id,
        }
        api = SimpleNamespace(account="acct2")
        conv = runtime.ConvRef(
            None,
            actual_conv_id="11111111-1111-4111-8111-111111111111",
            mismatch=True,
            reason="ambiguous conversation identity",
        )
        with (
            mock.patch.object(runtime, "ChatGPTAPI", return_value=api),
            mock.patch.object(
                runtime,
                "send_prompt_for_work_item",
                return_value=((1, None, conv), None, None),
            ),
        ):
            status, returned = runtime._attempt_create(
                {}, "acct2", "/tmp/prompt", [], work_item_context=context
            )
        self.assertEqual(status, "OUTCOME_UNKNOWN")
        self.assertIs(returned, conv)
        self.assertTrue(context["outcome_unknown"])
        self.assertEqual(context["status"], 200)
        self.assertIsNone(context["conversation_id"])
        self.assertIn("11111111-1111-4111-8111-111111111111", context["link_error"])

        with (
            mock.patch.object(runtime, "load_rotation", return_value={"cursor": 0}),
            mock.patch.object(runtime, "purge_expired", return_value=False),
            mock.patch.object(runtime, "is_unavailable", return_value=False),
            mock.patch.object(runtime, "rotation_plan", return_value=["acct2", "acct3"]),
            mock.patch.object(runtime, "_attempt_create", return_value=("OUTCOME_UNKNOWN", conv)) as attempt,
            mock.patch.object(runtime, "_mark_success") as mark_success,
        ):
            exit_code = runtime.create_chat_rotating(
                "/tmp/prompt", work_item_context=context
            )
        self.assertEqual(exit_code, 1)
        attempt.assert_called_once()
        mark_success.assert_not_called()


    def test_new_chat_http_503_is_outcome_unknown_and_does_not_rotate(self):
        store = object()
        principal = object()
        attempt_id = "att_00000000-0000-4000-8000-000000000001"
        context = {
            "store": store,
            "principal": principal,
            "work_item_id": "wi_00000000-0000-4000-8000-000000000001",
            "attempt_id": attempt_id,
        }
        api = SimpleNamespace(account="acct2")
        with (
            mock.patch.object(runtime, "ChatGPTAPI", return_value=api),
            mock.patch.object(
                runtime,
                "send_prompt_for_work_item",
                return_value=((503, None, None), None, None),
            ),
        ):
            status, conv = runtime._attempt_create(
                {}, "acct2", "/tmp/prompt", [], work_item_context=context
            )
        self.assertEqual(status, "OUTCOME_UNKNOWN")
        self.assertIsNone(conv)
        self.assertTrue(context["outcome_unknown"])
        self.assertEqual(context["status"], 503)

        with (
            mock.patch.object(runtime, "load_rotation", return_value={"cursor": 0}),
            mock.patch.object(runtime, "purge_expired", return_value=False),
            mock.patch.object(runtime, "is_unavailable", return_value=False),
            mock.patch.object(runtime, "rotation_plan", return_value=["acct2", "acct3"]),
            mock.patch.object(runtime, "_attempt_create", return_value=("OUTCOME_UNKNOWN", None)) as attempt,
            mock.patch.object(runtime, "_mark_success") as mark_success,
        ):
            exit_code = runtime.create_chat_rotating("/tmp/prompt", work_item_context=context)
        self.assertEqual(exit_code, 1)
        attempt.assert_called_once()
        mark_success.assert_not_called()

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
