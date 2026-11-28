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
        (root / "cookies.json").write_text(
            json.dumps([{"name": "session", "value": "test-cookie", "domain": ".chatgpt.com"}]),
            encoding="utf-8",
        )
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


def test_session_create_does_not_mutate_process_environment(tmp_path):
    client = make_client(tmp_path)
    client._validate_create_account = lambda _requested: ("acct1",)
    row = {
        "tenant_id": "t1",
        "principal_id": "p1",
        "requested_account": "acct1",
        "prompt_path": str(tmp_path / "prompt.txt"),
        "operation": "session.create",
        "job_id": "env-restore-job",
    }
    previous = {"last": {"conv_id": "old"}}
    completed = {"last": {"conv_id": "new", "account": "acct1"}}

    def fake_create(*args, **kwargs):
        assert client._cli.ROTATION_PRIMARY == ("acct1",)
        assert kwargs["account"] == "acct1"
        return 0

    with patch.dict(os.environ, {
        "CHATGPT_ANON_FALLBACK": "1",
        "CHATGPT_ALLOWED_ACCOUNTS": "acct2",
    }):
        with patch.object(client._cli, "load_rotation", side_effect=[previous, completed]), patch.object(
            client._cli, "create_chat_rotating", side_effect=fake_create
        ), patch.object(client, "_store_owner"):
            result = client._run_operation(row)
        assert result["conversation_id"] == "new"
        assert os.environ["CHATGPT_ANON_FALLBACK"] == "1"
        assert os.environ["CHATGPT_ALLOWED_ACCOUNTS"] == "acct2"
        assert client._cli.ROTATION_PRIMARY is None
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
    shutdown_deadline = time.time() + 1
    while not client._executor._shutdown and time.time() < shutdown_deadline:
        time.sleep(0.001)
    assert client._executor._shutdown is True
    release.set()
    stopper.join(2)
    assert not stopper.is_alive()

    conn = client._connect()
    try:
        statuses = {
            row["job_id"]: row["status"]
            for row in conn.execute(
                "SELECT job_id,status FROM chatgpt_jobs WHERE job_id IN (?,?)",
                (first["job_id"], second["job_id"]),
            )
        }
    finally:
        conn.close()  # sqlite3.Connection.__exit__ commits but does not close.
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

    recovered_started = threading.Event()

    def recovered_run(_self, _row):
        recovered_started.set()
        return {"conversation_id": "recovered", "account": "acct1", "receipt_durable": True}

    with patch.object(ChatGPTDirectClient, "_run_operation", recovered_run):
        restarted = ChatGPTDirectClient(
            client.database_path,
            repository_root=client.repository_root,
            accounts_dir=client.accounts_dir,
            runtime_dir=client.runtime_dir,
        )
        assert recovered_started.wait(1), "restart did not dispatch the durable queued job"
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


def test_direct_send_queue_rejects_capacity_above_nine(tmp_path):
    try:
        ChatGPTDirectClient(
            tmp_path / "mcp.sqlite3",
            repository_root=Path(__file__).resolve().parents[1],
            accounts_dir=tmp_path / "accounts",
            runtime_dir=tmp_path / "runtime",
            max_queue_size=10,
        )
    except ValueError as exc:
        assert "at most nine outstanding jobs" in str(exc)
    else:
        raise AssertionError("direct send queue accepted capacity above nine")


def test_direct_send_queue_rejects_non_integer_capacity_and_worker_values(tmp_path):
    for kwargs in ({"max_queue_size": 1.5}, {"max_queue_size": True}, {"max_workers": 1.0}, {"max_workers": True}):
        try:
            ChatGPTDirectClient(
                tmp_path / "mcp.sqlite3",
                repository_root=Path(__file__).resolve().parents[1],
                accounts_dir=tmp_path / "accounts",
                runtime_dir=tmp_path / "runtime",
                **kwargs,
            )
        except ValueError:
            continue
        raise AssertionError(f"direct send queue accepted invalid configuration: {kwargs!r}")


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


def test_adapter_respects_persistent_cli_operator_disable(tmp_path):
    client = make_client(tmp_path)
    with patch.object(client._cli, "load_rotation", return_value={"operator_disabled": ["acct2"]}):
        assert client._active_accounts() == ("acct1",)
        try:
            client._validate_create_account("acct2")
        except SessionOpsError as exc:
            assert exc.code == "RESOURCE_EXHAUSTED"
            assert "operator-disabled" in str(exc)
        else:
            raise AssertionError("adapter ignored the persistent CLI operator disable")


def test_adapter_pins_cli_auth_root_to_runtime_despite_legacy_env(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    legacy = tmp_path / "legacy-auth"
    legacy.mkdir()
    monkeypatch.setenv("CHATGPT_AUTH_DIR", str(legacy))
    cli = client._load_cli()
    assert cli._auth_base() == str(client.runtime_dir)


def test_read_only_vault_credentials_are_accepted_and_new_accounts_are_discovered(tmp_path):
    client = make_client(tmp_path)
    # The vault is intentionally not writable by SO-MCP; runtime state is.
    for account in ("acct1", "acct2", "acct4"):
        root = client.accounts_dir / account
        (root / "cookies.json").chmod(0o440)
        (root / "access_token.txt").chmod(0o440)
    assert client._active_accounts() == ("acct1", "acct2")

    new = client.accounts_dir / "acct10"
    new.mkdir()
    (new / "cookies.json").write_text(
        json.dumps([{"name": "session", "value": "new-cookie", "domain": ".chatgpt.com"}]),
        encoding="utf-8",
    )
    (new / "access_token.txt").write_text("new-token", encoding="utf-8")
    (new / "cookies.json").chmod(0o440)
    (new / "access_token.txt").chmod(0o440)

    assert client._active_accounts() == ("acct1", "acct2", "acct10")
    runtime_cookie = client.runtime_dir / "accounts" / "acct10" / "cookies.json"
    assert runtime_cookie.is_symlink()
    assert runtime_cookie.resolve() == (new / "cookies.json").resolve()


def test_legacy_root_acct1_credentials_are_mirrored_when_canonical_dir_is_absent(tmp_path):
    client = make_client(tmp_path)
    acct1 = client.accounts_dir / "acct1"
    legacy_cookie = client.accounts_dir.parent / "cookies.json"
    legacy_token = client.accounts_dir.parent / "access_token.txt"
    (acct1 / "cookies.json").replace(legacy_cookie)
    (acct1 / "access_token.txt").replace(legacy_token)
    acct1.rmdir()

    client._sync_runtime_accounts()
    runtime_cookie = client.runtime_dir / "accounts" / "acct1" / "cookies.json"
    runtime_token = client.runtime_dir / "accounts" / "acct1" / "access_token.txt"
    assert runtime_cookie.is_symlink() and runtime_cookie.resolve() == legacy_cookie.resolve()
    assert runtime_token.is_symlink() and runtime_token.resolve() == legacy_token.resolve()
    assert "acct1" in client._active_accounts()


def test_runtime_account_directory_symlink_is_rejected(tmp_path):
    client = make_client(tmp_path)
    external = tmp_path / "external-account"
    external.mkdir()
    vault_account = client.accounts_dir / "acct10"
    vault_account.mkdir()
    (vault_account / "cookies.json").write_text(
        json.dumps([{"name": "session", "value": "x", "domain": ".chatgpt.com"}]),
        encoding="utf-8",
    )
    (vault_account / "access_token.txt").write_text("token", encoding="utf-8")
    runtime_account = client.runtime_dir / "accounts" / "acct10"
    runtime_account.symlink_to(external, target_is_directory=True)
    try:
        client._sync_runtime_accounts()
    except SessionOpsError as exc:
        assert "must not be a symlink" in str(exc)
    else:
        raise AssertionError("runtime account directory symlink was accepted")


def test_cookie_warmup_atomic_replace_keeps_vault_read_only(tmp_path):
    client = make_client(tmp_path)
    vault_cookie = client.accounts_dir / "acct1" / "cookies.json"
    runtime_cookie = client.runtime_dir / "accounts" / "acct1" / "cookies.json"
    assert runtime_cookie.is_symlink()
    original = vault_cookie.read_text(encoding="utf-8")
    updated = [{"name": "session", "value": "warmed-cookie", "domain": ".chatgpt.com"}]
    client._cli._save_json(str(runtime_cookie), updated)
    assert not runtime_cookie.is_symlink()
    assert vault_cookie.read_text(encoding="utf-8") == original
    client._sync_runtime_accounts()
    assert json.loads(runtime_cookie.read_text(encoding="utf-8")) == updated


def test_explicit_create_uses_cli_strict_pin_and_work_item_context(tmp_path):
    client = make_client(tmp_path)
    previous = {"last": {"conv_id": "old"}}
    completed = {"last": {"conv_id": "new-conversation", "account": "acct2"}}
    def fake_create(*args, **kwargs):
        assert client._cli.ROTATION_PRIMARY == ("acct1", "acct2")
        assert kwargs["account"] == "acct2"
        return 0

    with patch.object(client._cli, "load_rotation", side_effect=[{"operator_disabled": []}, previous, completed]), patch.object(
        client._cli, "create_chat_rotating", side_effect=fake_create
    ) as create, patch.object(client, "_store_owner"):
        result = client._run_operation({
            "operation": "session.create",
            "requested_account": "acct2",
            "prompt_path": str(tmp_path / "prompt.txt"),
            "tenant_id": "tenant",
            "principal_id": "principal",
            "job_id": "job-1",
        })
    assert result["conversation_id"] == "new-conversation"
    assert result["account"] == "acct2"
    kwargs = create.call_args.kwargs
    assert kwargs["account"] == "acct2"
    assert kwargs["work_item_context"]["job_id"] == "job-1"
    assert client._cli.ROTATION_PRIMARY is None


def test_auto_create_all_429_returns_known_rate_limit_failure(tmp_path):
    client = make_client(tmp_path)
    previous = {"last": {"conv_id": "old"}}
    cooled = {
        "last": {"conv_id": "old"},
        "unavailable": {
            "acct1": {"reason": "HTTP 429"},
            "acct2": {"reason": "HTTP 429"},
        },
    }
    with patch.object(client._cli, "load_rotation", side_effect=[{"operator_disabled": []}, previous, cooled]), patch.object(
        client._cli, "create_chat_rotating", return_value=1
    ) as create:
        try:
            client._run_operation({
                "operation": "session.create",
                "requested_account": "auto",
                "prompt_path": str(tmp_path / "prompt.txt"),
                "tenant_id": "tenant",
                "principal_id": "principal",
                "job_id": "job-auto-429",
            })
        except SessionOpsError as exc:
            assert exc.code == "RESOURCE_EXHAUSTED"
            assert "all active accounts" in str(exc)
        else:
            raise AssertionError("all-account 429 was incorrectly reported as an unknown outcome")
    assert create.call_args.kwargs["account"] is None
    assert create.call_args.kwargs["work_item_context"]["job_id"] == "job-auto-429"


def test_explicit_create_429_returns_known_rate_limit_failure_after_cooldown(tmp_path):
    client = make_client(tmp_path)
    previous = {"last": {"conv_id": "old"}}
    cooled = {
        "last": {"conv_id": "old", "account": "acct2"},
        "unavailable": {"acct2": {"reason": "HTTP 429"}},
    }
    with patch.object(client._cli, "load_rotation", side_effect=[{"operator_disabled": []}, previous, cooled]), patch.object(
        client._cli, "create_chat_rotating", return_value=1
    ) as create:
        try:
            client._run_operation({
                "operation": "session.create",
                "requested_account": "acct2",
                "prompt_path": str(tmp_path / "prompt.txt"),
                "tenant_id": "tenant",
                "principal_id": "principal",
                "job_id": "job-2",
            })
        except SessionOpsError as exc:
            assert exc.code == "RESOURCE_EXHAUSTED"
        else:
            raise AssertionError("explicit-account 429 was not surfaced as a known rate-limit failure")
    assert create.call_args.kwargs["account"] == "acct2"
    assert create.call_args.kwargs["work_item_context"]["job_id"] == "job-2"
