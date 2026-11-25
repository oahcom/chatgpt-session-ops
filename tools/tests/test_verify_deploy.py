from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from tools.verify_deploy import main, verify


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

    def test_allow_list_admits_a_link_inside_a_managed_directory(self) -> None:
        # Counter-intuitive but measured: being inside a managed directory
        # does not protect a path. A pattern that covers the link's own path
        # exempts it, so `pkg/*` written to ignore build output also admits a
        # link pointing outside the deployment root. Pinned so the behaviour
        # is a documented property rather than a surprise, and so the
        # section 7 warning about `find -type l` stays justified.
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "auth.py").write_bytes(self.content)
        outside = Path(self.tmp.name) / "evil.py"
        outside.write_bytes(b"print('evil')\n")
        os.symlink(str(outside), str(self.root / "pkg" / "c.py"))

        # setUp already manages app.py; keep it so the fixture stays coherent.
        files = [make_entry("app.py", self.content),
                 make_entry("pkg/auth.py", self.content)]

        self.write_manifest(files, allowed_unmanaged=["pkg/*"])

        with_allow = verify(self.root, self.manifest_path)

        self.assertTrue(with_allow["ok"], with_allow["issues"])
        self.assertEqual(with_allow["issues"], [])
        self.assertEqual(with_allow["checked_files"], 2)

        # Without the pattern the same tree is rejected, which is what proves
        # the exemption is what admitted it.
        self.write_manifest(files, allowed_unmanaged=[])
        without_allow = verify(self.root, self.manifest_path)

        self.assertFalse(without_allow["ok"])
        self.assertEqual(
            [(i["code"], i["path"]) for i in without_allow["issues"]],
            [("UNEXPECTED_SYMLINK", "pkg/c.py")])

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

    # One case per issue code that had no test able to fail (#81). Each
    # assertion names the code it exists for, so deleting that branch in
    # tools/verify_deploy.py turns this specific method red.

    def load_manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def store_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def assert_code(self, result: dict, code: str, path: str | None = None) -> None:
        self.assertFalse(result["ok"])
        for issue in result["issues"]:
            if issue["code"] != code:
                continue
            if path is None or issue["path"] == path:
                return
        self.fail(
            "%s (path=%r) not in %r"
            % (code, path, [(i["code"], i["path"]) for i in result["issues"]])
        )

    def test_manifest_version_must_be_one(self) -> None:
        manifest = self.load_manifest()
        manifest["format_version"] = 99
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "MANIFEST_VERSION", str(self.manifest_path))

    def test_file_entry_without_a_path_is_rejected(self) -> None:
        manifest = self.load_manifest()
        manifest["files"].append({"path": ""})
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "INVALID_FILE_ENTRY", "files")

    def test_manifest_path_traversal_is_rejected(self) -> None:
        manifest = self.load_manifest()
        manifest["files"].append({"path": "../outside.py"})
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "UNSAFE_MANIFEST_PATH", "../outside.py")

    def test_duplicate_manifest_path_is_rejected(self) -> None:
        manifest = self.load_manifest()
        manifest["files"].append(dict(manifest["files"][0]))
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "DUPLICATE_MANIFEST_PATH", "app.py")

    def test_lf_byte_count_mismatch_is_rejected(self) -> None:
        manifest = self.load_manifest()
        manifest["files"][0]["bytes"] = len(self.content) + 1
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "BYTE_COUNT_MISMATCH", "app.py")

    def test_relative_external_object_path_is_rejected(self) -> None:
        manifest = self.load_manifest()
        manifest["external_objects"] = [{"path": "relative/unit.service"}]
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "INVALID_EXTERNAL_PATH", "relative/unit.service")

    def test_absent_external_object_is_rejected(self) -> None:
        absent = str(Path(self.tmp.name) / "absent.service")
        manifest = self.load_manifest()
        manifest["external_objects"] = [
            {"path": absent, "sha256": hashlib.sha256(b"x").hexdigest()}
        ]
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "EXTERNAL_OBJECT_ERROR", absent)

    def test_unreadable_manifest_is_rejected(self) -> None:
        self.manifest_path.write_bytes(b'{"format_version": 1,')

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "MANIFEST_ERROR", str(self.manifest_path))
        self.assertEqual(result["warnings"], [])

    def test_non_database_file_is_rejected(self) -> None:
        db_path = Path(self.tmp.name) / "not-a-database.sqlite3"
        db_path.write_bytes(b"this is not a SQLite header\n" * 64)
        manifest = self.load_manifest()
        manifest["database"] = {
            "path": str(db_path),
            "schema_version": 1,
            "required_columns": {"work_items": ["id"]},
        }
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "DB_SCHEMA_ERROR", str(db_path))

    def test_missing_root_exits_nonzero(self) -> None:
        absent_root = Path(self.tmp.name) / "no-such-root"
        argv = sys.argv
        sys.argv = [
            "verify_deploy.py",
            "--root",
            str(absent_root),
            "--manifest",
            str(self.manifest_path),
        ]
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                rc = main()
        finally:
            sys.argv = argv

        printed = stdout.getvalue()
        self.assertEqual(rc, 1, printed)
        self.assertIn("ROOT_MISSING", printed)
        self.assertNotIn("DEPLOY VERIFY OK", printed)

    def test_clean_tree_exits_zero_and_names_the_count(self) -> None:
        """The success path had no test at all.

        `test_missing_root_exits_nonzero` asserts the banner is absent when
        the run fails. Nothing asserted it is present, or correct, when the
        run succeeds — so a change that printed `0/0 files verified` on a
        fully verified tree, or that kept exit 0 while checking nothing,
        would have gone unnoticed. The deploy document's claim is that a
        human reads this line, so the numbers in it have to be asserted.
        """
        argv = sys.argv
        sys.argv = [
            "verify_deploy.py",
            "--root",
            str(self.root),
            "--manifest",
            str(self.manifest_path),
        ]
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                rc = main()
        finally:
            sys.argv = argv

        printed = stdout.getvalue()
        self.assertEqual(rc, 0, printed)
        self.assertIn("DEPLOY VERIFY OK: 1/1 files verified", printed)

    def test_file_entry_with_a_non_string_path_is_rejected(self) -> None:
        """`isinstance` and truthiness are two different guards.

        `test_file_entry_without_a_path_is_rejected` supplies `""`, which the
        truthiness half catches. A number is truthy and not a `str`, so it
        takes the `isinstance` half — the one a refactor to `if not rel:`
        would silently drop.
        """
        manifest = self.load_manifest()
        manifest["files"] = [{"path": 17}]
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assert_code(result, "INVALID_FILE_ENTRY", "files")

    # ------------------------------------------------------------------
    # Two fails recorded, not fixed.
    #
    # `expectedFailure` is deliberate: these pin the safe behaviour while the
    # production code still does the unsafe thing, so the suite stays green
    # and the defect stays visible in the source. When the tool is fixed, the
    # case turns into an unexpected success and the suite fails — that is the
    # signal to delete the decorator, not to widen the assertion.
    # ------------------------------------------------------------------

    @unittest.expectedFailure
    def test_empty_managed_list_is_not_a_pass(self) -> None:
        """A manifest managing nothing must not report OK.

        `ok` is `not issues`; an empty `expected` produces no issues, so the
        tool reports `DEPLOY VERIFY OK: 0/0 files verified` and exits 0.
        `gen_manifest.py` refuses to build such a manifest, so this arrives
        only through a truncated download or a hand-edited file — but
        "verified nothing, reported success" is the exact failure the deploy
        document exists to prevent, and `0/0` reads as success at a glance.
        """
        manifest = self.load_manifest()
        manifest["files"] = []
        self.store_manifest(manifest)
        # The managed file has to go too, or it becomes UNMANAGED_FILE and
        # the run fails for a reason unrelated to the empty list. The
        # reported scenario is an empty root and an empty manifest.
        (self.root / "app.py").unlink()

        result = verify(self.root, self.manifest_path)

        self.assertEqual(result["checked_files"], 0)
        self.assertFalse(
            result["ok"],
            "a manifest that manages no files reported OK: %r" % (result,),
        )

    @unittest.expectedFailure
    def test_star_allow_list_does_not_exempt_the_whole_tree(self) -> None:
        """`allowed_unmanaged: ["*"]` must not silently accept every file.

        `fnmatch.fnmatchcase` lets `*` cross `/`, so a single entry suppresses
        `UNMANAGED_FILE` everywhere. The manifest is untrusted input; an
        operator who writes `*` to ignore a build directory has exempted the
        entire deployment root, and nothing says so.
        """
        (self.root / "backdoor.py").write_bytes(b"raise SystemExit\n")
        manifest = self.load_manifest()
        manifest["files"] = []
        manifest["allowed_unmanaged"] = ["*"]
        self.store_manifest(manifest)

        result = verify(self.root, self.manifest_path)

        self.assertIn(
            "UNMANAGED_FILE",
            [issue["code"] for issue in result["issues"]],
            "a '*' allow-list exempted the whole tree: %r" % (result["issues"],),
        )


if __name__ == "__main__":
    unittest.main()
