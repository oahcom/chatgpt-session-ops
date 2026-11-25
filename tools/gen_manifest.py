#!/usr/bin/env python3
"""Generate a deployment manifest from Git objects and external config files.

The application file list is explicit. Git worktree contents are never read.
Only the external objects are read from local filesystem paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_EXTERNAL_KINDS = {
    "systemd-unit",
    "nginx-snippet",
    "environment-file",
}

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

_DATABASE_CONTRACT_VERSION = 1


class ManifestError(Exception):
    """Invalid input or an unavailable Git object."""


def _run_git(repo: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ManifestError("git executable was not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise ManifestError(
            "git command failed: %s%s"
            % (" ".join(args), (": " + detail) if detail else "")
        ) from exc
    return completed.stdout


def _validate_commit(repo: Path, commit: str) -> str:
    if not _COMMIT_RE.fullmatch(commit):
        raise ManifestError("--commit must be a full lowercase 40-character SHA")

    object_type = _run_git(repo, "cat-file", "-t", commit)
    if object_type.strip() != b"commit":
        raise ManifestError("--commit does not identify a commit object")

    resolved = _run_git(repo, "rev-parse", "--verify", commit).decode().strip()
    if resolved != commit:
        raise ManifestError("Git resolved commit does not equal supplied SHA")
    return resolved


def _read_paths(paths_from: str) -> list[str]:
    if paths_from == "-":
        lines = sys.stdin.read().splitlines()
    else:
        try:
            lines = Path(paths_from).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ManifestError(
                "cannot read paths file %r: %s" % (paths_from, exc)
            ) from exc

    paths: list[str] = []
    seen: set[str] = set()

    for raw in lines:
        path = raw.strip()
        if not path or path.startswith("#"):
            continue

        path = path.removeprefix("./")
        if (
            path.startswith("/")
            or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ManifestError("unsafe or non-normalized repository path: %r" % path)

        if path in seen:
            raise ManifestError("duplicate repository path: %s" % path)

        seen.add(path)
        paths.append(path)

    if not paths:
        raise ManifestError("the explicit managed path list is empty")

    return paths


def _git_blob(repo: Path, commit: str, path: str) -> bytes:
    spec = "%s:%s" % (commit, path)
    try:
        return _run_git(repo, "cat-file", "blob", spec)
    except ManifestError as exc:
        raise ManifestError(
            "cannot read Git blob for %s at %s" % (path, commit)
        ) from exc


def _git_blob_sha1(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lf_normalize(data: bytes) -> bytes:
    """Normalize CRLF only; preserve lone CR bytes and all other content."""
    return data.replace(b"\r\n", b"\n")


def _file_entry(repo: Path, commit: str, path: str) -> dict:
    raw = _git_blob(repo, commit, path)
    lf = _lf_normalize(raw)

    return {
        "path": path,
        "git_blob_sha1": _git_blob_sha1(raw),
        "sha256_lf": _sha256(lf),
        "sha256_raw": _sha256(raw),
        "bytes": len(lf),
    }


def _parse_external(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise ManifestError(
            "--external must have the form kind=/absolute/or/relative/path"
        )

    kind, path = spec.split("=", 1)
    if kind not in _EXTERNAL_KINDS:
        raise ManifestError(
            "unsupported external kind %r; expected one of %s"
            % (kind, ", ".join(sorted(_EXTERNAL_KINDS)))
        )
    if not path:
        raise ManifestError("external object path must not be empty")

    return kind, path


def _external_entry(spec: str) -> dict:
    kind, path_text = _parse_external(spec)
    path = Path(path_text)

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ManifestError(
            "cannot read external object %s: %s" % (path, exc)
        ) from exc

    # Environment files may contain credentials. Record metadata only;
    # never read their content into the manifest.
    return {
        "kind": kind,
        "path": str(path),
        "sha256": _sha256(data),
        "bytes": len(data),
    }


def _parse_required_column(value: str) -> tuple[str, str]:
    if "." not in value:
        raise ManifestError(
            "--db-required-column must have the form TABLE.COLUMN"
        )
    table, column = value.split(".", 1)
    if not table or not column:
        raise ManifestError(
            "--db-required-column must have the form TABLE.COLUMN"
        )
    return table, column


def _build_manifest(args: argparse.Namespace) -> dict:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise ManifestError("--repo is not a directory: %s" % repo)

    commit = _validate_commit(repo, args.commit)
    paths = _read_paths(args.paths_from)

    files = [
        _file_entry(repo, commit, path)
        for path in paths
    ]

    external_objects = [
        _external_entry(spec)
        for spec in args.external
    ]

    required_columns: dict[str, list[str]] = {}
    for value in args.db_required_column:
        table, column = _parse_required_column(value)
        columns = required_columns.setdefault(table, [])
        if column in columns:
            raise ManifestError(
                "duplicate required database column: %s.%s"
                % (table, column)
            )
        columns.append(column)

    database = None

    if args.db_path is None:
        if required_columns:
            raise ManifestError(
                "--db-path is required when --db-required-column is supplied"
            )
    else:
        db_path = Path(args.db_path)

        if not db_path.is_absolute() or "\x00" in args.db_path:
            raise ManifestError("--db-path must be an absolute path")

        if not db_path.is_file():
            raise ManifestError("--db-path must name an existing file")

        database = {
            "path": str(db_path),
            "schema_version": _DATABASE_CONTRACT_VERSION,
            "required_columns": {
                table: columns
                for table, columns in sorted(required_columns.items())
            },
        }

    generated_at = args.generated_at or datetime.now(
        timezone.utc
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    manifest = {
        "format_version": 1,
        "commit": commit,
        "generated_at": generated_at,
        "files": files,
        "external_objects": external_objects,
        "forbidden": [
            ".git",
            "tests",
            "deploy",
            "*.bak-*",
            "**/*.bak-*",
        ],
        "allowed_unmanaged": list(args.allow_unmanaged),
    }

    if database is not None:
        manifest["database"] = database

    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a deployment manifest from an explicit list of Git blobs."
        )
    )
    parser.add_argument("--repo", required=True, help="Git clone path")
    parser.add_argument("--commit", required=True, help="Full 40-character commit SHA")
    parser.add_argument(
        "--paths-from",
        required=True,
        metavar="FILE|-",
        help="Explicit repository paths, one per line; '-' reads stdin",
    )
    parser.add_argument(
        "--external",
        action="append",
        default=[],
        metavar="KIND=PATH",
        help="External object to hash; may be repeated",
    )
    parser.add_argument("--out", required=True, help="Output manifest path")
    parser.add_argument(
        "--generated-at",
        help="Override generated_at (useful for reproducible generation)",
    )
    parser.add_argument(
        "--db-path",
        help="Target SQLite database path on the deployment host",
    )
    parser.add_argument(
        "--allow-unmanaged",
        action="append",
        default=[],
        metavar="GLOB",
        help="allow an unmanaged deployment path matching this glob; repeatable",
    )
    parser.add_argument(
        "--db-required-column",
        action="append",
        default=[],
        metavar="TABLE.COLUMN",
        help="Required SQLite column; requires --db-path; may be repeated",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        manifest = _build_manifest(args)
        output = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                separators=(",", ": "),
            )
            + "\n"
        )

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8", newline="\n")
    except ManifestError as exc:
        print("gen_manifest.py: error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("gen_manifest.py: error: %s" % exc, file=sys.stderr)
        return 1

    print(
        "manifest written: %s (%d files, %d external objects, commit %s)"
        % (
            args.out,
            len(manifest["files"]),
            len(manifest["external_objects"]),
            manifest["commit"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
