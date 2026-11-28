from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.session_ops import chatgpt_cli
from src.session_ops.authority import Principal, SecureWorkItemStore
from src.session_ops.chatgpt_sessions import (
    ChatGPTSessionRegistry,
    ChatGPTSessionRegistryError,
)
from src.session_ops.mcp_auth import AuthContext, required_scopes_for_tool
from src.session_ops.mcp_server import MCPToolService, TOOL_SCHEMAS


CONV_ID = "12345678-1234-4234-8234-123456789abc"


class ChatGPTSessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = ChatGPTSessionRegistry(Path(self.temp.name) / "state.db")
        self.registry.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_owner_is_tenant_and_principal_bound(self):
        self.registry.register_session(CONV_ID, "tenant-a", "principal-a", "acct2")
        self.assertEqual(
            self.registry.get_owner(CONV_ID),
            {
                "tenant_id": "tenant-a",
                "principal_id": "principal-a",
                "account": "acct2",
            },
        )
        self.assertEqual(self.registry.list_owned("tenant-a", "principal-a"), {CONV_ID})
        self.assertEqual(self.registry.list_owned("tenant-b", "principal-a"), set())

    def test_conversation_owner_cannot_be_reassigned(self):
        self.registry.register_session(CONV_ID, "tenant-a", "principal-a", "acct2")
        with self.assertRaises(ChatGPTSessionRegistryError) as ctx:
            self.registry.register_session(CONV_ID, "tenant-b", "principal-b", "acct3")
        self.assertEqual(ctx.exception.code, "CONFLICT")

    def test_idempotency_replays_completed_result(self):
        payload = {"conversation_id": CONV_ID, "prompt": "hello"}
        self.assertIsNone(
            self.registry.begin_idempotent("tenant-a", "principal-a", "send", "key-1", payload)
        )
        response = {"conversation_id": CONV_ID, "sent": True}
        self.registry.complete_idempotent(
            "tenant-a", "principal-a", "send", "key-1", response
        )
        self.assertEqual(
            self.registry.begin_idempotent("tenant-a", "principal-a", "send", "key-1", payload),
            response,
        )

    def test_idempotency_rejects_reused_key_with_different_payload(self):
        self.registry.begin_idempotent(
            "tenant-a", "principal-a", "send", "key-1", {"prompt": "one"}
        )
        with self.assertRaises(ChatGPTSessionRegistryError) as ctx:
            self.registry.begin_idempotent(
                "tenant-a", "principal-a", "send", "key-1", {"prompt": "two"}
            )
        self.assertEqual(ctx.exception.code, "CONFLICT")

    def test_inflight_idempotency_key_fails_closed(self):
        self.registry.begin_idempotent(
            "tenant-a", "principal-a", "send", "key-1", {"prompt": "one"}
        )
        with self.assertRaises(ChatGPTSessionRegistryError) as ctx:
            self.registry.begin_idempotent(
                "tenant-a", "principal-a", "send", "key-1", {"prompt": "one"}
            )
        self.assertEqual(ctx.exception.code, "CONFLICT")

    def test_legacy_gateway_owner_rows_migrate_once_but_jobs_do_not(self):
        import sqlite3

        legacy_path = Path(self.temp.name) / "sender-gateway.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        legacy.execute(
            "CREATE TABLE session_owners (conversation_id TEXT PRIMARY KEY, tenant_id TEXT, "
            "principal_id TEXT, account TEXT, created_at TEXT)"
        )
        legacy.execute(
            "INSERT INTO session_owners VALUES (?, ?, ?, ?, ?)",
            (CONV_ID, "tenant-a", "principal-a", "acct2", "2026-01-01T00:00:00Z"),
        )
        legacy.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, status TEXT)")
        legacy.execute("INSERT INTO jobs VALUES ('legacy-job', 'queued')")
        legacy.commit()
        legacy.close()

        self.assertEqual(self.registry.migrate_legacy_owners(legacy_path), 1)
        self.assertEqual(self.registry.migrate_legacy_owners(legacy_path), 1)
        self.assertEqual(
            self.registry.get_owner(CONV_ID),
            {"tenant_id": "tenant-a", "principal_id": "principal-a", "account": "acct2"},
        )
        conn = sqlite3.connect(self.registry.database_path)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM chatgpt_session_owners").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name='jobs'").fetchone()[0], 0)
        finally:
            conn.close()


class ChatGPTCLIAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cli_path = Path(self.temp.name) / "chatgpt_send.py"
        self.cli_path.write_text("# fake cli\n", encoding="utf-8")
        self.adapter = chatgpt_cli.ChatGPTCLIAdapter(
            str(self.cli_path),
            owner_lookup=lambda _conv: {
                "tenant_id": "tenant-a",
                "principal_id": "principal-a",
                "account": "acct2",
            },
        )

    def tearDown(self):
        self.adapter.close()
        self.temp.cleanup()

    def test_auto_create_uses_cli_rotation_without_pinning_an_account(self):
        prompt_path = self.adapter._write_prompt("ping")
        argv = self.adapter._build_argv(
            "create", account="auto", prompt_path=prompt_path
        )
        self.assertEqual(argv, ("--new", prompt_path))

    def test_list_uses_cli_multi_account_discovery(self):
        self.assertEqual(self.adapter._build_argv("list"), ("--list-all",))

    def test_create_accepts_actual_account_selected_by_cli(self):
        result = chatgpt_cli._CliResult(
            0, "NEW_CONV %s account=acct2\n" % CONV_ID, "", (), "create"
        )
        with patch.object(chatgpt_cli, "_discover_logged_in_accounts", return_value=frozenset({"acct2"})):
            conv_id, account, _receipt = self.adapter._parse_create(result, "auto")
        self.assertEqual((conv_id, account), (CONV_ID, "acct2"))

    def test_owner_lookup_routes_only_for_matching_principal(self):
        with patch.object(chatgpt_cli, "_LOGGED_IN_ACCOUNTS", frozenset({"acct2"})), patch.object(
            chatgpt_cli, "_VALID_IDENTITIES", frozenset({"default", "nologin", "acct2"})
        ):
            self.assertEqual(
                self.adapter._resolve_account(
                    CONV_ID, "auto", tenant_id="tenant-a", principal_id="principal-a"
                ),
                "acct2",
            )
            with self.assertRaises(chatgpt_cli.SenderError) as ctx:
                self.adapter._resolve_account(
                    CONV_ID, "auto", tenant_id="tenant-b", principal_id="principal-a"
                )
        self.assertEqual(ctx.exception.code, "NOT_FOUND")


class DirectChatGPTMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = Path(self.temp.name) / "state.db"
        store = SecureWorkItemStore(db)
        store.initialize()
        self.service = MCPToolService(store)
        self.registry = ChatGPTSessionRegistry(db)
        self.registry.initialize()
        self.service.chatgpt_registry = self.registry
        self.cli = _FakeCLI()
        self.service.chatgpt_cli = self.cli
        self.auth = AuthContext(
            Principal("tenant-a", "principal-a"),
            ("session_ops.chatgpt.session.read", "session_ops.chatgpt.session.write"),
            "https://issuer.example/realms/test",
            "subject-a",
            "test",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_tool_surface_and_scopes(self):
        self.assertIn("chatgpt.session.create", TOOL_SCHEMAS)
        self.assertIn("chatgpt.session.send", TOOL_SCHEMAS)
        self.assertIn("chatgpt.session.read", TOOL_SCHEMAS)
        self.assertIn("chatgpt.session.list", TOOL_SCHEMAS)
        self.assertEqual(
            required_scopes_for_tool("chatgpt.session.send"),
            ("session_ops.chatgpt.session.write",),
        )

    def test_create_send_read_list_are_synchronous_and_owner_filtered(self):
        created = self.service.dispatch(
            "chatgpt.session.create", {"account": "auto"}, self.auth
        )
        self.assertTrue(created["ok"])
        self.assertEqual(created["data"]["conversation_id"], CONV_ID)
        sent = self.service.dispatch(
            "chatgpt.session.send",
            {"conversation_id": CONV_ID, "prompt": "hello"},
            self.auth,
        )
        self.assertTrue(sent["ok"])
        read = self.service.dispatch(
            "chatgpt.session.read", {"conversation_id": CONV_ID}, self.auth
        )
        self.assertTrue(read["ok"])
        listed = self.service.dispatch("chatgpt.session.list", {}, self.auth)
        self.assertTrue(listed["ok"])
        self.assertEqual(
            [item["conversation_id"] for item in listed["data"]["items"]],
            [CONV_ID],
        )

    def test_create_idempotency_returns_cached_result_without_second_cli_call(self):
        first = self.service.dispatch(
            "chatgpt.session.create",
            {"account": "auto", "idempotency_key": "12345678-1234-4234-8234-123456789abc"},
            self.auth,
        )
        second = self.service.dispatch(
            "chatgpt.session.create",
            {"account": "auto", "idempotency_key": "12345678-1234-4234-8234-123456789abc"},
            self.auth,
        )
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(self.cli.create_calls, 1)


class _FakeCLI:
    def __init__(self):
        self.create_calls = 0

    def create_session(self, account):
        self.create_calls += 1
        return {"conversation_id": CONV_ID, "account": "acct2"}

    def send(self, conversation_id, account, prompt, *, tenant_id, principal_id):
        return {
            "conversation_id": conversation_id,
            "account": "acct2",
            "sent": True,
            "prompt_length": len(prompt),
        }

    def read(self, conversation_id, account, after_message_id, limit, *, tenant_id, principal_id):
        return {
            "conversation_id": conversation_id,
            "messages": [{"message_id": "m1", "role": "assistant", "content": "reply"}],
            "complete": True,
            "has_more": False,
            "next_after_message_id": None,
        }

    def list_sessions(self, page_size, cursor, *, allowed_conversation_ids=None):
        items = [{"conversation_id": CONV_ID, "account": "acct2"}]
        if allowed_conversation_ids is not None:
            items = [item for item in items if item["conversation_id"] in allowed_conversation_ids]
        return {
            "items": items,
            "next_cursor": None,
            "partial": False,
            "errors": [],
        }


if __name__ == "__main__":
    unittest.main()
