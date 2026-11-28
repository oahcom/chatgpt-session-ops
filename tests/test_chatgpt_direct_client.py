import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
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


def test_read_only_credential_vault_remains_eligible_for_account_preflight(tmp_path):
    client = make_client(tmp_path)
    real_access = os.access

    def read_only_access(path, mode):
        if mode & os.W_OK:
            return False
        return real_access(path, mode)

    with patch("src.session_ops.chatgpt_direct_client.os.access", side_effect=read_only_access):
        assert client._active_accounts() == ("acct1", "acct2")
    client.stop()


def test_session_create_restores_process_environment_on_success_and_failure(tmp_path):
    client = make_client(tmp_path)
    api = SimpleNamespace(account="acct1")
    conversation = SimpleNamespace(
        id="12345678-1234-4234-8234-123456789abc",
        send_truncated=False,
    )
    client._validate_create_account = lambda _requested: ("acct1",)
    row = {
        "tenant_id": "t1",
        "principal_id": "p1",
        "requested_account": "acct1",
        "prompt_path": str(tmp_path / "prompt.txt"),
        "operation": "session.create",
    }

    with patch.dict(os.environ, {
        "CHATGPT_ANON_FALLBACK": "1",
        "CHATGPT_ALLOWED_ACCOUNTS": "acct2",
    }):
        with patch.object(client._cli, "ChatGPTAPI", return_value=api), patch.object(
            client._cli, "send_prompt", return_value=(200, "path", conversation)
        ), patch.object(client._cli, "record_conv"):
            result = client._run_operation(row)
        assert result["conversation_id"] == conversation.id
        assert os.environ["CHATGPT_ANON_FALLBACK"] == "1"
        assert os.environ["CHATGPT_ALLOWED_ACCOUNTS"] == "acct2"

    with patch.dict(os.environ, {
        "CHATGPT_ANON_FALLBACK": "1",
        "CHATGPT_ALLOWED_ACCOUNTS": "acct2",
    }):
        with patch.object(client._cli, "ChatGPTAPI", return_value=api), patch.object(
            client._cli, "send_prompt", return_value=(500, "path", conversation)
        ):
            try:
                client._run_operation(row)
            except SessionOpsError as exc:
                assert exc.code == "OUTCOME_UNKNOWN"
            else:
                raise AssertionError("unverified session create must fail closed")
        assert os.environ["CHATGPT_ANON_FALLBACK"] == "1"
        assert os.environ["CHATGPT_ALLOWED_ACCOUNTS"] == "acct2"
    client.stop()


def test_stop_cancels_waiting_futures_and_restart_recovers_durable_queue(tmp_path):
    client = make_client(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow_run(_row):
        started.set()
        assert release.wait(2)
        return {"conversation_id": "created", "account": "acct1", "receipt_durable": True}

    client._run_operation = slow_run
    first = client.enqueue(
        principal_id="p1", tenant_id="t1", operation="session.create",
        arguments={"account": "auto"}, idempotency_key="stop-active",
    )
    assert started.wait(1)
    second = client.enqueue(
        principal_id="p1", tenant_id="t1", operation="session.create",
        arguments={"account": "auto"}, idempotency_key="stop-queued",
    )

    stopper = threading.Thread(target=client.stop)
    stopper.start()
    time.sleep(0.05)
    release.set()
    stopper.join(2)
    assert not stopper.is_alive()

    with client._connect() as conn:
        statuses = {
            row["job_id"]: row["status"]
            for row in conn.execute(
                "SELECT job_id,status FROM chatgpt_jobs WHERE job_id IN (?,?)",
                (first["job_id"], second["job_id"]),
            )
        }
    assert statuses[first["job_id"]] == "succeeded"
    assert statuses[second["job_id"]] == "queued"

    try:
        client.enqueue(
            principal_id="p1", tenant_id="t1", operation="session.create",
            arguments={"account": "auto"}, idempotency_key="after-stop",
        )
    except SessionOpsError as exc:
        assert exc.code == "SERVICE_UNAVAILABLE"
    else:
        raise AssertionError("closed executor accepted a new ChatGPT job")

    def recovered_run(_client, _row):
        return {"conversation_id": "recovered", "account": "acct1", "receipt_durable": True}

    with patch.object(ChatGPTDirectClient, "_run_operation", recovered_run):
        restarted = ChatGPTDirectClient(
            client.database_path,
            repository_root=client.repository_root,
            accounts_dir=client.accounts_dir,
            runtime_dir=client.runtime_dir,
        )
        try:
            deadline = time.time() + 2
            while time.time() < deadline:
                recovered = restarted.get_job(
                    principal_id="p1", tenant_id="t1", job_id=second["job_id"]
                )
                if recovered["status"] == "succeeded":
                    break
                time.sleep(0.01)
            assert recovered["status"] == "succeeded"
        finally:
            restarted.stop()


def test_default_send_queue_is_one_active_plus_eight_waiting(tmp_path):
    client = make_client(tmp_path)
    assert client.max_workers == 1
    assert client._executor._max_workers == 1
    # Capacity includes the active job, leaving eight durable waiting slots.
    assert client.max_queue_size == 9


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


def test_idempotency_key_rejects_different_payload_or_operation(tmp_path):
    client = make_client(tmp_path)

    class DeferredExecutor:
        def submit(self, function, *args):
            return None

    client._executor.shutdown(wait=True)
    client._executor = DeferredExecutor()
    client._store_owner("c1", "t1", "p1", "acct1")
    first = client.enqueue(
        principal_id="p1", tenant_id="t1", operation="session.send",
        arguments={"conversation_id": "c1", "prompt": "first", "account": "auto"},
        idempotency_key="stable-key",
    )
    with client._connect() as conn:
        stored_hash = conn.execute(
            "SELECT payload_hash FROM chatgpt_jobs WHERE job_id=?", (first["job_id"],)
        ).fetchone()[0]
    assert len(stored_hash) == 64

    for operation, arguments in (
        ("session.send", {"conversation_id": "c1", "prompt": "changed", "account": "auto"}),
        ("session.create", {"account": "auto"}),
    ):
        try:
            client.enqueue(
                principal_id="p1", tenant_id="t1", operation=operation,
                arguments=arguments, idempotency_key="stable-key",
            )
        except SessionOpsError as exc:
            assert exc.code == "CONFLICT"
        else:
            raise AssertionError("idempotency key reuse with a different request was accepted")


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
