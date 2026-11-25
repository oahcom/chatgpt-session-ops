from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.verify_deploy import verify


COMMIT = "a" * 40


def blob_sha1(data: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    ).hexdigest()


def make_entry(path: str, data: bytes) -> dict:
    lf = data.replace(b"\r\n", b"\n")
    return {
        "path": path,
        "git_blob_sha1": blob_sha1(lf),
        "sha256_lf": hashlib.sha256(lf).hexdigest(),
        "sha256_raw": hashlib.sha256(lf).hexdigest(),
        "bytes": len(lf),
    }


class VerifyDeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        self.manifest_path = Path(self.tmp.name) / "manifest.json"

        self.content = b"print('session ops')\n"
        (self.root / "app.py").write_bytes(self.content)
        self.write_manifest([make_entry("app.py", self.content)])

    def write_manifest(self, files: list[dict]) -> None:
        self.manifest_path.write_text(
            json.dumps({
                "format_version": 1,
                "commit": COMMIT,
                "generated_at": "2026-11-25T00:00:00Z",
                "files": files,
                "external_objects": [],
                "allowed_unmanaged": [],
                "forbidden": [
                    ".git",
                    "tests",
                    "deploy",
                    "*.bak-*",
                    "**/*.bak-*",
                ],
            }),
            encoding="utf-8",
        )

    def test_content_matches(self) -> None:
        result = verify(self.root, self.manifest_path)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["warnings"], [])

    def test_all_zero_commit_is_rejected(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["commit"] = "0" * 40
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                issue["code"] == "INVALID_COMMIT"
                and issue["path"] == "commit"
                for issue in result["issues"]
            ),
            result["issues"],
        )

    def test_all_f_commit_is_rejected(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["commit"] = "f" * 40
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(issue["code"] == "INVALID_COMMIT"
                for issue in result["issues"]),
            result["issues"],
        )

    def test_crlf_content_produces_only_eol_drift(self) -> None:
        lf = b"print('session ops')\n"
        crlf = b"print('session ops')\r\n"
        (self.root / "app.py").write_bytes(crlf)

        entry = {
            "path": "app.py",
            "git_blob_sha1": blob_sha1(lf),
            "sha256_lf": hashlib.sha256(lf).hexdigest(),
            "sha256_raw": hashlib.sha256(lf).hexdigest(),
            "bytes": len(lf),
        }
        self.write_manifest([entry])

        result = verify(self.root, self.manifest_path)

        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(
            [w["code"] for w in result["warnings"]],
            ["EOL_DRIFT"],
        )
        self.assertFalse(
            any(i["code"] == "GIT_BLOB_MISMATCH" for i in result["issues"])
        )

        strict_result = verify(self.root, self.manifest_path, strict=True)
        self.assertFalse(strict_result["ok"])
        self.assertEqual(
            [i["code"] for i in strict_result["issues"]],
            ["EOL_DRIFT"],
        )

    def test_real_change_reports_both_codes(self) -> None:
        (self.root / "app.py").write_bytes(b"print('different content')\n")

        result = verify(self.root, self.manifest_path)

        codes = [i["code"] for i in result["issues"]]
        self.assertIn("CONTENT_MISMATCH", codes)
        self.assertIn("GIT_BLOB_MISMATCH", codes)

    def test_symlinked_managed_directory_is_rejected(self) -> None:
        pkg = self.root / "pkg"
        pkg.mkdir()
        (pkg / "auth.py").write_bytes(b"authentic = True\n")
        self.write_manifest([make_entry("pkg/auth.py", b"authentic = True\n")])

        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "auth.py").write_bytes(b"different = True\n")

        shutil.move(str(pkg), str(Path(self.tmp.name) / "moved"))

        os.symlink(str(elsewhere), str(pkg), target_is_directory=True)

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"], result)
        self.assertTrue(
            any(
                issue["code"] == "UNEXPECTED_SYMLINK"
                and issue["path"] == "pkg"
                for issue in result["issues"]
            ),
            result["issues"],
        )
        self.assertEqual(result["checked_files"], 0)
        self.assertEqual(result["skipped_files"], 1)

    def test_symlink_under_allowed_path_is_not_an_issue(self) -> None:
        venv = self.root / ".venv" / "bin"
        venv.mkdir(parents=True)
        real = Path(self.tmp.name) / "python-real"
        real.write_bytes(b"interpreter\n")
        os.symlink(str(real), str(venv / "python"))

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["allowed_unmanaged"] = [".venv/**"]
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = verify(self.root, self.manifest_path)

        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["checked_files"], 1)

    def test_checked_files_counts_only_verified_files(self) -> None:
        second = b"other = 2\n"
        (self.root / "b.py").write_bytes(second)
        self.write_manifest([
            make_entry("app.py", self.content),
            make_entry("b.py", second),
        ])

        result = verify(self.root, self.manifest_path)

        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["expected_files"], 2)
        self.assertEqual(result["read_files"], 2)
        self.assertEqual(result["checked_files"], 2)
        self.assertEqual(result["failed_files"], 0)
        self.assertEqual(result["skipped_files"], 0)

    def test_checked_files_excludes_failed_file(self) -> None:
        second = b"other = 2\n"
        (self.root / "b.py").write_bytes(second)
        self.write_manifest([
            make_entry("app.py", self.content),
            make_entry("b.py", second),
        ])
        (self.root / "b.py").write_bytes(b"tampered = 3\n")

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertEqual(result["expected_files"], 2)
        self.assertEqual(result["read_files"], 2)
        self.assertEqual(result["checked_files"], 1)
        self.assertEqual(result["failed_files"], 1)
        self.assertEqual(result["skipped_files"], 0)

    def test_modified_file_fails(self) -> None:
        (self.root / "app.py").write_bytes(b"print('tampered')\n")
        result = verify(self.root, self.manifest_path)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(issue["code"] == "CONTENT_MISMATCH"
                for issue in result["issues"])
        )

    def test_unmanaged_extra_file_fails(self) -> None:
        (self.root / "unexpected.txt").write_text("extra", encoding="utf-8")
        result = verify(self.root, self.manifest_path)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(issue["code"] == "UNMANAGED_FILE"
                for issue in result["issues"])
        )

    def test_crlf_only_difference_is_warning_unless_strict(self) -> None:
        (self.root / "app.py").write_bytes(
            self.content.replace(b"\n", b"\r\n")
        )

        result = verify(self.root, self.manifest_path)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(
            [warning["code"] for warning in result["warnings"]],
            ["EOL_DRIFT"],
        )

        strict_result = verify(self.root, self.manifest_path, strict=True)
        self.assertFalse(strict_result["ok"])
        self.assertTrue(
            any(issue["code"] == "EOL_DRIFT"
                for issue in strict_result["issues"])
        )

    def test_crlf_blob_and_crlf_worktree_match(self) -> None:
        crlf = b"print('session ops')\r\n"
        (self.root / "app.py").write_bytes(crlf)

        entry = {
            "path": "app.py",
            "git_blob_sha1": blob_sha1(crlf),
            "sha256_lf": hashlib.sha256(
                b"print('session ops')\n"
            ).hexdigest(),
            "sha256_raw": hashlib.sha256(crlf).hexdigest(),
            "bytes": len(b"print('session ops')\n"),
        }
        self.write_manifest([entry])

        result = verify(self.root, self.manifest_path)

        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["warnings"], [])

    def test_crlf_blob_and_lf_worktree_is_eol_warning(self) -> None:
        crlf = b"print('session ops')\r\n"
        lf = b"print('session ops')\n"
        (self.root / "app.py").write_bytes(lf)

        entry = {
            "path": "app.py",
            "git_blob_sha1": blob_sha1(crlf),
            "sha256_lf": hashlib.sha256(lf).hexdigest(),
            "sha256_raw": hashlib.sha256(crlf).hexdigest(),
            "bytes": len(lf),
        }
        self.write_manifest([entry])

        result = verify(self.root, self.manifest_path)

        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(
            [warning["code"] for warning in result["warnings"]],
            ["EOL_DRIFT"],
        )

        strict_result = verify(
            self.root,
            self.manifest_path,
            strict=True,
        )
        self.assertFalse(strict_result["ok"])
        self.assertTrue(
            any(
                issue["code"] == "EOL_DRIFT"
                for issue in strict_result["issues"]
            )
        )

    def test_missing_managed_file_fails(self) -> None:
        (self.root / "app.py").unlink()

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                issue["code"] == "MISSING_FILE"
                and issue["path"] == "app.py"
                for issue in result["issues"]
            ),
            result["issues"],
        )

    def test_external_object_drift_fails(self) -> None:
        external_path = Path(self.tmp.name) / "so-mcp.service"
        external_path.write_bytes(b"original unit\n")

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["external_objects"] = [{
            "kind": "systemd-unit",
            "path": str(external_path),
            "sha256": hashlib.sha256(b"original unit\n").hexdigest(),
            "bytes": len(b"original unit\n"),
        }]
        self.manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        external_path.write_bytes(b"modified unit\n")

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                issue["code"] == "EXTERNAL_DRIFT"
                and issue["path"] == str(external_path)
                for issue in result["issues"]
            ),
            result["issues"],
        )

    def test_database_schema_behind_fails(self) -> None:
        db_path = Path(self.tmp.name) / "work-items.sqlite3"

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE session_links (link_id TEXT PRIMARY KEY)"
            )
            conn.execute(
                """
                CREATE TABLE authority_landing_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["database"] = {
            "path": str(db_path),
            "schema_version": 1,
            "required_columns": {
                "session_links": ["link_id", "session_id"],
                "authority_landing_events": ["event_id", "session_id", "attempt_id"],
            },
        }
        self.manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        result = verify(self.root, self.manifest_path)

        self.assertIsNotNone(result["database"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["database"]["schema_contract_ok"])

        self.assertEqual(
            result["database"]["tables"]["session_links"]["missing_columns"],
            ["session_id"],
        )
        self.assertEqual(
            result["database"]["tables"]["authority_landing_events"]["missing_columns"],
            ["attempt_id"],
        )

        self.assertEqual(
            sum(
                issue["code"] == "DB_SCHEMA_BEHIND"
                for issue in result["issues"]
            ),
            2,
            result["issues"],
        )

    def test_missing_database_fails(self) -> None:
        db_path = Path(self.tmp.name) / "does-not-exist.sqlite3"

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["database"] = {
            "path": str(db_path),
            "schema_version": 2,
            "required_columns": {
                "session_links": ["link_id", "session_id"],
            },
        }
        self.manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                issue["code"] == "DB_MISSING"
                and issue["path"] == str(db_path)
                for issue in result["issues"]
            ),
            result["issues"],
        )

    def test_forbidden_deployment_directory_fails(self) -> None:
        (self.root / "tests").mkdir()

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                issue["code"] == "FORBIDDEN_PATH"
                and issue["path"] == "tests"
                for issue in result["issues"]
            ),
            result["issues"],
        )


if __name__ == "__main__":
    unittest.main()
