from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "chatgpt_send.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_send_mcp_contract", SCRIPT)
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

CONVERSATION_ID = "12345678-1234-4234-8234-123456789abc"


def mapping_body():
    return json.dumps({
        "mapping": {
            "root": {"message": None},
            "node-user-1": {"message": {
                "id": "message-user-1",
                "author": {"role": "user"},
                "content": {"parts": ["first prompt"]},
                "create_time": 1,
            }},
            "node-assistant-1": {"message": {
                "id": "message-assistant-1",
                "author": {"role": "assistant"},
                "content": {"parts": ["first answer"]},
                "create_time": 2,
            }},
            "node-user-2": {"message": {
                "id": "message-user-2",
                "author": {"role": "user"},
                "content": {"parts": ["second prompt"]},
                "create_time": 3,
            }},
        }
    })


class FakeAPI:
    def __init__(self, raw):
        self.raw = raw

    def live_tail(self, conversation_id, tries=2):
        return "second prompt", self.raw


class ChatGPTMCPCLIContractTests(unittest.TestCase):
    def test_mapping_messages_keep_ids_and_order(self):
        messages, complete = CLI._scan_mapping_messages(mapping_body())
        self.assertTrue(complete)
        self.assertEqual(
            [(item["id"], item["role"], item["content"]) for item in messages],
            [
                ("message-user-1", "user", "first prompt"),
                ("message-assistant-1", "assistant", "first answer"),
                ("message-user-2", "user", "second prompt"),
            ],
        )

    def test_tail_json_supports_after_message_id_and_limit(self):
        output = io.StringIO()
        argv = [
            "chatgpt_send.py", "--tail", CONVERSATION_ID, "--json",
            "--after-message-id", "message-user-1", "--limit", "1",
        ]
        with (
            patch.object(CLI, "resolve_conversation_account", return_value={
                "status": "known", "account": "acct1",
            }),
            patch.object(CLI, "build", return_value=FakeAPI(mapping_body())),
            patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(output),
        ):
            result = CLI.main()

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["complete"])
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_after_message_id"], "message-assistant-1")
        self.assertEqual(
            payload["messages"],
            [{
                "id": "message-assistant-1",
                "role": "assistant",
                "content": "first answer",
                "created_at": 2.0,
            }],
        )

    def test_tail_json_rejects_unknown_cursor_without_returning_success(self):
        output = io.StringIO()
        argv = [
            "chatgpt_send.py", "--tail", CONVERSATION_ID, "--json",
            "--after-message-id", "missing-message", "--limit", "5",
        ]
        with (
            patch.object(CLI, "resolve_conversation_account", return_value={
                "status": "known", "account": "acct1",
            }),
            patch.object(CLI, "build", return_value=FakeAPI(mapping_body())),
            patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(output),
        ):
            result = CLI.main()

        self.assertEqual(result, 1)
        self.assertIn("after_message_id not found", output.getvalue())


if __name__ == "__main__":
    unittest.main()
