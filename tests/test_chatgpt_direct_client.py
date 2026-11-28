import json
import time
from pathlib import Path
from unittest.mock import patch

from src.session_ops.chatgpt_direct_client import ChatGPTDirectClient, SessionOpsError


def make_client(tmp_path):
    accounts = tmp_path / "accounts"
    for account in ("acct1", "acct2", "acct4"):
        root = accounts / account
        root.mkdir(parents=True)
        (root / "cookies.json").write_text("{}", encoding="utf-8")
        (root / "access_token.txt").write_text("test-token", encoding="utf-8")
    client = ChatGPTDirectClient(
        tmp_path / "mcp.sqlite3",
        repository_root=Path(__file__).resolve().parents[1],
        accounts_dir=accounts,
        runtime_dir=tmp_path / "runtime",
    )
    with client._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chatgpt_account_health VALUES(?,?,?,?)",
            ("acct4", "operator_disabled", "disabled in test", "2026-01-01T00:00:00Z"),
        )
    return client


def test_active_accounts_excludes_disabled_accounts(tmp_path):
    client = make_client(tmp_path)
    assert client._active_accounts() == ("acct1", "acct2")


def test_enqueue_is_idempotent_and_job_state_is_persistent(tmp_path):
    client = make_client(tmp_path)

    def fake_run(row):
        return {"conversation_id": "c1", "account": "acct1", "receipt_durable": True}

    client._run_operation = fake_run
    first = client.enqueue(
        principal_id="p1", tenant_id="t1", operation="session.create",
        arguments={"account": "auto"}, idempotency_key="key-1",
    )
    deadline = time.time() + 2
    while time.time() < deadline:
        job = client.get_job(principal_id="p1", tenant_id="t1", job_id=first["job_id"])
        if job["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert job["status"] == "succeeded"
    retry = client.enqueue(
        principal_id="p1", tenant_id="t1", operation="session.create",
        arguments={"account": "auto"}, idempotency_key="key-1",
    )
    assert retry["job_id"] == first["job_id"]
    assert retry["status"] == "succeeded"


def test_job_and_session_ownership_are_scoped(tmp_path):
    client = make_client(tmp_path)
    client._store_owner("conv-1", "tenant-a", "principal-a", "acct1")
    client._store_owner("conv-2", "tenant-b", "principal-b", "acct2")
    page = client.list_sessions(principal_id="principal-a", tenant_id="tenant-a")
    assert [item["conversation_id"] for item in page["items"]] == ["conv-1"]
    try:
        client._owner("conv-2", "tenant-a", "principal-a")
    except SessionOpsError as exc:
        assert exc.code == "NOT_FOUND"
    else:
        raise AssertionError("cross-principal session access was allowed")


def test_read_session_after_message_id_and_response_contract(tmp_path):
    client = make_client(tmp_path)
    client._store_owner("conv-1", "tenant-a", "principal-a", "acct1")

    class FakeAPI:
        def live_tail(self, conversation_id, tries=2):
            return "tail", "mapping"

    with patch.object(client._cli, "ChatGPTAPI", lambda account: FakeAPI()), patch.object(
        client._cli, "_scan_mapping",
        lambda raw: ([("m1", "user", "question"), ("m2", "assistant", "answer")], True),
    ):
        result = client.read_session(
            principal_id="principal-a", tenant_id="tenant-a",
            conversation_id="conv-1", after_message_id="m1", limit=10,
        )
    assert result["conversation_id"] == "conv-1"
    assert result["messages"] == [{
        "message_id": "m2", "role": "assistant", "content": "answer", "created_at": None
    }]
    assert result["complete"] is True
    assert result["next_after_message_id"] is None
