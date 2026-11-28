import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.session_ops.chatgpt_direct_client import ChatGPTDirectClient, SessionOpsError


class _Executor:
    def __init__(self):
        self.submitted = []

    def submit(self, function, *args):
        self.submitted.append((function, args))


class ChatGPTDirectClientAccountPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accounts = root / "accounts"
        self.runtime = root / "runtime"
        self.prompts = self.runtime / "prompts"
        self.accounts.mkdir()
        self.prompts.mkdir(parents=True)
        for account in ("acct1", "acct5"):
            folder = self.accounts / account
            folder.mkdir()
            (folder / "cookies.json").write_text(
                '[{"name":"session","value":"test-cookie","domain":".chatgpt.com"}]'
            )
            (folder / "access_token.txt").write_text("token")

        self.db = root / "state.sqlite3"
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE chatgpt_account_health (
                    account TEXT PRIMARY KEY, state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
                );
                CREATE TABLE chatgpt_jobs (
                    job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL, operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
                    conversation_id TEXT, requested_account TEXT,
                    resolved_account TEXT, prompt_path TEXT, created_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT, result_json TEXT,
                    error_json TEXT, outcome_unknown INTEGER NOT NULL DEFAULT 0,
                    payload_hash TEXT,
                    UNIQUE(tenant_id, principal_id, operation, idempotency_key)
                );
                INSERT INTO chatgpt_account_health VALUES
                    ('acct5', 'operator_disabled', 'operator disabled; do not reauthenticate', 'now');
            """)
        self.client = ChatGPTDirectClient.__new__(ChatGPTDirectClient)
        self.client.database_path = self.db
        self.client.accounts_dir = self.accounts
        self.client.runtime_dir = self.runtime
        self.client.repository_root = Path(__file__).resolve().parents[1]
        self.client.prompt_dir = self.prompts
        self.client._db_lock = threading.RLock()
        self.client._executor = _Executor()
        self.client.max_queue_size = 1
        self.client._cli = self.client._load_cli()
        self.client._sync_runtime_accounts()

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_pinned_account_is_rejected_before_job_or_prompt_creation(self):
        with self.assertRaises(SessionOpsError) as caught:
            self.client.enqueue(
                principal_id="principal", tenant_id="tenant",
                operation="session.create", arguments={"account": "acct5"},
                idempotency_key="key-disabled",
            )
        self.assertEqual(caught.exception.code, "RESOURCE_EXHAUSTED")
        self.assertIn("operator_disabled", str(caught.exception))
        self.assertIn("do not reauthenticate", str(caught.exception))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM chatgpt_jobs").fetchone()[0], 0)
        self.assertEqual(list(self.prompts.iterdir()), [])
        self.assertEqual(self.client._executor.submitted, [])

    def test_auto_uses_healthy_pool_and_queues(self):
        result = self.client.enqueue(
            principal_id="principal", tenant_id="tenant",
            operation="session.create", arguments={"account": "auto"},
            idempotency_key="key-auto",
        )
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(self.client._executor.submitted), 1)

    def test_auto_rejects_when_no_active_accounts_before_enqueue(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO chatgpt_account_health VALUES ('acct1','auth_unavailable','401','now')")
        with self.assertRaises(SessionOpsError) as caught:
            self.client.enqueue(
                principal_id="principal", tenant_id="tenant",
                operation="session.create", arguments={"account": "auto"},
                idempotency_key="key-none",
            )
        self.assertEqual(caught.exception.code, "RESOURCE_EXHAUSTED")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM chatgpt_jobs").fetchone()[0], 0)
        self.assertEqual(list(self.prompts.iterdir()), [])

    def test_database_initialization_seeds_disabled_accounts_without_legacy_gateway(self):
        client = ChatGPTDirectClient.__new__(ChatGPTDirectClient)
        client.database_path = Path(self.tmp.name) / "fresh.sqlite3"
        client._db_lock = threading.RLock()
        client._initialize_database(None)
        with sqlite3.connect(client.database_path) as conn:
            health = {row[0]: row[1] for row in conn.execute(
                "SELECT account,state FROM chatgpt_account_health"
            )}
        self.assertEqual(health["acct4"], "auth_unavailable")
        self.assertEqual(health["acct5"], "operator_disabled")

    def test_queue_capacity_rejects_burst_without_persisting_extra_job(self):
        first = self.client.enqueue(
            principal_id="principal", tenant_id="tenant",
            operation="session.create", arguments={"account": "auto"},
            idempotency_key="key-first",
        )
        self.assertEqual(first["status"], "queued")
        with self.assertRaises(SessionOpsError) as caught:
            self.client.enqueue(
                principal_id="principal", tenant_id="tenant",
                operation="session.create", arguments={"account": "auto"},
                idempotency_key="key-overflow",
            )
        self.assertEqual(caught.exception.code, "RESOURCE_EXHAUSTED")
        self.assertIn("queue is full (1/1)", str(caught.exception))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM chatgpt_jobs").fetchone()[0], 1)
        self.assertEqual(len(self.client._executor.submitted), 1)
        self.assertEqual(len(list(self.prompts.iterdir())), 1)

    def test_restart_preserves_queued_and_marks_running_outcome_unknown(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""INSERT INTO chatgpt_jobs
                (job_id,tenant_id,principal_id,operation,idempotency_key,status,created_at)
                VALUES ('queued-1','tenant','principal','session.create','q1','queued','2026-01-01')""")
            conn.execute("""INSERT INTO chatgpt_jobs
                (job_id,tenant_id,principal_id,operation,idempotency_key,status,created_at)
                VALUES ('running-1','tenant','principal','session.send','r1','running','2026-01-02')""")
        client = ChatGPTDirectClient.__new__(ChatGPTDirectClient)
        client.database_path = self.db
        client._db_lock = threading.RLock()
        client._initialize_database(None)
        with sqlite3.connect(self.db) as conn:
            states = dict(conn.execute("SELECT job_id,status FROM chatgpt_jobs"))
            unknown = conn.execute("SELECT outcome_unknown FROM chatgpt_jobs WHERE job_id='running-1'").fetchone()[0]
        self.assertEqual(states["queued-1"], "queued")
        self.assertEqual(states["running-1"], "failed")
        self.assertEqual(unknown, 1)
        client._executor = _Executor()
        client._recover_queued_jobs()
        self.assertEqual(client._executor.submitted, [(client._execute_job, ("queued-1",))])

    def test_execution_rechecks_pinned_account_health(self):
        with self.assertRaises(SessionOpsError) as caught:
            self.client._validate_create_account("acct5")
        self.assertEqual(caught.exception.code, "RESOURCE_EXHAUSTED")
        self.assertIn("operator_disabled", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
