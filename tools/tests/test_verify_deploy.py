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

    def write_manifest(self, files: list[dict], allowed_unmanaged: list[str] | None = None) -> None:
        self.manifest_path.write_text(
            json.dumps({
                "format_version": 1,
                "commit": COMMIT,
                "generated_at": "2026-11-25T00:00:00Z",
                "files": files,
                "external_objects": [],
                "allowed_unmanaged": list(allowed_unmanaged or []),
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

    def test_blob_mismatch_is_independent_of_lf_and_raw_hashes(self) -> None:
        manifest = json.loads(
            self.manifest_path.read_text(encoding="utf-8")
        )
        manifest["files"][0]["git_blob_sha1"] = "f" * 40
        self.manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                issue["code"] == "GIT_BLOB_MISMATCH"
                and issue["path"] == "app.py"
                for issue in result["issues"]
            ),
            result["issues"],
        )
        self.assertFalse(
            any(
                warning["code"] == "EOL_DRIFT"
                for warning in result["warnings"]
            ),
            result["warnings"],
        )

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

    def test_symlink_covering_managed_files_is_rejected_despite_allow_list(self) -> None:
        """allow-list 覆盖的符号链接，如果下面藏着受管文件，仍然必须报错。

        事故形态：`allowed_unmanaged` 里写了 `pkg*`（本意只是忽略 `pkg/cache` 这类
        产物），有人把整个 `pkg/` 换成指向外部目录的链接。只看 `matches_any(rel,
        allowed)` 的话它命中白名单，于是链接不报，而受管文件因为链接不下钻也永远
        不会被读到——`ok=True`，零个文件被验证。

        这一条必须单独存在：已有的 `test_symlinked_managed_directory_is_rejected`
        用的 manifest 没有 allow-list，`matches_any` 恒假，所以两个 `or` 分支
        永远走右边，左侧的 `covers_managed` 从未被单独检验——删掉它那个测试照样绿。
        """
        pkg = self.root / "pkg"
        pkg.mkdir()
        (pkg / "auth.py").write_bytes(b"authentic = True\n")
        self.write_manifest(
            [make_entry("pkg/auth.py", b"authentic = True\n")],
            allowed_unmanaged=["pkg*"],
        )

        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "auth.py").write_bytes(b"different = True\n")

        shutil.move(str(pkg), str(Path(self.tmp.name) / "moved"))
        os.symlink(str(elsewhere), str(pkg), target_is_directory=True)

        result = verify(self.root, self.manifest_path)

        self.assertFalse(
            result["ok"],
            "被 allow-list 覆盖的符号链接目录藏了受管文件，却报了 OK：%r" % (result,),
        )
        self.assertTrue(
            any(
                issue["code"] == "UNEXPECTED_SYMLINK"
                and issue["path"] == "pkg"
                for issue in result["issues"]
            ),
            result["issues"],
        )
        self.assertEqual(
            result["checked_files"], 0, "链接下的受管文件一个都没读到，不能算已验证"
        )

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

    def test_byte_identical_count_agrees_across_modes(self) -> None:
        # One clean file plus one whose only difference is CRLF. Default
        # mode records a warning and keeps both files "verified"; strict
        # mode fails the drifted file. The byte-identical count must not
        # move with that classification: two files are byte-identical to
        # nothing, one is.
        lf = b"print('session ops')\n"
        crlf = b"print('session ops')\r\n"
        (self.root / "app.py").write_bytes(lf)
        (self.root / "b.py").write_bytes(crlf)

        self.write_manifest([
            make_entry("app.py", lf),
            {
                "path": "b.py",
                "git_blob_sha1": blob_sha1(lf),
                "sha256_lf": hashlib.sha256(lf).hexdigest(),
                "sha256_raw": hashlib.sha256(lf).hexdigest(),
                "bytes": len(lf),
            },
        ])

        default = verify(self.root, self.manifest_path)
        strict = verify(self.root, self.manifest_path, strict=True)

        self.assertTrue(default["ok"], default["issues"])
        self.assertEqual(default["checked_files"], 2)
        self.assertEqual(default["failed_files"], 0)
        self.assertEqual(default["byte_identical_files"], 1)

        self.assertFalse(strict["ok"])
        self.assertEqual(strict["checked_files"], 1)
        self.assertEqual(strict["failed_files"], 1)
        self.assertEqual(strict["byte_identical_files"], 1)

    def test_byte_identical_excludes_unread_file(self) -> None:
        # A skipped file is not evidence of byte identity, in either mode.
        (self.root / "app.py").unlink()

        default = verify(self.root, self.manifest_path)
        strict = verify(self.root, self.manifest_path, strict=True)

        self.assertEqual(default["byte_identical_files"], 0)
        self.assertEqual(strict["byte_identical_files"], 0)

    def test_byte_identical_can_be_nonzero_while_the_run_fails(self) -> None:
        # The count answers "how many read files have the manifest's raw
        # bytes", not "did this pass". A manifest whose LF digest is wrong
        # still lets the raw digest match, so the file is counted and the
        # run is rejected. Pinned so nobody later "fixes" the counter by
        # clearing it on failure, which would make it a second verdict
        # instead of a count.
        raw = b"print('session ops')\n"
        (self.root / "app.py").write_bytes(raw)

        entry = make_entry("app.py", raw)
        entry["sha256_lf"] = "0" * 64
        self.write_manifest([entry])

        result = verify(self.root, self.manifest_path)

        self.assertFalse(result["ok"])
        self.assertEqual([i["code"] for i in result["issues"]],
                         ["CONTENT_MISMATCH"])
        self.assertEqual(result["byte_identical_files"], 1)
        self.assertEqual(result["checked_files"], 0)
        self.assertEqual(result["failed_files"], 1)

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
