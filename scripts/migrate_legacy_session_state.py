#!/usr/bin/env python3
"""One-time, read-only-source migration of session ownership into SO-MCP.

This copies only the ownership and account-health records required by the
in-process client. The legacy jobs/outbox remain untouched in the source DB
and are preserved by the separate archive step.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def migrate(source_path: Path, target_path: Path, *, allow_nonterminal=False):
    if not source_path.is_file():
        raise RuntimeError("legacy state database does not exist")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=10)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(str(target_path), timeout=10)
    target.row_factory = sqlite3.Row
    try:
        if not _table_exists(source, "session_owners"):
            raise RuntimeError("legacy database has no session_owners table")
        nonterminal = 0
        if _table_exists(source, "jobs"):
            nonterminal = source.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
        if nonterminal and not allow_nonterminal:
            raise RuntimeError(f"refusing migration while {nonterminal} legacy jobs are nonterminal")

        owners = source.execute(
            "SELECT conversation_id,tenant_id,principal_id,account,created_at FROM session_owners"
        ).fetchall()
        health = []
        if _table_exists(source, "account_health"):
            health = source.execute(
                "SELECT account,state,reason,updated_at FROM account_health"
            ).fetchall()

        target.executescript("""
            CREATE TABLE IF NOT EXISTS chatgpt_session_owners (
                conversation_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                account TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chatgpt_owners_principal
                ON chatgpt_session_owners(tenant_id,principal_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS chatgpt_account_health (
                account TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
        """)
        target.execute("BEGIN IMMEDIATE")
        target.executemany(
            "INSERT OR IGNORE INTO chatgpt_session_owners VALUES(?,?,?,?,?)",
            [tuple(row) for row in owners],
        )
        target.executemany(
            "INSERT OR REPLACE INTO chatgpt_account_health VALUES(?,?,?,?)",
            [tuple(row) for row in health],
        )
        target.commit()

        owner_count = target.execute(
            "SELECT count(*) FROM chatgpt_session_owners"
        ).fetchone()[0]
        owner_scopes = [dict(row) for row in target.execute(
            "SELECT tenant_id,principal_id,count(*) AS sessions "
            "FROM chatgpt_session_owners GROUP BY tenant_id,principal_id "
            "ORDER BY tenant_id,principal_id"
        )]
        health_state = [dict(row) for row in target.execute(
            "SELECT account,state,reason,updated_at FROM chatgpt_account_health ORDER BY account"
        )]
        if owner_count < len(owners):
            raise RuntimeError("destination owner count is lower than the source; migration is incomplete")

        jobs_by_status = []
        outbox_by_status = []
        if _table_exists(source, "jobs"):
            jobs_by_status = [dict(row) for row in source.execute(
                "SELECT operation,status,count(*) AS count FROM jobs GROUP BY operation,status ORDER BY operation,status"
            )]
        if _table_exists(source, "job_event_outbox"):
            outbox_by_status = [dict(row) for row in source.execute(
                "SELECT status,count(*) AS count FROM job_event_outbox GROUP BY status ORDER BY status"
            )]
        return {
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "source": str(source_path),
            "target": str(target_path),
            "source_owner_rows": len(owners),
            "destination_owner_rows": owner_count,
            "owner_scopes": owner_scopes,
            "account_health": health_state,
            "legacy_jobs_by_status": jobs_by_status,
            "legacy_outbox_by_status": outbox_by_status,
            "legacy_nonterminal_jobs": nonterminal,
            "legacy_jobs_and_outbox_copied": False,
        }
    finally:
        target.close()
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--allow-nonterminal", action="store_true")
    args = parser.parse_args()
    report = migrate(args.source, args.target, allow_nonterminal=args.allow_nonterminal)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
