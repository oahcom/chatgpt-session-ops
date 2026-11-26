#!/usr/bin/env python3
"""Verify a deployed Session Ops tree against a commit-derived manifest."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha1(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def normalize_lf(data: bytes) -> bytes:
    # Deliberately normalize CRLF only. Do not rewrite lone CR bytes.
    return data.replace(b"\r\n", b"\n")


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def is_catch_all(pattern: str) -> bool:
    """True if `pattern` matches every path, not just a family of them.

    `fnmatch.fnmatchcase` treats `*` as "anything including `/`", so `*`, `**`
    and `*/*` all match a path of any depth. That makes an allow-list entry
    into an exemption for the entire tree — which is not what an operator
    writing `pkg*` or `*.pyc` means, and is a very easy typo to make.

    Detected by construction rather than by string comparison: a pattern is a
    catch-all if it matches a set of paths chosen so that no narrower pattern
    can match all of them.
    """
    probes = ("a", "a/b", "a/b/c", ".hidden", "deep/x/y/z.py", "pkg/cache/x")
    return all(fnmatch.fnmatchcase(probe, pattern) for probe in probes)


def add_issue(
    issues: list[dict[str, Any]],
    code: str,
    path: str,
    detail: str,
) -> None:
    issues.append({"code": code, "path": path, "detail": detail})


def verify_database(
    spec: dict[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    db_path = Path(spec["path"])
    result: dict[str, Any] = {
        "path": str(db_path),
        "expected_schema_version": spec["schema_version"],
        "schema_contract_ok": False,
        "tables": {},
    }

    if not db_path.is_file():
        add_issue(issues, "DB_MISSING", str(db_path), "database file is missing")
        return result

    # URI read-only mode prevents verification from creating or modifying DB.
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            for table, required in spec["required_columns"].items():
                rows = conn.execute(
                    "PRAGMA table_info(%s)" % quote_identifier(table)
                ).fetchall()
                actual = sorted(row[1] for row in rows)
                missing = sorted(set(required) - set(actual))
                result["tables"][table] = {
                    "actual_columns": actual,
                    "required_columns": sorted(required),
                    "missing_columns": missing,
                }
                if missing:
                    add_issue(
                        issues,
                        "DB_SCHEMA_BEHIND",
                        str(db_path),
                        "%s missing columns: %s"
                        % (table, ", ".join(missing)),
                    )

            result["schema_contract_ok"] = all(
                not value["missing_columns"]
                for value in result["tables"].values()
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        add_issue(issues, "DB_SCHEMA_ERROR", str(db_path), str(exc))

    return result


def quote_identifier(value: str) -> str:
    # SQLite identifiers cannot be bound as parameters.
    if not value or "\x00" in value:
        raise ValueError("invalid SQLite identifier")
    return '"' + value.replace('"', '""') + '"'


def verify(
    root: Path,
    manifest_path: Path,
    strict: bool = False,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "root": str(root),
            "manifest": str(manifest_path),
            "issues": [{
                "code": "MANIFEST_ERROR",
                "path": str(manifest_path),
                "detail": str(exc),
            }],
            "warnings": [],
        }

    if manifest.get("format_version") != 1:
        add_issue(issues, "MANIFEST_VERSION", str(manifest_path),
                  "unsupported format_version")

    commit = manifest.get("commit", "")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(c not in "0123456789abcdef" for c in commit)
    ):
        add_issue(issues, "INVALID_COMMIT", "commit",
                  "commit must be a full lowercase 40-character SHA")
    elif commit in {"0" * 40, "f" * 40}:
        add_issue(
            issues,
            "INVALID_COMMIT",
            "commit",
            "commit must not be an all-zero or all-f placeholder SHA",
        )

    expected: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("files", []):
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            add_issue(issues, "INVALID_FILE_ENTRY", "files",
                      "file entry has no valid path")
            continue
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts:
            add_issue(issues, "UNSAFE_MANIFEST_PATH", rel,
                      "manifest file path must be relative and stay under root")
            continue
        if rel in expected:
            add_issue(issues, "DUPLICATE_MANIFEST_PATH", rel,
                      "duplicate file entry")
            continue
        expected[rel] = entry

    allowed = manifest.get("allowed_unmanaged", [])
    forbidden = manifest.get("forbidden", [])

    # A manifest that manages nothing verifies nothing, and `ok` is `not
    # issues`, so it used to report `OK: 0/0 files verified` and exit 0. That
    # is the failure this tool exists to prevent: `0/0` reads as success at a
    # glance. `gen_manifest.py` refuses to build such a manifest, so this
    # arrives only through a truncated download or a hand-edited file — which
    # is exactly when you want the verifier to say so rather than agree.
    if not expected:
        add_issue(issues, "MANIFEST_EMPTY", str(manifest_path),
                  "manifest manages no files; nothing would be verified")

    # `fnmatch` lets `*` cross `/`, so one allow-list entry can exempt the
    # whole tree. The manifest is untrusted input and the reviewed pattern set
    # lives in a document, not in the tool, so this is reported rather than
    # silently honoured: an operator who wrote `*` to ignore a build directory
    # has exempted every file in the deployment root and nothing said so.
    for pattern in allowed:
        if is_catch_all(pattern):
            add_issue(issues, "ALLOW_LIST_CATCH_ALL", str(pattern),
                      "allow-list pattern matches every path, including "
                      "unmanaged files at any depth")

    # Forbidden artifacts are reported even if an allow-list could match them.
    for pattern in forbidden:
        if pattern == ".git" or pattern == "tests" or pattern == "deploy":
            candidate = root / pattern
            if candidate.exists():
                add_issue(issues, "FORBIDDEN_PATH", pattern,
                          "forbidden deployment path exists")

    read_paths: set[str] = set()
    skipped_paths: set[str] = set()

    read_files = 0
    checked_files = 0
    failed_files = 0
    byte_identical_files = 0

    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()

        is_forbidden = matches_any(rel, forbidden)
        if is_forbidden:
            add_issue(issues, "FORBIDDEN_PATH", rel,
                      "forbidden deployment artifact exists")

        # Check symlinks before is_dir(): Path.is_dir() follows symlinks.
        # rglob yields the symlink directory itself but does not descend
        # into it. A link on a managed path, or on any ancestor of one, is
        # a failure: the commit carries no links. Links that the allow-list
        # covers are exempt and their targets are not content-verified;
        # that exemption is about unmanaged paths, not about ancestors of
        # managed ones, which are caught above.
        if path.is_symlink():
            covers_managed = rel in expected or any(
                managed_rel.startswith(rel + "/") for managed_rel in expected
            )
            if covers_managed or not matches_any(rel, allowed):
                add_issue(
                    issues,
                    "UNEXPECTED_SYMLINK",
                    rel,
                    "deployment tree must not contain symlinks",
                )
            for managed_rel in expected:
                if (
                    managed_rel == rel
                    or managed_rel.startswith(rel + "/")
                ):
                    skipped_paths.add(managed_rel)
            continue

        if is_forbidden:
            if rel in expected:
                skipped_paths.add(rel)
            continue

        if path.is_dir():
            continue

        if rel not in expected:
            if not matches_any(rel, allowed):
                add_issue(issues, "UNMANAGED_FILE", rel,
                          "file is not listed in manifest")
            continue

        if path.is_symlink():
            add_issue(issues, "UNEXPECTED_SYMLINK", rel,
                      "managed files must be regular files")
            skipped_paths.add(rel)
            continue

        entry = expected[rel]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            add_issue(issues, "FILE_READ_ERROR", rel, str(exc))
            skipped_paths.add(rel)
            continue

        read_files += 1
        read_paths.add(rel)
        file_issue_start = len(issues)

        lf = normalize_lf(raw)

        # Git blob identity is defined over the exact bytes, not over the
        # LF-normalized representation.
        actual_blob = git_blob_sha1(raw)
        actual_lf_sha = sha256(lf)
        actual_raw_sha = sha256(raw)

        expected_blob = entry.get("git_blob_sha1")
        expected_lf_sha = entry.get("sha256_lf")
        expected_raw_sha = entry.get("sha256_raw")

        # Content equality is judged after the explicitly permitted
        # CRLF-to-LF normalization.
        lf_matches = actual_lf_sha == expected_lf_sha
        blob_matches = actual_blob == expected_blob
        raw_matches = actual_raw_sha == expected_raw_sha

        if not lf_matches:
            add_issue(
                issues,
                "CONTENT_MISMATCH",
                rel,
                "LF-normalized SHA-256 differs from manifest",
            )

        # Compare Git blob identity independently of the LF content check.
        # Keep this comparison independent: otherwise a bad manifest blob
        # can pass whenever the LF and raw SHA-256 happen to match.
        # A raw-byte difference with matching LF-normalized content is
        # line-ending drift, not a content identity failure.
        eol_drift = lf_matches and not raw_matches

        if not blob_matches and not eol_drift:
            add_issue(
                issues,
                "GIT_BLOB_MISMATCH",
                rel,
                "raw Git blob SHA-1 differs from manifest",
            )

        if eol_drift:
            warning = {
                "code": "EOL_DRIFT",
                "path": rel,
                "detail": (
                    "raw bytes or Git blob identity differs; "
                    "CRLF-normalized content matches"
                ),
            }
            if strict:
                issues.append(warning)
            else:
                warnings.append(warning)

        # The manifest byte count is the LF-normalized byte count.
        if len(lf) != entry.get("bytes"):
            add_issue(issues, "BYTE_COUNT_MISMATCH", rel,
                      "LF-normalized byte count differs from manifest")

        # Counted from the per-file raw digest, never derived from
        # checked_files or from the warning count: a warning need not be
        # EOL drift, and under --strict the same drift is an issue. Both
        # modes must therefore report the same value for one tree.
        if raw_matches:
            byte_identical_files += 1

        # checked_files means successfully read AND passed all per-file
        # checks. A non-strict EOL warning does not fail the file.
        if len(issues) == file_issue_start:
            checked_files += 1
        else:
            failed_files += 1

    # Every expected entry must have been read, or explicitly accounted
    # for as skipped. This also catches entries hidden by missing dirs.
    for rel in sorted(expected):
        if rel in read_paths or rel in skipped_paths:
            continue

        add_issue(issues, "MISSING_FILE", rel,
                  "manifest-managed file is absent")
        skipped_paths.add(rel)

    skipped_files = len(skipped_paths)

    for obj in manifest.get("external_objects", []):
        path_text = obj.get("path", "")
        path = Path(path_text)
        if not path.is_absolute():
            add_issue(issues, "INVALID_EXTERNAL_PATH", path_text,
                      "external object path must be absolute")
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            add_issue(issues, "EXTERNAL_OBJECT_ERROR", path_text, str(exc))
            continue
        if sha256(raw) != obj.get("sha256"):
            add_issue(issues, "EXTERNAL_DRIFT", path_text,
                      "external object SHA-256 differs from manifest")

    database_result = None
    if "database" in manifest:
        database_result = verify_database(manifest["database"], issues)

    return {
        "ok": not issues,
        "root": str(root),
        "manifest": str(manifest_path),
        "commit": commit,
        "strict": strict,
        "expected_files": len(expected),
        "read_files": read_files,
        "checked_files": checked_files,
        "failed_files": failed_files,
        "skipped_files": skipped_files,
        "byte_identical_files": byte_identical_files,
        "checked_external_objects": len(manifest.get("external_objects", [])),
        "database": database_result,
        "issues": issues,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        result = {
            "ok": False,
            "root": str(root),
            "issues": [{
                "code": "ROOT_MISSING",
                "path": str(root),
                "detail": "deployment root is not a directory",
            }],
            "warnings": [],
        }
    else:
        result = verify(root, args.manifest.resolve(), args.strict)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["ok"]:
        print(
            "DEPLOY VERIFY OK: %d/%d files verified, byte-identical %d/%d, "
            "%d external objects"
            % (
                result.get("checked_files", 0),
                result.get("expected_files", 0),
                result.get("byte_identical_files", 0),
                result.get("expected_files", 0),
                result.get("checked_external_objects", 0),
            )
        )
        if result.get("warnings"):
            print("WARNINGS: %d (EOL drift)" % len(result["warnings"]))
        return 0

    print("DEPLOY VERIFY FAILED: %d issue(s)" % len(result.get("issues", [])))
    return 1


if __name__ == "__main__":
    sys.exit(main())
