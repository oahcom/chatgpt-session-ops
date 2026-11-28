import sqlite3
from pathlib import Path

import pytest

from scripts.migrate_legacy_session_state import migrate


def make_legacy(path: Path, *, nonterminal=0):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE session_owners (
                conversation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                principal_id TEXT NOT NULL, account TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE account_health (
                account TEXT PRIMARY KEY, state TEXT NOT NULL,
                reason TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY, operation TEXT, status TEXT
            );
            CREATE TABLE job_event_outbox (
                event_id TEXT PRIMARY KEY, status TEXT NOT NULL
            );
            INSERT INTO session_owners VALUES
                ('c1','tenant-a','principal-a','acct1','2026-01-01T00:00:00Z'),
                ('c2','tenant-b','principal-b','acct2','2026-01-02T00:00:00Z');
            INSERT INTO account_health VALUES
                ('acct4','auth_unavailable','401','2026-01-01T00:00:00Z'),
                ('acct5','operator_disabled','disabled','2026-01-01T00:00:00Z');
            INSERT INTO job_event_outbox VALUES ('e1','pending');
        """)
        for i in range(nonterminal):
            conn.execute("INSERT INTO jobs VALUES(?,?,?)", (f"j{i}", "session.send", "running"))


def test_migration_preserves_each_principal_scope_and_health(tmp_path):
    source, target = tmp_path / "legacy.sqlite3", tmp_path / "mcp.sqlite3"
    make_legacy(source)
    report = migrate(source, target)
    assert report["source_owner_rows"] == 2
    assert report["destination_owner_rows"] == 2
    assert len(report["owner_scopes"]) == 2
    assert report["legacy_outbox_by_status"] == [{"status": "pending", "count": 1}]
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT conversation_id,tenant_id,principal_id,account FROM chatgpt_session_owners ORDER BY conversation_id").fetchall() == [
            ("c1", "tenant-a", "principal-a", "acct1"),
            ("c2", "tenant-b", "principal-b", "acct2"),
        ]
        assert conn.execute("SELECT account,state FROM chatgpt_account_health ORDER BY account").fetchall() == [
            ("acct4", "auth_unavailable"), ("acct5", "operator_disabled")
        ]
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT count(*) FROM job_event_outbox WHERE status='pending'").fetchone()[0] == 1


def test_migration_refuses_nonterminal_jobs_by_default(tmp_path):
    source, target = tmp_path / "legacy.sqlite3", tmp_path / "mcp.sqlite3"
    make_legacy(source, nonterminal=2)
    with pytest.raises(RuntimeError, match="nonterminal"):
        migrate(source, target)


def test_migration_can_be_repeated_without_duplicate_owners(tmp_path):
    source, target = tmp_path / "legacy.sqlite3", tmp_path / "mcp.sqlite3"
    make_legacy(source)
    migrate(source, target)
    migrate(source, target)
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM chatgpt_session_owners").fetchone()[0] == 2
