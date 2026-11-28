from __future__ import annotations

import threading
import time

from sender_gateway.app import SenderApplication


class Closable:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class FakeWorker:
    def __init__(self, callback, worker_id="test"):
        self.callback = callback
        self.worker_id = worker_id

    def reconcile_expired_leases(self):
        pass

    def run_once(self):
        return self.callback()


def make_app(workers, drain=2.0):
    app = SenderApplication.__new__(SenderApplication)
    app.workers = workers
    app.worker = workers[0]
    app._stop = threading.Event()
    app._maintenance_lock = threading.Lock()
    app._last_reconcile = time.monotonic()
    app._last_retention = time.monotonic()
    app._retention_days = 14
    app._poll_seconds = 0.01
    app._worker_started = False
    app._closed = False
    app._drain_timeout = drain
    app.adapter = Closable()
    app.store = Closable()
    app._worker_threads = [
        threading.Thread(target=app._worker_loop, args=(worker,), daemon=False)
        for worker in workers
    ]
    app._worker_thread = app._worker_threads[0]
    return app


def test_workers_execute_concurrently():
    both_entered = threading.Event()
    barrier = threading.Barrier(2, action=both_entered.set)
    app = make_app([
        FakeWorker(lambda: (barrier.wait(timeout=2), False)[1], "w1"),
        FakeWorker(lambda: (barrier.wait(timeout=2), False)[1], "w2"),
    ])
    app.start_worker()
    try:
        assert both_entered.wait(2)
    finally:
        app.close()


def test_worker_exception_does_not_stop_sibling():
    broken = threading.Event()
    healthy = threading.Event()

    def fail_once():
        if not broken.is_set():
            broken.set()
            raise RuntimeError("injected failure")
        return False

    app = make_app([FakeWorker(fail_once, "broken"), FakeWorker(lambda: (healthy.set(), False)[1], "healthy")])
    app.start_worker()
    try:
        assert broken.wait(2) and healthy.wait(2)
    finally:
        app.close()


def test_shutdown_drains_all_workers_before_closing_dependencies():
    entered = threading.Event()
    release = threading.Event()
    app = make_app([FakeWorker(lambda: (entered.set(), release.wait(2), False)[2])])
    app.start_worker()
    assert entered.wait(2)
    done = threading.Event()
    closer = threading.Thread(target=lambda: (app.close(), done.set()))
    closer.start()
    try:
        time.sleep(0.05)
        assert not app.adapter.closed.is_set()
        assert not app.store.closed.is_set()
    finally:
        release.set()
        closer.join(2)
    assert done.is_set()
    assert app.adapter.closed.is_set() and app.store.closed.is_set()


def test_shutdown_timeout_keeps_shared_dependencies_open():
    entered = threading.Event()
    release = threading.Event()
    app = make_app([FakeWorker(lambda: (entered.set(), release.wait(2), False)[2])], drain=0.05)
    app.start_worker()
    assert entered.wait(2)
    try:
        try:
            app.close()
            raise AssertionError("expected drain timeout")
        except RuntimeError as exc:
            assert "did not drain" in str(exc)
        assert not app.adapter.closed.is_set() and not app.store.closed.is_set()
    finally:
        release.set()
        time.sleep(0.05)
        app.close()


def test_start_worker_is_idempotent_and_close_is_idempotent():
    count = 0
    lock = threading.Lock()

    def run():
        nonlocal count
        with lock:
            count += 1
        return False

    app = make_app([FakeWorker(run)])
    app.start_worker()
    app.start_worker()
    time.sleep(0.05)
    app.close()
    app.close()
    assert count >= 1


def test_real_application_defaults_worker_count_to_send_concurrency(monkeypatch, tmp_path):
    import os
    from pathlib import Path
    from sender_gateway import app as app_module

    accounts = tmp_path / "accounts"
    accounts.mkdir()
    token = tmp_path / "service.token"
    token.write_bytes(b"service-token")
    token.chmod(0o600)
    identity = tmp_path / "identity.key"
    identity.write_bytes(b"i" * 32)
    identity.chmod(0o600)
    values = {
        "SO_SENDER_BIND_HOST": "127.0.0.1",
        "SO_SENDER_STATE_DIR": str(tmp_path / "state"),
        "SO_SENDER_ACCOUNTS_DIR": str(accounts),
        "SO_SENDER_CLI_PATH": "/opt/so-sender/cli/chatgpt_send.py",
        "SO_SENDER_CLI_PYTHON": "/opt/so-sender/.venv/bin/python",
        "SO_SENDER_SERVICE_TOKEN_FILE": str(token),
        "SO_SENDER_MAX_CONCURRENCY": "2",
        "SO_SENDER_READ_CONCURRENCY": "2",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SO_SENDER_WORKER_COUNT", raising=False)
    monkeypatch.setattr(app_module, "discover_accounts", lambda _path: ("acct1", "acct2"))
    monkeypatch.setattr(app_module, "IDENTITY_KEY_PATH", str(identity))
    app = SenderApplication()
    try:
        assert len(app.workers) == 2
        assert len(app._worker_threads) == 2
        assert app.adapter._max_concurrency == 2
    finally:
        app.close()


def _enqueue(store, account):
    import uuid
    return store.enqueue(
        principal_id="test-principal",
        tenant_id="test-tenant",
        operation="session.create",
        idempotency_key=str(uuid.uuid4()),
        payload={"account": account, "prompt": "isolated worker-pool test"},
    )["job_id"]


def test_workers_process_different_accounts_concurrently(tmp_path):
    import threading
    from sender_gateway.account_router import AccountRouter
    from sender_gateway.job_store import JobStore
    from sender_gateway.worker import Worker

    store = JobStore(str(tmp_path / "jobs.sqlite3"), lease_seconds=60)
    router = AccountRouter(store, accounts=("acct1", "acct2"))
    jobs = [_enqueue(store, account) for account in ("acct1", "acct2")]
    barrier = threading.Barrier(2)
    completed_accounts = []

    def create_session(account):
        import uuid
        barrier.wait(timeout=3)
        completed_accounts.append(account)
        return {"conversation_id": str(uuid.uuid4()), "turn_id": "turn-" + uuid.uuid4().hex}

    workers = [
        Worker(store, router, worker_id=f"worker-{i}", create_session=create_session, lease_seconds=60)
        for i in (1, 2)
    ]
    results = [None, None]
    threads = [threading.Thread(target=lambda i=i: results.__setitem__(i, workers[i].run_once())) for i in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert results == [True, True]
        assert sorted(completed_accounts) == ["acct1", "acct2"]
        assert [store.get("test-principal", "test-tenant", job)["status"] for job in jobs] == ["succeeded", "succeeded"]
    finally:
        store.close()


def test_workers_never_run_two_mutations_for_same_account_concurrently(tmp_path):
    import threading
    from sender_gateway.account_router import AccountRouter
    from sender_gateway.job_store import JobStore
    from sender_gateway.worker import Worker

    store = JobStore(str(tmp_path / "jobs.sqlite3"), lease_seconds=60)
    router = AccountRouter(store, accounts=("acct1",))
    jobs = [_enqueue(store, "acct1") for _ in range(2)]
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def create_session(account):
        calls.append(account)
        entered.set()
        release.wait(3)
        import uuid
        return {"conversation_id": str(uuid.uuid4()), "turn_id": "turn-" + uuid.uuid4().hex}

    workers = [
        Worker(store, router, worker_id=f"worker-{i}", create_session=create_session, lease_seconds=60)
        for i in (1, 2)
    ]
    first = threading.Thread(target=workers[0].run_once)
    try:
        first.start()
        assert entered.wait(2)
        # The first worker holds acct1. The second can claim but must release
        # its job without invoking the remote mutation callback.
        workers[1].run_once()
        assert calls == ["acct1"]
        release.set()
        first.join(5)
        statuses = [store.get("test-principal", "test-tenant", job)["status"] for job in jobs]
        assert statuses.count("succeeded") == 1
        assert statuses.count("queued") == 1
    finally:
        release.set()
        first.join(5)
        store.close()
