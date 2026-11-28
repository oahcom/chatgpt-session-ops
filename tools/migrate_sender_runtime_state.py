#!/usr/bin/env python3
"""Move the existing CLI runtime state out of the retired Sender code tree.

This copies only non-code runtime state. It never copies account credentials,
the Gateway database, job rows, or outbox events.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import pwd
import grp
import shutil
import stat
import tempfile
from pathlib import Path

STATE_FILES = (
    "account-rotation.json",
    "conv-accounts.json",
    "conv-accounts.json.highwater.json",
    "proxy.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrate_state(
    source_dir: str | Path,
    state_dir: str | Path,
    *,
    uid: int | None = None,
    gid: int | None = None,
) -> list[tuple[str, str, str]]:
    source = Path(source_dir).resolve(strict=True)
    target = Path(state_dir).resolve(strict=True)
    if source == target or source in target.parents or target in source.parents:
        raise RuntimeError("source and state directories must be separate")

    results = []
    for name in STATE_FILES:
        src = source / name
        dst = target / name
        try:
            source_mode = src.lstat().st_mode
        except FileNotFoundError as exc:
            raise RuntimeError("required CLI state file is missing: %s" % name) from exc
        if not stat.S_ISREG(source_mode):
            raise RuntimeError("CLI state source must be a regular file: %s" % name)

        source_digest = _sha256(src)
        if dst.exists():
            if not dst.is_file() or _sha256(dst) != source_digest:
                raise RuntimeError("state destination differs; refusing overwrite: %s" % dst)
            os.chmod(dst, 0o660)
            if uid is not None and gid is not None:
                os.chown(dst, uid, gid)
            results.append((name, "UNCHANGED", source_digest))
            continue

        fd, temp_name = tempfile.mkstemp(prefix=".%s." % name, dir=str(target))
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as output, src.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_path, 0o660)
            if uid is not None and gid is not None:
                os.chown(temp_path, uid, gid)
            if _sha256(temp_path) != source_digest:
                raise RuntimeError("copied state checksum mismatch: %s" % name)
            os.replace(temp_path, dst)
            dir_fd = os.open(str(target), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        results.append((name, "COPIED", source_digest))
    return results


def merge_legacy_account_health(
    state_dir: str | Path,
    legacy_db: str | Path,
    *,
    uid: int | None = None,
    gid: int | None = None,
) -> list[tuple[str, str]]:
    """Translate legacy account health into the CLI's native rotation state."""
    state_path = Path(state_dir).resolve(strict=True) / "account-rotation.json"
    source_path = Path(legacy_db)
    if not source_path.is_file():
        return []
    if state_path.is_symlink() or not state_path.is_file():
        raise RuntimeError("account-rotation.json is missing or not a regular file")

    uri = "file:" + str(source_path.resolve()) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "account_health" not in tables:
            return []
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(account_health)")
        }
        if not {"account", "state", "reason"}.issubset(columns):
            raise RuntimeError("legacy account_health schema is incomplete")
        health_rows = conn.execute(
            "SELECT account, state, reason FROM account_health ORDER BY account"
        ).fetchall()
    finally:
        conn.close()

    try:
        rotation = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("account-rotation.json cannot be read") from exc
    if not isinstance(rotation, dict):
        raise RuntimeError("account-rotation.json must contain an object")
    unavailable = rotation.setdefault("unavailable", {})
    if not isinstance(unavailable, dict):
        raise RuntimeError("account-rotation.json unavailable field is invalid")

    imported = []
    now = time.time()
    at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    # Both states require an explicit operator re-enable after credentials or
    # policy are reviewed; they must not silently re-enter rotation after 6h.
    permanent_ttl = 10 * 365 * 24 * 60 * 60
    for account, state, reason in health_rows:
        if (
            not isinstance(account, str)
            or not re.fullmatch(r"acct[1-9][0-9]*", account)
            or state not in ("auth_unavailable", "operator_disabled")
        ):
            continue
        current = unavailable.get(account)
        if isinstance(current, dict) and current.get("reason"):
            continue  # preserve an existing, more specific CLI disable reason
        unavailable[account] = {
            "reason": "legacy Gateway %s: %s" % (state, reason),
            "at": at,
            "epoch": now,
            "ttl": permanent_ttl,
        }
        imported.append((account, state))

    if imported:
        fd, temp_name = tempfile.mkstemp(
            prefix=".account-rotation.", dir=str(state_path.parent)
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(rotation, output, ensure_ascii=False, indent=2)
                output.write("\\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_path, 0o660)
            if uid is not None and gid is not None:
                os.chown(temp_path, uid, gid)
            os.replace(temp_path, state_path)
            dir_fd = os.open(str(state_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return imported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="/opt/so-sender/cli")
    parser.add_argument("--state", default="/var/lib/chatgpt-session-ops/mcp-state")
    parser.add_argument("--owner", default="chatgpt-sender")
    parser.add_argument("--group", default="chatgpt-sender")
    parser.add_argument(
        "--legacy-db",
        default="/var/lib/chatgpt-session-ops/state/sender-gateway.sqlite3",
    )
    args = parser.parse_args()

    user = pwd.getpwnam(args.owner)
    group = grp.getgrnam(args.group)
    results = migrate_state(args.source, args.state, uid=user.pw_uid, gid=group.gr_gid)
    for name, status, digest in results:
        print("%s %s sha256=%s" % (status, name, digest))
    health = merge_legacy_account_health(
        args.state, args.legacy_db, uid=user.pw_uid, gid=group.gr_gid
    )
    for account, state in health:
        print("IMPORTED_ACCOUNT_HEALTH %s state=%s" % (account, state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
