"""One-time, idempotent import of legacy Sender Gateway session ownership.

Only session_owners are migrated. Jobs, worker leases, HTTP state and event
outbox are intentionally not recreated: MCP now invokes the CLI synchronously.
The source database is opened read-only and is never modified.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from src.session_ops.chatgpt_sessions import ChatGPTSessionRegistry

_ACCOUNT = re.compile(r"^(?:default|acct[1-9][0-9]*)$")


def _rows(path: Path) -> list[dict[str, str]]:
    uri = "file:" + str(path.resolve()) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(session_owners)")
        }
        required = {"conversation_id", "tenant_id", "principal_id", "account", "created_at"}
        if not required.issubset(columns):
            raise RuntimeError("source session_owners schema is incomplete")
        result = []
        for row in conn.execute(
            "SELECT conversation_id, tenant_id, principal_id, account, created_at "
            "FROM session_owners ORDER BY conversation_id"
        ):
            item = dict(row)
            if not all(isinstance(item[k], str) and item[k] for k in required):
                raise RuntimeError("source contains an incomplete session owner")
            if not _ACCOUNT.fullmatch(item["account"]):
                raise RuntimeError("source contains an invalid account identity")
            result.append(item)
        return result
    finally:
        conn.close()


def migrate(source: Path, target: Path, *, apply: bool = False) -> dict[str, int]:
    if not source.is_file():
        raise RuntimeError("source database does not exist")
    if source.resolve() == target.resolve():
        raise RuntimeError("source and target databases must differ")

    source_rows = _rows(source)
    registry = ChatGPTSessionRegistry(target)
    registry.initialize()

    conn = sqlite3.connect(str(target), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        existing = {
            row["conversation_id"]: dict(row)
            for row in conn.execute(
                "SELECT conversation_id, tenant_id, principal_id, account, created_at "
                "FROM chatgpt_session_owners"
            )
        }
        inserts = []
        for row in source_rows:
            current = existing.get(row["conversation_id"])
            if current is None:
                inserts.append(row)
            elif any(current[key] != row[key] for key in (
                "tenant_id", "principal_id", "account"
            )):
                raise RuntimeError(
                    "owner conflict for conversation " + row["conversation_id"]
                )

        if apply:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in inserts:
                    conn.execute(
                        "INSERT INTO chatgpt_session_owners "
                        "(conversation_id, tenant_id, principal_id, account, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            row["conversation_id"], row["tenant_id"],
                            row["principal_id"], row["account"], row["created_at"],
                        ),
                    )
                for row in source_rows:
                    check = conn.execute(
                        "SELECT tenant_id, principal_id, account FROM chatgpt_session_owners "
                        "WHERE conversation_id = ?",
                        (row["conversation_id"],),
                    ).fetchone()
                    if check is None or any(
                        check[key] != row[key]
                        for key in ("tenant_id", "principal_id", "account")
                    ):
                        raise RuntimeError("post-migration owner verification failed")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return {
            "source_rows": len(source_rows),
            "already_present": len(source_rows) - len(inserts),
            "to_insert": len(inserts),
            "inserted": len(inserts) if apply else 0,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="commit the verified import; without this flag only report the plan",
    )
    args = parser.parse_args(argv)
    try:
        result = migrate(args.source, args.target, apply=args.apply)
    except Exception as exc:
        print("ERROR: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    print(
        "%s source_rows=%d already_present=%d to_insert=%d inserted=%d"
        % (
            "APPLIED" if args.apply else "DRY_RUN",
            result["source_rows"], result["already_present"],
            result["to_insert"], result["inserted"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
