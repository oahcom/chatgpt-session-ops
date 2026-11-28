#!/usr/bin/env python3
"""Move the existing CLI runtime state out of the retired Sender code tree.

This copies only non-code runtime state. It never copies account credentials,
the Gateway database, job rows, or outbox events.
"""
from __future__ import annotations

import argparse
import hashlib
import os
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="/opt/so-sender/cli")
    parser.add_argument("--state", default="/var/lib/chatgpt-session-ops/mcp-state")
    parser.add_argument("--owner", default="chatgpt-sender")
    parser.add_argument("--group", default="chatgpt-sender")
    args = parser.parse_args()

    user = pwd.getpwnam(args.owner)
    group = grp.getgrnam(args.group)
    results = migrate_state(args.source, args.state, uid=user.pw_uid, gid=group.gr_gid)
    for name, status, digest in results:
        print("%s %s sha256=%s" % (status, name, digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
