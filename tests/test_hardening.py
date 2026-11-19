# -*- coding: utf-8 -*-
"""chatgpt_send.py 的离线回归测试（不触网、不需要凭据）。

    python -m unittest discover -s tests -v
    python tests/test_hardening.py          # 直接跑也支持

覆盖的都是「出过事或一想就疼」的点：id 路径穿越、状态文件损坏被静默覆盖、
归档写半截、回复被静默截断、锁重入与 stale 回收、轮转兜底判定。
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("CHATGPT_SEND_PY") or os.path.join(HERE, "..", "scripts", "chatgpt_send.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_module():
    spec = importlib.util.spec_from_file_location("chatgpt_send_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


m = load_module()


class TmpBase(unittest.TestCase):
    """把模块级 BASE 指到临时目录，避免测试污染真实运行态。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cgpt-test-")
        self._base = m.BASE
        self._rot = m.ROTATION_FILE
        self._conv = m.CONV_MAP_FILE
        self._anon = m.ANON_SESSIONS_FILE
        m.BASE = self.tmp
        m.ROTATION_FILE = os.path.join(self.tmp, "account-rotation.json")
        m.CONV_MAP_FILE = os.path.join(self.tmp, "conv-accounts.json")
        m.ANON_SESSIONS_FILE = os.path.join(self.tmp, "anon-sessions.json")

    def tearDown(self):
        m.BASE = self._base
        m.ROTATION_FILE = self._rot
        m.CONV_MAP_FILE = self._conv
        m.ANON_SESSIONS_FILE = self._anon
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- id 校验
class TestIds(TmpBase):
    def test_accepts_real_uuid(self):
        cid = "11111111-2222-4333-8444-555555555555"
        self.assertEqual(m.safe_id(cid), cid)

    def test_rejects_traversal_and_junk(self):
        for bad in ["../../evil", r"..\..\evil", "a/b", "", "   ", "-lead", "x" * 200,
                    "6afe78eb/../../etc/passwd", "id with space", "id\x00null"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    m.safe_id(bad)

    def test_id8_never_emits_separators(self):
        for bad in ["../../evil", r"..\..\x", "a/b/c", ""]:
            short = m.id8(bad)
            self.assertNotIn("/", short)
            self.assertNotIn("\\", short)
            self.assertNotIn("..", short)

    def test_id8_empty_falls_back(self):
        self.assertEqual(m.id8("", "new"), "new")

    def test_archive_path_stays_inside(self):
        path = os.path.abspath(m.anon_archive_path("../../evil"))
        inside = os.path.abspath(os.path.join(self.tmp, "anon-sessions"))
        self.assertTrue(path.startswith(inside + os.sep), path)

    def test_archive_path_prefix_collision_gets_suffix(self):
        a = "aaaaaaa1-1111-1111-1111-111111111111"
        b = "aaaaaaa2-2222-2222-2222-222222222222"  # 同 8 位前缀
        p1 = m.anon_archive_path(a)
        m._atomic_write(p1, "# 匿名会话 %s\n" % a)
        p2 = m.anon_archive_path(b)
        self.assertNotEqual(p1, p2)
        self.assertTrue(p2.endswith(".md"))


# ---------------------------------------------------------------- 状态文件
class TestStateFiles(TmpBase):
    def test_corrupt_json_is_quarantined_not_silently_lost(self):
        data = {("id%d" % i): {"account": "acct1"} for i in range(5)}
        m._save_json(m.CONV_MAP_FILE, data)
        with open(m.CONV_MAP_FILE, "w", encoding="utf-8") as f:
            f.write('{"broken": ')  # 模拟进程被杀写了一半

        got = m._load_json(m.CONV_MAP_FILE, {})
        self.assertEqual(got, {})
        bads = [n for n in os.listdir(self.tmp) if ".corrupt-" in n]
        self.assertEqual(len(bads), 1, "损坏文件应被隔离而不是直接覆盖")
        self.assertEqual(_read(os.path.join(self.tmp, bads[0])), '{"broken": ')

    def test_default_is_deep_copied(self):
        shared = {}
        first = m._load_json(os.path.join(self.tmp, "nope.json"), shared)
        first["polluted"] = 1
        second = m._load_json(os.path.join(self.tmp, "nope.json"), shared)
        self.assertEqual(second, {})
        self.assertEqual(shared, {}, "默认值不能被上一次调用改脏")

    def test_empty_file_is_not_quarantined(self):
        p = os.path.join(self.tmp, "empty.json")
        m._atomic_write(p, "")
        self.assertEqual(m._load_json(p, {"k": 1}), {"k": 1})
        self.assertFalse([n for n in os.listdir(self.tmp) if ".corrupt-" in n])

    def test_atomic_write_leaves_no_tmp(self):
        p = os.path.join(self.tmp, "sub", "a.json")
        m._atomic_write(p, '{"a": 1}')
        self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".tmp"))

    def test_atomic_write_cleans_tmp_on_failure(self):
        target = os.path.join(self.tmp, "sub")
        os.makedirs(target)  # 目标是目录 → 写入/替换必失败
        with self.assertRaises(Exception):
            m._atomic_write(target, "x")
        self.assertFalse(os.path.exists(target + ".tmp"))

    def test_tmp_is_in_same_dir_and_fsync_failure_cleans_up(self):
        """A20：tmp 必须同目录（同文件系统才原子）；fsync 失败要清 tmp 并抛出。"""
        path = os.path.join(self.tmp, "sub", "fsync.json")
        seen = {}
        real_fsync = os.fsync

        def boom(fd):
            seen["called"] = True
            raise OSError("disk says no")

        os.fsync = boom
        try:
            with self.assertRaises(OSError):
                m._atomic_write(path, "{}")
        finally:
            os.fsync = real_fsync
        self.assertTrue(seen.get("called"), "必须真的调用过 fsync")
        self.assertEqual([f for f in os.listdir(os.path.dirname(path)) if ".tmp" in f], [],
                         "fsync 失败后没清 tmp")
        self.assertFalse(os.path.exists(path), "失败不该留下目标文件")

    def test_replace_failure_cleans_up_and_keeps_old_content(self):
        path = os.path.join(self.tmp, "keep.json")
        m._atomic_write(path, '{"v": 1}')
        real_replace = os.replace

        def boom(src, dst):
            raise PermissionError("locked by another process")

        os.replace = boom
        try:
            with self.assertRaises(PermissionError):
                m._atomic_write(path, '{"v": 2}')
        finally:
            os.replace = real_replace
        self.assertEqual([f for f in os.listdir(self.tmp) if ".tmp" in f], [], "失败后留了 tmp")
        self.assertEqual(json.load(open(path, encoding="utf-8"))["v"], 1, "旧内容必须完好")

    def test_tmp_is_created_in_target_directory(self):
        """A20：tmp 必须与目标同目录（同文件系统才谈得上原子 replace）。"""
        path = os.path.join(self.tmp, "nested", "same-dir.json")
        seen = {}
        real = os.replace

        def spy(src, dst):
            seen["src"] = src
            return real(src, dst)

        os.replace = spy
        try:
            m._atomic_write(path, "{}")
        finally:
            os.replace = real
        self.assertEqual(os.path.dirname(os.path.abspath(seen["src"])),
                         os.path.dirname(os.path.abspath(path)))

    @unittest.skipUnless(os.name == "posix", "Windows 无法 open 目录，目录 fsync 无对应语义")
    def test_atomic_write_fsyncs_parent_directory(self):
        """A20（仅 POSIX）：replace 后必须 fsync 目录，否则断电可能丢目录项。

        Windows 上 os.open(dir) 直接 PermissionError，实现按降级处理——这也是 A20 只能
        记 PARTIAL 的原因：跨平台持久化语义无法用离线单测证明。
        """
        path = os.path.join(self.tmp, "dir-sync.json")
        seen = []
        real = os.fsync

        def spy(fd):
            try:
                seen.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            except OSError:
                seen.append(None)
            return real(fd)

        os.fsync = spy
        try:
            m._atomic_write(path, "{}")
        finally:
            os.fsync = real
        self.assertIn(True, seen, "no directory fsync (seen=%r)" % (seen,))
        self.assertIn(False, seen, "no file fsync")

    def test_atomic_write_uses_unique_tmp_names(self):        # 事故：固定 `.tmp` 时同进程两个线程写同一个临时文件，Windows 上 os.replace
        # 抛 WinError 32，线程被打死、锁留在原地 → 4 线程 ×20 次只落 60 条。
        # 真实用法是「先取 A22 锁再写」，所以这里也按锁串行，只校验 tmp 名唯一。
        path = os.path.join(self.tmp, "uniq.json")
        seen = []
        real = os.replace

        def spy(src, dst):
            seen.append(src)
            return real(src, dst)

        def w(i):
            with m._file_lock(path):
                m._atomic_write(path, json.dumps({"i": i}))

        os.replace = spy
        try:
            ts = [threading.Thread(target=w, args=(i,)) for i in range(4)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        finally:
            os.replace = real
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), len(seen), "临时文件名重复：%r" % (seen,))
        for s in seen:
            self.assertIn(".tmp-%d-" % os.getpid(), s)
        self.assertEqual([f for f in os.listdir(self.tmp) if ".tmp" in f], [], "留下临时文件")

    def test_lock_is_reentrant(self):
        p = os.path.join(self.tmp, "x.json")
        with m._file_lock(p):
            with m._file_lock(p):  # 重入不能自锁
                m._atomic_write(p, "{}")
        self.assertFalse(os.path.exists(p + ".lock"), "退出后应清掉锁文件")

    def test_stale_lock_is_reclaimed(self):
        p = os.path.join(self.tmp, "y.json")
        lock = p + ".lock"
        open(lock, "w").close()
        old = time.time() - (m.LOCK_STALE_SEC + 30)
        os.utime(lock, (old, old))
        t0 = time.time()
        with m._file_lock(p):
            m._atomic_write(p, "{}")
        self.assertLess(time.time() - t0, 3, "残留锁应被立即回收，不该等到超时")
        self.assertFalse(os.path.exists(lock))

    def test_concurrent_updates_do_not_lose_writes(self):
        p = os.path.join(self.tmp, "cnt.json")
        m._save_json(p, {"n": 0})

        def worker():
            for _ in range(20):
                m._update_json(p, lambda d: d.__setitem__("n", d["n"] + 1), {"n": 0})

        ts = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(m._load_json(p, {})["n"], 80)

    def test_thread_lock_serialises_same_process(self):
        """同进程两线程必须由 threading.Lock 串行——不依赖文件锁（Windows 上 O_EXCL+fsync 太慢）。"""
        p = os.path.join(self.tmp, "tl.json")
        lk1 = m._thread_lock_for(p)
        lk2 = m._thread_lock_for(p)
        self.assertIs(lk1, lk2, "同路径应返回同一个线程锁")
        self.assertTrue(lk1.acquire(blocking=False))
        self.assertFalse(lk2.acquire(blocking=False), "第一个线程持有时第二个不该拿到")
        lk1.release()
        self.assertTrue(lk2.acquire(blocking=False), "释放后应可获取")
        lk2.release()

    def test_strict_update_raises_locktimeout_not_silent(self):
        """strict=True 超时必须抛 LockTimeout，绝不「跳过加锁继续写」。"""
        p = os.path.join(self.tmp, "st.json")
        m._save_json(p, {"n": 0})
        lk = m._thread_lock_for(p)
        lk.acquire()
        try:
            with self.assertRaises(m.LockTimeout):
                m._update_json(p, lambda d: d.__setitem__("n", 1), {"n": 0},
                               timeout=0.2, retries=1)
        finally:
            lk.release()
        self.assertEqual(m._load_json(p, {})["n"], 0, "超时后不该写入")

    def test_strict_save_raises_locktimeout_not_silent(self):
        """strict=True 的 _save_json 超时必须抛 LockTimeout，绝不无锁写。"""
        p = os.path.join(self.tmp, "ss.json")
        m._save_json(p, {"n": 0})
        lk = m._thread_lock_for(p)
        lk.acquire()
        try:
            with self.assertRaises(m.LockTimeout):
                m._save_json(p, {"n": 99}, timeout=0.2)
        finally:
            lk.release()
        self.assertEqual(m._load_json(p, {})["n"], 0, "超时后不该写入")

    def test_file_lock_defaults_to_strict(self):
        """默认必须 fail-closed：业务代码不显式写 strict=False 就拿不到宽松路径（误用边界）。"""
        import inspect
        sig = inspect.signature(m._file_lock)
        self.assertIs(sig.parameters["strict"].default, True,
                      "_file_lock 默认应为 strict=True")

    def test_proc_state_three_way_semantics(self):
        """F-03：进程判定必须三态。查不到（UNKNOWN）≠ 死了（DEAD）。"""
        self.assertEqual(m._proc_state(os.getpid()), m.PROC_ALIVE, "自己应判 ALIVE")
        self.assertEqual(m._proc_state(999999), m.PROC_DEAD, "不存在的 pid 应判 DEAD")
        self.assertEqual(m._proc_state("garbage"), m.PROC_UNKNOWN, "不可解析 pid 应判 UNKNOWN")
        self.assertEqual(m._proc_state(None), m.PROC_UNKNOWN)
        self.assertEqual(m._proc_state(-5), m.PROC_UNKNOWN, "非法 pid 应判 UNKNOWN")
        # Windows 错误码映射：明确不存在 → DEAD，无权限等 → UNKNOWN
        self.assertEqual(m._win_open_error_to_state(87), m.PROC_DEAD)    # ERROR_INVALID_PARAMETER
        self.assertEqual(m._win_open_error_to_state(1168), m.PROC_DEAD)  # ERROR_NOT_FOUND
        self.assertEqual(m._win_open_error_to_state(5), m.PROC_UNKNOWN)  # ERROR_ACCESS_DENIED
        self.assertEqual(m._win_open_error_to_state(0), m.PROC_UNKNOWN)  # 意外值保守处理

    def test_proc_state_unknown_blocks_lock_reclaim(self):
        """F-03 事故路径：查询失败被当成死亡 → 活锁被误删。UNKNOWN 绝不能触发回收。"""
        p = os.path.join(self.tmp, "f03.json")
        lock = p + ".lock"
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 4242, "process_start_time": 7,
                       "created_at": time.time() - 999}, f)
        real_state = m._proc_state
        m._proc_state = lambda pid: m.PROC_UNKNOWN   # 模拟无权限/查询失败
        try:
            self.assertFalse(m._lock_holder_stale(lock),
                             "UNKNOWN 不是死亡证据，不得判 stale")
            self.assertFalse(m._try_reclaim_stale(lock),
                             "UNKNOWN 状态下回收必须失败")
            self.assertTrue(os.path.exists(lock), "活锁被误删 = 跨进程互斥失效")
        finally:
            m._proc_state = real_state

    def test_proc_state_unknown_blocks_lease_reclaim(self):
        """F-03 租约同款：UNKNOWN 不得按 process_gone 回收。"""
        real_state = m._proc_state
        m._proc_state = lambda pid: m.PROC_UNKNOWN
        try:
            ok, why = m._lease_reclaimable({
                "run_id": "r1", "lease_until": time.time() - 1,
                "pid": 4242, "process_start_time": 7})
        finally:
            m._proc_state = real_state
        self.assertFalse(ok, "UNKNOWN 状态的租约禁止回收")
        self.assertEqual(why, "identity_unreadable")

    def test_proc_state_unknown_blocks_force_release(self):
        """F-03 强制恢复同款：UNKNOWN 无独立确认依据时必须拒绝。"""
        path = os.path.join(self.tmp, "lease-f03.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": 4242, "process_start_time": 7, "run_id": "rid"}, f)
        approval = os.path.join(self.tmp, "approve-f03.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "looks dead"}, f)
        real_state = m._proc_state
        m._proc_state = lambda pid: m.PROC_UNKNOWN
        try:
            ok, why = m.lease_force_release(path, approval)
        finally:
            m._proc_state = real_state
        self.assertFalse(ok, "UNKNOWN 状态不得凭批准放行")
        self.assertEqual(why, "identity_unconfirmed")
        self.assertTrue(os.path.exists(path))

    def test_lock_pid_field_falsy_not_reclaimed_by_mtime(self):
        """F-04：pid 字段存在但假值（0/空串/None）= 新格式损坏，不得借 mtime 逃生。"""
        for bad in (0, "", None):
            lock = os.path.join(self.tmp, "f04-%s.lock" % bad)
            with open(lock, "w", encoding="utf-8") as f:
                json.dump({"pid": bad, "process_start_time": 7,
                           "created_at": time.time() - 999}, f)
            old = time.time() - (m.LOCK_STALE_SEC + 30)
            os.utime(lock, (old, old))   # mtime 很老，旧逻辑会直接判 stale
            self.assertFalse(m._lock_holder_stale(lock),
                             "假值 pid=%r 不得按 mtime 判 stale" % (bad,))
            self.assertFalse(m._try_reclaim_stale(lock))
            self.assertTrue(os.path.exists(lock), "假值 pid 锁被误删")

    def test_lock_pid_field_garbage_not_reclaimed(self):
        """F-04：pid 字段不可解析（字符串垃圾）→ UNKNOWN，不回收。"""
        lock = os.path.join(self.tmp, "f04-g.lock")
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": "not-a-pid", "process_start_time": 7,
                       "created_at": time.time() - 999}, f)
        old = time.time() - (m.LOCK_STALE_SEC + 30)
        os.utime(lock, (old, old))
        self.assertFalse(m._lock_holder_stale(lock))
        self.assertTrue(os.path.exists(lock))

    def test_lock_no_pid_field_uses_mtime(self):
        """老格式（无 pid 字段）仍走 mtime 判据——F-04 只堵「字段在但值坏」。"""
        lock = os.path.join(self.tmp, "f04-legacy.lock")
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"created_at": 1}, f)   # 有内容但无 pid 字段
        old = time.time() - (m.LOCK_STALE_SEC + 30)
        os.utime(lock, (old, old))
        self.assertTrue(m._lock_holder_stale(lock), "老格式应按 mtime 判 stale")

    def test_force_release_missing_pid_needs_identity_evidence(self):
        """F-05：可读租约但 pid 缺失——不得用 if pid 短路跳过身份确认。"""
        path = os.path.join(self.tmp, "lease-f05.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"run_id": "rid"}, f)   # 可读但无 pid
        approval = os.path.join(self.tmp, "approve-f05.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "looks dead"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok, "无 pid 的可读租约必须先要独立确认依据")
        self.assertEqual(why, "identity_unconfirmed")
        self.assertTrue(os.path.exists(path))
        # 带独立确认依据后按正常闸口放行
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "looks dead",
                       "identity_evidence": "ps snapshot 2026-11-19"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertTrue(ok)
        self.assertEqual(why, "quarantined")

    def test_force_release_pid_zero_needs_identity_evidence(self):
        """F-05：pid=0 假值同款——不得绕过身份确认。"""
        path = os.path.join(self.tmp, "lease-f05b.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": 0, "run_id": "rid"}, f)
        approval = os.path.join(self.tmp, "approve-f05b.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "looks dead"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok)
        self.assertEqual(why, "identity_unconfirmed")

    @unittest.skipIf(os.name != "nt", "Windows-only ctypes 原型")
    def test_win_k32_declares_handle_prototypes(self):
        """F-06：OpenProcess restype 必须是 HANDLE（指针宽度），否则 64 位截断句柄。"""
        import ctypes
        from ctypes import wintypes
        k32 = m._win_k32()
        self.assertIs(k32.OpenProcess.restype, wintypes.HANDLE,
                      "restype 必须 HANDLE，c_int 会在 64 位截断")
        self.assertIs(k32.GetExitCodeProcess.restype, wintypes.BOOL)
        self.assertIs(k32.CloseHandle.restype, wintypes.BOOL)
        self.assertIs(k32.GetProcessTimes.restype, wintypes.BOOL)
        # 真实句柄往返：拿当前进程、查退出码、拿启动时间、关闭——任何一步截断即炸
        h = k32.OpenProcess(0x1000, False, os.getpid())
        self.assertTrue(h)
        try:
            code = wintypes.DWORD()
            self.assertTrue(k32.GetExitCodeProcess(h, ctypes.byref(code)))
            self.assertEqual(code.value, 259)   # STILL_ACTIVE
        finally:
            self.assertTrue(k32.CloseHandle(h))
        self.assertIsNotNone(m._proc_start_time(os.getpid()),
                             "启动时间读取失败多半是句柄截断")

    def test_parse_pid_rejects_lossy_coercion(self):
        """F-07：int() 折叠通道全部封死——999999.9 不是 999999，True 不是 1。"""
        self.assertIsNone(m._parse_pid(999999.9), "float 截断会把非法 pid 变合法")
        self.assertIsNone(m._parse_pid(True), "bool 是 int 子类，int(True)==1")
        self.assertIsNone(m._parse_pid(False))
        self.assertIsNone(m._parse_pid(123.0), "整数值 float 也不收")
        self.assertIsNone(m._parse_pid(" 4242"), "带空白的串不是规范 pid")
        self.assertIsNone(m._parse_pid("42.2"))
        self.assertIsNone(m._parse_pid("0x10"))
        self.assertIsNone(m._parse_pid(""))
        self.assertIsNone(m._parse_pid(None))
        self.assertIsNone(m._parse_pid(-5))
        self.assertIsNone(m._parse_pid(0))
        self.assertEqual(m._parse_pid(4242), 4242, "合法正整数照常收")
        self.assertEqual(m._parse_pid("4242"), 4242, "纯数字串照常收")

    def test_parse_pid_overflow_string_returns_none(self):
        """F-07 残留（第二十三轮）：超长十进制串 int() 抛 ValueError/OverflowError 不得逃逸。"""
        huge = "9" * 5000   # Python 3.11+ int() 十进制串有位数限制
        self.assertIsNone(m._parse_pid(huge), "超长数字串必须返回 None，不得抛异常")
        # _proc_state 同样不得炸
        self.assertEqual(m._proc_state(huge), m.PROC_UNKNOWN,
                         "超长 pid 串查进程状态必须 UNKNOWN，不得异常穿透")
        # _lock_holder_stale 不回收
        lock = os.path.join(self.tmp, "f07-overflow.lock")
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": huge, "process_start_time": 7,
                       "created_at": time.time() - 999}, f)
        self.assertFalse(m._lock_holder_stale(lock),
                         "超长 pid 串的锁不得被回收")
        # _lease_reclaimable 返回 identity_unreadable
        info = {"run_id": "r1", "pid": huge,
                "lease_until": 0, "process_start_time": 7}
        ok, why = m._lease_reclaimable(info, now=time.time())
        self.assertFalse(ok)
        self.assertEqual(why, "identity_unreadable")

    def test_proc_state_rejects_float_and_bool_pid(self):
        """F-07：float/bool pid 必须 UNKNOWN，不得借 int() 折叠后查到 DEAD。"""
        self.assertEqual(m._proc_state(999999.9), m.PROC_UNKNOWN,
                         "int(999999.9)==999999 不存在会误判 DEAD")
        self.assertEqual(m._proc_state(True), m.PROC_UNKNOWN,
                         "int(True)==1 多半是活进程，会误判 ALIVE 放行另一侧")
        self.assertEqual(m._proc_state(False), m.PROC_UNKNOWN)

    def test_lock_float_pid_not_reclaimed(self):
        """F-07 事故复现：pid=999999.9 的锁，旧逻辑截断成 999999 判 DEAD 后误删。"""
        lock = os.path.join(self.tmp, "f07.lock")
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999.9, "process_start_time": 7,
                       "created_at": time.time() - 999}, f)
        old = time.time() - (m.LOCK_STALE_SEC + 30)
        os.utime(lock, (old, old))
        self.assertFalse(m._lock_holder_stale(lock),
                         "float pid 不得被截断成整数后判死")
        self.assertTrue(os.path.exists(lock))

    def test_lock_corrupt_json_not_reclaimed_by_mtime(self):
        """F-08：非空损坏内容不是老格式，mtime 过期也不得借兼容路径回收。"""
        lock = os.path.join(self.tmp, "f08a.lock")
        with open(lock, "w", encoding="utf-8") as f:
            f.write("{broken")
        old = time.time() - (m.LOCK_STALE_SEC + 30)
        os.utime(lock, (old, old))
        self.assertFalse(m._lock_holder_stale(lock),
                         "损坏 JSON 不是「无 pid 字段」，不得走 mtime")
        self.assertFalse(m._try_reclaim_stale(lock))
        self.assertTrue(os.path.exists(lock))

    def test_lock_non_dict_json_not_reclaimed(self):
        """F-08：可解析但顶层不是字典（数组/标量）同样保守不回收。"""
        for raw in ("[1, 2]", "42", '"str"'):
            lock = os.path.join(self.tmp, "f08b-%s.lock" % abs(hash(raw)))
            with open(lock, "w", encoding="utf-8") as f:
                f.write(raw)
            old = time.time() - (m.LOCK_STALE_SEC + 30)
            os.utime(lock, (old, old))
            self.assertFalse(m._lock_holder_stale(lock),
                             "非字典内容 %r 不得回收" % raw)

    def test_lock_unreadable_not_reclaimed(self):
        """F-08：读取失败（路径是目录）≠ 旧格式，保守不回收。"""
        lock = os.path.join(self.tmp, "f08c.lock")
        os.makedirs(lock, exist_ok=True)   # open() 读目录必失败
        self.assertFalse(m._lock_holder_stale(lock),
                         "读不出来不得当老格式判 stale")

    def test_reclaim_removes_only_stale_lock(self):
        """确认已死的残留锁：回收成功，且只删这一份。"""
        p = os.path.join(self.tmp, "rc1.json")
        lock = p + ".lock"
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "process_start_time": 1,
                       "created_at": time.time() - 999}, f)
        self.assertTrue(m._try_reclaim_stale(lock))
        self.assertFalse(os.path.exists(lock), "stale 锁应被删除")
        self.assertTrue(os.path.exists(lock + ".reclaim"),
                        "守卫文件是内核锁载体，必须常驻")

    def test_reclaim_does_not_delete_fresh_lock(self):
        """F-02 本质：迟到的回收者不得删掉「先删后建」的新锁（活锁）。"""
        p = os.path.join(self.tmp, "rc2.json")
        lock = p + ".lock"
        with m._file_lock(p):
            # 进程 A 持有新锁（活锁）。进程 B 按旧判定（stale）来回收——
            # 守卫内重新判定必须看到活锁，拒绝删除。
            self.assertFalse(m._try_reclaim_stale(lock),
                             "活锁绝不能被回收")
            self.assertTrue(os.path.exists(lock), "新锁必须原样存活")
        self.assertFalse(os.path.exists(lock), "退出后正常清理")

    def test_reclaim_race_two_threads_never_double_enter(self):
        """两线程同时抢一份 stale 锁：恰好一个回收成功，临界区绝不重叠。"""
        p = os.path.join(self.tmp, "rc3.json")
        lock = p + ".lock"
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "process_start_time": 1,
                       "created_at": time.time() - 999}, f)
        events = []
        ev_lock = threading.Lock()

        def worker():
            with m._file_lock(p, timeout=15, strict=True):
                with ev_lock:
                    events.append(("ENTER", time.time()))
                time.sleep(0.05)
                with ev_lock:
                    events.append(("LEAVE", time.time()))

        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(events), 4, "两线程都应进过临界区")
        events.sort(key=lambda e: e[1])
        self.assertEqual([e[0] for e in events], ["ENTER", "LEAVE", "ENTER", "LEAVE"],
                         "临界区必须严格串行，绝不重叠")
        self.assertFalse(os.path.exists(lock))

    def test_reclaim_race_multi_process_mutual_exclusion(self):
        """真多进程竞争（审查 F-02 要求）：两进程同时回收同一份 stale 锁，
        全程最多一个进程在临界区内，且先取得新锁后另一进程不得删掉它。"""
        p = os.path.join(self.tmp, "rc4.json")
        lock = p + ".lock"
        log = os.path.join(self.tmp, "rc4.log")
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "process_start_time": 1,
                       "created_at": time.time() - 999}, f)
        worker = os.path.join(self.tmp, "rc4_worker.py")
        gate = os.path.join(self.tmp, "rc4.gate")
        with open(worker, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys, time, importlib.util\n"
                "spec = importlib.util.spec_from_file_location('cgs', sys.argv[1])\n"
                "m = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(m)\n"
                "path, log, n, gate = sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]\n"
                "while not os.path.exists(gate):\n"   # 启动屏障：两进程同时起跑才构成真实竞争
                "    time.sleep(0.005)\n"
                "for i in range(n):\n"
                "    with m._file_lock(path, timeout=15, strict=True):\n"
                "        open(log, 'a', encoding='utf-8').write('ENTER %s %f\\n' % (os.getpid(), time.time()))\n"
                "        time.sleep(0.05)\n"
                "        open(log, 'a', encoding='utf-8').write('LEAVE %s %f\\n' % (os.getpid(), time.time()))\n"
            )
        procs = []
        err_logs = []
        for i in range(2):
            ef = open(os.path.join(self.tmp, "rc4_err%d.log" % i), "wb")
            err_logs.append(ef)
            procs.append(subprocess.Popen(
                [sys.executable, worker, os.path.abspath(SCRIPT), p, log, "2", gate],
                stdout=subprocess.DEVNULL, stderr=ef))
        open(gate, "w").close()   # 两进程都起来后放行
        for ef in err_logs:
            ef.close()
        errs = ""
        for i, pr in enumerate(procs):
            rc = pr.wait(timeout=60)
            if rc != 0:
                with open(os.path.join(self.tmp, "rc4_err%d.log" % i), "rb") as f:
                    errs += f.read().decode("utf-8", "replace")[-800:]
            self.assertEqual(rc, 0, "worker 不应异常退出：%s" % errs)
        with open(log, encoding="utf-8") as f:
            ev = [ln.split() for ln in f if ln.strip()]
        self.assertEqual(len(ev), 8, "2 进程 × 2 次 = 8 条事件（丢了更新就是互斥破了）")
        ev.sort(key=lambda e: float(e[2]))
        depth = 0
        for kind, _pid, _ts in ev:
            if kind == "ENTER":
                depth += 1
                self.assertLessEqual(depth, 1, "同一时刻最多一个进程在临界区内")
            else:
                depth -= 1
                self.assertGreaterEqual(depth, 0, "LEAVE 不得多于 ENTER")
        self.assertEqual(depth, 0)
        self.assertFalse(os.path.exists(lock))


# ---------------------------------------------------------------- 轮转
class TestRotation(TmpBase):
    def test_normalize_fills_all_identities(self):
        st = m._normalize_rotation({"cursor": 99})
        self.assertEqual(set(st["order"]), set(m.ROTATION_ORDER))
        self.assertLess(st["cursor"], len(m.ROTATION_PRIMARY))
        for a in m.ROTATION_ORDER:
            self.assertIn(a, st["created"])
            self.assertIn(a, st["failures"])

    def test_purge_expired_drops_only_expired(self):
        st = m._normalize_rotation({})
        m.mark_unavailable(st, "acct1", "HTTP 401", ttl=-1)   # 已过期
        m.mark_unavailable(st, "acct2", "HTTP 403", ttl=3600)  # 未过期
        self.assertTrue(m.purge_expired(st))
        self.assertNotIn("acct1", st["unavailable"])
        self.assertIn("acct2", st["unavailable"])

    def test_fallback_requires_all_login_429(self):
        st = m._normalize_rotation({})
        self.assertIsNone(m.anon_fallback_block_reason(st, {"acct1": 429, "acct2": 429}))
        for outcomes in ({"acct1": 429, "acct2": 401}, {"acct1": 403, "acct2": 403},
                         {"acct1": 429, "acct2": "SKIP:cooldown"}, {}):
            with self.subTest(outcomes=outcomes):
                self.assertIsNotNone(m.anon_fallback_block_reason(st, outcomes))

    def test_fallback_blocked_when_disabled_by_env(self):
        st = m._normalize_rotation({})
        old = os.environ.get("CHATGPT_ANON_FALLBACK")
        os.environ["CHATGPT_ANON_FALLBACK"] = "0"
        try:
            self.assertIsNotNone(m.anon_fallback_block_reason(st, {"acct1": 429, "acct2": 429}))
        finally:
            if old is None:
                os.environ.pop("CHATGPT_ANON_FALLBACK", None)
            else:
                os.environ["CHATGPT_ANON_FALLBACK"] = old

    def test_rotation_plan_puts_unavailable_last(self):
        st = m._normalize_rotation({"cursor": 0})
        m.mark_unavailable(st, "acct1", "HTTP 401")
        plan = m.rotation_plan(st)
        self.assertEqual(plan[-1], "acct1")
        self.assertEqual(len(plan), len(m.ROTATION_PRIMARY))


# ---------------------------------------------------------------- 落盘
class TestReplySave(TmpBase):
    def _api(self):
        api = object.__new__(m.ChatGPTAPI)
        api.base_dir = os.path.join(self.tmp, "auth")
        api.account = "acct1"
        api.is_anon = False
        return api

    def test_full_text_is_kept(self):
        api = self._api()
        head = "HEAD" * 1000
        tail = "TAIL" * 6000           # 总长约 2.6 万字符，超过旧的 2 万字截断
        text = head + tail
        path = api.save_reply("11111111-2222-4333-8444-555555555555", "p.md", text)
        body = _read(path)
        self.assertIn(text, body, "回复必须全文落盘，不能只留末尾 2 万字")
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_optional_truncation_is_labelled(self):
        api = self._api()
        old = os.environ.get("CHATGPT_REPLY_MAX_CHARS")
        os.environ["CHATGPT_REPLY_MAX_CHARS"] = "10"
        try:
            path = api.save_reply("11111111-2222-4333-8444-555555555555", "p.md", "0123456789ABCDEF")
        finally:
            if old is None:
                os.environ.pop("CHATGPT_REPLY_MAX_CHARS", None)
            else:
                os.environ["CHATGPT_REPLY_MAX_CHARS"] = old
        body = _read(path)
        self.assertIn("已按 CHATGPT_REPLY_MAX_CHARS=10 截断", body)
        self.assertIn("0123456789", body)

    def test_same_second_does_not_overwrite(self):
        api = self._api()
        cid = "11111111-2222-4333-8444-555555555555"
        p1 = api.save_reply(cid, "p.md", "first")
        p2 = api.save_reply(cid, "p.md", "second")
        self.assertNotEqual(p1, p2)
        self.assertIn("first", _read(p1))
        self.assertIn("second", _read(p2))

    def test_malicious_conv_id_cannot_escape(self):
        api = self._api()
        path = api.save_reply("../../evil", "p.md", "x")
        inside = os.path.abspath(os.path.join(api.base_dir, "replies"))
        self.assertTrue(os.path.abspath(path).startswith(inside + os.sep), path)


# ---------------------------------------------------------------- 其他
class TestMisc(TmpBase):
    def test_record_conv_rejects_bad_id(self):
        m.record_conv("../../evil", "acct1", "p.md")
        self.assertEqual(m._load_json(m.CONV_MAP_FILE, {}), {})

    def test_device_cookie_rejects_traversal(self):
        m.save_device_cookie("oai-did=../../evil; other=1")
        files = os.listdir(m.anon_device_dir()) if os.path.isdir(m.anon_device_dir()) else []
        self.assertEqual([f for f in files if ".." in f or "/" in f], [])
        self.assertEqual(m.load_device_cookie("../../evil"), "")

    def test_retry_after_is_clamped(self):
        api = object.__new__(m.ChatGPTAPI)
        for raw, want in (("1", 5), ("30", 30), ("99999", 3600), (None, 42), ("junk", 42)):
            api.last_headers = {} if raw is None else {"retry-after": raw}
            self.assertEqual(api._retry_after(42), want)

    def test_retry_after_prefers_body_rate_limit(self):
        # 用户裁定 2026-11-19：限速等待按返回体真实时限，body 优先于 Retry-After 头
        api = object.__new__(m.ChatGPTAPI)
        api.last_headers = {"retry-after": "30"}
        self.assertEqual(api._retry_after(90, '{"detail":{"rate_limit":{"resets_in":300}}}'), 300)
        self.assertEqual(api._retry_after(90, '{"resets_in":45}'), 45)
        self.assertEqual(api._retry_after(90, '{"retry_after":60}'), 60)
        # body 没有时限字段 -> 回退 Retry-After 头
        self.assertEqual(api._retry_after(90, '{"detail":"quota"}'), 30)
        # 代理/网络类失败定性：status 0 / EXC 一律 proxy，429 是 ratelimit
        self.assertEqual(m.ChatGPTAPI.classify_failure(0, "EXC SSL EOF"), "proxy")
        self.assertEqual(m.ChatGPTAPI.classify_failure(429, '{"detail":"rate"}'), "ratelimit")
        self.assertEqual(m.ChatGPTAPI.classify_failure(403, "Unusual activity"), "risk")
        self.assertEqual(m.ChatGPTAPI.classify_failure(500, "oops"), "other")

    def test_extract_reply_sse_takes_last_assistant(self):
        sse = "\n".join([
            'data: {"message":{"author":{"role":"assistant"},"content":{"parts":["one"]}}}',
            'data: not-json',
            'data: {"message":{"author":{"role":"user"},"content":{"parts":["u"]}}}',
            'data: {"message":{"author":{"role":"assistant"},"content":{"parts":["two","!"]}}}',
            'data: [DONE]',
        ])
        self.assertEqual(m.ChatGPTAPI.extract_reply_sse(sse), "two!")

    def test_tail_from_mapping_picks_newest(self):
        raw = json.dumps({"mapping": {
            "a": {"message": {"create_time": 1}},
            "b": {"message": {"create_time": 9}},
            "c": {"message": None},
        }})
        self.assertEqual(m.ChatGPTAPI.tail_from_mapping(raw), "b")

    def test_pow_solver_is_stable(self):
        from pow_solver import solve
        self.assertTrue(solve("seed", "0"))
        self.assertTrue(solve("", "0"))

    def test_continue_full_does_not_string_substitute_js(self):
        """占位符替换会连 `function(text)` 一起换掉，产出语法错误的 JS。"""
        src = _read(os.path.join(HERE, "..", "scripts", "continue_full.py"))
        self.assertNotIn('.replace("text"', src)
        self.assertIn("Input.insertText", src)
        self.assertNotIn("wait=", src, "cdp.navigate 的参数名是 timeout")


# ---------------------------------------------------------------- 设备身份留存
DEV_A = "aaaaaaaa-1111-4111-8111-111111111111"
DEV_B = "bbbbbbbb-2222-4222-8222-222222222222"


class TestDeviceIdentity(TmpBase):
    def _jar(self, did, extra=None):
        jar = [{"name": "oai-did", "value": did, "domain": ".chatgpt.com", "path": "/",
                "httpOnly": False, "secure": True},
               {"name": "__cf_bm", "value": "cf-value", "domain": ".chatgpt.com", "path": "/",
                "httpOnly": True}]
        jar.extend(extra or [])
        return jar

    def test_parse_header_tolerates_junk_and_equals_in_value(self):
        got = m.parse_cookie_header(" a=1; b=2=3 ;; =empty; c ;d=4 ")
        self.assertEqual(got, {"a": "1", "b": "2=3", "d": "4"})

    def test_jar_is_persisted_and_header_is_readable(self):
        did = m.save_device_cookie(self._jar(DEV_A))
        self.assertEqual(did, DEV_A)
        self.assertTrue(os.path.exists(os.path.join(m.anon_device_dir(), DEV_A + ".json")))
        self.assertTrue(os.path.exists(os.path.join(m.anon_device_dir(), DEV_A + ".txt")))
        header = m.load_device_cookie(DEV_A)
        self.assertIn("oai-did=" + DEV_A, header)
        self.assertIn("__cf_bm=cf-value", header, "HttpOnly cookie 也必须留在快照里")
        self.assertEqual(len(m.load_device_jar(DEV_A)), 2)

    def test_auth_cookies_are_never_written_to_disk(self):
        jar = self._jar(DEV_A, [{"name": "__Secure-next-auth.session-token", "value": "SECRET",
                                 "domain": ".chatgpt.com", "path": "/"}])
        m.save_device_cookie(jar)
        raw = _read(os.path.join(m.anon_device_dir(), DEV_A + ".json")) + \
            _read(os.path.join(m.anon_device_dir(), DEV_A + ".txt"))
        self.assertNotIn("SECRET", raw, "登录态 token 不该落到 nologin 目录")
        self.assertNotIn("session-token", raw)

    def test_cookie_header_only_snapshot_still_works(self):
        m.save_device_cookie("oai-did=%s; __cf_bm=x" % DEV_A)
        self.assertTrue(m.load_device_cookie(DEV_A).startswith("oai-did="))
        # 删掉 .txt 后应能从 jar 重建（老快照只有 json 的兼容路径）
        os.remove(os.path.join(m.anon_device_dir(), DEV_A + ".txt"))
        self.assertIn(DEV_A, m.load_device_cookie(DEV_A))

    def test_header_string_also_drops_auth_cookie(self):
        m.save_device_cookie("oai-did=%s; __Secure-next-auth.session-token=SECRET2" % DEV_A)
        self.assertNotIn("SECRET2", m.load_device_cookie(DEV_A))

    def test_device_list_is_newest_first(self):
        m.save_device_cookie(self._jar(DEV_A))
        time.sleep(0.05)
        m.save_device_cookie(self._jar(DEV_B))
        self.assertEqual(m.list_device_ids()[:2], [DEV_B, DEV_A])

    def test_device_summary_counts_bound_sessions(self):
        m.save_device_cookie(self._jar(DEV_A))
        m.record_anon_session("11111111-2222-4333-8444-555555555555", device=DEV_A)
        m.record_anon_session("22222222-3333-4444-8555-666666666666", device=DEV_A)
        row = [r for r in m.device_summary()["devices"] if r["device"] == DEV_A][0]
        self.assertEqual(row["sessions"], 2)
        self.assertTrue(row["full_jar"])
        self.assertEqual(row["cookies"], 2)

    def test_read_order_prefers_recorded_then_rest(self):
        m.save_device_cookie(self._jar(DEV_A))
        m.save_device_cookie(self._jar(DEV_B))
        order = m._read_device_order(DEV_B)
        self.assertEqual(order[0], DEV_B)
        self.assertEqual(set(order), {DEV_A, DEV_B})


class FakeAnonAPI:
    """read_anon 用：按 cookie 里的设备号返回预设状态码。"""

    account = "nologin"
    is_anon = True

    def __init__(self, status_by_device, body='{"mapping": {}}'):
        self.cookie = ""
        self.status_by_device = status_by_device
        self.body = body
        self.calls = []

    def req(self, method, url, body=None, extra_headers=None, timeout=120):
        did = m.device_id_from_cookie(self.cookie)
        self.calls.append(did)
        return self.status_by_device.get(did, 404), self.body

    def harvest_transcript(self, raw):
        return [("user", "q"), ("assistant", "a")]

    def harvest_transcript_checked(self, raw):
        return [("user", "q"), ("assistant", "a")], True

    def _device_id(self):
        return m.device_id_from_cookie(self.cookie)

    def ensure_device_cookie(self, refresh=False):
        self.cookie = "oai-did=%s" % DEV_B
        return self.cookie


class TestAnonReadFallback(TmpBase):
    CID = "33333333-4444-4555-8666-777777777777"

    def test_falls_back_to_other_device_and_repairs_mapping(self):
        m.save_device_cookie("oai-did=%s" % DEV_A)
        m.save_device_cookie("oai-did=%s" % DEV_B)
        m.record_anon_session(self.CID, device=DEV_A)   # 记录的设备已失效
        api = FakeAnonAPI({DEV_B: 200})
        status, last, transcript = m.read_anon(api, self.CID)
        self.assertEqual(status, 200)
        self.assertEqual(last, "a")
        self.assertEqual(api.calls, [DEV_A, DEV_B])
        self.assertEqual(m.anon_session(self.CID)["device"], DEV_B, "命中后应自动修正归属")

    def test_stops_immediately_on_rate_limit(self):
        m.save_device_cookie("oai-did=%s" % DEV_A)
        m.save_device_cookie("oai-did=%s" % DEV_B)
        m.record_anon_session(self.CID, device=DEV_A)   # 先试登记设备，它是 429
        api = FakeAnonAPI({DEV_A: 429, DEV_B: 200})
        status, _last, _t = m.read_anon(api, self.CID)
        self.assertEqual(status, 429)
        self.assertEqual(api.calls, [DEV_A], "429 是通道问题，换设备没有意义")

    def test_no_snapshot_falls_back_to_live_browser(self):
        api = FakeAnonAPI({DEV_B: 200})
        status, _last, _t = m.read_anon(api, self.CID)
        self.assertEqual(status, 200)
        self.assertEqual(api.calls, [DEV_B])

    def test_archive_written_when_transcript_saved(self):
        m.save_device_cookie("oai-did=%s" % DEV_A)
        m.record_anon_session(self.CID, device=DEV_A)
        api = FakeAnonAPI({DEV_A: 200})
        m.read_anon(api, self.CID)
        path = m.anon_archive_path(self.CID)
        self.assertTrue(os.path.exists(path))
        self.assertIn(self.CID, _read(path))


class FakeBrowser:
    """send_anon 用：只实现发送前的身份留存与发送失败。"""

    def __init__(self, did, last_cid=None):
        self.did = did
        self.last_cid = last_cid
        self.last_tab = "tab-1"

    def cookie_jar(self):
        return [{"name": "oai-did", "value": self.did, "domain": ".chatgpt.com", "path": "/"}]

    def identity(self):
        return m.save_device_cookie(self.cookie_jar())

    def cookie(self):
        return "oai-did=" + self.did

    def send(self, text, conv_id=None):
        raise RuntimeError("boom")


class TestAnonSendFailure(TmpBase):
    def test_orphan_session_keeps_its_device(self):
        api = object.__new__(m.ChatGPTAPI)
        api.account = "nologin"
        api.is_anon = True
        api.cookie = ""
        api.anon = FakeBrowser(DEV_A, last_cid="44444444-5555-4666-8777-888888888888")
        with self.assertRaises(RuntimeError):
            m.send_anon(api, None, "p.md", "hello")
        s = m.anon_session("44444444-5555-4666-8777-888888888888")
        self.assertIsNotNone(s, "异常路径也必须登记会话，否则就是查不到的孤儿")
        self.assertEqual(s.get("device"), DEV_A, "设备号必须取浏览器侧的，不能取空的 api.cookie")


# ---------------------------------------------------------------- 投递流程（离线桩）
class FakeAPI:
    """只实现 send_prompt 用到的那几个方法，用来在没有网络/凭据时跑通主流程。"""

    is_anon = False
    account = "acct1"

    def __init__(self, base_dir, send_status=200, sse=None):
        self.base_dir = base_dir
        self.send_status = send_status
        self.sse = sse
        self.sent = []
        self.last_headers = {}
        self.last_truncated = False
        self.gets = []
        self.get_status = 200
        self.get_body = "{}"

    def req(self, method, url, body=None, extra_headers=None, timeout=120):
        self.gets.append((method, url))
        return self.get_status, self.get_body

    def live_tail(self, conv_id, tries=2):
        return "parent-msg-1", '{"mapping": {}}'

    def cached_tail(self, conv_id):
        return None

    def requirements(self):
        return "req-token", "proof-token"

    def send(self, conv_id, text, parent_message_id, req_token, proof, timeout=420, model=None):
        self.sent.append({"conv_id": conv_id, "parent": parent_message_id, "text": text,
                          "model": model, "timeout": timeout})
        return self.send_status, self.sse or ""

    def warm_cookies(self):
        return False

    def extract_reply_sse(self, raw):
        return m.ChatGPTAPI.extract_reply_sse(raw)

    def extract_reply_sse_checked(self, raw):
        return m.ChatGPTAPI.extract_reply_sse_checked(raw)

    def harvest_mapping(self, raw):
        return m.ChatGPTAPI.harvest_mapping(raw)

    def harvest_mapping_checked(self, raw):
        return m.ChatGPTAPI.harvest_mapping_checked(raw)

    def save_reply(self, conv_id, pfile, text):
        return m.ChatGPTAPI.save_reply(self, conv_id, pfile, text)

    def quota(self):
        return {}


class TestSendFlow(TmpBase):
    def _prompt(self, body):
        p = os.path.join(self.tmp, "prompt.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    def _sse(self, conv_id, reply):
        return "\n".join([
            'data: {"conversation_id": "%s", "message": {"author": {"role": "assistant"},'
            ' "content": {"parts": ["%s"]}}}' % (conv_id, reply),
            "data: [DONE]",
        ])

    def test_happy_path_keeps_conversation_id(self):
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "hello"))
        prompt = self._prompt("# 注释行会被去掉\n真正的正文\n")
        status, path, conv = m.send_prompt(api, cid, prompt)
        self.assertEqual(status, 200)
        self.assertEqual(conv, cid, "必须认回原会话，不能静默落新会话")
        self.assertEqual(api.sent[0]["conv_id"], cid)
        self.assertEqual(api.sent[0]["text"], "真正的正文")
        self.assertIn("hello", _read(path))

    def test_thinking_uses_thinking_slug(self):
        """CHATGPT_THINKING 开启时必须把 -t- 思考模型传进 POST body。"""
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"),
                      sse='data: {"conversation_id": "%s"}\n\ndata: [DONE]' % cid)
        api.resolve_thinking_model = lambda _a: "gpt-6-t-mini"
        old = os.environ.get("CHATGPT_THINKING")
        os.environ["CHATGPT_THINKING"] = "1"
        try:
            status, _p, _c = m.send_prompt(api, None, self._prompt("x"), new_chat=True)
        finally:
            if old is None:
                os.environ.pop("CHATGPT_THINKING", None)
            else:
                os.environ["CHATGPT_THINKING"] = old
        self.assertEqual(status, 200)
        self.assertEqual(api.sent[-1]["model"], "gpt-6-t-mini")

    def test_truncated_stream_completed_by_get(self):
        """A28：流被截断时用 GET 补全文落盘，且不得换身份重投（sent 只 1 条）。"""
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"),
                      sse='data: {"conversation_id": "%s"}\n\ndata: [DONE]' % cid)
        api.last_truncated = True
        api.extract_reply_sse = lambda raw: "HALF-ANSWER"
        api.get_body = json.dumps({"mapping": {"n1": {"message": {
            "author": {"role": "assistant"},
            "content": {"parts": ["FULL-ANSWER-END"]},
            "create_time": 9}}}})
        status, path, conv = m.send_prompt(api, None, self._prompt("x"), new_chat=True,
                                           thinking=False)
        self.assertEqual(status, 200)
        self.assertEqual(conv, cid)
        self.assertEqual(len(api.sent), 1, "截断不得触发重投")
        self.assertTrue(any(cid in u for _m, u in api.gets), "必须 GET 拉全文")
        with open(path, encoding="utf-8") as f:
            self.assertIn("FULL-ANSWER-END", f.read())

    def test_missing_conversation_id_is_failure_not_silent_success(self):
        """A11：200 但解析不出 id → 判失败；禁止回落原 id 继续落盘，也禁止重投。"""
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse="data: {\"v\": 1}\n\ndata: [DONE]")
        status, path, conv = m.send_prompt(api, cid, self._prompt("x"), thinking=False)
        self.assertEqual(status, 1, "解析不出 id 必须判失败")
        self.assertIsNone(path, "没有落点证据就不许落盘")
        self.assertEqual(len(api.sent), 1, "不得换身份重投")

    def test_invalid_conversation_id_is_rejected(self):
        """A11：解析出的 id 不合规（如路径穿越串）→ 判失败，不得回落原 id。"""
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"),
                      sse='data: {"conversation_id": "../../evil"}\n\ndata: [DONE]')
        status, path, _conv = m.send_prompt(api, cid, self._prompt("x"), thinking=False)
        self.assertEqual(status, 1)
        self.assertIsNone(path)
        self.assertEqual(len(api.sent), 1)

    def test_reply_digest_reports_parts_and_sha(self):
        """A12：GET 全文要按 part 数 + 拼接 SHA-256 对账，不能只看「变长了」。"""
        raw = json.dumps({"mapping": {
            "u": {"message": {"author": {"role": "user"},
                              "content": {"parts": ["ask"]}, "create_time": 1}},
            "a": {"message": {"id": "msg-1", "author": {"role": "assistant"},
                              "content": {"parts": ["abc", "defgh"]}, "create_time": 5}},
        }})
        dg = m.reply_digest(raw)
        self.assertEqual(dg["parts"], [3, 5])
        self.assertEqual(dg["chars"], 8)
        self.assertEqual(dg["sha256"], __import__("hashlib").sha256(b"abcdefgh").hexdigest())
        self.assertEqual(dg["message_id"], "msg-1")
        self.assertIsNone(m.reply_digest('{"mapping": {}}'))
        self.assertIsNone(m.reply_digest("not json"))

    def test_reply_digest_matches_harvest_with_whitespace(self):
        """F-26：digest 与 harvest 必须同口径——首尾带空白的合法回复不能被误判不完整。

        旧版 reply_digest 按未 strip 的 parts 算 chars/sha，harvest_mapping_checked
        返回 strip 后的 full；send_prompt 逐字节比对两者，合法完整回复只要首尾带
        空白（LLM 输出极常见）就永不相等 → 被误标「完整性未证明」，F-24 门禁失去
        区分度。修复后两者共用 _assistant_text，本用例钉住口径一致。
        """
        import hashlib
        raw = json.dumps({"mapping": {"a": {"message": {
            "id": "msg-ws", "author": {"role": "assistant"},
            "content": {"parts": ["\nHello  ", " world\n"]},
            "create_time": 5}}}})
        dg = m.reply_digest(raw)
        full, complete = m.ChatGPTAPI.harvest_mapping_checked(raw)
        self.assertTrue(complete)
        self.assertEqual(full, "Hello   world", "harvest 按 strip 口径返回")
        self.assertEqual(dg["chars"], len(full), "摘要字数必须与落盘全文同口径")
        self.assertEqual(dg["sha256"], hashlib.sha256(full.encode("utf-8")).hexdigest(),
                         "摘要 sha 必须与落盘全文逐字节一致（F-26）")

    def test_timeout_is_split_by_thinking(self):
        """A16：思考模式给 30 分钟，非思考给 7 分钟；同一条预算会误杀 19 分钟的思考回复。"""
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"),
                      sse='data: {"conversation_id": "%s"}\n\ndata: [DONE]' % cid)
        api.resolve_thinking_model = lambda _a: "gpt-6-t-mini"
        m.send_prompt(api, None, self._prompt("x"), new_chat=True, thinking=True)
        self.assertEqual(api.sent[-1]["timeout"], 1800)
        m.send_prompt(api, None, self._prompt("x"), new_chat=True, thinking=False)
        self.assertEqual(api.sent[-1]["timeout"], 420)

    def test_default_keeps_auto_model(self):
        """未开启思考模式时不覆写 model（保持 auto）。"""
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"),
                      sse='data: {"conversation_id": "%s"}\n\ndata: [DONE]' % cid)
        status, _p, _c = m.send_prompt(api, None, self._prompt("x"), new_chat=True)
        self.assertEqual(status, 200)
        self.assertIsNone(api.sent[-1]["model"])

    def test_new_chat_has_no_parent(self):
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "owner ruling"))
        status, _p, conv = m.send_prompt(api, None, self._prompt("x"), new_chat=True)
        self.assertEqual(status, 200)
        self.assertEqual(conv, cid, "新会话必须从 SSE 里认出 id")
        self.assertIsNone(api.sent[0]["parent"], "新会话不该带 parent_message_id")
        self.assertIsNone(api.sent[0]["conv_id"], "新会话请求体不许带 conversation_id（A3）")

    def test_post_payload_carries_conversation_id_only_when_given(self):
        """A3/A10 的请求体级别证据：--new 时 body 里根本没有 conversation_id 字段。"""
        api = m.ChatGPTAPI.__new__(m.ChatGPTAPI)
        seen = {}

        def fake_req(method, url, body=None, extra_headers=None, timeout=120):
            seen.clear()
            seen.update(body or {})
            return 200, ""

        api.req = fake_req
        api.send(None, "hi", None, "tok", "proof", model=None)
        self.assertNotIn("conversation_id", seen, "新会话不许带该字段")
        api.send("11111111-2222-4333-8444-555555555555", "hi", "p1", "tok", "proof")
        self.assertEqual(seen.get("conversation_id"), "11111111-2222-4333-8444-555555555555")

    def test_413_is_reported_not_swallowed(self):
        api = FakeAPI(os.path.join(self.tmp, "auth"), send_status=413,
                      sse='{"detail": "conversation_too_large"}')
        status, path, conv = m.send_prompt(api, "11111111-2222-4333-8444-555555555555", self._prompt("x"))
        self.assertEqual(status, 413)
        self.assertIsNone(path)

    def test_missing_prompt_file_is_a_clean_error(self):
        api = FakeAPI(os.path.join(self.tmp, "auth"))
        with self.assertRaises(RuntimeError):
            m.send_prompt(api, None, os.path.join(self.tmp, "nope.md"), new_chat=True)

    def test_empty_or_comment_only_prompt_is_rejected(self):
        api = FakeAPI(os.path.join(self.tmp, "auth"))
        for body in ("", "   \n", "# 只有注释\n# 还是注释\n"):
            with self.subTest(body=body):
                with self.assertRaises(RuntimeError):
                    m.send_prompt(api, None, self._prompt(body), new_chat=True)

    def test_invalid_conv_id_rejected_before_network(self):
        api = FakeAPI(os.path.join(self.tmp, "auth"))
        with self.assertRaises(ValueError):
            m.send_prompt(api, "../../evil", self._prompt("x"))
        self.assertEqual(api.sent, [])


class _StubServer(object):
    """起一个只接一次连接的本地 TCP 服务，用 handler(conn) 定义协议。"""

    def __init__(self, handler):
        self.seen = {}
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self._handler = handler
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        conn = None
        try:
            conn, _ = self.srv.accept()
            self._handler(conn, self.seen)
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            self.srv.close()

    def close(self):
        try:
            self.srv.close()
        except OSError:
            pass


class TestProxy(TmpBase):
    """统一出口：解析优先级、fail-closed、http/socks 真实分流、出口判定。"""

    ENV_KEYS = ("CHATGPT_PROXY", "CHATGPT_PROXY_URL", "CHATGPT_ALLOW_DIRECT",
                "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
                "all_proxy", "ALL_PROXY")

    def setUp(self):
        super().setUp()
        self._pf = m.PROXY_FILE
        m.PROXY_FILE = os.path.join(self.tmp, "proxy.json")
        m._PROXY_CACHE.clear()
        self._env = {}
        for k in self.ENV_KEYS:
            self._env[k] = os.environ.pop(k, None)

    def tearDown(self):
        m.PROXY_FILE = self._pf
        m._PROXY_CACHE.clear()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _cfg(self, **kw):
        base = {"autodetect": False}
        base.update(kw)
        with open(m.PROXY_FILE, "w", encoding="utf-8") as f:
            json.dump(base, f)

    # ---- 形态与优先级 ----
    def test_normalize_proxy_variants(self):
        self.assertEqual(m.normalize_proxy("127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(m.normalize_proxy(" socks5h://u:p@1.2.3.4:1080 "),
                         "socks5h://u:p@1.2.3.4:1080")
        self.assertEqual(m.normalize_proxy(""), "")
        for bad in ("ftp://x:1", "http://", "socks4://x:1"):
            with self.assertRaises(ValueError):
                m.normalize_proxy(bad)

    def test_env_beats_file(self):
        self._cfg(url="socks5h://file:1080")
        os.environ["CHATGPT_PROXY"] = "http://env:8080"
        self.assertEqual(m.resolve_proxy(refresh=True), "http://env:8080")
        del os.environ["CHATGPT_PROXY"]
        self.assertEqual(m.resolve_proxy(refresh=True), "socks5h://file:1080")

    def test_fail_closed_when_nothing_configured(self):
        self._cfg()
        with self.assertRaises(SystemExit) as cm:
            m.require_proxy(refresh=True)
        # 3 = 「未满足联网条件」，与参数错(2)、业务失败(1) 区分开
        self.assertEqual(cm.exception.code, 3)

    def test_invalid_env_proxy_exits_cleanly(self):
        self._cfg()
        os.environ["CHATGPT_PROXY"] = "ftp://x:1"
        with self.assertRaises(SystemExit) as cm:
            m.resolve_proxy_info(refresh=True)
        self.assertEqual(cm.exception.code, 3)

    def test_allow_direct_is_explicit_only(self):
        self._cfg()
        os.environ["CHATGPT_ALLOW_DIRECT"] = "1"
        self.assertEqual(m.require_proxy(refresh=True), "")
        self.assertTrue(m.allow_direct())

    def test_proxy_set_then_clear(self):
        m.save_proxy_config("socks5h://u:p@1.2.3.4:1080")
        self.assertEqual(m.load_proxy_config()["url"], "socks5h://u:p@1.2.3.4:1080")
        m.save_proxy_config("")
        self.assertNotIn("url", m.load_proxy_config())

    # ---- 真分流 ----
    def test_http_request_goes_through_proxy(self):
        def handler(conn, seen):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            seen["line"] = data.split(b"\r\n")[0].decode("latin1")
            body = b"via-proxy"
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: %d\r\n\r\n" % len(body) + body)

        stub = _StubServer(handler)
        try:
            st, txt = m._http_get_text("http://example.invalid/x",
                                       "http://127.0.0.1:%d" % stub.port, timeout=10)
        finally:
            stub.close()
        self.assertEqual(st, 200)
        self.assertEqual(txt, "via-proxy")
        # 走代理时请求行是 absolute-form，证明没在本地直连
        self.assertIn("http://example.invalid/x", stub.seen.get("line", ""))

    def test_socks5_handshake_reaches_target(self):
        def handler(conn, seen):
            head = conn.recv(2)
            conn.recv(head[1])                      # 客户端支持的方法列表
            conn.sendall(b"\x05\x00")
            req = conn.recv(4)                      # VER CMD RSV ATYP
            if req[3] == 3:
                n = conn.recv(1)[0]
                seen["host"] = conn.recv(n).decode()
            elif req[3] == 1:
                seen["host"] = socket.inet_ntoa(conn.recv(4))
            seen["port"] = struct.unpack(">H", conn.recv(2))[0]
            conn.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0")
                         + struct.pack(">H", 0))
            data = conn.recv(4096)                  # 隧道通了之后回显
            conn.sendall(b"ECHO:" + data)

        stub = _StubServer(handler)
        try:
            cfg = {"host": "127.0.0.1", "port": stub.port, "user": "",
                   "password": "", "rdns": True}
            sock = m._socks5_open(cfg, "example.com", 443, 5)
            try:
                sock.sendall(b"hi")
                got = sock.recv(64)
            finally:
                sock.close()
        finally:
            stub.close()
        self.assertEqual(got, b"ECHO:hi")
        self.assertEqual(stub.seen.get("host"), "example.com")   # socks5h：远端解析
        self.assertEqual(stub.seen.get("port"), 443)

    def test_socks5_username_password_auth(self):
        def handler(conn, seen):
            head = conn.recv(2)
            conn.recv(head[1])
            conn.sendall(b"\x05\x02")               # 要求用户名密码
            ver = conn.recv(1)
            ulen = conn.recv(1)[0]
            seen["user"] = conn.recv(ulen).decode()
            plen = conn.recv(1)[0]
            seen["pwd"] = conn.recv(plen).decode()
            conn.sendall(b"\x01\x00")
            conn.recv(4)
            conn.recv(1)                            # 域名长度
            conn.recv(64)
            conn.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0")
                         + struct.pack(">H", 0))

        stub = _StubServer(handler)
        try:
            cfg = {"host": "127.0.0.1", "port": stub.port, "user": "u1",
                   "password": "p1", "rdns": True}
            sock = m._socks5_open(cfg, "a.example", 443, 5)
            sock.close()
        finally:
            stub.close()
        self.assertEqual((stub.seen.get("user"), stub.seen.get("pwd")), ("u1", "p1"))

    def test_local_hosts_bypass_proxy(self):
        h = m._BypassProxyHandler({"https": "http://127.0.0.1:1"})
        h.proxy_open(m.urllib.request.Request("http://127.0.0.1:9333/json"), "http://p:1", "http")
        # 只有本机被放过：远端请求会被改写 host 指到代理
        rq = m.urllib.request.Request("https://chatgpt.com/x")
        m._BypassProxyHandler({"https": "http://127.0.0.1:1"}).proxy_open(rq, "http://127.0.0.1:1", "https")
        self.assertEqual(rq.host, "127.0.0.1:1")

    # ---- 探活与出口判定 ----
    def test_dead_proxy_detected_even_if_port_accepts(self):
        """端口开着、CONNECT 也回 200，但隧道立刻断——这种代理必须判死。"""
        def handler(conn, seen):
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        stub = _StubServer(handler)
        try:
            ok, why = m._proxy_alive("http://127.0.0.1:%d" % stub.port, timeout=5)
        finally:
            stub.close()
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_check_egress_verdicts(self):
        real = m._http_get_text
        try:
            m._http_get_text = lambda url, proxy, timeout=10: (200, "1.2.3.4")
            self.assertEqual(m.check_egress("http://p:1")["verdict"],
                             "代理未生效：代理出口与直连出口是同一个 IP")

            m._http_get_text = lambda url, proxy, timeout=10: (200, "1.2.3.4" if proxy else "5.6.7.8")
            res = m.check_egress("http://p:1")
            self.assertFalse(res["same"])
            self.assertEqual(res["verdict"], "代理生效")

            def boom(url, proxy, timeout=10):
                if proxy:
                    raise OSError("connection refused")
                return 200, "5.6.7.8"
            m._http_get_text = boom
            self.assertEqual(m.check_egress("http://p:1")["verdict"][:5], "代理不可用")
        finally:
            m._http_get_text = real


    # ---- A26：探活必须验目标站，不能只量 IP ----
    def _probe_with(self, status, body):
        class _Resp(object):
            def __init__(self, st, b):
                self.status = st
                self._b = b

            def read(self, n=None):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Opener(object):
            def open(self, rq, timeout=None):
                if status >= 400:
                    raise urllib.error.HTTPError(rq.full_url, status, "x", {}, io.BytesIO(body))
                return _Resp(status, body)

        real = m.build_opener_for
        m.build_opener_for = lambda *a, **k: _Opener()
        try:
            return m.probe_site("http://127.0.0.1:1", timeout=1)
        finally:
            m.build_opener_for = real

    def test_probe_site_ok(self):
        res = self._probe_with(200, b"<html>ok</html>")
        self.assertEqual(res["verdict"], "ok")
        self.assertTrue(res["ok"])

    def test_probe_site_blocked_by_vpn(self):
        """实测事故页型：IP 通、站点 403「Unable to load site」→ 必须判死。"""
        res = self._probe_with(403, b"<p>Unable to load site. If you are using a VPN, try turning it off.</p>")
        self.assertEqual(res["verdict"], "blocked_vpn")
        self.assertFalse(res["ok"])

    def test_probe_site_challenge_is_usable(self):
        """CF 挑战页说明出口没被封，浏览器能过 → 不算失败。"""
        res = self._probe_with(403, b"<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>")
        self.assertEqual(res["verdict"], "challenge")
        self.assertTrue(res["ok"])

    def test_probe_site_other_status_not_ok(self):
        res = self._probe_with(500, b"boom")
        self.assertEqual(res["verdict"], "http_500")
        self.assertFalse(res["ok"])

    # ---- A23：WebRTC 泄漏判定（纯函数）----
    def test_webrtc_no_candidates_is_clean(self):
        self.assertEqual(m._classify_webrtc([])["status"], "clean")

    def test_webrtc_private_only(self):
        self.assertEqual(m._classify_webrtc(["10.0.0.3", "192.168.1.9"])["status"], "private_only")

    def test_webrtc_via_proxy(self):
        self.assertEqual(m._classify_webrtc(["1.2.3.4"], "1.2.3.4")["status"], "via_proxy")

    def test_webrtc_leak_detected(self):
        res = m._classify_webrtc(["1.2.3.4"], "5.6.7.8")
        self.assertEqual(res["status"], "leaked")
        self.assertIn("泄漏", res["error"])

    def test_webrtc_public_without_expected_ip(self):
        self.assertEqual(m._classify_webrtc(["1.2.3.4"], None)["status"], "public_unknown")

    # ---- A22：锁持有者身份校验 ----
    def _write_lock(self, path, payload):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_lock_live_holder_not_reclaimed(self):
        """进程还活着但租约过期：不得回收（旧逻辑会按 mtime 直接删）。"""
        lock = os.path.join(self.tmp, "a.json.lock")
        self._write_lock(lock, {"pid": os.getpid(),
                                "process_start_time": m._proc_start_time(os.getpid()),
                                "created_at": time.time() - 999})
        self.assertFalse(m._lock_holder_stale(lock))

    def test_lock_dead_holder_reclaimed(self):
        lock = os.path.join(self.tmp, "b.json.lock")
        self._write_lock(lock, {"pid": 999999, "process_start_time": 1,
                                "created_at": time.time() - 999})
        self.assertTrue(m._lock_holder_stale(lock))

    def test_lock_pid_reused_reclaimed(self):
        """PID 被复用（存活但启动时间不符）→ 视为残留。"""
        lock = os.path.join(self.tmp, "c.json.lock")
        real = m._proc_start_time(os.getpid())
        self._write_lock(lock, {"pid": os.getpid(),
                                "process_start_time": (real or 0) + 12345,
                                "created_at": time.time() - 999})
        self.assertTrue(m._lock_holder_stale(lock))

    def test_lock_unknown_identity_kept(self):
        """进程身份读不到 → 宁可等超时也不误删活锁。"""
        lock = os.path.join(self.tmp, "d.json.lock")
        real_state, real_start = m._proc_state, m._proc_start_time
        try:
            m._proc_state = lambda pid: m.PROC_ALIVE
            m._proc_start_time = lambda pid: None
            self._write_lock(lock, {"pid": 4242, "process_start_time": 7,
                                    "created_at": time.time() - 999})
            self.assertFalse(m._lock_holder_stale(lock))
        finally:
            m._proc_state, m._proc_start_time = real_state, real_start

    def test_lock_legacy_format_uses_mtime(self):
        """老格式（无 pid）退化为 mtime 判据，保持兼容。"""
        lock = os.path.join(self.tmp, "e.json.lock")
        with open(lock, "w", encoding="utf-8") as f:
            f.write("")
        os.utime(lock, (time.time() - 999, time.time() - 999))
        self.assertTrue(m._lock_holder_stale(lock))

    def test_lock_records_holder_identity(self):
        """加锁时必须把 pid/启动时间写进锁文件（供下一次回收判定）。"""
        path = os.path.join(self.tmp, "f.json")
        with m._file_lock(path):
            data = json.load(open(path + ".lock", encoding="utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        self.assertIsNotNone(data["process_start_time"])

    # ---- F-08 补充：非法 UTF-8 不得穿透为异常 ----
    def test_lock_invalid_utf8_not_reclaimed(self):
        """锁文件含非法 UTF-8 字节：读取失败 → 不回收，不得抛 UnicodeDecodeError。"""
        lock = os.path.join(self.tmp, "bad-utf8.lock")
        with open(lock, "wb") as f:
            f.write(b"\xff\xfe\x00garbage\x80\x81")
        os.utime(lock, (time.time() - 999, time.time() - 999))
        self.assertFalse(m._lock_holder_stale(lock))  # 不抛异常、不回收

    # ---- F-09：锁身份写入失败/短写不得进临界区 ----
    def test_lock_write_failure_does_not_enter_critical_section(self):
        """os.write 抛 OSError：必须抛 LockTimeout，临界区不执行，锁文件被清理。"""
        path = os.path.join(self.tmp, "f09a.json")
        entered = []
        real_write = m.os.write

        def boom(fd, data):
            raise OSError("模拟磁盘写失败")
        try:
            m.os.write = boom
            with self.assertRaises(m.LockTimeout):
                with m._file_lock(path, strict=True):
                    entered.append(1)
        finally:
            m.os.write = real_write
        self.assertEqual(entered, [], "写入失败绝不能进临界区")
        self.assertFalse(os.path.exists(path + ".lock"), "半成品锁必须清理")

    def test_lock_short_write_does_not_enter_critical_section(self):
        """os.write 短写（返回字节数不足）：必须抛 LockTimeout，不得当成功。"""
        path = os.path.join(self.tmp, "f09b.json")
        entered = []
        real_write = m.os.write
        state = {"first": True}

        def short(fd, data):
            if state["first"]:
                state["first"] = False
                return max(1, len(data) // 3)   # 只写 1/3
            raise OSError("模拟后续写失败")
        try:
            m.os.write = short
            with self.assertRaises(m.LockTimeout):
                with m._file_lock(path, strict=True):
                    entered.append(1)
        finally:
            m.os.write = real_write
        self.assertEqual(entered, [], "短写不得进临界区")

    def test_lock_write_failure_non_strict_also_blocks(self):
        """非 strict 模式下写入失败同样不得进临界区（身份缺失 ≠ 锁竞争超时）。"""
        path = os.path.join(self.tmp, "f09c.json")
        entered = []
        real_write = m.os.write

        def boom(fd, data):
            raise OSError("模拟写失败")
        try:
            m.os.write = boom
            with self.assertRaises(m.LockTimeout):
                with m._file_lock(path, strict=False):
                    entered.append(1)
        finally:
            m.os.write = real_write
        self.assertEqual(entered, [], "非 strict 也不能带着空锁进临界区")

    # ---- F-10：process_start_time 损坏值不得当 pid_reused 证据 ----
    def test_lock_corrupt_start_time_not_reclaimed(self):
        """存储的 process_start_time 是垃圾串/±inf/负数：不得当 pid_reused 证据回收。"""
        for bad in ("corrupted", True, False, [1], {"x": 1}, float("nan"),
                    float("inf"), float("-inf"), -1, -0.5):
            lock = os.path.join(self.tmp, "f10-%s.lock" % str(bad)[:8])
            self._write_lock(lock, {"pid": os.getpid(),
                                    "process_start_time": bad,
                                    "created_at": time.time() - 999})
            with self.subTest(value=bad):
                self.assertFalse(m._lock_holder_stale(lock),
                                 "损坏 start_time=%r 不得触发回收" % (bad,))

    def test_lock_valid_start_time_mismatch_is_reclaimed(self):
        """合法数字但确实不等（PID 复用）→ 可回收（不能矫枉过正）。"""
        lock = os.path.join(self.tmp, "f10-valid.lock")
        real = m._proc_start_time(os.getpid())
        self._write_lock(lock, {"pid": os.getpid(),
                                "process_start_time": (real or 0) + 99999,
                                "created_at": time.time() - 999})
        self.assertTrue(m._lock_holder_stale(lock))

    def test_parse_start_time_contract(self):
        """_parse_start_time 直接契约测试：None/合法/损坏三类。"""
        # None → 旧格式兼容
        ok, v = m._parse_start_time(None)
        self.assertTrue(ok)
        self.assertIsNone(v)
        # 合法 int / float → 原值返回（不转 float，防 FILETIME 精度丢失）
        for good in (12345, 12345.678, 0, 0.0):
            ok, v = m._parse_start_time(good)
            self.assertTrue(ok, "合法值 %r 应通过" % (good,))
            self.assertEqual(v, good, "必须原值返回，不得 float() 归一化")
        # 损坏 → False（含 F-10 第二十一轮补：±inf / 负数）
        for bad in (True, False, "12345", [1], {"a": 1}, float("nan"),
                    float("inf"), float("-inf"), -1, -0.5):
            ok, v = m._parse_start_time(bad)
            self.assertFalse(ok, "损坏值 %r 应判无效" % (bad,))
            self.assertIsNone(v)

    def test_proxy_check_requires_site_ok(self):
        """--proxy-check 的 ok 必须同时满足「代理生效」与「目标站可用」。"""
        real_eg, real_site, real_res = m.check_egress, m.probe_site, m.resolve_proxy_info
        real_argv = sys.argv
        try:
            m.resolve_proxy_info = lambda refresh=False: {"url": "http://p:1", "source": "test"}
            m.check_egress = lambda url=None, timeout=10: {
                "proxy": url, "proxy_ip": "1.2.3.4", "direct_ip": "5.6.7.8",
                "same": False, "verdict": "代理生效", "errors": []}
            m.probe_site = lambda url=None, timeout=25: {
                "verdict": "blocked_vpn", "ok": False, "note": "turn it off", "status": 403}
            sys.argv = ["chatgpt_send.py", "--proxy-check"]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main()
            self.assertEqual(rc, 3)   # 出口不满足 = 未满足联网条件，不是业务失败
            self.assertIn("目标站不可用", buf.getvalue())
        finally:
            m.check_egress, m.probe_site, m.resolve_proxy_info = real_eg, real_site, real_res
            sys.argv = real_argv


class TestMalformedResponse(TmpBase):
    """F-12：解析器对「JSON 合法但形状不符」的响应体必须优雅降级，绝不抛异常。

    事故场景：reply_digest 崩在截断回复的恢复路径上 → save_reply 永不执行 →
    归档（唯一副本）彻底丢失。解析失败降级是这些函数既有的策略，形状不符
    必须同等对待。
    """

    BAD_BODIES = [
        ("顶层是数组", "[]"),
        ("顶层是 null", "null"),
        ("顶层是数字", "123"),
        ("顶层是字符串", '"x"'),
        ("mapping 是列表", json.dumps({"mapping": []})),
        ("mapping 是字符串", json.dumps({"mapping": "x"})),
        ("node 是字符串", json.dumps({"mapping": {"n1": "notadict"}})),
        ("author 是字符串",
         json.dumps({"mapping": {"n1": {"message": {"author": "root",
                                                    "content": {"parts": ["x"]}}}}})),
        ("content 是字符串",
         json.dumps({"mapping": {"n1": {"message": {"author": {"role": "assistant"},
                                                    "content": "text"}}}})),
        ("create_time 类型混",
         json.dumps({"mapping": {
             "a": {"message": {"author": {"role": "assistant"},
                                "content": {"parts": ["x"]},
                                "create_time": "9", "id": "z"}},
             "b": {"message": {"author": {"role": "assistant"},
                                "content": {"parts": ["y"]},
                                "create_time": 5, "id": "a"}}}})),
    ]

    BAD_SSE = [
        ("data 是数字", "data: 123"),
        ("data 是数组", "data: []"),
        ("data 是字符串", 'data: "x"'),
        ("data 是 null", "data: null"),
        ("message 是字符串", 'data: {"message": "s"}'),
        ("content 是字符串",
         'data: {"message": {"author": {"role": "assistant"}, "content": "t"}}'),
        ("author 是字符串",
         'data: {"message": {"author": "root", "content": {"parts": ["x"]}}}'),
    ]

    CONV = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def test_malformed_bodies_never_raise(self):
        """所有解析器 × 所有畸形形状：不得抛任何异常。"""
        parsers = [("reply_digest", m.reply_digest),
                   ("harvest_mapping", m.ChatGPTAPI.harvest_mapping),
                   ("harvest_transcript", m.ChatGPTAPI.harvest_transcript),
                   ("tail_from_mapping", m.ChatGPTAPI.tail_from_mapping)]
        for pname, fn in parsers:
            for bname, body in self.BAD_BODIES:
                with self.subTest(parser=pname, body=bname):
                    try:
                        fn(body)
                    except Exception as e:
                        self.fail("%s 遇到 %s 抛 %s: %s" % (pname, bname, type(e).__name__, e))

    def test_malformed_sse_never_raises(self):
        """SSE 收割：畸形 data 行跳过即可，不得让整次收割崩掉。"""
        for bname, line in self.BAD_SSE:
            with self.subTest(line=bname):
                try:
                    m.ChatGPTAPI.extract_reply_sse(line)
                except Exception as e:
                    self.fail("extract_reply_sse 遇到 %s 抛 %s: %s"
                              % (bname, type(e).__name__, e))

    def test_malformed_sse_skipped_but_good_line_kept(self):
        """坏行跳过后，好行仍要收得到（降级不能变成「全丢」）。"""
        raw = ("data: 123\n"
               "data: {\"message\": \"s\"}\n"
               "data: {\"message\": {\"author\": {\"role\": \"assistant\"}, "
               "\"content\": {\"parts\": [\"真正的回复\"]}}}\n")
        self.assertEqual(m.ChatGPTAPI.extract_reply_sse(raw), "真正的回复")

    def test_reply_digest_malformed_returns_none(self):
        """对账器遇到畸形体：一律返回 None（核不了），不得抛异常。

        第二十六轮审查口径：对账器是**完整性证明器**，不是内容提取器——
        跳过一个坏 node 后拿剩余 part 算摘要，等于把缺失的 part 藏进一个
        「看起来正常」的摘要里。因此任何畸形都整体判不可用。
        """
        for bname, body in self.BAD_BODIES:
            with self.subTest(body=bname):
                self.assertIsNone(m.reply_digest(body),
                                  "畸形体 %r 必须整体判不可用" % (bname,))

    def test_shape_helper_contract(self):
        """_as_dict / _as_seq / _as_ctime 直接契约。"""
        self.assertEqual(m._as_dict({"a": 1}), {"a": 1})
        for bad in (None, [], "x", 1, 1.5, True):
            self.assertEqual(m._as_dict(bad), {}, "_as_dict(%r) 应返回 {}" % (bad,))
        self.assertEqual(m._as_seq([1, 2]), [1, 2])
        for bad in (None, {}, "x", 1):
            self.assertEqual(m._as_seq(bad), [], "_as_seq(%r) 应返回 []" % (bad,))
        # _as_ctime 返回 (ok, float)：ok=False = 时间值不可用，记录无效
        self.assertEqual(m._as_ctime(5), (True, 5.0))
        self.assertEqual(m._as_ctime(5.5), (True, 5.5))
        self.assertEqual(m._as_ctime(None), (True, 0.0))       # 缺失 = 最旧，内容照收
        self.assertEqual(m._as_ctime("9"), (False, 0.0))       # 类型不对 = 不可用
        self.assertEqual(m._as_ctime(True), (False, 0.0))      # bool 不算时间戳

    def test_conv_owner_tolerates_non_dict_state_file(self):
        """会话归属文件内容不是 dict 时：不得抛异常（返回 None）。"""
        path = os.path.join(self.tmp, "conv-accounts.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]")
        real = m.CONV_MAP_FILE
        try:
            m.CONV_MAP_FILE = path
            self.assertIsNone(m.conv_owner("whatever"))
        finally:
            m.CONV_MAP_FILE = real

    # ---- 第二十六轮审查口径：跳过坏 node ≠ 证明完整 ----
    def test_mapping_checked_reports_incomplete(self):
        """收割必须能识别「不完整」——坏 node 不能被静默当成完整 mapping。"""
        for bname, body in self.BAD_BODIES:
            with self.subTest(body=bname):
                _text, complete = m.ChatGPTAPI.harvest_mapping_checked(body)
                self.assertFalse(complete, "畸形体 %r 必须标记为不完整" % (bname,))

    def test_transcript_checked_reports_incomplete(self):
        """匿名留存也必须能识别不完整（否则「尽力而为」冒充完整对话）。"""
        for bname, body in self.BAD_BODIES:
            with self.subTest(body=bname):
                _rows, complete = m.ChatGPTAPI.harvest_transcript_checked(body)
                self.assertFalse(complete, "畸形体 %r 必须标记为不完整" % (bname,))

    def test_mapping_checked_complete_when_clean(self):
        """干净的 mapping：complete=True（不能把降级做成永远报警）。"""
        body = json.dumps({"mapping": {
            "a": {"message": {"author": {"role": "user"},
                               "content": {"parts": ["q"]},
                               "create_time": 1.0}},
            "b": {"message": {"author": {"role": "assistant"},
                               "content": {"parts": ["a"]},
                               "create_time": 2.0}},
        }})
        text, complete = m.ChatGPTAPI.harvest_mapping_checked(body)
        self.assertTrue(complete)
        self.assertEqual(text, "a")

    def test_sse_checked_reports_bad_events_and_done(self):
        """SSE 必须记录坏事件数与是否 [DONE] 收尾——部分流不得冒充完整回复。"""
        clean = ('data: {"message": {"author": {"role": "assistant"}, '
                 '"content": {"parts": ["ok"]}}}\ndata: [DONE]\n')
        text, complete, detail = m.ChatGPTAPI.extract_reply_sse_checked(clean)
        self.assertEqual(text, "ok")
        self.assertTrue(complete, "干净流应判完整")
        self.assertIn("[DONE]", detail)

        bad = 'data: 123\ndata: [DONE]\n'
        text, complete, detail = m.ChatGPTAPI.extract_reply_sse_checked(bad)
        self.assertFalse(complete, "含坏事件必须判不完整")
        self.assertIn("坏事件 1", detail)

        no_done = ('data: {"message": {"author": {"role": "assistant"}, '
                   '"content": {"parts": ["ok"]}}}\n')
        text, complete, detail = m.ChatGPTAPI.extract_reply_sse_checked(no_done)
        self.assertEqual(text, "ok", "内容照收")
        self.assertFalse(complete, "未见 [DONE] 必须判不完整")
        self.assertIn("未见 [DONE]", detail)

    def test_tail_from_mapping_returns_none_when_malformed(self):
        """尾部确认：mapping 有畸形就整体返回 None——不把不完整的尾部当真实尾部。"""
        for bname, body in self.BAD_BODIES:
            with self.subTest(body=bname):
                self.assertIsNone(m.ChatGPTAPI.tail_from_mapping(body),
                                  "畸形体 %r 的尾部不可确认" % (bname,))

    def test_archiving_not_blocked_by_digest_failure(self):
        """辅助对账器不得成为归档的前置硬依赖：对账失败/崩溃都仍要落盘。

        事故场景：对账器抛异常 → 控制流跳出 send_prompt → save_reply 永不执行
        → 归档（唯一副本）彻底丢失。
        """
        path = os.path.join(self.tmp, "f12-block.json")
        saved = []
        CONV = self.CONV

        class Stub:
            is_anon = False
            account = "acct1"
            last_truncated = True
            last_status = 200
            last_body = ""

            def live_tail(self, conv_id, tries=2):
                return ("p1", "{}")

            def cached_tail(self, conv_id):
                return "p1"

            def resolve_thinking_model(self, api):
                return "auto"

            def requirements(self):
                return "tok", "proof"

            def send(self, conv_id, text, tail, req_token, proof, model=None, timeout=420):
                sse = ('data: {"conversation_id": "%s"}\n'
                       'data: {"message": {"author": {"role": "assistant"}, '
                       '"content": {"parts": ["半截回复"]}}}\n' % CONV)
                return 200, sse

            def req(self, method, url, body=None, timeout=60):
                return 200, "null"          # 畸形 GET body → 对账器核不了

            def extract_reply_sse_checked(self, raw):
                return m.ChatGPTAPI.extract_reply_sse_checked(raw)

            def harvest_mapping_checked(self, raw):
                return m.ChatGPTAPI.harvest_mapping_checked(raw)

            def save_reply(self, conv_id, pfile, text):
                saved.append((conv_id, text))
                return "<path>"

        prompt = os.path.join(self.tmp, "p12.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("提示词\n")
        st, _p, conv = m.send_prompt(Stub(), CONV, prompt)
        self.assertEqual(st, 200)
        self.assertTrue(saved, "对账器核不了也必须落盘——归档是唯一副本")
        self.assertEqual(saved[0][0], CONV)


class TestMalformedParts(TmpBase):
    """F-12a/b/c：内容元素形状不符与时间戳不可用的处置。

    事故场景：`"".join(p for p in parts if isinstance(p, str))` 把 parts 里的
    非字符串元素**静默丢掉**——内容凭空消失，而完整性却报告为 True；
    SSE 里缺 author / author 不是 dict 的消息事件也不进坏事件计数，
    于是残缺流被当成完整回复落盘；时间戳侧，NaN/±inf 与超出 float64
    精确整数范围的大整数都会让「顺序」变成编出来的。
    """

    CONV = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def _body(self, parts):
        return json.dumps({"mapping": {"n1": {"message": {
            "author": {"role": "assistant"},
            "content": {"parts": parts},
            "create_time": 9}}}})

    def test_non_string_part_makes_mapping_incomplete(self):
        rows, complete = m._scan_mapping(self._body(["ok", 42]))
        self.assertFalse(complete, "parts 里有非字符串 → mapping 必须报不完整")
        self.assertEqual(rows, [], "内容不完整的节点不得当有效记录收下")

    def test_non_string_part_makes_reply_digest_none(self):
        self.assertIsNone(m.reply_digest(self._body(["ok", 42])),
                          "完整性证明器遇到畸形必须整体判不可用，不得拿剩余 part 算摘要")

    def test_non_string_part_marks_sse_bad_event(self):
        raw = ('data: {"message": {"author": {"role": "assistant"}, '
               '"content": {"parts": ["ok", 42]}}}\ndata: [DONE]')
        text, complete, detail = m._scan_sse(raw)
        self.assertFalse(complete, "坏 part 的事件必须计入坏事件")
        self.assertIn("坏事件 1 个", detail)
        self.assertEqual(text, "", "坏事件不得产出内容")

    def test_sse_missing_author_counts_as_bad_event(self):
        raw = 'data: {"message": {"content": {"parts": ["hi"]}}}\ndata: [DONE]'
        _t, complete, detail = m._scan_sse(raw)
        self.assertFalse(complete, "缺 author 的消息事件是畸形，不能静默跳过")
        self.assertIn("坏事件 1 个", detail)

    def test_sse_non_dict_author_counts_as_bad_event(self):
        raw = ('data: {"message": {"author": "assistant", '
               '"content": {"parts": ["hi"]}}}\ndata: [DONE]')
        _t, complete, detail = m._scan_sse(raw)
        self.assertFalse(complete, "author 不是 dict 的消息事件是畸形")
        self.assertIn("坏事件 1 个", detail)

    def test_sse_event_without_message_is_not_bad(self):
        """合法非消息事件（如只带 conversation_id）不算畸形，否则正常流会被误报残缺。"""
        raw = 'data: {"conversation_id": "x"}\ndata: [DONE]'
        _t, complete, _d = m._scan_sse(raw)
        self.assertTrue(complete, "没有 message 的事件是正常的")

    def test_as_ctime_rejects_non_finite_and_huge(self):
        for bad in (float("nan"), float("inf"), float("-inf"), 10 ** 20, -(10 ** 20)):
            with self.subTest(value=bad):
                ok, val = m._as_ctime(bad)
                self.assertFalse(ok, "非有限/超精度时间戳必须判不可用，不得进排序")
                self.assertEqual(val, 0.0)

    def test_as_ctime_accepts_normal_values(self):
        self.assertEqual(m._as_ctime(None), (True, 0.0))
        self.assertEqual(m._as_ctime(5), (True, 5.0))
        self.assertEqual(m._as_ctime(1.5), (True, 1.5))


class TestParserEdges(TmpBase):
    """F-15/F-16/F-19：解析边界——显式 null、DONE 终点、多模态 part。

    F-15：`obj.get("message")` 分不清「没有 message 字段」与「显式
    `message: null`」。后者是畸形事件，却按正常非消息事件放过去了——
    于是 `complete=True` 里混进了畸形事件。
    F-16：`[DONE]` 是解析终点；其后的数据不能参与落点证明。
    F-19：多模态 part（`{"content_type": ...}` 对象）**合法但不可文本化**——
    把它当畸形会把正常的用户多模态消息误报成「assistant 回复残缺」；
    当成普通字符串拼进去又会让内容凭空消失。必须分开认。
    """

    CONV = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    IMG = {"content_type": "image_asset_pointer", "asset_pointer": "file-service://x"}

    def test_explicit_null_message_is_bad_event(self):
        raw = 'data: {"message": null}\ndata: [DONE]'
        _t, complete, detail = m._scan_sse(raw)
        self.assertFalse(complete, "显式 message: null 是畸形事件，不能放行")
        self.assertIn("坏事件 1 个", detail)

    def test_explicit_non_dict_message_is_bad_event(self):
        raw = 'data: {"message": "hi"}\ndata: [DONE]'
        _t, complete, detail = m._scan_sse(raw)
        self.assertFalse(complete)
        self.assertIn("坏事件 1 个", detail)

    def test_absent_message_key_is_not_bad_event(self):
        """合法非消息事件（只有 conversation_id）不算畸形。"""
        raw = 'data: {"conversation_id": "x"}\ndata: [DONE]'
        _t, complete, _d = m._scan_sse(raw)
        self.assertTrue(complete)

    def test_events_after_done_are_ignored(self):
        """F-16：[DONE] 之后的事件不参与身份判断——结束标记之后没有发言权。"""
        raw = ('data: {"conversation_id": "%s"}\n' % self.CONV +
               "data: [DONE]\n" +
               'data: {"conversation_id": "ffffffff-9999-4aaa-8bbb-cccccccccccc"}\n')
        first, uniq = m._sse_conv_ids(raw)
        self.assertEqual(first, self.CONV)
        self.assertEqual(uniq, [self.CONV], "[DONE] 之后的 id 不得进入候选集")

    def test_id_after_done_alone_is_not_accepted(self):
        """只有 [DONE] 之后才有 id → 解析不出 id，必须失败关闭。"""
        raw = ('data: [DONE]\n'
               'data: {"conversation_id": "ffffffff-9999-4aaa-8bbb-cccccccccccc"}\n')
        first, uniq = m._sse_conv_ids(raw)
        self.assertIsNone(first)
        self.assertEqual(uniq, [])

    def test_sse_content_after_done_is_not_harvested(self):
        raw = ('data: {"message": {"author": {"role": "assistant"}, '
               '"content": {"parts": ["DONE 之前的话"]}}}\n'
               "data: [DONE]\n"
               'data: {"message": {"author": {"role": "assistant"}, '
               '"content": {"parts": ["DONE 之后的话"]}}}\n')
        text, complete, _d = m._scan_sse(raw)
        self.assertEqual(text, "DONE 之前的话", "[DONE] 之后的内容不得被采信")
        self.assertNotIn("DONE 之后的话", text)
        self.assertTrue(complete, "见到 [DONE] 且无坏事件")

    def test_structured_user_part_does_not_break_mapping(self):
        """F-19：用户消息含合法多模态 part，不得把 assistant 回复误报成残缺。"""
        body = json.dumps({"mapping": {
            "u": {"message": {"author": {"role": "user"},
                              "content": {"parts": ["看图", self.IMG]},
                              "create_time": 1}},
            "a": {"message": {"id": "msg-1", "author": {"role": "assistant"},
                              "content": {"parts": ["这是回答"]}, "create_time": 5}},
        }})
        rows, complete = m._scan_mapping(body)
        self.assertTrue(complete, "用户多模态消息不是畸形")
        self.assertEqual([r for r in rows if r[1] == "assistant"], [(5.0, "assistant", "这是回答")])

    def test_structured_user_part_does_not_break_digest(self):
        """F-19：用户多模态消息不得让对账器整体判不可用（它核的是 assistant 消息）。"""
        body = json.dumps({"mapping": {
            "u": {"message": {"author": {"role": "user"},
                              "content": {"parts": ["看图", self.IMG]},
                              "create_time": 1}},
            "a": {"message": {"id": "msg-1", "author": {"role": "assistant"},
                              "content": {"parts": ["这是回答"]}, "create_time": 5}},
        }})
        dg = m.reply_digest(body)
        self.assertIsNotNone(dg, "用户多模态消息不影响 assistant 消息的对账")
        self.assertEqual(dg["chars"], 4)

    def test_illegal_part_in_user_message_makes_digest_none(self):
        """D-01（第31轮）：完整性证明器承诺「body 有一处畸形就整体 None」——
        user 节点的非法 part（既非字符串也非结构化对象）也不能被跳过。"""
        body = json.dumps({"mapping": {
            "u": {"message": {"author": {"role": "user"},
                              "content": {"parts": [123]}, "create_time": 1}},
            "a": {"message": {"id": "msg-1", "author": {"role": "assistant"},
                              "content": {"parts": ["正常回复"]}, "create_time": 5}},
        }})
        self.assertIsNone(m.reply_digest(body),
                          "user 非法 part 不得被忽略后仍出具可信摘要")

    def test_structured_assistant_part_makes_digest_none(self):
        """F-19：assistant 消息含非文本内容 → 核不了全文，整体判不可用。"""
        body = json.dumps({"mapping": {
            "a": {"message": {"id": "msg-1", "author": {"role": "assistant"},
                              "content": {"parts": ["文字", self.IMG]},
                              "create_time": 5}},
        }})
        self.assertIsNone(m.reply_digest(body))
        _rows, complete = m._scan_mapping(body)
        self.assertFalse(complete, "assistant 的文本只是投影，不能报完整")

    def test_structured_user_part_in_sse_is_not_bad_event(self):
        raw = ('data: {"message": {"author": {"role": "user"}, '
               '"content": {"parts": ["看图", %s]}}}\n'
               'data: {"message": {"author": {"role": "assistant"}, '
               '"content": {"parts": ["回答"]}}}\ndata: [DONE]' % json.dumps(self.IMG))
        text, complete, detail = m._scan_sse(raw)
        self.assertTrue(complete, "用户多模态事件不是坏事件")
        self.assertEqual(text, "回答")

    def test_illegal_part_still_counts_as_bad_event(self):
        """F-12a 仍然成立：既不是字符串也不是结构化对象的 part 是非法 part。"""
        raw = ('data: {"message": {"author": {"role": "assistant"}, '
               '"content": {"parts": ["ok", 42]}}}\ndata: [DONE]')
        _t, complete, detail = m._scan_sse(raw)
        self.assertFalse(complete)
        self.assertIn("坏事件 1 个", detail)

    def test_atomic_create_exclusive_never_overwrites(self):
        """F-21：O_EXCL 原子独占创建——已存在的文件绝不能被覆盖（TOCTOU 封堵）。"""
        p = os.path.join(self.tmp, "excl.md")
        m._atomic_create_exclusive(p, "第一份证据")
        self.assertEqual(_read(p), "第一份证据")
        with self.assertRaises(FileExistsError):
            m._atomic_create_exclusive(p, "第二份证据——不得覆盖")
        self.assertEqual(_read(p), "第一份证据", "已存在的隔离证据必须原样保留")

    def test_as_ctime_rejects_large_integer_valued_float(self):
        """F-12c（第30轮）：整数值型 float 超 ±2^53−1 必须拒——
        9007199254740992.0 / 1e20 不能被当可靠时间戳参与排序。"""
        for bad in (9007199254740992.0, 1e20, -1e20):
            with self.subTest(v=bad):
                self.assertEqual(m._as_ctime(bad), (False, 0.0))
        # 安全范围内与非整数型 float 仍接受
        self.assertEqual(m._as_ctime(9007199254740991.0)[0], True)
        self.assertEqual(m._as_ctime(1.5), (True, 1.5))


class TestLandingMismatch(TmpBase):
    """A10/F-13：投递目标与服务器实际落点不一致时的处置。

    事故场景：投递 A，服务器实际把回复落在 B。旧版只 WARN 一句，随后
    `save_reply(conv_id or real_conv)` 把落在 B 的回复归档在 A 名下、返回 A + 200
    ——会话身份与回复归属分裂，下游拿着错误的 conversation id 继续操作。
    现在必须 fail-closed：判失败 + 隔离保全 + 携带两个 id + 不静默改归属。
    """

    TARGET = "11111111-2222-4333-8444-555555555555"
    ACTUAL = "99999999-8888-4777-8666-555555555555"

    class _Stub(object):
        is_anon = False
        account = "acct1"
        last_status = 200
        last_body = ""

        def __init__(self, base_dir, sse, saved, gets, truncated=False):
            self.base_dir = base_dir
            self.sse = sse
            self.saved = saved
            self.gets = gets
            self.last_truncated = truncated

        def live_tail(self, conv_id, tries=2):
            return ("p1", "{}")

        def cached_tail(self, conv_id):
            return "p1"

        def resolve_thinking_model(self, api):
            return "auto"

        def requirements(self):
            return ("tok", "proof")

        def send(self, conv_id, text, tail, req_token, proof, model=None, timeout=420):
            return 200, self.sse

        def req(self, method, url, body=None, timeout=60):
            self.gets.append(url)
            return 200, "{}"

        def extract_reply_sse_checked(self, raw):
            return m.ChatGPTAPI.extract_reply_sse_checked(raw)

        def harvest_mapping_checked(self, raw):
            return m.ChatGPTAPI.harvest_mapping_checked(raw)

        def save_reply(self, conv_id, pfile, text):
            self.saved.append((conv_id, text))
            return "<path>"

    def _prompt(self, body="提示词"):
        p = os.path.join(self.tmp, "p13.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    def _sse(self, conv_id, reply="回复正文"):
        return ('data: {"conversation_id": "%s", "message": {"author": {"role": "assistant"}, '
                '"content": {"parts": ["%s"]}}}\ndata: [DONE]' % (conv_id, reply))

    def _run(self, sse, target=None, truncated=False):
        saved, gets = [], []
        api = self._Stub(os.path.join(self.tmp, "auth"), sse, saved, gets, truncated)
        status, path, conv = m.send_prompt(api, target or self.TARGET,
                                           self._prompt(), thinking=False)
        return status, path, conv, saved, gets

    def test_landing_mismatch_is_failure_not_success(self):
        status, _path, _conv, saved, _gets = self._run(self._sse(self.ACTUAL, "落在 B 的回复"))
        self.assertNotEqual(status, 200, "落点不一致不得判成功")
        self.assertEqual(status, m.LANDING_MISMATCH_STATUS)
        self.assertEqual(saved, [], "不得把落在 B 的回复归档成 A 的正常回复")

    def test_landing_mismatch_quarantines_reply_with_both_ids(self):
        _status, path, _conv, _saved, _gets = self._run(self._sse(self.ACTUAL, "落在 B 的回复"))
        self.assertTrue(path, "必须给出隔离归档路径")
        self.assertTrue(os.path.exists(path), "隔离归档必须真的落盘")
        self.assertIn("quarantine", path.replace("\\", "/"), "隔离件必须与正常归档分开存放")
        content = _read(path)
        self.assertIn(self.TARGET, content)
        self.assertIn(self.ACTUAL, content)
        self.assertIn("落在 B 的回复", content, "回复正文必须被保全，不得丢")
        self.assertIn("landing_mismatch", content)

    def test_quarantine_dir_is_outside_replies_tree(self):
        """隔离目录必须在 `replies/` 之外——下游按 `replies/**` 递归扫描也够不到它，
        不靠文件名 glob 兜底。"""
        _status, path, _conv, _saved, _gets = self._run(self._sse(self.ACTUAL))
        rel = os.path.relpath(path, os.path.join(self.tmp, "auth")).replace("\\", "/")
        self.assertFalse(rel.startswith("replies/"),
                         "隔离件不得落在 replies/ 树里（会被递归扫描扫到）：%s" % rel)

    def test_quarantine_preserves_full_raw_response(self):
        """F-18：800 字符的头部不足以保全证据——关键 id 可能正好落在截断部分。

        必须把**完整**原始响应另存，并在隔离头部记下它的路径/长度/sha256。
        """
        filler = "x" * 1200
        tail_marker = "TAIL-MARKER-AFTER-800"
        sse = (self._sse(self.ACTUAL, "回复") + "\n" +
               "data: {\"note\": \"%s\", \"marker\": \"%s\"}\n" % (filler, tail_marker))
        _status, path, _conv, _saved, _gets = self._run(sse)
        content = _read(path)
        self.assertIn("resp_path", content)
        self.assertIn("resp_sha256", content)
        self.assertIn("resp_chars", content)
        # 头部记录的 sha256/长度必须对得上**完整**响应
        head = json.loads(content.split("```json")[1].split("```")[0])
        self.assertEqual(head["resp_chars"], len(sse))
        self.assertEqual(head["resp_sha256"],
                         __import__("hashlib").sha256(sse.encode("utf-8")).hexdigest())
        # 完整响应单独存一份，尾部标记必须在里面
        resp_file = os.path.join(os.path.join(self.tmp, "auth"),
                                 head["resp_path"].replace("/", os.sep))
        self.assertTrue(os.path.exists(resp_file), "完整原始响应必须单独落盘")
        raw = _read(resp_file)
        self.assertIn(tail_marker, raw, "截断部分之后的证据必须保全")
        self.assertEqual(len(raw), len(sse))

    def test_quarantine_same_second_never_overwrites(self):
        """F-21：秒级时间戳不够唯一——同秒同对会话的两次不一致投递
        不得互相覆盖隔离证据（R7「归档不覆盖」）。"""
        _s1, p1, _c1, _saved1, _g1 = self._run(self._sse(self.ACTUAL, "第一次回复"))
        _s2, p2, _c2, _saved2, _g2 = self._run(self._sse(self.ACTUAL, "第二次回复"))
        self.assertNotEqual(p1, p2, "隔离档案必须各有唯一名，不得覆盖")
        self.assertIn("第一次回复", _read(p1))
        self.assertIn("第二次回复", _read(p2))

    def test_pending_claim_registry_records_actual_conversation(self):
        """F-13：服务器实际落点 ≠ 已确认归属——记**待认领登记**，不自动登记归属。"""
        _status, path, _conv, _saved, _gets = self._run(self._sse(self.ACTUAL))
        reg = os.path.join(os.path.join(self.tmp, "auth"), m.PENDING_CLAIMS_FILE)
        self.assertTrue(os.path.exists(reg), "必须留一份待认领登记")
        rows = [json.loads(l) for l in _read(reg).splitlines() if l.strip()]
        self.assertTrue(rows)
        last = rows[-1]
        self.assertEqual(last["target_conv_id"], self.TARGET)
        self.assertEqual(last["actual_conv_id"], self.ACTUAL)
        self.assertEqual(last["quarantine_path"],
                         os.path.relpath(path, os.path.join(self.tmp, "auth")).replace("\\", "/"))
        self.assertEqual(last["claim_status"], "pending_human_review")

    def test_ambiguous_ids_failure_carries_candidates(self):
        """F-17：id 歧义时失败结果必须带 target 与候选 id，不能把诊断信息丢掉。"""
        sse = ('data: {"conversation_id": "%s"}\n' % self.TARGET +
               'data: {"conversation_id": "%s"}\n' % self.ACTUAL + "data: [DONE]")
        status, path, conv, saved, _gets = self._run(sse)
        self.assertEqual(status, 1)
        self.assertIsNone(path)
        self.assertEqual(saved, [])
        self.assertEqual(conv.target_conv_id, self.TARGET)
        self.assertEqual(sorted(conv.candidate_ids), sorted([self.TARGET, self.ACTUAL]))

    def test_landing_mismatch_return_carries_both_ids(self):
        _status, _path, conv, _saved, _gets = self._run(self._sse(self.ACTUAL))
        self.assertTrue(getattr(conv, "mismatch", False), "失败返回必须标记落点不一致")
        self.assertEqual(conv.target_conv_id, self.TARGET)
        self.assertEqual(conv.actual_conv_id, self.ACTUAL)
        self.assertEqual(str(conv), self.TARGET, "默认呈现目标 id")

    def test_same_conversation_is_success(self):
        status, path, conv, saved, _gets = self._run(self._sse(self.TARGET))
        self.assertEqual(status, 200)
        self.assertEqual(conv, self.TARGET)
        self.assertEqual(len(saved), 1, "正常路径照旧归档")
        self.assertEqual(saved[0][0], self.TARGET)

    def test_first_event_id_wins_and_repeated_id_is_fine(self):
        """同一个 id 在多个事件里重复出现是正常的——必须取**首个**事件的 id。"""
        sse = ("data: {\"conversation_id\": \"%s\"}\n" % self.TARGET +
               "data: {\"conversation_id\": \"%s\", \"message\": {\"author\": {\"role\": \"assistant\"}, "
               "\"content\": {\"parts\": [\"ok\"]}}}\n" % self.TARGET + "data: [DONE]")
        status, _path, conv, _saved, _gets = self._run(sse)
        self.assertEqual(status, 200)
        self.assertEqual(conv, self.TARGET)

    def test_multiple_different_ids_fail_closed(self):
        """同一响应里出现两个不同 id → 无法确认落点，失败关闭，不得落盘。"""
        sse = ("data: {\"conversation_id\": \"%s\"}\n" % self.TARGET +
               "data: {\"conversation_id\": \"%s\"}\n" % self.ACTUAL + "data: [DONE]")
        status, path, _conv, saved, _gets = self._run(sse)
        self.assertEqual(status, 1, "多个 id 必须判失败")
        self.assertIsNone(path)
        self.assertEqual(saved, [])

    def test_truncated_get_targets_actual_conversation(self):
        """落点不一致时 GET 必须拉服务器实际写入的会话——拉目标会话拿到的是别的内容。"""
        _status, _path, _conv, _saved, gets = self._run(
            self._sse(self.ACTUAL, "半截"), truncated=True)
        self.assertTrue(gets, "截断必须走 GET 补全文")
        self.assertIn(self.ACTUAL, gets[0])
        self.assertNotIn(self.TARGET, gets[0])

    def test_id_after_done_is_not_used_by_regex_fallback(self):
        """F-13/F-16 反例：先 [DONE]、后写 B——旧版正文正则兜底会捞到 B 当落点。

        落点证明只信 SSE 结构化事件里首个带 id 的事件；解不出就失败关闭，
        绝不扫整包正文（那等于「响应里出现过什么 id 就用哪个」）。
        """
        sse = ('data: {"event":"start"}\ndata: [DONE]\n'
               'data: {"conversation_id": "%s"}\n' % self.ACTUAL)
        status, path, conv, saved, gets = self._run(sse)
        self.assertNotEqual(status, 200, "不得接受 [DONE] 之后的 id 当落点")
        self.assertIsNone(path)
        self.assertEqual(saved, [])
        self.assertEqual(getattr(conv, "candidate_ids", []), [])

    def test_quarantine_body_reflects_resp_save_failure(self):
        """N-01/F-18：响应保全失败时，隔离档案不得仍声称『完整原始响应已保全』。"""
        orig = m._atomic_create_exclusive
        def fail_resp(path, text):
            if path.endswith(".resp.txt"):
                raise OSError("simulated disk full")
            return orig(path, text)
        try:
            m._atomic_create_exclusive = fail_resp
            _status, path, conv, _saved, _gets = self._run(self._sse(self.ACTUAL))
            body = _read(path)
            self.assertIn("保全失败", body, "必须如实记录响应保全失败")
            self.assertNotIn("完整原始响应已保全", body, "失败的保全不得声称成功")
            self.assertIs(conv.evidence_complete, False)
        finally:
            m._atomic_create_exclusive = orig

    def test_evidence_complete_false_when_claim_fails(self):
        """F-25：待认领登记失败必须进入机器可识别的失败状态，不能报告完整隔离保全。"""
        orig = m._record_pending_claim
        try:
            m._record_pending_claim = lambda base, rec: False
            _status, path, conv, _saved, _gets = self._run(self._sse(self.ACTUAL))
            self.assertIsNotNone(path, "隔离档案本身仍要落盘")
            self.assertIs(getattr(conv, "evidence_complete", None), False,
                          "登记失败必须让 evidence_complete=False")
        finally:
            m._record_pending_claim = orig

    def test_evidence_complete_false_when_resp_save_fails(self):
        """F-18：完整原始响应保全失败同样必须让 evidence_complete=False。"""
        orig = m._atomic_create_exclusive
        def fail_resp(path, text):
            if path.endswith(".resp.txt"):
                raise OSError("simulated resp save failure")
            return orig(path, text)
        try:
            m._atomic_create_exclusive = fail_resp
            _status, path, conv, _saved, _gets = self._run(self._sse(self.ACTUAL))
            self.assertIsNotNone(path)
            self.assertIs(getattr(conv, "evidence_complete", None), False)
        finally:
            m._atomic_create_exclusive = orig

    def test_incomplete_sse_is_labelled_not_silent(self):
        """F-23：SSE 无 [DONE] 且没触发 IncompleteRead——旧版直接正常归档。

        修复后：内容照收（保住唯一副本），但归档文本必须自带「完整性未证明」
        声明，且 ConvRef.reply_complete=False。
        """
        sse = ('data: {"conversation_id": "%s"}\n' % self.TARGET +
               'data: {"message": {"author": {"role": "assistant"}, '
               '"content": {"parts": ["半截回复"]}}}\n')   # 没有 [DONE]
        status, _path, conv, saved, gets = self._run(sse)
        self.assertEqual(status, 200, "归档不被阻断（保住唯一副本）")
        self.assertTrue(gets, "不完整必须触发 GET 补全尝试")
        self.assertIs(getattr(conv, "reply_complete", None), False)
        self.assertTrue(saved and "完整性未证明" in saved[0][1],
                        "归档文本必须自带不完整声明，不得冒充定稿")

    def test_whitespace_padded_full_reply_is_proven_complete(self):
        """F-26：GET 补全拉回带首尾空白的完整回复，仍须证明完整、不误标「完整性未证明」。

        端到端钉住 F-24 门禁：旧版 digest（未 strip）与 full（strip）口径不一致，
        GET 回来的完整回复只要首尾带空白就被判 digest_ok=False → reply_complete=False
        → 归档件错误地挂上「完整性未证明」。这里断言 reply_complete=True 且归档正文
        不含该声明。
        """
        class WsGetStub(self._Stub):
            def req(self, method, url, body=None, timeout=60):
                self.gets.append(url)
                gbody = json.dumps({"mapping": {"n": {"message": {
                    "id": "m-ws", "author": {"role": "assistant"},
                    "content": {"parts": ["\n这是完整回复的正文  \n"]},
                    "create_time": 9}}}})
                return 200, gbody

        saved, gets = [], []
        api = WsGetStub(os.path.join(self.tmp, "auth"), self._plain_incomplete(),
                        saved, gets, False)
        status, path, conv = m.send_prompt(api, self.TARGET, self._prompt(), thinking=False)
        self.assertEqual(status, 200)
        self.assertTrue(gets, "不完整 SSE 必须触发 GET 补全")
        self.assertIs(getattr(conv, "reply_complete", None), True,
                      "带首尾空白的完整回复仍须被证明完整（F-26）")
        self.assertTrue(saved, "必须落盘归档")
        self.assertNotIn("完整性未证明", saved[0][1],
                         "完整回复不得被误标不完整")

    def test_completeness_requires_get_evidence_not_selfratio(self):
        """F-24：digest 必须真正参与放行——GET 补全失败时不得标 reply_complete=True。"""
        class NoGetStub(self._Stub):
            def req(self, method, url, body=None, timeout=60):
                return 404, "{}"      # GET 补全失败
        saved, gets = [], []
        api = NoGetStub(os.path.join(self.tmp, "auth"), self._plain_incomplete(),
                        saved, gets, False)
        status, _path, conv = m.send_prompt(api, self.TARGET, self._prompt(), thinking=False)
        self.assertIs(getattr(conv, "reply_complete", None), False,
                      "GET 补全失败时不能证明完整")

    def _plain_incomplete(self):
        return ('data: {"conversation_id": "%s"}\n' % self.TARGET +
                'data: {"message": {"author": {"role": "assistant"}, '
                '"content": {"parts": ["半截"]}}}\n')


class TestSlotsAndLease(TmpBase):
    """B8 原子领槽 + B15 租约回收的回归（审查要求的实现证据）。"""

    def _now(self):
        return time.time()

    def _hold_lock(self, path):
        """造一把「活人持有」的锁：当前 pid 存活、created_at 新鲜，因此不可回收。"""
        lock = path + ".lock"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(lock, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(),
                                "process_start_time": m._proc_start_time(os.getpid()),
                                "created_at": time.time(), "thread": 0}))
        return lock

    def test_strict_lock_raises_instead_of_skipping(self):
        # 事故：超时后打印 WARN 并「跳过加锁继续跑」，导致 6 进程抢槽全部拿到槽位。
        target = os.path.join(self.tmp, "slots", ".slots")
        self._hold_lock(target)
        with self.assertRaises(m.LockTimeout):
            with m._file_lock(target, timeout=0.3, strict=True):
                self.fail("严格模式下不该进入临界区")

    def test_acquire_slot_fails_closed_on_lock_timeout(self):
        self._hold_lock(os.path.join(self.tmp, "slots", ".slots"))
        slot, _rid = m.acquire_slot("race", base=self.tmp, lock_timeout=0.3)
        self.assertIsNone(slot)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "slots", "slot-1.slot")),
                         "抢不到锁却占了槽位")

    def test_fresh_empty_slot_file_is_not_reclaimed(self):
        # 别人刚 os.open 还没写完时读到空 json，旧逻辑会把它当死槽删掉 → 同一槽位发两张票
        d = os.path.join(self.tmp, "slots")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "slot-1.slot"), "w", encoding="utf-8"):
            pass
        slot, _rid = m.acquire_slot("race", base=self.tmp)
        self.assertTrue(slot and slot.endswith("slot-2.slot"), slot)
        self.assertTrue(os.path.exists(os.path.join(d, "slot-1.slot")), "活槽被误删")

    def test_partial_slot_json_is_not_quarantined(self):
        # 事故根因：读的人用 _load_json，把「别人正在写」的半截 json 当损坏隔离改名，
        # 文件名一空出来，第二个进程就能占同一个槽位 → 同一槽位发两张票。
        d = os.path.join(self.tmp, "slots")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "slot-1.slot"), "w", encoding="utf-8") as f:
            f.write('{"task_id": "race", "run_id"')      # 半截
        slot, _rid = m.acquire_slot("race", base=self.tmp)
        self.assertTrue(slot and slot.endswith("slot-2.slot"), slot)
        self.assertTrue(os.path.exists(os.path.join(d, "slot-1.slot")), "半截槽位被改名/删掉")
        self.assertEqual([f for f in os.listdir(d) if ".corrupt-" in f], [])

    def test_lease_reclaim_refuses_unreadable_identity(self):
        path = os.path.join(self.tmp, "lease-unreadable.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json at all")
        ok, why = m.lease_reclaim(path)
        self.assertFalse(ok)
        self.assertEqual(why, "identity_unreadable")
        self.assertTrue(os.path.exists(path), "身份读不懂却把租约删了")

    def test_lease_acquire_refuses_unreadable_lease(self):
        path = os.path.join(self.tmp, "lease-busy.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        self.assertIsNotNone(m.lease_acquire(path, "t1"))   # 空文件=无租约，可领
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertIsNone(m.lease_acquire(path, "t2"))      # 读不懂=当有人在跑

    def test_lease_ops_never_run_unlocked(self):
        """B15：租约类操作抢不到锁必须拒绝，不允许「无锁进临界区」。"""
        path = os.path.join(self.tmp, "lease.json")
        self._hold_lock(path)
        self.assertIsNone(m.lease_acquire(path, "t1", lock_timeout=0.3))
        ok, why = m.lease_reclaim(path, lock_timeout=0.3)
        self.assertFalse(ok)
        self.assertEqual(why, "lock_timeout")
        self.assertFalse(m.lease_heartbeat(path, "rid", lock_timeout=0.3))
        self.assertFalse(m.lease_release(path, "rid", lock_timeout=0.3))

    def test_unreadable_nonempty_slot_is_never_auto_reclaimed(self):
        """身份不明 ≠ 死槽。旧版「非空但读不懂 + 超过 5s」就删，等于拿文件年龄冒充进程死亡。"""
        d = os.path.join(self.tmp, "slots")
        os.makedirs(d, exist_ok=True)
        sp = os.path.join(d, "slot-1.slot")
        with open(sp, "w", encoding="utf-8") as f:
            f.write("{broken json")
        old = time.time() - 3600
        os.utime(sp, (old, old))
        slot, _rid = m.acquire_slot("race", base=self.tmp, lock_timeout=1.0)
        self.assertTrue(slot and slot.endswith("slot-2.slot"), slot)
        self.assertTrue(os.path.exists(sp), "身份不明的槽位被自动删除了")

    def test_empty_slot_older_than_ttl_is_reclaimed(self):
        d = os.path.join(self.tmp, "slots")
        os.makedirs(d, exist_ok=True)
        sp = os.path.join(d, "slot-1.slot")
        open(sp, "w", encoding="utf-8").close()
        old = time.time() - 3600
        os.utime(sp, (old, old))
        slot, _rid = m.acquire_slot("race", base=self.tmp, lock_timeout=1.0)
        self.assertTrue(slot and slot.endswith("slot-1.slot"), slot)

    def test_corrupt_lease_needs_approval_to_release(self):
        """B15：损坏租约只能经「独立批准标记」隔离，不能自动删、也不能凭口头放行。"""
        path = os.path.join(self.tmp, "lease-c.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        approval = os.path.join(self.tmp, "approve.json")
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok)
        self.assertEqual(why, "no_approval")
        self.assertTrue(os.path.exists(path), "没有批准就动了租约")

        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": "deadbeef"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok)
        self.assertEqual(why, "approval_incomplete", "缺批准人/原因/时间就是口头放行")

        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": "deadbeef", "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "test"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok)
        self.assertEqual(why, "sha256_mismatch")
        self.assertTrue(os.path.exists(path))

        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "holder confirmed dead",
                       "identity_evidence": "process snapshot + B7 state check #4512"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertTrue(ok, why)
        self.assertEqual(why, "quarantined")
        self.assertFalse(os.path.exists(path))
        left = [f for f in os.listdir(self.tmp) if ".corrupt-" in f]
        self.assertEqual(len(left), 1, "隔离文件必须保留原字节")

    def test_process_alive_blocks_force_release(self):
        """R3/R4：文件坏了不等于进程死了；持有者还活着就不许恢复。"""
        path = os.path.join(self.tmp, "lease-alive.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(),
                       "process_start_time": m._proc_start_time(os.getpid()),
                       "run_id": "rid"}, f)
        approval = os.path.join(self.tmp, "approve-alive.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "looks dead"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok)
        self.assertEqual(why, "process_alive")
        self.assertTrue(os.path.exists(path))

    def test_corrupt_slot_is_registered(self):
        """损坏槽位不能静默占坑：必须进 corrupt-registry.jsonl，带 sha256 便于事后核对。"""
        d = os.path.join(self.tmp, "slots")
        os.makedirs(d, exist_ok=True)
        sp = os.path.join(d, "slot-1.slot")
        with open(sp, "w", encoding="utf-8") as f:
            f.write("{broken")
        m.acquire_slot("race", base=self.tmp, lock_timeout=1.0)
        reg = os.path.join(self.tmp, "corrupt-registry.jsonl")
        self.assertTrue(os.path.exists(reg), "损坏槽位没有登记")
        row = json.loads(open(reg, encoding="utf-8").read().splitlines()[0])
        self.assertEqual(row["kind"], "slot")
        self.assertEqual(row["why"], "identity_unreadable")
        self.assertEqual(row["sha256"], m._sha256_file(sp))
        self.assertEqual(row["size"], os.path.getsize(sp))

    def test_force_release_refuses_without_lock(self):
        """R2：恢复必须在严格锁内完成；抢不到锁就拒绝，避免 TOCTOU。"""
        path = os.path.join(self.tmp, "lease-toctou.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        approval = os.path.join(self.tmp, "approve-toctou.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "approved"}, f)
        self._hold_lock(path)
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok)
        self.assertEqual(why, "lock_timeout")
        self.assertTrue(os.path.exists(path))

    def test_force_release_needs_identity_evidence_when_unreadable(self):
        """R3：读不出 pid 不等于进程死了；批准人必须给出独立确认依据。"""
        path = os.path.join(self.tmp, "lease-id.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        approval = os.path.join(self.tmp, "approve-id.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "assume dead"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok)
        self.assertEqual(why, "identity_unconfirmed")
        self.assertTrue(os.path.exists(path))

    def test_rejected_recovery_leaves_no_fake_audit_row(self):
        """被拒绝的恢复不许留下「已批准恢复」记录——先登记后检查等于谎报一次恢复。"""
        path = os.path.join(self.tmp, "lease-nofake.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(),
                       "process_start_time": m._proc_start_time(os.getpid()),
                       "run_id": "rid"}, f)
        approval = os.path.join(self.tmp, "approve-nofake.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "looks dead",
                       "identity_evidence": "snapshot"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertFalse(ok)
        self.assertEqual(why, "process_alive")
        reg = os.path.join(self.tmp, "corrupt-registry.jsonl")
        if os.path.exists(reg):
            kinds = [json.loads(l)["kind"] for l in open(reg, encoding="utf-8").read().splitlines()]
            self.assertNotIn("lease-recovery", kinds, "被拒绝的恢复留下了批准记录")

    def test_force_release_blocks_when_audit_registration_fails(self):
        """R1：登记失败必须阻断恢复——没有审计记录的「恢复成功」比不恢复更危险。"""
        path = os.path.join(self.tmp, "lease-audit.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        approval = os.path.join(self.tmp, "approve-audit.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "dead",
                       "identity_evidence": "ps snapshot"}, f)
        real = m._record_corruption
        try:
            m._record_corruption = lambda *a, **k: None
            ok, why = m.lease_force_release(path, approval)
        finally:
            m._record_corruption = real
        self.assertFalse(ok)
        self.assertEqual(why, "audit_registration_failed")
        self.assertTrue(os.path.exists(path))

    def test_force_release_reason_is_stable_enum(self):
        """F-01：reason 必须是稳定枚举，不许把异常文本拼进去。

        事故：`"quarantine_failed:%s" % e`、`"unreadable:%s" % e` 让调用方无法用
        等值比较分流，而且换个环境错误消息一变，判定就跟着变——对外契约被实现细节污染。
        这里把所有拒绝路径跑一遍，断言每个返回值都在 FORCE_RELEASE_REASONS 内且不含 ':'。
        """
        seen = []

        def broken_path(tag):
            p = os.path.join(self.tmp, "lease-%s.json" % tag)
            with open(p, "w", encoding="utf-8") as f:
                f.write("{broken")
            return p

        def approve(tag, payload):
            a = os.path.join(self.tmp, "appr-%s.json" % tag)
            with open(a, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            return a

        def full(sha, **extra):
            d = {"sha256": sha, "approved_at": "2026-11-19T16:00:00+08:00",
                 "approver": "owner", "reason": "test"}
            d.update(extra)
            return d

        def check(tag, path, approval):
            ok, why = m.lease_force_release(path, approval)
            seen.append(why)
            self.assertIn(why, m.FORCE_RELEASE_REASONS, "%s 返回了枚举外的 reason" % tag)
            self.assertNotIn(":", why, "%s 把异常文本拼进了 reason" % tag)
            return ok, why

        # absent
        self.assertEqual(check("absent", os.path.join(self.tmp, "none.json"),
                               os.path.join(self.tmp, "none-appr.json"))[1], "absent")
        # no_approval：批准文件不存在
        p = broken_path("noappr")
        self.assertEqual(check("noappr", p, os.path.join(self.tmp, "ghost.json"))[1],
                         "no_approval")
        # approval_incomplete：只有 sha256，没有批准人/时间/原因
        p = broken_path("partial")
        self.assertEqual(check("partial", p,
                               approve("partial", {"sha256": "deadbeef"}))[1],
                         "approval_incomplete")
        # sha256_mismatch
        p = broken_path("wrong")
        self.assertEqual(check("wrong", p, approve("wrong", full("0" * 64)))[1],
                         "sha256_mismatch")
        # identity_unconfirmed：读不出 pid 且没有独立依据
        p = broken_path("noid")
        self.assertEqual(check("noid", p, approve("noid", full(m._sha256_file(p))))[1],
                         "identity_unconfirmed")
        # process_alive：持有者还活着，哪怕批准齐全也不许恢复
        p = os.path.join(self.tmp, "lease-alive.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(),
                       "process_start_time": m._proc_start_time(os.getpid()),
                       "run_id": "rid"}, f)
        self.assertEqual(check("alive", p,
                               approve("alive", full(m._sha256_file(p))))[1],
                         "process_alive")
        # lock_timeout：抢不到锁（批准内容无关，锁先挡住）
        p = broken_path("lock")
        held = self._hold_lock(p)
        try:
            self.assertEqual(check("lock", p, approve("lock", full("x")))[1],
                             "lock_timeout")
        finally:
            os.remove(held)
        self.assertIn("absent", seen)
        self.assertIn("no_approval", seen)
        self.assertIn("approval_incomplete", seen)
        self.assertIn("sha256_mismatch", seen)
        self.assertIn("identity_unconfirmed", seen)
        self.assertIn("process_alive", seen)
        self.assertIn("lock_timeout", seen)

    def test_force_release_unreadable_hash_is_stable(self):
        """F-01：哈希读不出来时返回 "unreadable"（稳定值），细节只进日志。"""
        path = os.path.join(self.tmp, "lease-unreadable.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        approval = os.path.join(self.tmp, "approve-unreadable.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": "deadbeef",
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "test"}, f)
        real = m._sha256_file

        def boom(_p):
            raise OSError("模拟读失败 WinError 32")
        try:
            m._sha256_file = boom
            ok, why = m.lease_force_release(path, approval)
        finally:
            m._sha256_file = real
        self.assertFalse(ok)
        self.assertEqual(why, "unreadable")
        self.assertTrue(os.path.exists(path), "哈希读不出来时不许动租约")

    def test_quarantine_failure_is_stable(self):
        """F-01：隔离改名失败返回 "quarantine_failed"（不带异常文本），租约保持原样。"""
        path = os.path.join(self.tmp, "lease-qfail.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        approval = os.path.join(self.tmp, "approve-qfail.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "dead",
                       "identity_evidence": "ps snapshot"}, f)
        real = m._rename_no_overwrite

        def boom(src, dst):
            raise OSError("模拟改名失败 WinError 5")
        try:
            m._rename_no_overwrite = boom
            ok, why = m.lease_force_release(path, approval)
        finally:
            m._rename_no_overwrite = real
        self.assertFalse(ok)
        self.assertEqual(why, "quarantine_failed")
        self.assertTrue(os.path.exists(path), "改名失败却动了租约")
        reg = os.path.join(self.tmp, "corrupt-registry.jsonl")
        rows = [json.loads(l) for l in
                open(reg, encoding="utf-8").read().splitlines() if l.strip()]
        kinds = [r["kind"] for r in rows]
        self.assertIn("lease-recovery", kinds)
        self.assertEqual(kinds[-1], "lease-recovery-failed",
                         "批准了但没隔离成，必须补一条失败行，不能只留 approved_recovery")
        self.assertEqual(rows[-1]["why"], "quarantine_failed")

    def test_quarantine_failed_row_write_failure_is_survivable(self):
        """隔离失败的补记行自己写不进去时：仍返回 quarantine_failed，租约原样留着。

        第十四轮审查残余风险 3：`_record_corruption("lease-recovery-failed", ...)` 自身
        失败的行为没被证明过。这里让第一次登记成功、第二次返回 None（模拟磁盘写失败），
        断言不抛异常、返回值仍是稳定枚举、原文件没被搬走。
        """
        path = os.path.join(self.tmp, "lease-rows.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        approval = os.path.join(self.tmp, "approve-rows.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "dead",
                       "identity_evidence": "ps snapshot"}, f)
        real_record, real_rename = m._record_corruption, m._rename_no_overwrite
        calls = []

        def record_once(kind, p, why):
            calls.append(kind)
            if len(calls) == 1:
                return real_record(kind, p, why)
            return None          # 第二次（失败行）写不进去

        def boom(src, dst):
            raise OSError("模拟改名失败 WinError 5")
        try:
            m._record_corruption = record_once
            m._rename_no_overwrite = boom
            ok, why = m.lease_force_release(path, approval)
        finally:
            m._record_corruption, m._rename_no_overwrite = real_record, real_rename
        self.assertFalse(ok)
        self.assertEqual(why, "quarantine_failed")
        self.assertEqual(calls, ["lease-recovery", "lease-recovery-failed"])
        self.assertTrue(os.path.exists(path), "补记行写失败不该把租约搞丢")

    # ---- F-10：_lease_reclaimable 的 process_start_time 损坏值不得当 pid_reused ----
    def test_lease_reclaimable_corrupt_start_time_not_reclaimed(self):
        """存储的 process_start_time 是垃圾值/±inf/负数：返回 identity_unreadable，禁止回收。"""
        for bad in ("corrupted", True, False, [1], {"x": 1}, float("nan"),
                    float("inf"), float("-inf"), -1, -0.5):
            info = {"run_id": "r1", "pid": os.getpid(),
                    "lease_until": 0, "process_start_time": bad}
            with self.subTest(value=bad):
                ok, why = m._lease_reclaimable(info, now=self._now())
                self.assertFalse(ok, "损坏 start_time=%r 不得回收" % (bad,))
                self.assertEqual(why, "identity_unreadable")

    def test_lease_reclaimable_valid_start_time_mismatch_is_pid_reused(self):
        """合法数字但确实不等（PID 复用）→ pid_reused（不能矫枉过正）。"""
        real = m._proc_start_time(os.getpid())
        info = {"run_id": "r1", "pid": os.getpid(),
                "lease_until": 0, "process_start_time": (real or 0) + 99999}
        ok, why = m._lease_reclaimable(info, now=self._now())
        self.assertTrue(ok)
        self.assertEqual(why, "pid_reused")

    def test_lease_reclaimable_none_start_time_alive(self):
        """process_start_time=None（旧格式）且进程存活 → alive，不回收。"""
        info = {"run_id": "r1", "pid": os.getpid(),
                "lease_until": 0, "process_start_time": None}
        ok, why = m._lease_reclaimable(info, now=self._now())
        self.assertFalse(ok)
        self.assertEqual(why, "alive")

    # ---- F-11：隔离归档不覆盖已有归档 ----
    def test_force_release_preserves_existing_archive(self):
        """目标归档已存在时：旧归档内容不得被覆盖，新损坏租约落到唯一名。"""
        path = os.path.join(self.tmp, "lease-f11.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken-lease")
        # 预建同秒归档（模拟同一秒二次恢复/预存文件）
        ts = time.strftime("%Y%m%d-%H%M%S")
        existing = "%s.corrupt-%s" % (path, ts)
        with open(existing, "w", encoding="utf-8") as f:
            f.write("PRECIOUS-OLD-ARCHIVE")
        approval = os.path.join(self.tmp, "approve-f11.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "dead",
                       "identity_evidence": "ps snapshot"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertTrue(ok, "归档冲突时应自动找唯一名，不该失败")
        self.assertEqual(why, "quarantined")
        # 旧归档内容不变
        with open(existing, encoding="utf-8") as f:
            self.assertEqual(f.read(), "PRECIOUS-OLD-ARCHIVE",
                             "R7：旧归档不得被覆盖")
        # 新归档落到不同名字
        found = [fn for fn in os.listdir(self.tmp)
                 if fn.startswith(os.path.basename(path) + ".corrupt-")
                 and fn != os.path.basename(existing)]
        self.assertTrue(found, "新损坏租约必须归档到唯一名")
        # 原路径空出
        self.assertFalse(os.path.exists(path))

    def test_force_release_archive_conflict_many(self):
        """多个同名归档冲突时逐个追加序号，绝不覆盖。"""
        path = os.path.join(self.tmp, "lease-f11b.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken-b")
        ts = time.strftime("%Y%m%d-%H%M%S")
        for i in range(3):
            p = "%s.corrupt-%s" % (path, ts) if i == 0 else "%s.corrupt-%s.%d" % (path, ts, i)
            with open(p, "w", encoding="utf-8") as f:
                f.write("OLD-%d" % i)
        approval = os.path.join(self.tmp, "approve-f11b.json")
        with open(approval, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(path),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "dead",
                       "identity_evidence": "ps snapshot"}, f)
        ok, why = m.lease_force_release(path, approval)
        self.assertTrue(ok)
        self.assertEqual(why, "quarantined")
        # 三个旧归档都完好
        for i in range(3):
            p = "%s.corrupt-%s" % (path, ts) if i == 0 else "%s.corrupt-%s.%d" % (path, ts, i)
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), "OLD-%d" % i, "旧归档 %d 被覆盖了" % i)

    def test_rename_no_overwrite_conflict_raises(self):
        """_rename_no_overwrite 基础契约：目标存在时抛 FileExistsError。"""
        src = os.path.join(self.tmp, "rn-src.txt")
        dst = os.path.join(self.tmp, "rn-dst.txt")
        with open(src, "w") as f:
            f.write("new")
        with open(dst, "w") as f:
            f.write("old")
        with self.assertRaises(FileExistsError):
            m._rename_no_overwrite(src, dst)
        with open(dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old", "目标不得被覆盖")
        with open(src, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new", "源不得被搬走")

    def test_rejected_recovery_leaves_no_audit_row_for_hash_and_identity(self):
        """被 sha256/身份依据拒绝的恢复，同样不许留下「已批准恢复」的假记录。"""
        cases = []

        def broken(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("{broken")

        # (1) 哈希不匹配
        p1 = os.path.join(self.tmp, "lease-h1.json")
        broken(p1)
        a1 = os.path.join(self.tmp, "a-h1.json")
        with open(a1, "w", encoding="utf-8") as f:
            json.dump({"sha256": "0" * 64,
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "test",
                       "identity_evidence": "snapshot"}, f)
        cases.append((p1, a1, "sha256_mismatch"))

        # (2) 身份读不出来且没有独立依据
        p2 = os.path.join(self.tmp, "lease-h2.json")
        broken(p2)
        a2 = os.path.join(self.tmp, "a-h2.json")
        with open(a2, "w", encoding="utf-8") as f:
            json.dump({"sha256": m._sha256_file(p2),
                       "approved_at": "2026-11-19T16:00:00+08:00",
                       "approver": "owner", "reason": "assume dead"}, f)
        cases.append((p2, a2, "identity_unconfirmed"))

        for p, a, want in cases:
            ok, why = m.lease_force_release(p, a)
            self.assertFalse(ok, want)
            self.assertEqual(why, want)
            self.assertTrue(os.path.exists(p), "%s：被拒绝却动了租约" % want)
        reg = os.path.join(self.tmp, "corrupt-registry.jsonl")
        if os.path.exists(reg):
            kinds = [json.loads(l)["kind"]
                     for l in open(reg, encoding="utf-8").read().splitlines() if l.strip()]
            self.assertNotIn("lease-recovery", kinds, "被拒绝的恢复留下了批准记录")

    def test_heartbeat_and_release_timeout_keep_lease_bytes(self):
        """B15：抢不到锁时，心跳/释放都必须什么都不做——文件与字节都不许变。"""
        path = os.path.join(self.tmp, "lease-keep.json")
        m._atomic_write(path, json.dumps({
            "task_id": "t", "run_id": "rid", "pid": os.getpid(),
            "process_start_time": m._proc_start_time(os.getpid()),
            "started_at": time.time(), "heartbeat_at": time.time(),
            "lease_until": time.time() + 60}, ensure_ascii=False, indent=2))
        before = open(path, "rb").read()
        self._hold_lock(path)
        self.assertFalse(m.lease_heartbeat(path, "rid", lock_timeout=0.2))
        self.assertFalse(m.lease_release(path, "rid", lock_timeout=0.2))
        self.assertTrue(os.path.exists(path), "lease file removed by timeout path")
        self.assertEqual(open(path, "rb").read(), before, "lease content changed")

    def test_lease_conflict_logs_reason(self):
        """审计要能分辨「有人在跑」与「抢不到锁」。"""
        path = os.path.join(self.tmp, "lease-log.json")
        m._atomic_write(path, json.dumps({
            "task_id": "t", "run_id": "other", "pid": os.getpid(),
            "process_start_time": m._proc_start_time(os.getpid()),
            "lease_until": time.time() + 60}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertIsNone(m.lease_acquire(path, "t2"))
        self.assertIn("租约冲突", buf.getvalue())

    def test_lease_valid_blocks_reclaim(self):
        ok, why = m._lease_reclaimable({
            "run_id": "r1", "lease_until": self._now() + 60,
            "pid": os.getpid(), "process_start_time": m._proc_start_time(os.getpid())})
        self.assertFalse(ok)
        self.assertEqual(why, "lease_valid")

    def test_lease_expired_process_gone(self):
        ok, why = m._lease_reclaimable({
            "run_id": "r1", "lease_until": self._now() - 1,
            "pid": 999999, "process_start_time": 1})
        self.assertTrue(ok)
        self.assertEqual(why, "process_gone")

    def test_lease_pid_reused(self):
        real_start = m._proc_start_time(os.getpid())
        ok, why = m._lease_reclaimable({
            "run_id": "r1", "lease_until": self._now() - 1,
            "pid": os.getpid(), "process_start_time": (real_start or 0) + 999})
        self.assertTrue(ok)
        self.assertEqual(why, "pid_reused")

    def test_lease_identity_unreadable_keeps(self):
        state, start = m._proc_state, m._proc_start_time
        try:
            m._proc_state = lambda pid: m.PROC_ALIVE
            m._proc_start_time = lambda pid: None
            ok, why = m._lease_reclaimable({
                "run_id": "r1", "lease_until": self._now() - 1,
                "pid": 4242, "process_start_time": 7})
        finally:
            m._proc_state, m._proc_start_time = state, start
        self.assertFalse(ok)
        self.assertEqual(why, "identity_unreadable")

    def test_lease_run_id_mismatch_refused(self):
        ok, why = m._lease_reclaimable({
            "run_id": "r1", "lease_until": self._now() - 1, "pid": 999999},
            expect_run_id="r2")
        self.assertFalse(ok)
        self.assertEqual(why, "run_id_mismatch")

    def test_lease_lifecycle(self):
        p = os.path.join(self.tmp, "lease.json")
        rid = m.lease_acquire(p, "task-A", run_id="run-A", lease_sec=1)
        self.assertEqual(rid, "run-A")
        self.assertIsNone(m.lease_acquire(p, "task-A"), "有效租约在，不得二次领取")
        self.assertFalse(m.lease_heartbeat(p, "run-B"), "只有持有者能续期")
        self.assertTrue(m.lease_heartbeat(p, "run-A"))
        self.assertFalse(m.lease_release(p, "run-B"), "只有持有者能释放")
        self.assertTrue(m.lease_release(p, "run-A"))
        self.assertEqual(m.lease_acquire(p, "task-A", run_id="run-A2"), "run-A2")

    def test_lease_reclaim_removes_file(self):
        p = os.path.join(self.tmp, "lease2.json")
        m._atomic_write(p, json.dumps({"run_id": "x", "lease_until": 1, "pid": 999999}))
        ok, why = m.lease_reclaim(p)
        self.assertTrue(ok)
        self.assertEqual(why, "process_gone")
        self.assertFalse(os.path.exists(p))

    # ---- B15：心跳 / 续期 / 超时回收（时间参数按比例缩小，可重复）----
    def test_lease_run_done(self):
        p = os.path.join(self.tmp, "run1.json")
        val, st = m.lease_run(p, "t", lambda: 42, lease_sec=0.5, heartbeat_sec=0.1, max_sec=5)
        self.assertEqual((val, st), (42, "DONE"))
        self.assertFalse(os.path.exists(p), "结束后必须释放租约")

    def test_heartbeat_keeps_lease_alive(self):
        """运行时长超过租约时，心跳必须续期（否则会被判过期而重复启动）。"""
        p = os.path.join(self.tmp, "run2.json")
        seen = {}

        def body():
            time.sleep(1.2)                     # 远超 lease_sec=0.4
            info = json.load(open(p, encoding="utf-8"))
            seen["alive"], seen["why"] = m._lease_reclaimable(info)
            return "ok"

        val, st = m.lease_run(p, "t", body, lease_sec=0.4, heartbeat_sec=0.1, max_sec=10)
        self.assertEqual(st, "DONE")
        self.assertFalse(seen.get("alive"), "心跳续期后租约不应可回收：%r" % (seen,))

    def test_lease_run_timeout_cancels_body(self):
        """超时后执行体必须真的停了：收到 cancel 就返回，且不再产生副作用。"""
        p = os.path.join(self.tmp, "run3.json")
        hits = {"stopped": False, "side_effects": 0}

        def body(cancel):
            while True:
                if cancel.is_set():
                    hits["stopped"] = True
                    return "stopped"
                hits["side_effects"] += 1     # 模拟副作用
                time.sleep(0.02)

        val, st = m.lease_run(p, "t", body, lease_sec=5, heartbeat_sec=1,
                              max_sec=0.2, grace_sec=2)
        self.assertEqual((val, st), (None, "TIMEOUT"))
        self.assertTrue(hits["stopped"], "超时后必须通知执行体停止")
        self.assertFalse(os.path.exists(p), "执行体已停 → 释放租约")
        n = hits["side_effects"]
        time.sleep(0.2)
        self.assertEqual(hits["side_effects"], n, "返回后不得再有副作用")

    def test_lease_run_timeout_orphan_keeps_lease(self):
        """执行体不响应取消：保留租约，绝不放开第二个实例（双跑比僵尸更危险）。"""
        p = os.path.join(self.tmp, "run3b.json")
        val, st = m.lease_run(p, "t", lambda: time.sleep(1.5), lease_sec=30,
                              heartbeat_sec=1, max_sec=0.2, grace_sec=0.1)
        self.assertEqual((val, st), (None, "TIMEOUT_ORPHAN"))
        self.assertTrue(os.path.exists(p), "执行体仍在跑时必须保留租约")
        other, ost = m.lease_run(p, "t", lambda: 1, lease_sec=30)
        self.assertEqual((other, ost), (None, "LEASE_BUSY"))

    def test_lease_run_busy(self):
        p = os.path.join(self.tmp, "run4.json")
        rid = m.lease_acquire(p, "t", run_id="other", lease_sec=60)
        self.assertEqual(rid, "other")
        val, st = m.lease_run(p, "t", lambda: 1, lease_sec=60)
        self.assertEqual((val, st), (None, "LEASE_BUSY"))

    def test_run_max_defaults(self):
        """超时上限与心跳间隔是常量，不是散落的魔法数。"""
        self.assertEqual(m.HEARTBEAT_SEC, 15)
        self.assertEqual(m.RUN_MAX_SEC, 3600)
        self.assertEqual(m.LEASE_SEC, 60)
        self.assertEqual(m.SLOT_CAP, 3)

    def test_slot_cap_sequential(self):
        base = os.path.join(self.tmp, "slots")
        got = [m.acquire_slot("t%d" % i, base=base) for i in range(4)]
        self.assertTrue(all(g[0] for g in got[:3]))
        self.assertIsNone(got[3][0], "第 4 个必须满员")
        self.assertEqual(len(set(g[0] for g in got[:3])), 3, "槽位必须互异")
        for sp, rid in got[:3]:
            self.assertTrue(m.release_slot(sp, rid))

    def test_slot_cap_under_process_race(self):
        """6 个进程同时抢，有效槽位任何时刻都不得超过 3（B8 的核心断言）。"""
        base = os.path.join(self.tmp, "slots-race")
        script = (
            "import sys, time\n"
            "sys.path.insert(0, %r)\n"
            "import chatgpt_send as C\n"
            "time.sleep(float(sys.argv[1]))\n"
            "sp, rid = C.acquire_slot('race', base=sys.argv[2], lease_sec=300)\n"
            "print(sp or 'FULL')\n"
        ) % (os.path.dirname(os.path.abspath(m.__file__)),)
        procs = [subprocess.Popen([sys.executable, "-c", script, "0.15", base],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(6)]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=120)
            outs.append((out or "").strip())
        # 子进程可能先打印 WARN 再打印结果：只认最后一行，且必须是槽位路径
        got = [o.strip().splitlines()[-1] for o in outs
               if o and o.strip().splitlines() and o.strip().splitlines()[-1].endswith(".slot")]
        self.assertLessEqual(len(got), 3, "超发槽位：%r" % (outs,))
        self.assertEqual(len(set(got)), len(got), "同一槽位被两个进程同时持有")


class TestCustody(TmpBase):
    """A1/A2/A4：交付正文的字节级托管。

    事故背景：正文长期靠「人眼看、手复制」，于是出现两种静默改写——用「最长代码块」
    误取别的段落；以及提取时顺手 strip/Unicode 规范化，把 CRLF 与全角空格改掉。
    两者都不会报错，只会让投递出去的正文与 ChatGPT 自产的正文不是同一份字节。
    """

    def _src(self, body, name="src.md"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(body)
        return path

    def test_between_extracts_exact_bytes_and_offsets(self):
        body = (b"preamble\r\n<<<BEGIN>>>\r\nline1 \r\nline2\t\r\n<<<END>>>\r\ntrailer\r\n")
        src = self._src(body)
        out = os.path.join(self.tmp, "body.md")
        res = m.extract_deliverable(src, begin="<<<BEGIN>>>", end="<<<END>>>", out=out)
        got = open(out, "rb").read()
        # CRLF、行尾空格、制表符都必须原样保留（A1：不得转换换行、不得规范化）
        self.assertEqual(got, b"line1 \r\nline2\t\r\n")
        self.assertEqual(res["bytes"], len(got))
        self.assertEqual(res["sha256"], m._sha256_bytes(got))
        self.assertEqual(body[res["offset_start"]:res["offset_end"]], got)

    def test_only_blank_lines_are_trimmed(self):
        src = self._src(b"<b>\n\n  \n" + "正文  ".encode() + b"\n\n</e>\n")
        out = os.path.join(self.tmp, "o.md")
        m.extract_deliverable(src, begin="<b>", end="</e>", out=out)
        # 首尾空行可裁，但「正文  」的尾随空格属于正文字节，不得裁
        self.assertEqual(open(out, "rb").read(), ("正文  " + "\n").encode())

    def test_byte_exact_matrix(self):
        """A1 唯一口径：删首尾整行空白；首尾非空行的全部原始字节（含行终止符）保留。

        四种边界各断言精确字节：LF、CRLF、末行无换行、尾随空格 + 多空行。
        """
        cases = [
            (b"<b>\nabc\n</e>\n", b"abc\n"),
            (b"<b>\r\nabc\r\n</e>\r\n", b"abc\r\n"),
            (b"<b>\nabc\n\n\n</e>", b"abc\n"),
            (b"<b>\n\n  \nabc  \n \n\n</e>\n", b"abc  \n"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                src = self._src(raw, "case.md")
                out = os.path.join(self.tmp, "case.out")
                m.extract_deliverable(src, begin="<b>", end="</e>", out=out)
                got = open(out, "rb").read()
                self.assertEqual(got, want)
                self.assertEqual(got.hex(), want.hex())

    def test_duplicate_marker_rejected(self):
        src = self._src(b"<b>\na\n</e>\n<b>\nb\n</e>\n")
        with self.assertRaises(m.CustodyError):
            m.extract_deliverable(src, begin="<b>", end="</e>",
                                  out=os.path.join(self.tmp, "x.md"))

    def test_reversed_marker_rejected(self):
        src = self._src(b"</e>\na\n<b>\n")
        with self.assertRaises(m.CustodyError):
            m.extract_deliverable(src, begin="<b>", end="</e>",
                                  out=os.path.join(self.tmp, "x.md"))

    def test_fence_picks_nth_not_longest(self):
        # 最长块是第 1 个（干扰项），必须取第 2 个（A2：不按「最长」定位）
        src = self._src(b"```\n" + b"x" * 400 + b"\n```\n```\nreal\n```\n")
        out = os.path.join(self.tmp, "o.md")
        res = m.extract_deliverable(src, fence=2, out=out)
        self.assertEqual(open(out, "rb").read(), b"real\n")
        self.assertEqual(res["locator"], "fence:2")

    def test_fence_out_of_range_rejected(self):
        src = self._src(b"```\nonly\n```\n")
        with self.assertRaises(m.CustodyError):
            m.extract_deliverable(src, fence=2, out=os.path.join(self.tmp, "x.md"))

    def test_empty_block_rejected(self):
        src = self._src(b"<b>\n\n   \n</e>\n")
        with self.assertRaises(m.CustodyError):
            m.extract_deliverable(src, begin="<b>", end="</e>",
                                  out=os.path.join(self.tmp, "x.md"))

    def test_locator_is_mandatory(self):
        src = self._src(b"<b>\na\n</e>\n")
        with self.assertRaises(m.CustodyError):
            m.extract_deliverable(src, out=os.path.join(self.tmp, "x.md"))

    def test_verify_copy_detects_single_byte_change(self):
        a = self._src(b"same\n", "a.md")
        b = self._src(b"same\n", "b.md")
        self.assertTrue(m.verify_copy(a, b)["equal"])
        with open(b, "wb") as f:
            f.write(b"Same\n")           # 只差一个大小写，也算不一致
        res = m.verify_copy(a, b)
        self.assertFalse(res["equal"])
        self.assertNotEqual(res["src_sha256"], res["dst_sha256"])

    def _cli(self, *argv):
        p = subprocess.run([sys.executable, SCRIPT] + list(argv),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        return p.returncode, (p.stdout or "")

    def test_cli_exit_codes(self):
        good = self._src(b"<b>\nreal\n</e>\n", "good.md")
        dup = self._src(b"<b>\na\n</e>\n<b>\nb\n</e>\n", "dup.md")
        out = os.path.join(self.tmp, "cli-body.md")
        code, so = self._cli("--extract", good, "--between", "<b>", "</e>", "--out", out)
        self.assertEqual(code, 0, so)
        self.assertIn("\"result\": \"ok\"", so)
        self.assertEqual(open(out, "rb").read(), b"real\n")
        code, _ = self._cli("--extract", dup, "--between", "<b>", "</e>", "--out", out)
        self.assertEqual(code, 2)         # 标记不唯一：禁止投递
        code, _ = self._cli("--verify-copy", good, dup)
        self.assertEqual(code, 2)         # 不一致：禁止回灌


class TestScheduleContract(unittest.TestCase):
    """B1/B10/B11：命名即契约、1 小时维度。

    事故背景：审查两次抓到这里——先是用 `^.+ .+ .+$` 放行四段名，
    后用 `startswith("--rotation")` 把 `--rotation-reset` 放进只读白名单。
    两个漏洞的共同点是「人眼正则代替分词与整段比对」。
    """

    def test_readonly_name_ok(self):
        res = m.check_automation_name("配额 盘点（只读）")
        self.assertEqual(res["kind"], "readonly")
        self.assertEqual(res["verb"], "盘点")

    def test_write_name_ok(self):
        for name, verb in (("到期项 处置 执行", "执行"), ("协调器 收敛 处置", "处置")):
            with self.subTest(name=name):
                res = m.check_automation_name(name)
                self.assertEqual(res["kind"], "write")
                self.assertEqual(res["verb"], verb)

    def test_fullwidth_space_counts_as_separator(self):
        # 一个全角空格 = 2 段（只读合法）；两个全角空格 = 3 段，会被判成写入并因动词不认识而拒绝
        res = m.check_automation_name("配额\u3000盘点（只读）")
        self.assertEqual(res["segments"], ["配额", "盘点（只读）"])
        with self.assertRaises(m.ScheduleError):
            m.check_automation_name("配额\u3000盘点\u3000（只读）")

    def test_single_segment_rejected(self):
        with self.assertRaises(m.ScheduleError):
            m.check_automation_name("每小时收敛巡检（协调器值班）")

    def test_four_segments_rejected(self):
        # 贪婪正则 `^.+ .+ .+$` 会放行这个
        with self.assertRaises(m.ScheduleError):
            m.check_automation_name("协调器 收敛 巡检 执行")

    def test_two_segments_without_suffix_rejected(self):
        with self.assertRaises(m.ScheduleError):
            m.check_automation_name("配额 盘点")

    def test_readonly_suffix_with_write_verb_rejected(self):
        with self.assertRaises(m.ScheduleError):
            m.check_automation_name("会话 删除（只读）")

    def test_write_name_with_readonly_verb_rejected(self):
        # 写入名的动作词取第 3 段；把读词表动词放在第 3 段，就是「无后缀 + 读动词」的矛盾
        with self.assertRaises(m.ScheduleError):
            m.check_automation_name("会话 执行 盘点")

    def test_verb_must_match_whole_segment(self):
        # `查看并删除` 不是只读词表里的词，包含匹配会误放行
        with self.assertRaises(m.ScheduleError):
            m.check_automation_name("会话 查看并删除（只读）")

    def test_readonly_argv_whitelist_is_exact(self):
        ok = m.check_automation_name("配额 盘点（只读）", ["--rotation", "--list-all"])
        self.assertEqual(ok["argv"], ["--rotation", "--list-all"])
        for bad in ("--rotation-reset", "--anon-send", "--new", "--proxy-set"):
            with self.subTest(cmd=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_automation_name("配额 盘点（只读）", ["--rotation", bad])

    def test_write_name_may_use_write_argv(self):
        res = m.check_automation_name("到期项 处置 执行", ["--new", "p.md"])
        self.assertEqual(res["kind"], "write")

    def test_hourly_rrule_ok(self):
        self.assertEqual(m.check_schedule("FREQ=HOURLY;INTERVAL=1")["interval"], 1)
        self.assertEqual(m.check_schedule("FREQ=HOURLY")["interval"], 1)

    def test_non_hourly_rrule_rejected(self):
        for bad in ("FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
                    "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0",
                    "FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0",
                    "FREQ=MINUTELY;INTERVAL=5"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_interval_not_one_rejected(self):
        for bad in ("FREQ=HOURLY;INTERVAL=0", "FREQ=HOURLY;INTERVAL=2", "FREQ=HOURLY;INTERVAL=x"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    # ---- F-14：校验语义必须等于下发语义 ----

    def test_duplicate_rrule_part_rejected(self):
        """RFC 5545 §3.3.10：rule part 不得重复。

        旧版用字典解析（同名 key 取最后一个）：`FREQ=DAILY;FREQ=HOURLY` 校验时
        读到 HOURLY 判通过，下发的却是原始串——调度器若取第一个就成了 DAILY，
        「周期固定 1 小时」被确定性绕过。
        """
        for bad in ("FREQ=DAILY;FREQ=HOURLY",
                    "FREQ=HOURLY;INTERVAL=1;INTERVAL=1",
                    "FREQ=HOURLY;INTERVAL=1;INTERVAL=2"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_rrule_output_is_canonical(self):
        """下发必须用已验证字段重新构造的规范化串，不下发用户原始串。"""
        self.assertEqual(m.check_schedule("FREQ=HOURLY;INTERVAL=1")["rrule"],
                         "FREQ=HOURLY;INTERVAL=1")
        self.assertEqual(m.check_schedule("FREQ=HOURLY")["rrule"],
                         "FREQ=HOURLY;INTERVAL=1")

    def test_lowercase_freq_accepted_and_normalised(self):
        """RFC 5545 §2：枚举属性值不区分大小写——`freq=hourly` 不构成违规。

        大小写本身不能当拒绝理由：合法输入被拒 = 校验比 RFC 还严，
        那不是安全，是把用户的合法排程挡在门外。
        """
        res = m.check_schedule("freq=hourly;interval=1")
        self.assertEqual(res["freq"], "HOURLY")
        self.assertEqual(res["interval"], 1)
        self.assertEqual(res["rrule"], "FREQ=HOURLY;INTERVAL=1")

    def test_internal_whitespace_rejected(self):
        """旧版 `.replace(" ", "")` 会把 `FREQ=HO URLY` 悄悄修补成合法输入。"""
        for bad in ("FREQ=HO URLY", "FREQ=HOURLY ;INTERVAL=1", "FREQ=HOUR LY;INTERVAL=1"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_interval_strict_decimal(self):
        """INTERVAL 必须按 RFC 十进制整数解析：`int()` 会接受 '1_0'、' 1' 这类串。"""
        for bad in ("FREQ=HOURLY;INTERVAL=1_0", "FREQ=HOURLY;INTERVAL=0x1",
                    "FREQ=HOURLY;INTERVAL=1.0", "FREQ=HOURLY;INTERVAL=+2",
                    "FREQ=HOURLY;INTERVAL=-1", "FREQ=HOURLY;INTERVAL=１"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_unsupported_rule_part_rejected(self):
        """BYxxx/UNTIL/COUNT 会改变触发语义，不得静默忽略后照样下发。"""
        for bad in ("FREQ=HOURLY;BYHOUR=9", "FREQ=HOURLY;UNTIL=20260101T000000Z",
                    "FREQ=HOURLY;COUNT=5"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_malformed_rule_part_rejected(self):
        """没有 '='、空值、空 rule part 一律拒绝——旧版 `if "=" in chunk` 会整段忽略。"""
        for bad in ("FREQ", "FREQ=", "FREQ=HOURLY;;INTERVAL=1", "FREQ=HOURLY;INTERVAL",
                    ";FREQ=HOURLY"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_rrule_part_regex_rejects_newline_in_value(self):
        """F-22：`_RRULE_PART_RE` 的 `.` 配 re.S 会跨行，`INTERVAL=1\\nX=2`
        曾被当成单个合法 VALUE 放行——换行必须是畸形 rule part。"""
        for bad in ("FREQ=HOURLY;INTERVAL=1\nX=2", "FREQ=HO\r\nHOURLY",
                    "FREQ=HOURLY\n;INTERVAL=1"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_rrule_rejects_leading_trailing_newline(self):
        """F-22（第30轮）：`.strip()` 会吃掉首尾换行——必须在清理前拒绝 CR/LF。"""
        for bad in ("FREQ=HOURLY\n", "\nFREQ=HOURLY", "FREQ=HOURLY\r\n",
                    "\r\nFREQ=HOURLY\r\n"):
            with self.subTest(rrule=bad):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_huge_interval_does_not_escape_as_exception(self):
        """Python 3.11+ 对超长十进制串 int() 会抛 ValueError（位数上限 4300）——
        从 check_schedule 里逃逸出去就是异常逃逸（与 F-07 同型）。"""
        for bad in ("FREQ=HOURLY;INTERVAL=" + "9" * 5000,
                    "FREQ=HOURLY;INTERVAL=" + "9" * 4301,
                    "FREQ=HOURLY;INTERVAL=" + "0" * 5000):
            with self.subTest(length=len(bad)):
                with self.assertRaises(m.ScheduleError):
                    m.check_schedule(bad)

    def test_only_rrule_field_is_executable(self):
        """契约：唯一可执行字段是 `rrule`；原始串只能作为审计留档，不得被误当下发值。"""
        res = m.check_schedule("freq=hourly")
        self.assertEqual(res["rrule"], "FREQ=HOURLY;INTERVAL=1")
        self.assertEqual(res["executable_field"], "rrule")
        self.assertEqual(res["original_input_for_audit_only"], "freq=hourly")
        for k in res:
            if k != "executable_field":
                self.assertNotIn(k, ("rrule_input_echo", "rrule_canonical"),
                                 "不得再留容易被误当下发值的字段名：%s" % k)

    def test_oneshot_within_hour_ok(self):
        from datetime import datetime, timedelta
        at = (datetime.now().astimezone() + timedelta(minutes=30)).isoformat()
        self.assertTrue(m.check_oneshot(at)["lifetime_ok"])
        # Z 后缀等价于 +00:00，也要能过
        utc = (datetime.now().astimezone() + timedelta(minutes=30)).astimezone(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(m.check_oneshot(utc)["lifetime_ok"])

    def test_oneshot_in_the_past_rejected(self):
        from datetime import datetime, timedelta
        at = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()
        with self.assertRaises(m.ScheduleError):
            m.check_oneshot(at)

    def test_oneshot_without_timezone_rejected(self):
        with self.assertRaises(m.ScheduleError):
            m.check_oneshot("2026-11-19T16:00:00")

    def test_oneshot_beyond_one_hour_rejected(self):
        from datetime import datetime, timedelta
        at = (datetime.now().astimezone() + timedelta(hours=3)).isoformat()
        with self.assertRaises(m.ScheduleError):
            m.check_oneshot(at)

    def test_cli_check_name_and_schedule(self):
        p = subprocess.run([sys.executable, SCRIPT, "--check-name", "配额 盘点（只读）",
                            "--argv", "--rotation"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout)
        p = subprocess.run([sys.executable, SCRIPT, "--check-name", "配额 删除（只读）"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        self.assertEqual(p.returncode, 2)
        p = subprocess.run([sys.executable, SCRIPT, "--check-schedule", "FREQ=DAILY;BYHOUR=9"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        self.assertEqual(p.returncode, 2)


class TestEvidenceMatrix(unittest.TestCase):
    """反夸大校验：`baselines.md` 的每条判定口径都必须在矩阵里有且只有一行，
    且引用的用例名/符号必须真实存在。

    这一条测试是给「文档说可判定、代码里没有」这类漂移上闸：只要有人新增条款
    不补证据、或删了用例却留着引用，测试立刻红。
    """

    ROOT = os.path.dirname(HERE)

    def _read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def _clauses(self):
        text = self._read("references", "baselines.md")
        ids = re.findall(r"^\|\s*\*\*([A-E]\d{1,2})\*\*\s*\|", text, re.M)
        ids += re.findall(r"^###\s+([A-E]\d{1,2})\s", text, re.M)
        return ids

    def _rows(self):
        rows = {}
        for line in self._read("references", "evidence-matrix.md").splitlines():
            m = re.match(r"^\|\s*([A-E]\d{1,2})\s*\|(.*)$", line)
            if m:
                rows.setdefault(m.group(1), []).append(m.group(2))
        return rows

    def test_every_clause_has_exactly_one_row(self):
        clauses, rows = self._clauses(), self._rows()
        self.assertTrue(clauses, "没解析到任何条款，解析规则失效")
        missing = sorted(set(clauses) - set(rows))
        extra = sorted(set(rows) - set(clauses))
        dup = sorted(c for c, v in rows.items() if len(v) > 1)
        self.assertEqual(missing, [], "这些条款在证据矩阵里没有对应行")
        self.assertEqual(extra, [], "证据矩阵里有 baselines.md 不存在的条款")
        self.assertEqual(dup, [], "同一术语出现多行（重复计数）")

    def test_citations_resolve(self):
        tests_src = open(os.path.join(HERE, "test_hardening.py"), encoding="utf-8").read()
        test_names = set(re.findall(r"def (test_\w+)\s*\(", tests_src))
        scripts = ""
        for fn in os.listdir(os.path.join(self.ROOT, "scripts")):
            if fn.endswith(".py"):
                scripts += open(os.path.join(self.ROOT, "scripts", fn), encoding="utf-8").read()
        symbols = set(re.findall(r"^\s*(?:def|class)\s+([A-Za-z_]\w*)", scripts, re.M))
        symbols |= set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=", scripts, re.M))
        for clause, cells in self._rows().items():
            for tok in re.findall(r"(test|code|live|human):([^\s|`]+)", " ".join(cells)):
                kind, value = tok
                with self.subTest(clause=clause, kind=kind, value=value):
                    if kind == "test":
                        self.assertIn(value, test_names, "%s 引用了不存在的用例 %s" % (clause, value))
                    elif kind == "code":
                        self.assertIn(value.split(".")[-1], symbols,
                                      "%s 引用了不存在的符号 %s" % (clause, value))
                    elif kind == "live":
                        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")
                        self.assertLessEqual(value, time.strftime("%Y-%m-%d"),
                                             "%s 的实测日期在未来" % clause)
                    else:
                        self.assertTrue(value.strip(), "%s 的人判理由为空" % clause)

    def test_levels_are_consistent_with_evidence(self):
        """等级不是标签，得和证据类型对得上：FULL 必须有 test:，CODE_ONLY 不许有 test:。

        第九轮审查抓到的正是「有 test: 就算兜底」的高估；等级列必须能反过来约束证据。
        """
        allowed = {"FULL", "PARTIAL", "CODE_ONLY", "HUMAN"}
        for clause, cells in self._rows().items():
            joined = " ".join(cells)
            m = re.search(r"\*\*([A-Z_]+)\*\*", joined)
            with self.subTest(clause=clause):
                self.assertIsNotNone(m, "%s 缺少等级" % clause)
                level = m.group(1)
                self.assertIn(level, allowed)
                has_test = "test:" in joined
                has_code = "code:" in joined
                has_human = "human:" in joined
                if level == "FULL":
                    self.assertTrue(has_test, "FULL 必须有用例")
                elif level == "PARTIAL":
                    self.assertTrue(has_test or has_code, "PARTIAL 至少要有用例或符号")
                    self.assertTrue(has_human, "PARTIAL 必须写清缺哪一半")
                elif level == "CODE_ONLY":
                    self.assertTrue(has_code, "CODE_ONLY 必须有符号")
                    self.assertFalse(has_test, "CODE_ONLY 不该引用用例")
                else:
                    self.assertTrue(has_human, "HUMAN 必须给理由")

    def _level_of(self):
        out = {}
        for clause, cells in self._rows().items():
            m = re.search(r"\*\*([A-Z_]+)\*\*", " ".join(cells))
            out[clause] = m.group(1) if m else None
        return out

    def test_stats_table_matches_rows(self):
        """统计表与正文行必须逐条对得上。

        事故：统计是手写的，改了一条等级却忘了改表，于是「20 条 FULL」和表里
        列了 19 个 ID 长期并存——读者只信数字，不会去数。这里双向核：
        数字 == 列出的 ID 个数 == 正文里该等级的行数。
        """
        text = self._read("references", "evidence-matrix.md")
        level_of = self._level_of()
        stated = {}
        for line in text.splitlines():
            m = re.match(r"^\|\s*(FULL|PARTIAL|CODE_ONLY|HUMAN)\s*\|\s*(\d+)\s*\|(.*)$", line)
            if m:
                stated[m.group(1)] = (int(m.group(2)),
                                      m.group(3).replace("|", " ").split())
        self.assertEqual(sorted(stated), ["CODE_ONLY", "FULL", "HUMAN", "PARTIAL"],
                         "统计表解析失败：四个等级没找齐")
        for level, (count, ids) in stated.items():
            actual = sorted(c for c, v in level_of.items() if v == level)
            with self.subTest(level=level):
                self.assertEqual(count, len(ids), "%s 的条数与列出的 ID 个数不符" % level)
                self.assertEqual(sorted(ids), actual, "%s 的 ID 列表与正文行不符" % level)
        m = re.search(r"^\|\s*合计\s*\|\s*(\d+)\s*\|", text, re.M)
        self.assertIsNotNone(m, "统计表缺合计行")
        self.assertEqual(int(m.group(1)), len(level_of), "合计与正文行数不符")

    def test_prose_counts_match_rows(self):
        """正文里手写的「49 条 / FULL 20 条 / 32 条有离线用例」也必须对得上行。"""
        level_of = self._level_of()
        total = len(level_of)
        counts = {lv: sum(1 for v in level_of.values() if v == lv)
                  for lv in ("FULL", "PARTIAL", "CODE_ONLY", "HUMAN")}

        base = self._read("references", "baselines.md")
        for n in re.findall(r"当前分布（(\d+) 条", base):
            self.assertEqual(int(n), total, "baselines.md 的当前分布总数与矩阵行数不符")
        for level, n in re.findall(r"\*\*(FULL|PARTIAL|CODE_ONLY|HUMAN) (\d+) 条\*\*", base):
            self.assertEqual(int(n), counts[level],
                             "baselines.md 里 %s 的条数与矩阵不符" % level)

        matrix = self._read("references", "evidence-matrix.md")
        for n in re.findall(r"的 (\d+) 条判定口径", matrix):
            self.assertEqual(int(n), total, "evidence-matrix.md 开头声明的条款数与行数不符")
        m = re.search(r"(\d+) 条里 \*\*(\d+) 条（\d+%）真正机械化\*\*，(\d+) 条半机械化，"
                      r"(\d+) 条只有代码，(\d+) 条人判", matrix)
        self.assertIsNotNone(m, "矩阵结论段解析失败")
        self.assertEqual((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5))),
                         (total, counts["FULL"], counts["PARTIAL"],
                          counts["CODE_ONLY"], counts["HUMAN"]),
                         "矩阵结论段的四个数字与正文行不符")

        skill = self._read("SKILL.md")
        m = re.search(r"(\d+) 条有离线用例、(\d+) 条只有代码、(\d+) 条纯人判", skill)
        self.assertIsNotNone(m, "SKILL.md 的证据分布解析失败")
        self.assertEqual(int(m.group(1)), counts["FULL"] + counts["PARTIAL"],
                         "「有离线用例」应等于 FULL + PARTIAL")
        self.assertEqual(int(m.group(2)), counts["CODE_ONLY"])
        self.assertEqual(int(m.group(3)), counts["HUMAN"])

    def test_claimed_regression_count_matches_actual(self):
        """文档里写的「N 条回归」必须等于真实用例数。

        事故：一轮里改了三次用例数，`SKILL.md` 还写着 158、矩阵写 151——这种漂移
        人眼抓不住，只能让测试自己数 `def test_`。
        """
        tests_src = open(os.path.join(HERE, "test_hardening.py"), encoding="utf-8").read()
        actual = len(re.findall(r"^\s+def (test_\w+)\s*\(", tests_src, re.M))
        self.assertGreater(actual, 100, "用例数解析失败，规则可能没匹配上")
        claims = []
        files = (("SKILL.md", ("SKILL.md",)),
                 ("baselines.md", ("references", "baselines.md")),
                 ("evidence-matrix.md", ("references", "evidence-matrix.md")))
        pats = (r"当前回归 (\d+) 条", r"当前 (\d+) 条回归", r"全绿（当前 (\d+) 条）",
                r"离线回归（(\d+) 条）", r"(\d+) 条，覆盖 id 校验",
                r"会直接红。当前 (\d+) 条。")
        for label, parts in files:
            text = self._read(*parts)
            for pat in pats:
                for n in re.findall(pat, text):
                    claims.append((label, int(n)))
        self.assertTrue(claims, "一个回归条数声明都没找到，规则失效")
        for fn, n in claims:
            with self.subTest(file=fn, claimed=n):
                self.assertEqual(n, actual,
                                 "%s 声称 %d 条回归，实际 %d 条" % (fn, n, actual))

    def test_no_row_is_evidence_free(self):
        for clause, cells in self._rows().items():
            joined = " ".join(cells)
            with self.subTest(clause=clause):
                self.assertRegex(joined, r"(test|code|live|human):",
                                 "%s 没有任何证据标注" % clause)


class TestReviewLog(unittest.TestCase):
    """§F 审查记录自洽：轮数声明、轮次连续性、每轮有裁定。

    第六轮审查抓到过「轮次表不全」，第十四轮之后这条仍是手写的——手写就会漏。
    这里只做三件可判定的事：声明几轮 = 表里有几行；行号从 1 连续到 N；
    每行都写了裁定（`PASS` / `NEEDS-REVISION`），不许有「待裁定」蒙混。
    """

    ROOT = os.path.dirname(HERE)
    CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9, "十": 10}

    @classmethod
    def _cn2int(cls, s):
        if "十" not in s:
            return cls.CN[s]
        head, _, tail = s.partition("十")
        return (cls.CN[head] if head else 1) * 10 + (cls.CN[tail] if tail else 0)

    def _text(self):
        with open(os.path.join(self.ROOT, "references", "baselines.md"), encoding="utf-8") as f:
            return f.read()

    def _section(self):
        text = self._text()
        i = text.index("## F. 审查记录与未采纳项")
        j = text.index("## G. 证据矩阵")
        return text[i:j]

    def _rows(self):
        # 只在 §F 内找「| 数字 | 版本 | 裁定 |」的行：整篇扫描会把 C 类配方表也算进来
        return re.findall(r"^\|\s*(\d{1,2})\s*\|\s*(v[\d.]+[^|]*)\|([^|]*)\|([^|]*)\|",
                          self._section(), re.M)

    def test_declared_round_count_matches_table(self):
        text = self._text()
        m = re.search(r"经 ChatGPT 思考模式\*\*([一二三四五六七八九十]+)轮对外对抗审查", text)
        self.assertIsNotNone(m, "找不到「N 轮对外对抗审查」的声明")
        declared = self._cn2int(m.group(1))
        rows = self._rows()
        self.assertTrue(rows, "§F 轮次表一行都没解析到")
        nums = [int(r[0]) for r in rows]
        self.assertEqual(nums, list(range(1, len(nums) + 1)),
                         "轮次编号不连续或有重复：%s" % nums)
        self.assertEqual(declared, len(nums),
                         "开头声明 %d 轮，表里有 %d 行" % (declared, len(nums)))

    def test_every_round_has_verdict(self):
        for num, ver, _verdict, _note in self._rows():
            with self.subTest(round=num):
                self.assertRegex(ver, r"v\d\.\d", "第 %s 轮没写受审版本" % num)
                self.assertRegex(_verdict, r"PASS|NEEDS-REVISION",
                                 "第 %s 轮没写裁定" % num)


if __name__ == "__main__":
    unittest.main(verbosity=2)
