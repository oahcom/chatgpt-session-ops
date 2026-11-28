#!/usr/bin/env python3
"""Create a verified, permission-restricted SQLite snapshot of legacy Sender state.

The source is opened read-only and is never modified. The archive includes the
entire database (jobs, idempotency records, event outbox, owners and health),
not just the rows needed by the replacement in-process client.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def archive(source_path: Path, destination_path: Path) -> dict:
    source_path = source_path.expanduser().absolute()
    destination_path = destination_path.expanduser().absolute()
    if source_path.is_symlink() or not source_path.is_file():
        raise RuntimeError("source must be an existing regular file, not a symlink")
    if source_path.resolve() == destination_path.resolve(strict=False):
        raise RuntimeError("archive destination must differ from source")

    manifest_path = destination_path.with_name(destination_path.name + ".manifest.json")
    if destination_path.parent.is_symlink():
        raise RuntimeError("archive directory must not be a symlink")
    destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(destination_path.parent.stat().st_mode) & 0o077:
        raise RuntimeError("archive directory must not be accessible by group or others")
    if (destination_path.exists() or destination_path.is_symlink()
            or manifest_path.exists() or manifest_path.is_symlink()):
        raise RuntimeError("archive destination or manifest already exists; refusing overwrite")

    previous_umask = os.umask(0o077)
    source = target = None
    validated = False
    try:
        source = sqlite3.connect(
            f"file:{source_path}?mode=ro", uri=True, timeout=15
        )
        source.row_factory = sqlite3.Row
        target = sqlite3.connect(str(destination_path), timeout=15)
        target.row_factory = sqlite3.Row
        source.backup(target, pages=256, sleep=0.05)
        target.commit()

        integrity = [row[0] for row in target.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError("archive integrity_check failed: " + "; ".join(integrity[:5]))

        table_names = [row[0] for row in target.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        table_counts = {
            name: target.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for name in table_names
        }
        job_statuses = {}
        if _table_exists(target, "jobs"):
            job_statuses = {
                row["status"]: row["count"]
                for row in target.execute("SELECT status,count(*) AS count FROM jobs GROUP BY status")
            }
        outbox_statuses = {}
        if _table_exists(target, "job_event_outbox"):
            outbox_statuses = {
                row["status"]: row["count"]
                for row in target.execute("SELECT status,count(*) AS count FROM job_event_outbox GROUP BY status")
            }
        active_jobs = []
        if _table_exists(target, "jobs"):
            active_jobs = [dict(row) for row in target.execute(
                "SELECT job_id,operation,status,created_at,started_at,attempt_count,outcome_unknown "
                "FROM jobs WHERE status IN ('queued','running') ORDER BY created_at,job_id"
            )]
        target.close()
        target = None

        os.chmod(destination_path, 0o600)
        with destination_path.open("rb") as stream:
            os.fsync(stream.fileno())
        digest = _sha256(destination_path)
        validated = True
        report = {
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "source": str(source_path),
            "archive": str(destination_path),
            "archive_bytes": destination_path.stat().st_size,
            "sha256": digest,
            "integrity_check": "ok",
            "table_counts": table_counts,
            "job_statuses": job_statuses,
            "outbox_statuses": outbox_statuses,
            "nonterminal_jobs": active_jobs,
            "source_modified": False,
        }
        payload = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        fd = os.open(manifest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(destination_path.parent)
        return report
    except Exception:
        if not validated:
            destination_path.unlink(missing_ok=True)
        raise
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        os.umask(previous_umask)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(archive(args.source, args.destination), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"archive failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
