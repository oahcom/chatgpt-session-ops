from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import gen_manifest


class ManifestValidationTests(unittest.TestCase):
    """每个用例钉住一条参数校验分支。

    这些分支此前**一条测试都没有**：把 `tools/gen_manifest.py` 里的校验条件改成
    `if False:` 之后，整套测试仍然全绿（4 条已用变异证明）。原因是这个工具只有
    一个 happy-path 测试，校验分支只被走过、从未被拒绝过一次。

    写法约定：每条用例同时断言**退出码**与**具体报错片段**。只断言 rc=1 的话，
    分支换成别的错误也能通过，等于没测。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", str(self.repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"],
                       check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email",
                        "test@example.invalid"], check=True)
        (self.repo / "app.py").write_text("print('test')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "test"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

        self.paths_file = self.base / "paths.txt"
        self.paths_file.write_text("app.py\n", encoding="utf-8")
        self.manifest_path = self.base / "manifest.json"

    def _args(self, **overrides) -> list[str]:
        """按 flag 覆盖构造 argv；值为 None 表示删掉该 flag。"""
        base = {
            "--repo": str(self.repo),
            "--commit": self.commit,
            "--paths-from": str(self.paths_file),
            "--out": str(self.manifest_path),
        }
        base.update(overrides)
        argv: list[str] = []
        for flag, value in base.items():
            if value is None:
                continue
            argv.extend([flag, str(value)])
        return argv

    def _run(self, argv: list[str]):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = gen_manifest.main(argv)
        return result, stderr.getvalue()

    def _assert_rejected(self, argv, fragment: str, label: str) -> None:
        result, err = self._run(argv)
        self.assertEqual(result, 1, "%s 未被拒绝（rc=%r）\n%s" % (label, result, err))
        self.assertIn(fragment, err, "%s 的报错里没有 %r\n%s" % (label, fragment, err))
        self.assertFalse(self.manifest_path.exists(),
                         "%s 被拒后仍写出了 manifest" % label)

    # --- commit 校验 ---------------------------------------------------

    def test_short_commit_is_rejected(self) -> None:
        """12 位短 SHA 不是 40 位完整 SHA，必须拒（GM-8）。"""
        self._assert_rejected(self._args(**{"--commit": self.commit[:12]}),
                              "40-character SHA", "短 SHA")

    def test_uppercase_or_nonhex_commit_is_rejected(self) -> None:
        """大写十六进制同样不是规范化 SHA（GM-8）。"""
        self._assert_rejected(self._args(**{"--commit": "ABC"}),
                              "40-character SHA", "非十六进制 SHA")

    def test_blob_object_is_not_a_commit(self) -> None:
        """tree/blob 的 SHA 也能被 git 解析，但不是 commit（GM-9）。"""
        tree = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"], text=True).strip()
        self._assert_rejected(self._args(**{"--commit": tree}),
                              "does not identify a commit", "tree SHA")

    # --- 路径列表校验 ---------------------------------------------------

    def test_escaping_path_is_rejected(self) -> None:
        """`../` 越出仓库根，不得进入清单（GM-12）。"""
        self.paths_file.write_text("../app.py\n", encoding="utf-8")
        self._assert_rejected(self._args(), "unsafe or non-normalized", "../ 路径")

    def test_absolute_path_in_list_is_rejected(self) -> None:
        self.paths_file.write_text("/etc/passwd\n", encoding="utf-8")
        self._assert_rejected(self._args(), "unsafe or non-normalized", "绝对路径")

    def test_duplicate_path_is_rejected(self) -> None:
        self.paths_file.write_text("app.py\napp.py\n", encoding="utf-8")
        self._assert_rejected(self._args(), "duplicate repository path", "重复路径")

    def test_empty_path_list_is_rejected(self) -> None:
        """只有注释的清单等于空清单——空清单会让整个发布'验证 0 个文件'。"""
        self.paths_file.write_text("# nothing here\n\n", encoding="utf-8")
        self._assert_rejected(self._args(), "explicit managed path list is empty", "空清单")

    # --- --external 校验 ------------------------------------------------

    def test_external_without_equals_is_rejected(self) -> None:
        self._assert_rejected(self._args(**{"--external": "systemd-unit"}),
                              'must have the form kind=', "无 = 的 external")

    def test_unknown_external_kind_is_rejected(self) -> None:
        self._assert_rejected(self._args(**{"--external": "bogus=/tmp/x"}),
                              "unsupported external kind", "未知 kind")

    def test_external_with_empty_path_is_rejected(self) -> None:
        self._assert_rejected(self._args(**{"--external": "systemd-unit="}),
                              "must not be empty", "空 external 路径")

    # --- 数据库列校验 ---------------------------------------------------

    def test_column_without_table_qualifier_is_rejected(self) -> None:
        db = self.base / "db.sqlite3"
        db.write_bytes(b"")
        self._assert_rejected(
            self._args(**{"--db-path": str(db),
                          "--db-required-column": "session_links"}),
            "TABLE.COLUMN", "无表名的列")

    def test_column_with_empty_half_is_rejected(self) -> None:
        db = self.base / "db2.sqlite3"
        db.write_bytes(b"")
        self._assert_rejected(
            self._args(**{"--db-path": str(db),
                          "--db-required-column": ".col"}),
            "TABLE.COLUMN", "空表名的列")

    def test_duplicate_required_column_is_rejected(self) -> None:
        db = self.base / "db3.sqlite3"
        db.write_bytes(b"")
        argv = self._args(**{"--db-path": str(db)})
        argv += ["--db-required-column", "session_links.session_id",
                 "--db-required-column", "session_links.session_id"]
        self._assert_rejected(argv, "duplicate required database column", "重复列")

    # --- --repo / --db-path 路径校验 -------------------------------------

    def test_repo_must_be_a_directory(self) -> None:
        not_a_dir = self.base / "a_file.txt"
        not_a_dir.write_text("x", encoding="utf-8")
        self._assert_rejected(self._args(**{"--repo": str(not_a_dir)}),
                              "--repo is not a directory", "非目录 repo")

    def test_relative_db_path_is_rejected(self) -> None:
        self._assert_rejected(
            self._args(**{"--db-path": "foo.db"}),
            "must be an absolute path", "相对 db 路径")

    def test_missing_db_file_is_rejected(self) -> None:
        missing = self.base / "nope.sqlite3"
        self._assert_rejected(
            self._args(**{"--db-path": str(missing)}),
            "must name an existing file", "不存在的 db")


if __name__ == "__main__":
    unittest.main()
