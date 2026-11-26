# -*- coding: utf-8 -*-
"""#137：钉住 chatgpt_send.py 的原子 IO / 锁 / 基准 / 杂项守卫。

每个用例只针对一条守卫：把它改成 `pass` 后，至少一条断言必须失败。
测试不触网、不需要凭据；锁与超时参数按毫秒级缩小，避免真实长等待。
"""
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "chatgpt_send.py")

SPEC = importlib.util.spec_from_file_location(
    "chatgpt_send_137_io_guards",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def make_permission_error(winerror):
    exc = PermissionError("simulated Windows replace failure")
    exc.winerror = winerror
    return exc


class TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cgpt-137-")

    def tearDown(self):
        for name in os.listdir(self.tmp):
            path = os.path.join(self.tmp, name)
            try:
                if os.path.isdir(path):
                    os.rmdir(path)
                else:
                    os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def make_tmp_file(self, name="tmpfile"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"x")
        return path


class ReplaceCleanupFailureTests(TmpDirTestCase):
    """L242 / L256：replace 失败且临时文件清理也失败，必须报「双重失败」。"""

    def test_retryable_exhausted_and_cleanup_failure_reports_both(self):
        tmp = self.make_tmp_file("atomic-a")
        with mock.patch.object(
            MODULE.os, "replace", side_effect=make_permission_error(5)
        ), mock.patch.object(
            MODULE.os, "unlink", side_effect=OSError("unlink denied")
        ), mock.patch("time.sleep"):
            with self.assertRaises(OSError) as raised:
                MODULE._replace_with_bounded_retry(tmp, os.path.join(self.tmp, "dst"))

        self.assertIs(type(raised.exception), OSError)
        self.assertIn("cleanup also failed", str(raised.exception))
        self.assertIn("unlink denied", str(raised.exception))

    def test_non_permission_error_and_cleanup_failure_reports_both(self):
        tmp = self.make_tmp_file("atomic-b")
        with mock.patch.object(
            MODULE.os, "replace", side_effect=OSError("disk exploded")
        ), mock.patch.object(
            MODULE.os, "unlink", side_effect=OSError("unlink denied")
        ):
            with self.assertRaises(OSError) as raised:
                MODULE._replace_with_bounded_retry(tmp, os.path.join(self.tmp, "dst"))

        self.assertIs(type(raised.exception), OSError)
        self.assertIn("cleanup also failed", str(raised.exception))
        self.assertIn("unlink denied", str(raised.exception))


class AtomicCreateExclusiveTests(TmpDirTestCase):
    """L326：独占创建写入失败必须删掉空壳并原样回抛。"""

    def test_write_failure_removes_shell_and_reraises(self):
        path = os.path.join(self.tmp, "exclusive.bin")
        with mock.patch.object(MODULE.os, "fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError) as raised:
                MODULE._atomic_create_exclusive(path, "payload")

        self.assertIs(type(raised.exception), OSError)
        self.assertIn("fsync failed", str(raised.exception))
        self.assertFalse(os.path.exists(path))


class AtomicWriteBytesTests(TmpDirTestCase):
    """L357：二进制原子写失败必须清掉临时文件并原样回抛。"""

    def test_replace_failure_removes_tmp_and_reraises(self):
        path = os.path.join(self.tmp, "body.bin")
        with mock.patch.object(
            MODULE.os, "replace", side_effect=OSError("replace exploded")
        ):
            with self.assertRaises(OSError) as raised:
                MODULE._atomic_write_bytes(path, b"payload")

        self.assertIn("replace exploded", str(raised.exception))
        self.assertFalse(os.path.exists(path))
        leftovers = [n for n in os.listdir(self.tmp) if ".tmp-" in n]
        self.assertEqual(leftovers, [], "临时文件未清理：%r" % (leftovers,))


class CustodyGuardTests(TmpDirTestCase):
    """L420/L423/L434/L444/L476：交付正文托管的显式拒绝。"""

    def test_missing_source_file(self):
        missing = os.path.join(self.tmp, "does-not-exist.md")
        with self.assertRaises(MODULE.CustodyError) as raised:
            MODULE.extract_deliverable(missing, begin="B", end="E")
        self.assertIn("源文件不存在", str(raised.exception))
        self.assertIn(missing, str(raised.exception))

    def test_empty_source_file(self):
        empty = os.path.join(self.tmp, "empty.md")
        open(empty, "wb").close()
        with self.assertRaises(MODULE.CustodyError) as raised:
            MODULE.extract_deliverable(empty, begin="B", end="E")
        self.assertIn("源文件为空", str(raised.exception))
        self.assertIn(empty, str(raised.exception))

    def test_marker_order_reversed(self):
        src = os.path.join(self.tmp, "reversed.md")
        with open(src, "wb") as f:
            f.write(b"</e>\na\n<b>\n")
        with self.assertRaises(MODULE.CustodyError) as raised:
            MODULE.extract_deliverable(
                src, begin="<b>", end="</e>", out=os.path.join(self.tmp, "o.md")
            )
        self.assertIn("标记顺序错", str(raised.exception))

    def test_fence_must_start_at_one(self):
        src = os.path.join(self.tmp, "fence.md")
        with open(src, "wb") as f:
            f.write(b"```\nbody\n```\n")
        for bad in (0, -1, "-2"):
            with self.subTest(fence=bad):
                with self.assertRaises(MODULE.CustodyError) as raised:
                    MODULE.extract_deliverable(
                        src, fence=bad, out=os.path.join(self.tmp, "o.md")
                    )
                self.assertIn("--fence 从 1 开始", str(raised.exception))

    def test_verify_copy_missing_file(self):
        present = self.make_tmp_file("present.md")
        missing = os.path.join(self.tmp, "missing-copy.md")
        with self.assertRaises(MODULE.CustodyError) as raised:
            MODULE.verify_copy(present, missing)
        self.assertIn("文件不存在", str(raised.exception))
        self.assertIn(missing, str(raised.exception))


class WriteAllGuardTests(unittest.TestCase):
    """L1359：os.write 返回非正数必须立刻失败，不得无限重试。"""

    def test_non_positive_write_raises_oserror(self):
        calls = {"n": 0}

        def fake_write(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0
            raise RuntimeError("loop did not stop after a zero-length write")

        with mock.patch.object(MODULE.os, "write", side_effect=fake_write):
            with self.assertRaises(OSError) as raised:
                MODULE._write_all(-1, b"abc")

        self.assertIs(type(raised.exception), OSError)
        self.assertIn("write returned 0", str(raised.exception))
        self.assertEqual(calls["n"], 1, "零字节写入后不得继续循环")


class FileLockGuardTests(TmpDirTestCase):
    """L1643：无法建立锁文件（非 FileExistsError 的 OSError）必须抛 LockTimeout。"""

    def test_lock_open_failure_raises_locktimeout(self):
        path = os.path.join(self.tmp, "missing-dir", "state.json")
        with self.assertRaises(MODULE.LockTimeout) as raised:
            with MODULE._file_lock(path, timeout=0.2, strict=True):
                pass
        self.assertIn("加锁失败", str(raised.exception))
        self.assertIn("state.json", str(raised.exception))


class LeaseRunGuardTests(TmpDirTestCase):
    """L2060：执行体异常必须原样回抛，不得被当成 DONE。"""

    def test_body_exception_is_reraised(self):
        path = os.path.join(self.tmp, "lease.json")

        def body():
            raise ValueError("body exploded")

        with self.assertRaises(ValueError) as raised:
            MODULE.lease_run(
                path, "task-137", body,
                lease_sec=5, heartbeat_sec=1, max_sec=5,
            )
        self.assertIn("body exploded", str(raised.exception))
        self.assertFalse(os.path.exists(path), "执行体失败后仍须释放租约")


class RegistryBaselineGuardTests(TmpDirTestCase):
    """L2203 / L2316：基准不可写 / 基准推进失败都必须拒绝并显式报错。"""

    def test_probe_unwritable_baseline(self):
        baseline = os.path.join(self.tmp, "no-such-dir", "conv-accounts.json.highwater.json")
        with self.assertRaises(MODULE.RegistryBaselineUnavailable) as raised:
            MODULE._probe_baseline_writable(baseline)
        self.assertIn("baseline not writable", str(raised.exception))

    def test_commit_baseline_update_failure(self):
        path = os.path.join(self.tmp, "conv-accounts.json")
        with mock.patch.object(
            MODULE, "_write_baseline", side_effect=OSError("disk full")
        ):
            with self.assertRaises(MODULE.RegistryBaselineUnavailable) as raised:
                MODULE._registry_commit_baseline(path, {"id-1": {}})
        self.assertIn(
            "registry committed but baseline update failed", str(raised.exception)
        )
        self.assertIn("disk full", str(raised.exception))


class ListConversationsRangeGuardTests(unittest.TestCase):
    """L4696：page_size / max_pages 越界必须在任何网络动作之前断言失败。"""

    def test_out_of_range_pagination_is_rejected(self):
        api = object.__new__(MODULE.ChatGPTAPI)

        def no_network(*args, **kwargs):
            raise RuntimeError("network must not be reached")

        api.req = no_network
        cases = [(0, 28), (101, 28), (28, 0), (28, 1001)]
        for page_size, max_pages in cases:
            with self.subTest(page_size=page_size, max_pages=max_pages):
                with self.assertRaises(AssertionError):
                    api.list_conversations(page_size=page_size, max_pages=max_pages)


class ListAllNologinGuardTests(unittest.TestCase):
    """L8415：nologin 凭据损坏必须原样回抛，不得降级成普通枚举失败。"""

    def test_nologin_credential_error_is_reraised(self):
        failure = OSError("credentials corrupt")
        argv = ["chatgpt_send.py", "--list-all", "--include-nologin"]
        with mock.patch.object(
            MODULE, "ChatGPTAPI", side_effect=failure
        ), mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError) as raised:
                MODULE.main()

        self.assertIs(raised.exception, failure)
        self.assertIn("credentials corrupt", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
