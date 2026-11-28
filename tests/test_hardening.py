# -*- coding: utf-8 -*-
"""chatgpt_send.py 的离线回归测试（不触网、不需要凭据）。

    python -m unittest discover -s tests -v
    python tests/test_hardening.py          # 直接跑也支持

覆盖的都是「出过事或一想就疼」的点：id 路径穿越、状态文件损坏被静默覆盖、
归档写半截、回复被静默截断、锁重入与 stale 回收、轮转兜底判定。
"""
import contextlib
import hashlib
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
from unittest import mock

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

# `_replace_with_bounded_retry` 的退避预算是 0.05+0.10+0.20+0.40+0.80+1.00 = 2.55s：
# Windows 上 AV/索引器短暂持有目标文件时，一次**合法**的 replace 重试就能吃掉整个
# 预算，CI 负载高时更久。所以锁路径的墙钟断言只做 10s「死锁/挂死保护」：真正等满
# LOCK_TIMEOUT_SEC=3s 的情况由 `_file_lock(strict=True)` 抛 LockTimeout 兜住，
# 不靠这条上界；这条只兜死锁或超出 timeout 的挂死。原先的 1.0s/3s 上界低于
# 2.55s 预算，一次瞬时 WinError 32/5 就会误红。
LOCK_WALL_CLOCK_GUARD_SEC = 10.0


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

    def test_non_str_ids_use_contract_error_not_attributeerror(self):
        """F-37：safe_id 契约是「不合法 → ValueError」——非 str 不得借 .strip 炸
        AttributeError；id8/cli_id 同步不得抛异常（id8 退 fallback，cli_id 返回 None）。"""
        for bad in (-1, 3.5, True, float("inf"), [1], {"a": 1}, object(), b"bytes"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    m.safe_id(bad)
                self.assertIsInstance(m.id8(bad), str)
                self.assertIsNone(m.cli_id(bad))

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

    def test_top_level_type_mismatch_is_quarantined(self):
        """F-34：合法 JSON 但顶层不是预期形状（手改成 []）＝损坏，与语法坏同罪。"""
        p = os.path.join(self.tmp, "shape.json")
        m._atomic_write(p, "[1, 2, 3]")
        got = m._load_json(p, {})
        self.assertEqual(got, {}, "顶层类型不符必须回落默认值，不得把 list 当 dict 返回")
        bads = [n for n in os.listdir(self.tmp) if n.startswith("shape.json.corrupt-")]
        self.assertEqual(len(bads), 1, "类型坏也要隔离，否则下一次写会静默覆盖旧内容")

    def test_anon_sessions_shape_pollution_never_escapes(self):
        """F-34：sessions/条目/顶层形状坏时，读写 API 全部确定性结局。"""
        cid = "44444444-5555-4666-7777-888888888888"
        for blob in ({"sessions": "x"}, {"sessions": {cid: "notadict"}}, [1, 2]):
            with self.subTest(blob=blob):
                m._atomic_write(m.ANON_SESSIONS_FILE, json.dumps(blob))
                self.assertIsInstance(m.load_anon_sessions().get("sessions"), dict)
                self.assertIsNone(m.anon_session(cid), "坏条目不算会话")
                m.record_anon_session(cid, tab="t1")
                m.mark_anon_archived(cid, reason="r")
                self.assertEqual(m.anon_session(cid).get("status"), "archived")

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
        self.assertLess(time.time() - t0, LOCK_WALL_CLOCK_GUARD_SEC,
                        "残留锁回收挂死（10s 挂死保护；正常应毫秒级，不该等满 timeout）")
        self.assertFalse(os.path.exists(lock))

    def test_lock_dead_holder_reclaimed_before_stale_age(self):
        """持有者确认已死 → 立即可回收，与锁的年龄无关。

        年龄门槛 LOCK_STALE_SEC=60 比等待上限 LOCK_TIMEOUT_SEC=3 大一个数量级，
        「先看年龄再看死活」等于「崩在 60 秒内留下的锁谁都回收不了」。
        """
        p = os.path.join(self.tmp, "young-dead.json")
        lock = p + ".lock"
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "process_start_time": 1,
                       "created_at": time.time()}, f)   # 刚创建，远未满 60s
        self.assertTrue(m._lock_holder_stale(lock),
                        "确认已死的锁与年龄无关，必须可回收")
        t0 = time.time()
        with m._file_lock(p, timeout=2, strict=True):
            pass
        self.assertLess(time.time() - t0, LOCK_WALL_CLOCK_GUARD_SEC,
                        "回收已死的锁挂死（10s 挂死保护；正常应毫秒级）")
        self.assertFalse(os.path.exists(lock))

    @unittest.skipUnless(os.name == "nt", "WinError 32（共享冲突）是 Windows 独有")
    def test_lock_released_while_waiter_holds_it_open_is_reclaimable(self):
        """事故复现：释放的瞬间恰好有等待者 open 着锁文件。

        `_lock_holder_stale` 每次抢锁失败都会 open(lock) 读身份——等待者的句柄
        让 `os.remove` 抛 WinError 32。旧实现 `except OSError: pass` 吞掉它，
        留下「活 pid + 匹配启动时间」的锁：判定永远为活，之后每次抢锁都要等满
        timeout 抛 LockTimeout，跨进程永久死锁（不是等 60 秒就能好）。
        """
        p = os.path.join(self.tmp, "hold.json")
        lock = p + ".lock"
        opened, done = threading.Event(), threading.Event()

        def waiter():                     # 与 _lock_holder_stale 的读法一致
            while not os.path.exists(lock):
                time.sleep(0.001)
            with open(lock, "r", encoding="utf-8") as f:
                opened.set()
                done.wait(1.0)            # 把句柄横跨整个释放过程
                f.read()

        t = threading.Thread(target=waiter)
        t.start()
        try:
            with m._file_lock(p, timeout=5, strict=True):
                self.assertTrue(opened.wait(2.0), "等待者应已打开锁文件")
                time.sleep(0.05)
            # 删不掉也必须放弃持有者身份，否则就是永久死锁
            self.assertTrue(m._lock_holder_stale(lock),
                            "删不掉的残留锁必须立刻可回收，而不是继续宣称活持有者")
        finally:
            done.set()
            t.join()
        t0 = time.time()
        with m._file_lock(p, timeout=2, strict=True):
            pass
        self.assertLess(time.time() - t0, LOCK_WALL_CLOCK_GUARD_SEC,
                        "释放后重新抢锁挂死（10s 挂死保护；正常应毫秒级，不该等满 timeout）")
        self.assertFalse(os.path.exists(lock), "重新抢锁并释放后不该残留锁文件")

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

    # ---- F-34：状态文件形状/类型畸形 → 确定性结局 ----
    def test_normalize_converges_polluted_shapes(self):
        for blob in ({"cursor": "abc"}, {"cursor": float("inf")}, {"cursor": [1]},
                     {"order": 5}, {"order": {}}, {"created": "x"}, {"created": []},
                     {"failures": 5}, {"unavailable": "x"}, {"unavailable": [1]},
                     {"fallback_uses": "5"}, [1, 2], "top-level-str"):
            with self.subTest(blob=blob):
                st = m._normalize_rotation(blob)
                self.assertIsInstance(st["cursor"], int)
                self.assertLess(st["cursor"], len(m.ROTATION_PRIMARY))
                self.assertEqual(set(st["order"]), set(m.ROTATION_ORDER))
                for a in m.ROTATION_ORDER:
                    self.assertIsInstance(st["created"][a], int)
                    self.assertIsInstance(st["failures"][a], int)
                self.assertIsInstance(st["unavailable"], dict)
                self.assertIsInstance(st["fallback_uses"], int)

    def test_normalize_does_not_fold_bool_or_float_cursor(self):
        """F-07 口径：int(True)==1、int(3.5)==3 都是把非法折叠成合法下标。"""
        self.assertEqual(m._normalize_rotation({"cursor": True})["cursor"], 0)
        self.assertEqual(m._normalize_rotation({"cursor": 3.5})["cursor"], 0)
        self.assertEqual(m._normalize_rotation({"cursor": "3.5"})["cursor"], 0)
        self.assertEqual(m._normalize_rotation({"cursor": "7"})["cursor"],
                         7 % len(m.ROTATION_PRIMARY), "纯数字串是合法整数，要保住计数")

    def test_rotation_helpers_total_on_polluted_unavailable(self):
        for blob in ({"unavailable": "x"}, {"unavailable": {"acct1": "str"}},
                     {"unavailable": {"acct1": []}}, {"unavailable": 7}):
            with self.subTest(blob=blob):
                st = json.loads(json.dumps(blob))   # 深拷贝，避免用例间互相污染
                self.assertFalse(m.is_unavailable(st, "acct1"))
                self.assertIn(m.purge_expired(st), (True, False))
                m.mark_unavailable(st, "acct1", "r")
                self.assertEqual(len(m.rotation_plan(st)), len(m.ROTATION_PRIMARY))
                reason = m.anon_fallback_block_reason(st, {"acct1": 429, "acct2": 429})
                self.assertTrue(reason is None or isinstance(reason, str),
                                "结局必须是 None（放行）或人话，不得抛异常")

    def test_polluted_counters_increment_without_typeerror(self):
        st = {"fallback_uses": "5", "failures": {"acct1": "2"}, "created": {"acct1": "x"}}
        m._normalize_rotation(st)
        st["fallback_uses"] = st.get("fallback_uses", 0) + 1
        st["failures"]["acct1"] = st["failures"].get("acct1", 0) + 1
        self.assertEqual(st["fallback_uses"], 6, "纯数字串保住计数再自增")
        self.assertEqual(st["failures"]["acct1"], 3)
        self.assertEqual(st["created"]["acct1"], 0, "非法计数归零，不折叠出假计数")

    # ---- F-33：429 限流时换账号 / 走匿名（用户裁定） ----
    def _run_new(self, account, status, conv=None):
        """跑 `--account <account> --new p.md`，返回 (rc, 是否走到轮转兜底, 输出)。"""
        real_api, real_send = m.ChatGPTAPI, m.send_prompt
        real_rot, real_argv = m.create_chat_rotating, sys.argv
        hit = {"rotate": False}

        class _Api(object):
            account = "x"
            is_anon = False

        m.ChatGPTAPI = lambda account=None: _Api()
        m.send_prompt = lambda *a, **k: (status, None, conv)
        m.create_chat_rotating = lambda *a, **k: hit.update(rotate=True) or 0
        sys.argv = ["chatgpt_send.py", "--account", account, "--new", "p.md"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = m.main()
        finally:
            m.ChatGPTAPI, m.send_prompt = real_api, real_send
            m.create_chat_rotating, sys.argv = real_rot, real_argv
        return rc, hit["rotate"], buf.getvalue()

    def test_explicit_account_429_escalates_to_rotation(self):
        """显式指定 acct1 却 429：必须换账号（换不动则匿名），不能原地失败。"""
        rc, rotated, out = self._run_new("acct1", 429)
        self.assertTrue(rotated, "429 未进入轮转兜底：用户裁定要求换账号/走匿名\n%s" % out)
        self.assertEqual(rc, 0)
        # 该账号要被记进冷却，否则轮转计划会先把它自己再试一遍
        st = m.load_rotation()
        self.assertIn("acct1", st["unavailable"])
        self.assertIn("429", st["unavailable"]["acct1"]["reason"])

    def test_explicit_account_401_does_not_escalate(self):
        """401 = 凭据失效，换账号/匿名都治不了，不得掩盖成「换个身份试试」。"""
        rc, rotated, out = self._run_new("acct1", 401)
        self.assertFalse(rotated, "401 不该触发轮转\n%s" % out)
        self.assertEqual(rc, 1)

    def test_explicit_nologin_429_does_not_recurse(self):
        """显式 nologin 已 429：再兜底还是匿名，不能自己调自己。"""
        rc, rotated, out = self._run_new("nologin", 429)
        self.assertFalse(rotated, "nologin 429 触发匿名兜底 = 无限自递归\n%s" % out)
        self.assertEqual(rc, 1)

    def _run_rotating_thinking(self, responses):
        """不触网、不加载凭据，直接测试默认思考模式轮转。

        responses:
            {
                "acct1": ThinkingModelUnavailable(...),
                "acct2": (200, None, conv_id),
                ...
            }
        """

        real_api = m.ChatGPTAPI
        real_send = m.send_prompt

        calls = []

        class _Api(object):
            def __init__(self, account):
                self.account = account
                self.is_anon = account == "nologin"

        def fake_api(account=None):
            return _Api(account)

        def fake_send(api, *args, **kwargs):
            calls.append(api.account)

            result = responses[api.account]

            if isinstance(result, Exception):
                raise result

            return result

        m.ChatGPTAPI = fake_api
        m.send_prompt = fake_send

        # 明确固定轮转起点，保证 acct1 -> acct2 -> acct3 -> acct4。
        m.save_rotation(
            m._normalize_rotation({"cursor": 0})
        )

        buf = io.StringIO()

        try:
            with contextlib.redirect_stdout(buf):
                rc = m.create_chat_rotating(
                    "p.md",
                    thinking=True,
                )
        finally:
            m.ChatGPTAPI = real_api
            m.send_prompt = real_send

        return rc, calls, buf.getvalue()

    def test_rotating_thinking_401_on_first_account_uses_next(self):
        """默认轮转：acct1 models 401，acct2 成功解析思考模型并建会话。

        401 必须转成账号级不可用，而不是让 SystemExit 终止进程。
        """

        conv = "11111111-2222-4333-8444-555555555555"

        responses = {
            "acct1": m.ThinkingModelUnavailable(
                "AUTH_INVALID",
                "acct1",
                "/backend-api/models HTTP 401",
            ),
            "acct2": (200, None, conv),
            "acct3": (200, None, "22222222-3333-4444-8555-666666666666"),
            "acct4": (200, None, "33333333-4444-4555-8666-777777777777"),
        }

        rc, calls, out = self._run_rotating_thinking(responses)

        self.assertEqual(rc, 0, out)

        self.assertEqual(
            calls,
            ["acct1", "acct2"],
            "acct1 失败后必须尝试 acct2，成功后立即停止",
        )

        self.assertNotIn("nologin", calls)

        st = m.load_rotation()

        self.assertIn("acct1", st["unavailable"])
        self.assertIn(
            "THINKING_UNAVAILABLE[AUTH_INVALID]",
            st["unavailable"]["acct1"]["reason"],
        )

        self.assertEqual(
            st["last"]["account"],
            "acct2",
        )

        self.assertIn(
            "NEW_CONV %s account=acct2" % conv,
            out,
        )

    def test_rotating_thinking_all_accounts_unavailable_fails(self):
        """四个登录态均无法满足思考要求时，最终 exit 1。

        不得：
        - 降级 auto；
        - 尝试 nologin；
        - 无限重试；
        - 把失败伪装成成功。
        """

        responses = {
            "acct1": m.ThinkingModelUnavailable(
                "AUTH_INVALID",
                "acct1",
                "HTTP 401",
            ),
            "acct2": m.ThinkingModelUnavailable(
                "MODEL_ENDPOINT_UNAVAILABLE",
                "acct2",
                "HTTP 503",
            ),
            "acct3": m.ThinkingModelUnavailable(
                "THINKING_MODEL_UNAVAILABLE",
                "acct3",
                "没有 -t- 模型",
            ),
            "acct4": m.ThinkingModelUnavailable(
                "AUTH_INVALID",
                "acct4",
                "HTTP 401",
            ),
            "acct5": m.ThinkingModelUnavailable(
                "AUTH_INVALID",
                "acct5",
                "HTTP 401",
            ),
            "acct6": m.ThinkingModelUnavailable(
                "MODEL_ENDPOINT_UNAVAILABLE",
                "acct6",
                "HTTP 502",
            ),
        }

        rc, calls, out = self._run_rotating_thinking(responses)

        self.assertEqual(rc, 1, out)

        self.assertEqual(
            calls,
            list(m.ROTATION_PRIMARY),
            "全部登录态必须各尝试一次",
        )

        self.assertNotIn("nologin", calls)

        self.assertIn(
            '"terminal_cause": "THINKING_MODEL_UNAVAILABLE"',
            out,
        )

        self.assertNotIn(
            "thinking model: auto",
            out,
        )

        st = m.load_rotation()

        for account in m.ROTATION_PRIMARY:
            with self.subTest(account=account):
                self.assertIn(account, st["unavailable"])
                self.assertIn(
                    "THINKING_UNAVAILABLE[",
                    st["unavailable"][account]["reason"],
                )

    def test_explicit_account_thinking_401_does_not_escalate(self):
        """显式 --account acct1，models 401 必须失败，不得自动换号。"""

        real_api = m.ChatGPTAPI
        real_send = m.send_prompt
        real_rotate = m.create_chat_rotating
        real_argv = sys.argv

        old_thinking = os.environ.get("CHATGPT_THINKING")

        hit = {"rotate": False}

        class _Api(object):
            account = "acct1"
            is_anon = False

        def fake_send(*args, **kwargs):
            raise m.ThinkingModelUnavailable(
                "AUTH_INVALID",
                "acct1",
                "/backend-api/models HTTP 401",
            )

        def fake_rotate(*args, **kwargs):
            hit["rotate"] = True
            return 0

        m.ChatGPTAPI = lambda account=None: _Api()
        m.send_prompt = fake_send
        m.create_chat_rotating = fake_rotate

        os.environ["CHATGPT_THINKING"] = "1"

        sys.argv = [
            "chatgpt_send.py",
            "--account",
            "acct1",
            "--new",
            "p.md",
        ]

        buf = io.StringIO()

        try:
            with contextlib.redirect_stdout(buf):
                rc = m.main()
        finally:
            m.ChatGPTAPI = real_api
            m.send_prompt = real_send
            m.create_chat_rotating = real_rotate
            sys.argv = real_argv

            if old_thinking is None:
                os.environ.pop("CHATGPT_THINKING", None)
            else:
                os.environ["CHATGPT_THINKING"] = old_thinking

        out = buf.getvalue()

        self.assertEqual(rc, 1, out)
        self.assertFalse(
            hit["rotate"],
            "显式账号思考模型失败不得触发默认轮转",
        )

        self.assertIn(
            '"reason_code": "AUTH_INVALID"',
            out,
        )

        self.assertIn(
            '"account": "acct1"',
            out,
        )


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

    def test_cookie_helpers_never_raise_on_non_str(self):
        """F-37：parse_cookie_header 文档承诺「不抛错」——非 str 经 _as_text 收敛；
        cookie_header 对不可迭代 jar 按空处理；device_id_from_cookie 随之收敛。"""
        for bad in (-1, 3.5, True, float("inf"), [1], {"a": 1}, object()):
            with self.subTest(bad=bad):
                self.assertEqual(m.parse_cookie_header(bad), {})
                self.assertEqual(m.device_id_from_cookie(bad), "")
                self.assertEqual(m.cookie_header(bad), "")
        self.assertEqual(m.cookie_header("not-a-jar"), "")

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
    """匿名浏览器失败路径测试。

    使用临时 proxy.json 满足生产 require_proxy() 的 fail-closed 前置条件。
    假代理只通过配置校验，不建立真实网络连接。
    """

    ENV_KEYS = (
        "CHATGPT_PROXY",
        "CHATGPT_PROXY_URL",
        "CHATGPT_ALLOW_DIRECT",
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "all_proxy",
        "ALL_PROXY",
    )

    def setUp(self):
        super().setUp()

        # PROXY_FILE 是模块导入时计算的常量。
        # 仅修改 BASE 不会改变它，必须显式重定向。
        self._proxy_file = m.PROXY_FILE
        m.PROXY_FILE = os.path.join(
            self.tmp,
            "proxy.json",
        )

        # 保存原有缓存，避免测试之间互相污染。
        self._proxy_cache = dict(m._PROXY_CACHE)
        m._PROXY_CACHE.clear()

        # 清除外部代理和直连许可，确保只使用本测试配置。
        self._proxy_env = {}

        for key in self.ENV_KEYS:
            self._proxy_env[key] = os.environ.pop(
                key,
                None,
            )

        # 假代理：require_proxy() 只解析配置字符串，
        # AnonBrowser.__init__ 不建立代理连接。
        with open(
            m.PROXY_FILE,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                {
                    "autodetect": False,
                    "url": "socks5h://127.0.0.1:1080",
                },
                f,
            )

        # 防止其他测试留下的缓存覆盖临时配置。
        m._PROXY_CACHE.clear()

    def tearDown(self):
        # 恢复模块级路径。
        m.PROXY_FILE = self._proxy_file

        # 恢复原缓存，而不是留下测试代理的缓存。
        m._PROXY_CACHE.clear()
        m._PROXY_CACHE.update(self._proxy_cache)

        # 恢复所有环境变量。
        for key, value in self._proxy_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        super().tearDown()

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

    def test_anon_browser_port_pollution_degrades_to_default(self):
        """F-36：CDP 端口是不可信配置——int() 的三类异常不得穿出构造器；
        变量写错不该让匿名通道起不来（降级 9333 + WARN，与 CHATGPT_REPLY_MAX_CHARS 同口径）。"""
        old = os.environ.pop("CHATGPT_ANON_CDP_PORT", None)
        try:
            for raw in ("notaport", "99999", "inf", "", "0"):
                with self.subTest(env=raw):
                    os.environ["CHATGPT_ANON_CDP_PORT"] = raw
                    self.assertEqual(m.AnonBrowser().port, 9333)
            for port in ([1], float("inf"), {"p": 1}, True, 70000):
                with self.subTest(port=port):
                    self.assertEqual(m.AnonBrowser(port=port).port, 9333)
            self.assertEqual(m.AnonBrowser(port=9444).port, 9444, "合法端口不得被改")
        finally:
            os.environ.pop("CHATGPT_ANON_CDP_PORT", None)
            if old is not None:
                os.environ["CHATGPT_ANON_CDP_PORT"] = old


class TestAnonSendSuccess(TmpBase):
    """匿名通道**成功**路径：建会话 → 收割 → 归档。

    2026-11-21 实证过的真实事故：`send_anon` 把 `harvest_mapping_checked()`
    的二元组按三元组解包，于是匿名会话**一旦建成就在收割处必抛 ValueError**，
    调用方只看到「失败」。结果是匿名通道被整体误判为不可用（并写进了治理
    记录：nologin「审计载体缺失」），实际是收割代码崩了。
    本用例钉住成功路径能走完并落盘，防止回归。
    """

    class _Browser:
        def __init__(self, did, cid, reply):
            self.did, self.cid, self.reply = did, cid, reply
            self.last_cid = self.last_tab = None

        def cookie_jar(self):
            return [{"name": "oai-did", "value": self.did,
                     "domain": ".chatgpt.com", "path": "/"}]

        def identity(self):
            return m.save_device_cookie(self.cookie_jar())

        def send(self, text, conv_id=None):
            self.last_cid, self.last_tab = self.cid, "tab-ok"
            return self.cid, self.reply, "tab-ok"

    def test_anon_send_harvests_and_archives(self):
        did = "cccccccc-3333-4333-8333-333333333333"
        cid = "dddddddd-4444-4444-8444-444444444444"
        body = json.dumps({"mapping": {"m1": {"message": {
            "id": "m1", "author": {"role": "assistant"},
            "content": {"parts": ["ANON_OK"]}, "create_time": 1}}}})

        api = object.__new__(m.ChatGPTAPI)
        api.account, api.is_anon, api.cookie = "nologin", True, ""
        api.base_dir = self.tmp
        api.anon = self._Browser(did, cid, "ANON_OK")
        api.req = lambda *a, **k: (200, body)

        status, path, got = m.send_anon(api, None, "p.md", "hello")

        self.assertEqual(status, 200, "成功路径不得因解包崩溃而退化成失败")
        self.assertEqual(got, cid)
        self.assertTrue(path and os.path.exists(path), "回复必须落盘")
        with open(path, encoding="utf-8") as f:
            self.assertIn("ANON_OK", f.read())
        s = m.anon_session(cid)
        self.assertIsNotNone(s, "成功路径同样要登记会话归属")
        self.assertEqual(s.get("device"), did)

    def test_harvest_returns_two_tuple(self):
        """契约钉死：harvest_mapping_checked 是二元组，不是三元组。"""
        raw = json.dumps({"mapping": {"m": {"message": {
            "id": "m", "author": {"role": "assistant"},
            "content": {"parts": ["x"]}, "create_time": 1}}}})
        out = m.ChatGPTAPI.harvest_mapping_checked(raw)
        self.assertEqual(len(out), 2, "签名是 (text, complete)")
        self.assertEqual(out, ("x", True))


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
        self.last_partial_path = None
        self.last_partial_bytes = 0
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
        # `#` 开头的行**不删**：旧版按注释头删行，把 Rust 的 `#[derive]`/`#[test]`/
        # `#[cfg]` 和 markdown 标题一起吃掉，投递的代码正文被静默截断
        # （2026-11-21 r1d/r1e 实证）。本用例钉住「原文照发」。
        prompt = self._prompt("# 注释行会被去掉\n真正的正文\n")
        status, path, conv = m.send_prompt(api, cid, prompt)
        self.assertEqual(status, 200)
        self.assertEqual(conv, cid, "必须认回原会话，不能静默落新会话")
        self.assertEqual(api.sent[0]["conv_id"], cid)
        self.assertEqual(api.sent[0]["text"], "# 注释行会被去掉\n真正的正文",
                         "`#` 行必须原样保留，删行会静默截断 Rust 属性")
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

    def test_reply_digest_selects_same_assistant_as_harvest_on_ct_tie(self):
        """F-27：多条 assistant 的 create_time 相同时，digest 选中的节点必须与 harvest 一致。

        旧版 reply_digest 用 (ct, id字符串) 选字典序最大者，harvest 经 _scan_mapping
        稳定排序 + reversed 取插入序最后者。两者在 ct 相同（含都缺失归 0）时选出
        不同节点 → sha 不等 → 合法完整回复被误标「完整性未证明」（F-24 门禁口径分裂）。
        """
        import hashlib
        # 插入序 zzz 先、aaa 后；都缺 create_time → ct 均归 0；id 字典序 zzz > aaa
        raw = json.dumps({"mapping": {
            "zzz": {"message": {"id": "zzz", "author": {"role": "assistant"},
                                "content": {"parts": ["ZZZ 节点"]}}},
            "aaa": {"message": {"id": "aaa", "author": {"role": "assistant"},
                                "content": {"parts": ["AAA 节点"]}}},
        }})
        dg = m.reply_digest(raw)
        full, complete = m.ChatGPTAPI.harvest_mapping_checked(raw)
        self.assertTrue(complete)
        self.assertEqual(full, "AAA 节点", "harvest 取插入序最后者")
        self.assertEqual(dg["message_id"], "aaa", "digest 必须选中同一节点，而非 id 字典序最大者")
        self.assertEqual(dg["sha256"], hashlib.sha256(full.encode("utf-8")).hexdigest(),
                         "同 ct 时摘要仍须与落盘全文一致（F-27）")

    def test_digest_none_never_leaves_harvest_complete(self):
        """加证（PASS 后加固）：对账器判不可用的 body，harvest 不得以 complete=True 放行。

        这不是新条款——F-24 门禁本就要求 digest 参与放行；这里把「两边对畸形输入的
        判定对称」这个不变量钉住，防止未来改动让 digest 返回 None 而 harvest 却报
        complete=True（那样 send_prompt 会把 digest_ok=False 当不完整处理，行为仍
        保守，但两侧语义分裂本身是隐患）。固定用例覆盖：非法 part / assistant 结构化
        part / 畸形 create_time。
        """
        import hashlib
        bodies = [
            # 非法 part（non-string 且非结构化）
            json.dumps({"mapping": {"n": {"message": {
                "id": "m", "author": {"role": "assistant"},
                "content": {"parts": ["ok", 123]}, "create_time": 1}}}}),
            # assistant 含合法结构化 part（多模态）——文本只是投影
            json.dumps({"mapping": {"n": {"message": {
                "id": "m", "author": {"role": "assistant"},
                "content": {"parts": [{"content_type": "image/png"}]}, "create_time": 1}}}}),
            # 不可用时间戳
            json.dumps({"mapping": {"n": {"message": {
                "id": "m", "author": {"role": "assistant"},
                "content": {"parts": ["ok"]}, "create_time": "bad"}}}}),
        ]
        for body in bodies:
            with self.subTest(body=body[:60]):
                dg = m.reply_digest(body)
                full, complete = m.ChatGPTAPI.harvest_mapping_checked(body)
                self.assertIsNone(dg, "这些畸形输入对账器必须判不可用")
                self.assertFalse(complete,
                                 "对账器判不可用时，harvest 不得报 complete=True 完整放行"
                                 "（可尽力收割部分文本，但须标记不完整）")

    def _run_cli(self, argv, api):
        """跑**真实 CLI 入口**（sys.argv → _main_impl），只把账号构造换成桩。

        为什么必须走 CLI：DEF-ops-04 的缺陷在 `_main_impl` 的超时装配里（无 --timeout
        时把 SEND_TIMEOUT 无条件写成 600），库路径 `send_prompt(...)` 根本不经过那段，
        所以只测库路径的用例永远绿、缺陷永远在。
        """
        out = io.StringIO()
        ctor = m.ChatGPTAPI

        class _Ctor(ctor):
            # 只替换「构造出哪个 api」，类上的静态解析器保持原样：
            # FakeAPI 内部就靠 ChatGPTAPI.extract_reply_sse_checked 解析 SSE。
            def __new__(cls, *a, **kw):
                return api

        m.ChatGPTAPI = _Ctor
        old_argv = sys.argv
        try:
            sys.argv = ["chatgpt_send.py"] + argv
            with contextlib.redirect_stdout(out):
                code = m.main()
        finally:
            sys.argv = old_argv
            m.ChatGPTAPI = ctor
        return code, out.getvalue()

    def test_cli_thinking_keeps_1800s_tier_without_timeout_flag(self):
        """--thinking 且没给 --timeout：必须按分档拿 1800s（旧版 CLI 会压成 600s）。"""
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "deep"))
        api.resolve_thinking_model = lambda _a: "gpt-6-t-mini"
        code, out = self._run_cli(
            ["--account", "acct1", "--thinking", "--new", self._prompt("x")], api)
        self.assertEqual(api.sent[-1]["timeout"], 1800,
                         "CLI 不传 --timeout 时不得覆盖 --thinking 的 1800s 分档")
        self.assertEqual(code, 0)
        self.assertIn("SEND prompt.md -> 200", out)

    def test_cli_thinking_tier_is_420_when_not_thinking(self):
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "fast"))
        code, _out = self._run_cli(
            ["--account", "acct1", "--new", self._prompt("x")], api)
        self.assertEqual(api.sent[-1]["timeout"], 420)
        self.assertEqual(code, 0)

    def test_cli_explicit_timeout_still_overrides_thinking(self):
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "deep"))
        api.resolve_thinking_model = lambda _a: "gpt-6-t-mini"
        code, out = self._run_cli(
            ["--account", "acct1", "--thinking", "--timeout", "900", "--new",
             self._prompt("x")], api)
        self.assertEqual(api.sent[-1]["timeout"], 900, "显式 --timeout 必须在分档之后覆盖")
        self.assertIn("timeout: 900s (source=cli)", out)
        self.assertEqual(code, 0)

    def test_truncated_send_prints_no_send_line_and_exits_3(self):
        """DEF-ops-01：截断响应不得打 `SEND ... -> 200`，且退出码必须是 3。"""
        cid = "11111111-2222-4333-8444-555555555555"
        # 有 conversation_id、但流没有 [DONE]：这就是被截断的半截响应
        api = FakeAPI(os.path.join(self.tmp, "auth"),
                      sse='data: {"conversation_id": "%s", "message": '
                          '{"author": {"role": "assistant"}, "content": {"parts": ["半截"]}}}'
                          % cid)
        api.last_truncated = True
        api.last_partial_bytes = 4096
        api.last_partial_path = os.path.join(self.tmp, "partial.txt")
        code, out = self._run_cli(
            ["--account", "acct1", "--new", self._prompt("x")], api)
        self.assertFalse(re.search(r"^SEND .* -> 200", out, re.M),
                         "截断响应不得打印唯一送达凭证 `SEND ... -> 200`")
        self.assertIn("SEND_TRUNCATED pfile=prompt.md st=200 bytes=4096", out)
        self.assertEqual(code, 3, "截断投递必须以 exit 3 收场，不能 exit 0")

    def test_complete_send_keeps_send_line_and_exits_0(self):
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "完整回复"))
        code, out = self._run_cli(
            ["--account", "acct1", "--new", self._prompt("x")], api)
        self.assertIn("SEND prompt.md -> 200", out)
        self.assertNotIn("SEND_TRUNCATED", out)
        self.assertEqual(code, 0)


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


class TestSendLogVerdict(TmpBase):
    """CB-B8 的机读版：三合取必须先看进程死活，否则在飞投递会被判死并重复投递。"""

    def _log(self, body, age=0, name="send.log"):
        p = os.path.join(self.tmp, name)
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        with open(p, "wb") as f:
            f.write(data)
        stamp = time.time() - age
        os.utime(p, (stamp, stamp))
        return p

    def test_send_line_is_sent(self):
        p = self._log("SEND prompt.md -> 200 respLen=12 in 8s\n")
        v = m.judge_send_log(p, None, live_pids=[])
        self.assertEqual(v["verdict"], "SENT")
        self.assertTrue(v["sent"])

    def test_truncated_line_is_not_a_delivery_credential(self):
        p = self._log("SEND_TRUNCATED pfile=prompt.md st=200 bytes=4096\n")
        v = m.judge_send_log(p, None, live_pids=[])
        self.assertEqual(v["verdict"], "TRUNCATED")
        self.assertFalse(v["sent"], "截断不是送达凭证")
        self.assertFalse(v["not_sent"], "已发出去的内容不该判 NOT_SENT 触发重投")

    def test_dead_empty_log_is_not_sent(self):
        p = self._log(b"", age=7200)
        v = m.judge_send_log(p, None, live_pids=[])
        self.assertEqual(v["verdict"], "NOT_SENT")
        self.assertTrue(v["not_sent"])

    def test_live_sender_process_blocks_not_sent(self):
        """在飞：0 字节 + mtime 早破 5 分钟，但同 pfile 的进程还活着 → 不得判死。"""
        p = self._log(b"", age=1200)
        v = m.judge_send_log(p, "prompt.md", live_pids=[os.getpid()])
        self.assertEqual(v["verdict"], "IN_FLIGHT")
        self.assertIn("sender_process_alive", v["reasons"])
        self.assertFalse(v["not_sent"])

    def test_unknown_liveness_is_conservative(self):
        """枚举不到进程（None）不等于没有进程：按可能活着处理，绝不判死。"""
        p = self._log(b"", age=3600 * 3)
        with mock.patch.object(m, "live_senders", return_value=None):
            v = m.judge_send_log(p, "prompt.md")
        self.assertEqual(v["verdict"], "IN_FLIGHT")
        self.assertTrue(v["liveness_unknown"])
        self.assertIn("liveness_unknown", v["reasons"])

    def test_backoff_window_with_timeout_first_line_is_in_flight(self):
        """`timeout:` 首行 + mtime 在 3600s 退避窗口内 → 进程可能正在退避，不得判死。"""
        p = self._log("timeout: 1800s (source=cli)\n", age=900)
        v = m.judge_send_log(p, None, live_pids=[])
        self.assertEqual(v["verdict"], "IN_FLIGHT")
        self.assertIn("inside_backoff_window", v["reasons"])

    def test_same_log_past_backoff_window_is_not_sent(self):
        """同一形态但超出 3600s 退避上限、且无存活进程 → NOT_SENT。"""
        p = self._log("timeout: 1800s (source=cli)\n", age=4000, name="old.log")
        v = m.judge_send_log(p, None, live_pids=[])
        self.assertEqual(v["verdict"], "NOT_SENT")

    def test_utf16_log_is_decoded_by_bom(self):
        """PowerShell 重定向产物是 UTF-16LE；按 utf-8 硬读会漏掉 SEND 行（CB-B10）。"""
        p = self._log("SEND prompt.md -> 200 respLen=5 in 3s\r\n".encode("utf-16"),
                      name="utf16.log")
        v = m.judge_send_log(p, None, live_pids=[])
        self.assertEqual(v["verdict"], "SENT")

    def test_live_senders_matches_prompt_file_in_cmdline(self):
        rows = [
            (101, "python chatgpt_send.py 11111111 prompt.md"),
            (102, "python other.py --list"),
        ]
        with mock.patch.object(m, "_iter_process_cmdlines", return_value=rows):
            self.assertEqual(m.live_senders("/work/prompt.md"), [101])
        with mock.patch.object(m, "_iter_process_cmdlines", return_value=None):
            self.assertIsNone(m.live_senders("/work/prompt.md"),
                              "枚举不可用必须返回 None，不能返回空列表冒充「没有进程」")

    def test_live_senders_never_counts_the_checker_itself(self):
        """判定器自己的命令行里就有 pfile（--prompt），不排除自己就永远判不出 NOT_SENT。"""
        rows = [(os.getpid(), "python chatgpt_send.py --check-send-log l.log --prompt prompt.md")]
        with mock.patch.object(m, "_iter_process_cmdlines", return_value=rows):
            self.assertEqual(m.live_senders("/work/prompt.md"), [])

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

    # ---- F-35：代理端口字段收敛 ----
    def test_normalize_proxy_rejects_illegal_port(self):
        """坏端口（超范围/非数字）＝配置非法，与坏 scheme 同待遇；
        不得一路潜到 _parse_socks_proxy 的 u.port 才炸（错误出处变成 urllib 内部）。"""
        for bad in ("socks5://127.0.0.1:99999", "socks5h://127.0.0.1:notaport",
                    "http://127.0.0.1:70000"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError) as cm:
                    m.normalize_proxy(bad)
                self.assertIn("端口", str(cm.exception))
        self.assertEqual(m.normalize_proxy("socks5://127.0.0.1:1080"),
                         "socks5://127.0.0.1:1080")

    def test_tcp_alive_never_raises_on_junk_port(self):
        """_tcp_alive 契约 = 返回 bool；int() 的 TypeError/OverflowError 不得穿出。"""
        for port in ([1], None, float("inf"), {"a": 1}, True, "abc", 0, -1, 65536, 10**400):
            with self.subTest(port=port):
                self.assertIs(m._tcp_alive("127.0.0.1", port, timeout=0.1), False)

    def test_autodetect_ports_pollution_never_escapes(self):
        """proxy.json 的 autodetect_ports 是不可信状态：畸形项跳过，不得崩整轮解析。"""
        for ports in ([float("inf")], ["abc"], [True], [None], [{"p": 1}], ["65536"], [0]):
            with self.subTest(ports=ports):
                self._cfg(autodetect=True, autodetect_ports=ports)
                info = m.resolve_proxy_info(refresh=True)
                self.assertEqual(info["url"], "", "畸形端口不得被探测成出口")

    def test_socks5_open_junk_port_raises_oserror_not_typeerror(self):
        """F-36：_socks5_open 的失败契约是 OSError——端口/配置垃圾不得让
        int()/struct.pack 的 TypeError/OverflowError 穿出（那不是调用方预期的异常族）。"""
        for cfg, port in (({"host": "127.0.0.1", "port": 1080}, [1]),
                          ({"host": "127.0.0.1", "port": 1080}, float("inf")),
                          ({"host": "127.0.0.1", "port": 1080}, 99999),
                          ({"host": "127.0.0.1", "port": "abc"}, 443),
                          ({"host": "127.0.0.1"}, 443),
                          ("junk", 443)):
            with self.subTest(cfg=cfg, port=port):
                with self.assertRaises(OSError):
                    m._socks5_open(cfg, "example.com", port, 0.2)

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

    # ---- F-30：状态文件里的超大数值不得让 OverflowError 穿出锁判定 ----
    def test_as_float_never_raises_on_overflow(self):
        """`float(10**400)` 抛的是 OverflowError，不是 TypeError/ValueError。

        历史上六处 `except (TypeError, ValueError)` 全都漏了它——损坏的
        `created_at`/`lease_until`/`ttl` 会让异常穿过锁判定路径。
        """
        huge = 10 ** 400
        for bad in (huge, -huge, float("inf"), float("-inf"), float("nan"),
                    None, True, False, "abc", {"a": 1}, [1]):
            with self.subTest(value=type(bad).__name__):
                self.assertEqual(m._as_float(bad, 0.0), 0.0)
        # 正常值必须原样通过
        self.assertEqual(m._as_float("1.5", 0.0), 1.5)
        self.assertEqual(m._as_float(1.5, 0.0), 1.5)
        self.assertEqual(m._as_float(10 ** 18, 0.0), 1e18)
        # default 可为 None（调用方用「没有可用值」表达缺失）
        self.assertIsNone(m._as_float("abc", None))

    def test_overflow_in_server_rate_fields_does_not_raise(self):
        """服务端响应体的超大限速字段：解析降级为 None，不得抛 OverflowError。"""
        api = m.ChatGPTAPI.__new__(m.ChatGPTAPI)
        for body in ({"detail": {"resets_in": 10 ** 400}},
                     {"detail": {"reset": 10 ** 400}},
                     {"error": {"retry_after": 10 ** 400}}):
            with self.subTest(body=str(body)[:30]):
                self.assertIsNone(api._rate_seconds_from(body))
        self.assertEqual(api._rate_seconds_from({"detail": {"resets_in": 42}}), 42.0)

    def test_overflow_created_at_does_not_break_lock_judgement(self):
        """坏 created_at 的残留锁：判定返回 False，`_file_lock` 抛 LockTimeout 而非 OverflowError。"""
        path = os.path.join(self.tmp, "f30a.json")
        with open(path + ".lock", "w", encoding="utf-8") as f:
            f.write('{"pid": %d, "created_at": %s}' % (os.getppid(), "9" * 400))
        self.assertFalse(m._lock_holder_stale(path + ".lock"),
                         "损坏的时间戳只能降级为「不可回收」，不得抛异常")
        entered = []
        with self.assertRaises(m.LockTimeout):
            with m._file_lock(path, timeout=0.3, strict=True):
                entered.append(1)
        self.assertEqual(entered, [], "锁判定崩溃不得退化成无锁进临界区")

    def test_overflow_lease_and_ttl_do_not_raise(self):
        """租约与不可用标记的超大 ttl/epoch 同样只降级，不抛异常。"""
        st = {"unavailable": {"a": {"ttl": 10 ** 400, "epoch": 10 ** 400},
                              "b": {"ttl": "x", "epoch": None}}}
        self.assertFalse(m.is_unavailable(st, "a"))
        self.assertFalse(m.is_unavailable(st, "b"))
        self.assertIsInstance(m.purge_expired(st), bool)
        # lease_until 是超大值 → 降级为 0.0 → 租约视为已过期，走进程身份判定；
        # 关键是不得抛 OverflowError。这里只断言「不抛异常且返回二元组」。
        ok, reason = m._lease_reclaimable(
            {"run_id": "r", "lease_until": 10 ** 400, "pid": os.getppid()},
            now=time.time())
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(reason, str)

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

    def test_sse_parsers_accept_non_str_without_raising(self):
        """F-31：`_scan_sse`/`_sse_conv_ids` 收到 bytes/int/None 只能降级，不得抛异常。

        旧版直接 `(raw or "").splitlines()`：bytes 抛
        `TypeError: startswith first arg must be bytes`、int 抛 AttributeError。
        而 `reply_digest`/`harvest_mapping_checked` 对同一类输入都降级成 None——
        同一份不可信输入不能两种待遇（F-12 口径）。
        """
        for bad in (None, 123, 1.5, True, [], {}, object()):
            with self.subTest(kind=type(bad).__name__):
                self.assertEqual(m._scan_sse(bad)[:2], ("", False))
                self.assertEqual(m._sse_conv_ids(bad), (None, []))

    def test_lock_judgement_rejects_non_path(self):
        """F-31b：`_lock_holder_stale`/`_try_reclaim_stale` 收到非路径只能返回 False。

        `open(True)` 在 Python 中等价于 `open(1)`——**把 stdout 当文件读**，
        fuzz 时实测会挂住；`open(10**400)` 抛 `OverflowError`（既非 OSError
        也非 ValueError，旧 except 抓不到）。
        """
        for bad in (True, False, 0, 1, 10 ** 400, 1.5, None, [], {}, object()):
            with self.subTest(kind=type(bad).__name__):
                self.assertFalse(m._lock_holder_stale(bad))
                self.assertFalse(m._try_reclaim_stale(bad))

    def test_sse_parsers_decode_bytes(self):
        """bytes 是 HTTP 体的天然形态：解码后照常解析，不因类型差异丢掉证据。"""
        sse = ('data: {"conversation_id": "11111111-2222-4333-8444-555555555555"}\n'
               'data: {"message": {"author": {"role": "assistant"}, '
               '"content": {"parts": ["hi"]}, "create_time": 1}}\n'
               "data: [DONE]\n")
        self.assertEqual(m._sse_conv_ids(sse.encode("utf-8")),
                         ("11111111-2222-4333-8444-555555555555",
                          ["11111111-2222-4333-8444-555555555555"]))
        self.assertEqual(m._scan_sse(sse.encode("utf-8"))[:2], ("hi", True))

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

    def test_ct_tie_full_reply_is_proven_complete(self):
        """F-27：GET 拉回多条同 ct 的 assistant、最新那条约完整回复，仍须证明完整。

        端到端钉住 F-24 门禁：旧版 digest 选 id 字典序最大者、harvest 选插入序
        最后者，同 ct 时算出不同 sha → 合法完整回复被误标「完整性未证明」。
        """
        class TieGetStub(self._Stub):
            def req(self, method, url, body=None, timeout=60):
                self.gets.append(url)
                gbody = json.dumps({"mapping": {
                    "zzz": {"message": {"id": "zzz", "author": {"role": "assistant"},
                                        "content": {"parts": ["旧节点"]}}},
                    "aaa": {"message": {"id": "aaa", "author": {"role": "assistant"},
                                        "content": {"parts": ["\n最新完整回复  \n"]}}},
                }})
                return 200, gbody

        saved, gets = [], []
        api = TieGetStub(os.path.join(self.tmp, "auth"), self._plain_incomplete(),
                         saved, gets, False)
        status, path, conv = m.send_prompt(api, self.TARGET, self._prompt(), thinking=False)
        self.assertEqual(status, 200)
        self.assertTrue(gets, "不完整 SSE 必须触发 GET 补全")
        self.assertIs(getattr(conv, "reply_complete", None), True,
                      "同 ct 的完整回复仍须被证明完整（F-27）")
        self.assertTrue(saved)
        self.assertIn("最新完整回复", saved[0][1])
        self.assertNotIn("完整性未证明", saved[0][1],
                         "同 ct 不得导致完整回复被误标不完整")

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

    def test_release_slot_non_path_returns_false(self):
        """F-37：slot_path 不是路径时返回 False（契约值），不得让 os.path 炸 TypeError。"""
        for bad in ([1], True, float("inf"), {"p": 1}, object()):
            with self.subTest(bad=bad):
                self.assertIs(m.release_slot(bad, run_id="r1"), False)

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

    # ---- F-32：磁盘写失败时租约 API 必须降级，不得让 OSError 逃逸 ----
    def _disk_full(self):
        """模拟磁盘满：_atomic_write 与 os.remove 都失败。"""
        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        return boom

    def test_lease_heartbeat_write_failure_returns_false(self):
        """文档承诺返回 bool。OSError 穿出去 = 心跳线程静默死亡、租约悄悄过期。"""
        p = os.path.join(self.tmp, "lease-f32a.json")
        m._atomic_write(p, json.dumps({"run_id": "r1", "pid": os.getpid(),
                                       "lease_until": time.time() + 999}))
        real = m._atomic_write
        m._atomic_write = self._disk_full()
        try:
            self.assertFalse(m.lease_heartbeat(p, "r1"))
        finally:
            m._atomic_write = real

    def test_lease_release_write_failure_returns_false(self):
        """释放失败按「租约仍在」处理：宁可留僵尸等人工闸口，也不谎报已释放。"""
        p = os.path.join(self.tmp, "lease-f32b.json")
        m._atomic_write(p, json.dumps({"run_id": "r1", "pid": os.getpid(),
                                       "lease_until": time.time() + 999}))
        real_w, real_rm = m._atomic_write, m.os.remove
        m._atomic_write = self._disk_full()
        m.os.remove = self._disk_full()
        try:
            self.assertFalse(m.lease_release(p, "r1"))
        finally:
            m._atomic_write, m.os.remove = real_w, real_rm

    def test_lease_reclaim_write_failure_returns_stable_reason(self):
        """回收是自动化巡检入口：写失败必须落成枚举 (False,'write_failed')，
        而不是抛异常把整轮巡检崩掉。租约必须过期，否则走不到写分支。"""
        p = os.path.join(self.tmp, "lease-f32c.json")
        m._atomic_write(p, json.dumps({"run_id": "r2", "pid": 999999,
                                       "lease_until": time.time() - 10,
                                       "process_start_time": 1.0}))
        real_w, real_rm = m._atomic_write, m.os.remove
        m._atomic_write = self._disk_full()
        m.os.remove = self._disk_full()
        try:
            ok, why = m.lease_reclaim(p)
        finally:
            m._atomic_write, m.os.remove = real_w, real_rm
        self.assertFalse(ok)
        self.assertEqual(why, "write_failed")
        self.assertTrue(os.path.exists(p), "写失败却把租约弄丢了")
        self.assertIn("write_failed", (m.lease_reclaim.__doc__ or "") + why,
                      "reason 必须是稳定枚举，不得混入异常文本")

    def test_lease_acquire_write_failure_returns_none(self):
        """没写成 = 没拿到租约。返回 None 让调用方按冲突退出，绝不带病继续跑。"""
        p = os.path.join(self.tmp, "lease-f32d.json")
        real = m._atomic_write
        m._atomic_write = self._disk_full()
        try:
            self.assertIsNone(m.lease_acquire(p, "task-x"))
        finally:
            m._atomic_write = real

    # ---- B15：心跳 / 续期 / 超时回收（时间参数按比例缩小，可重复）----
    def assert_lease_released(self, path):
        """租约已释放：文件不存在，或内容是空租约 `{}`（含空白）。

        `_lease_release_locked` 在 os.remove 抛 OSError（Windows 共享冲突
        WinError 32/5）时，会把内容改写成 `{}` 并保留文件——这是**有意降级**：
        抢锁者读到空租约立即回收，避免「活 pid + 匹配启动时间」的锁变成永久死锁。
        因此断言只接受「不存在 / 空租约」两种终态；内容非空必须失败并打印原文，
        不能让一次瞬时 OSError 把用例误红。
        """
        if not os.path.exists(path):
            return
        try:
            raw = _read(path)
        except OSError as e:
            self.fail("租约文件存在但读不到：%r（%s）" % (path, e))
        if not raw.strip():
            return
        try:
            info = json.loads(raw)
        except ValueError:
            info = raw
        self.assertEqual(info, {}, "租约未释放：文件仍存在且内容非空（%r）" % (raw,))

    def test_lease_run_done(self):
        p = os.path.join(self.tmp, "run1.json")
        val, st = m.lease_run(p, "t", lambda: 42, lease_sec=0.5, heartbeat_sec=0.1, max_sec=5)
        self.assertEqual((val, st), (42, "DONE"))
        self.assert_lease_released(p)

    def test_lease_release_degrades_to_empty_when_remove_fails(self):
        """os.remove 持续失败（WinError 32/5）时，租约降级为 `{}` 且结果仍是 DONE。

        这条自测覆盖 helper 的「空租约」分支：没有它，helper 放宽后可能永远
        走不到降级路径，等于把断言改松却没证明降级仍然发生。
        """
        p = os.path.join(self.tmp, "run-degraded.json")
        real_remove = os.remove

        def locked(target):
            # 只让「租约文件」的删除失败：真实场景是别的进程 open 着租约
            # （WinError 32），锁文件本身仍可回收。若把 os.remove 全打挂，
            # _file_lock 自己就抢不到锁，走不到 _lease_release_locked 的降级分支。
            if os.path.abspath(target) == os.path.abspath(p):
                raise PermissionError(13, "simulated sharing violation", target)
            return real_remove(target)

        os.remove = locked
        try:
            val, st = m.lease_run(p, "t", lambda: 42, lease_sec=0.5,
                                  heartbeat_sec=0.1, max_sec=5)
        finally:
            os.remove = real_remove
        self.assertEqual((val, st), (42, "DONE"))
        self.assertTrue(os.path.exists(p), "删除被拒时应保留文件并降级，而不是假装删掉")
        self.assert_lease_released(p)
        self.assertEqual(json.loads(_read(p)), {}, "降级终态必须是空租约")

    def test_heartbeat_keeps_lease_alive(self):
        """运行时长超过租约时，心跳必须续期（否则会被判过期而重复启动）。"""
        p = os.path.join(self.tmp, "run2.json")
        seen = {}

        def body():
            time.sleep(1.2)                     # 远超 lease_sec=0.4
            # 心跳线程正在 os.replace 时，Windows 会让目标文件短暂「删除挂起」，
            # 此刻 open 抛 PermissionError（不是租约丢了）。读的是同一个瞬时文件，
            # 有界重试即可，不能让这类瞬时共享冲突把用例误红。
            last_err = None
            for _ in range(20):
                try:
                    with open(p, encoding="utf-8") as f:
                        info = json.load(f)
                    break
                except OSError as e:
                    last_err = e
                    time.sleep(0.01)
            else:
                raise last_err
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
        self.assert_lease_released(p)
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

    def _spawn_race_child(self, argv, attempts=3):
        """Popen 有界重试：冷启动/AV 扫描下偶发 OSError 不该让用例直接 ERROR。"""
        last_err = None
        for i in range(attempts):
            try:
                return subprocess.Popen(argv, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
            except OSError as e:
                last_err = e
                if i + 1 < attempts:
                    time.sleep(0.2 * (i + 1))
        self.fail("Popen 连续 %d 次失败（最后一次 OSError：%r；进程从未启动，无 stderr）"
                  % (attempts, last_err))

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
        procs, results = [], []
        try:
            for _ in range(6):
                procs.append(self._spawn_race_child(
                    [sys.executable, "-c", script, "0.15", base]))
            for p in procs:
                try:
                    out, err = p.communicate(timeout=120)
                except subprocess.TimeoutExpired:
                    p.kill()
                    out, err = p.communicate()
                    self.fail("抢槽子进程 120s 未退出（stderr=%r）"
                              % ((err or "").strip(),))
                results.append((p.returncode, (out or "").strip(), (err or "").strip()))
        finally:
            for p in procs:                  # 超时/断言中断后也必须回收，不留僵尸
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=5)
        outs = [out for _rc, out, _err in results]
        # 子进程可能先打印 WARN 再打印结果：只认最后一行，且必须是槽位路径
        got = [o.strip().splitlines()[-1] for o in outs
               if o and o.strip().splitlines() and o.strip().splitlines()[-1].endswith(".slot")]
        ctx = "results=%r" % (results,)
        self.assertLessEqual(len(got), 3, "超发槽位：%s" % ctx)
        self.assertEqual(len(set(got)), len(got), "同一槽位被两个进程同时持有：%s" % ctx)


class TestCustody(TmpBase):
    """A1/A2/A4：交付正文的字节级托管。

    事故背景：正文长期靠「人眼看、手复制」，于是出现两种静默改写——用「最长代码块」
    误取别的段落；以及提取时顺手 strip/Unicode 规范化，把 CRLF 与全角空格改掉。
    两者都不会报错，只会让投递出去的正文与 ChatGPT 自产的正文不是同一份字节。
    """

    def test_extract_deliverable_non_path_is_custodyerror(self):
        """F-37：路径参数非法 → CustodyError（契约），不得让 os.path.isfile 炸 TypeError
        （isfile(True) 更是把 True 当 fd 1，F-31b 同款陷阱）。"""
        for bad in (None, 3.5, True, float("inf"), [1], object()):
            with self.subTest(bad=bad):
                with self.assertRaises(m.CustodyError):
                    m.extract_deliverable(bad, begin="B", end="E")

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

    def test_oneshot_bad_now_never_escapes_typeerror(self):
        """F-28：`now` 非 datetime / naive 时只能抛 ScheduleError，不得让 TypeError 逃逸。

        旧版 `base = now or datetime.now(dt.tzinfo)` 直接参与减法：naive datetime
        抛 "can't subtract offset-naive and offset-aware"，字符串抛 TypeError。
        调用方按 ScheduleError 处理，异常一逃逸就等于校验器自己崩了（与 F-07 同型）。
        """
        from datetime import datetime, timedelta
        at = (datetime.now().astimezone() + timedelta(minutes=30)).isoformat()
        for bad in (datetime.now(),              # naive
                    "2026-11-19T00:00:00+08:00",  # 字符串
                    12345):                       # 非时间类型
            with self.assertRaises(m.ScheduleError):
                m.check_oneshot(at, now=bad)

    def test_oneshot_echoes_canonical_iso(self):
        """F-29：回显必须是规范化 ISO，原始串只进 audit 字段（校验语义=下发语义）。"""
        from datetime import datetime, timedelta, timezone
        base = datetime.now(timezone.utc) + timedelta(minutes=30)
        for raw in (base.strftime("%Y-%m-%d %H:%M:%S+00:00"),   # 空格分隔
                    base.strftime("%Y%m%dT%H%M%S+0000"),        # 紧凑
                    base.strftime("%Y-%m-%dT%H:%M:%SZ"),        # Z 后缀
                    base.strftime("%Y-%m-%dT%H:%M+00:00")):     # 无秒
            res = m.check_oneshot(raw)
            self.assertEqual(res["scheduled_at"], datetime.fromisoformat(raw).isoformat(),
                             "回显必须是规范化 ISO，不得原样透传用户写法（F-29）")
            self.assertEqual(res["original_input_for_audit_only"], raw)
            self.assertEqual(res["executable_field"], "scheduled_at")

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


# 条款 id 语法：字母前缀 + 1~2 位数字 + 可选「-大写字母」后缀。
# 后缀不是装饰：`B15-R` 已经存在（B8 第 5 条列表项），而 `([A-E]\d{1,2})`
# 这一族正则看不见它。若将来有人把带后缀的条款写成表格行或 `### 标题`，
# 旧正则仍会静默跳过——条款与证据行会同时隐形，集合相等断言也照样通过，
# 因为两边被同一个过滤器剪掉了（对称盲区）。
CLAUSE_ID = r"[A-E]\d{1,2}(?:-[A-Z])?"


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
        ids = re.findall(r"^\|\s*\*\*(" + CLAUSE_ID + r")\*\*\s*\|", text, re.M)
        ids += re.findall(r"^###\s+(" + CLAUSE_ID + r")\s", text, re.M)
        return ids

    def _rows(self):
        rows = {}
        for line in self._read("references", "evidence-matrix.md").splitlines():
            m = re.match(r"^\|\s*(" + CLAUSE_ID + r")\s*\|(.*)$", line)
            if m:
                rows.setdefault(m.group(1), []).append(m.group(2))
        return rows

    def test_every_clause_has_exactly_one_row(self):
        # 阳性对照：守卫必须能识别带后缀的条款 id。
        # 没有这一条，把 CLAUSE_ID 退回 `[A-E]\d{1,2}` 不会有任何用例变红——
        # 一个只能靠「今天恰好没有后缀条款」才成立的守卫，等于没守卫。
        # 探针用与 _clauses/_rows 完全相同的三段脱衣，不是单拿 CLAUSE_ID 试。
        probe = "| **B15-R** | x |\n### A4-R 标题\n| B15-R | row |\n"
        with self.subTest(probe="table-row"):
            self.assertEqual(
                re.findall(r"^\|\s*\*\*(" + CLAUSE_ID + r")\*\*\s*\|", probe, re.M),
                ["B15-R"],
                "条款表格行解析不认带后缀的 id",
            )
        with self.subTest(probe="heading"):
            self.assertEqual(
                re.findall(r"^###\s+(" + CLAUSE_ID + r")\s", probe, re.M), ["A4-R"],
                "条款标题解析不认带后缀的 id",
            )
        with self.subTest(probe="matrix-row"):
            self.assertEqual(
                [m.group(1) for m in re.finditer(
                    r"^\|\s*(" + CLAUSE_ID + r")\s*\|(.*)$", probe, re.M)],
                ["B15-R"],
                "证据矩阵行解析不认带后缀的 id",
            )
            # 类别字母仍然是封闭集：不能为了兼容后缀把 Z9 也放进来
            self.assertIsNone(re.search(r"^\|\s*(" + CLAUSE_ID + r")\s*\|", "| Z9 | x |"),
                              "条款 id 语法不能接受 A–E 之外的类别")

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


class TestRegistryMonotonicGuard(TmpBase):
    """DECISION-143：注册表只增不删，删除阈值 0。"""

    def setUp(self):
        super().setUp()
        self._conv = m.CONV_MAP_FILE
        m.CONV_MAP_FILE = os.path.join(self.tmp, "conv-accounts.json")
        self._env = os.environ.pop("CHATGPT_REGISTRY_HIGHWATER_RECONCILE", None)

    def tearDown(self):
        m.CONV_MAP_FILE = self._conv
        if self._env is not None:
            os.environ["CHATGPT_REGISTRY_HIGHWATER_RECONCILE"] = self._env
        else:
            os.environ.pop("CHATGPT_REGISTRY_HIGHWATER_RECONCILE", None)
        super().tearDown()

    def test_record_conv_refuses_to_drop_existing_id(self):
        m.record_conv("6afd0001-1111-83ee-8000-000000000001", "acct1", "p1.md")
        m.record_conv("6afd0002-2222-83ee-8000-000000000002", "acct1", "p2.md")

        def shrink(data):
            data.pop("6afd0001-1111-83ee-8000-000000000001")
            return True

        with self.assertRaises(m.RegistryShrinkRefused):
            m._update_json(m.CONV_MAP_FILE, shrink, {})

        on_disk = m._load_json(m.CONV_MAP_FILE, {})
        self.assertIn("6afd0001-1111-83ee-8000-000000000001", on_disk)
        self.assertIn("6afd0002-2222-83ee-8000-000000000002", on_disk)

        # 第二层（高水位）在场时，上面那两条断言其实是被它兜住的，不是被
        # `lost` 兜住的——把 `if lost:` 改成永假，这一半仍然绿。`lost` 唯一
        # 不可替代的场景是 A 态：高水位基准还不存在。删掉基准再测一次，
        # 才能让那个分支真的被覆盖。
        #
        # 这一段的现实意义：A 态意味着这是本机第一次跑带守卫的版本。基准用
        # 当前注册表初始化，所以如果没有 `lost`，删除会静默落盘，而且基准会
        # 用**删完之后**的集合写下去——高水位从此永久抹平，后面再也发现不了。
        os.remove(m.CONV_MAP_FILE + ".highwater.json")

        with self.assertRaises(m.RegistryShrinkRefused):
            m._update_json(m.CONV_MAP_FILE, shrink, {})

        self.assertIn("6afd0001-1111-83ee-8000-000000000001",
                      m._load_json(m.CONV_MAP_FILE, {}),
                      "A 态下删除被放行——lost 守卫不是冗余的")
        self.assertFalse(
            os.path.exists(m.CONV_MAP_FILE + ".highwater.json"),
            "守卫必须在初始化基准之前就拒绝，否则基准会用删完的集合落盘",
        )

    def test_guard_detects_cross_process_deletion_via_highwater(self):
        path = m.CONV_MAP_FILE
        m.record_conv("6afd0003-3333-83ee-8000-000000000003", "acct1", "p3.md")
        self.assertTrue(os.path.exists(path + ".highwater.json"))

        # 模拟另一个写入者绕过 guard 直接删掉一条（legacy 脚本行为）。
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f)

        with self.assertRaises(m.RegistryShrinkRefused):
            m.record_conv("6afd0004-4444-83ee-8000-000000000004", "acct1", "p4.md")

    def test_guard_allows_pure_addition(self):
        m.record_conv("6afd0005-5555-83ee-8000-000000000005", "acct2", "p5.md")
        m.record_conv("6afd0006-6666-83ee-8000-000000000006", "acct2", "p6.md")
        self.assertEqual(
            sorted(m._load_json(m.CONV_MAP_FILE, {})),
            ["6afd0005-5555-83ee-8000-000000000005",
             "6afd0006-6666-83ee-8000-000000000006"],
        )


class TestRegistryHighwaterLifecycle(TmpBase):
    """DECISION-144 D2：高水位四态 A/B/C/D 与提交顺序。"""

    def setUp(self):
        super().setUp()
        self._conv = m.CONV_MAP_FILE
        m.CONV_MAP_FILE = os.path.join(self.tmp, "conv-accounts.json")
        self.path = m.CONV_MAP_FILE
        self.baseline = self.path + ".highwater.json"
        self._env = os.environ.pop("CHATGPT_REGISTRY_HIGHWATER_RECONCILE", None)

    def tearDown(self):
        m.CONV_MAP_FILE = self._conv
        if self._env is not None:
            os.environ["CHATGPT_REGISTRY_HIGHWATER_RECONCILE"] = self._env
        else:
            os.environ.pop("CHATGPT_REGISTRY_HIGHWATER_RECONCILE", None)
        super().tearDown()

    def _seed(self, ids):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({i: {"account": "acct1", "prompt": None, "ts": "t"} for i in ids}, f)

    def _baseline_ids(self):
        with open(self.baseline, "r", encoding="utf-8") as f:
            return set(json.load(f)["ids"])

    # ---- D2-A 首次初始化 ----
    def test_first_init_uses_real_registry_not_empty(self):
        self._seed(["6afd1001-0000-83ee-8000-000000000001"])
        self.assertFalse(os.path.exists(self.baseline))

        m.record_conv("6afd1002-0000-83ee-8000-000000000002", "acct1", "p.md")

        self.assertTrue(os.path.exists(self.baseline))
        self.assertEqual(
            self._baseline_ids(),
            {"6afd1001-0000-83ee-8000-000000000001",
             "6afd1002-0000-83ee-8000-000000000002"},
        )

    def test_first_init_refuses_empty_registry(self):
        with self.assertRaises(m.RegistryBaselineUnavailable):
            m._registry_guard(self.path, {}, {})
        self.assertFalse(os.path.exists(self.baseline))

    # ---- D2-B 正常比对 ----
    def test_valid_baseline_allows_addition(self):
        self._seed(["6afd2001-0000-83ee-8000-000000000001"])
        m.record_conv("6afd2002-0000-83ee-8000-000000000002", "acct1", "p.md")
        m.record_conv("6afd2003-0000-83ee-8000-000000000003", "acct1", "p.md")
        self.assertEqual(len(m._load_json(self.path, {})), 3)

    # ---- D2-C 基准损坏/不可写 ----
    def test_corrupt_baseline_refuses_write(self):
        self._seed(["6afd3001-0000-83ee-8000-000000000001"])
        with open(self.baseline, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(m.RegistryBaselineUnavailable):
            m.record_conv("6afd3002-0000-83ee-8000-000000000002", "acct1", "p.md")
        # 注册表未被写入
        self.assertEqual(len(m._load_json(self.path, {})), 1)

    def test_baseline_missing_ids_key_refuses_write(self):
        self._seed(["6afd3003-0000-83ee-8000-000000000001"])
        with open(self.baseline, "w", encoding="utf-8") as f:
            json.dump({"count": 0}, f)
        with self.assertRaises(m.RegistryBaselineUnavailable):
            m.record_conv("6afd3004-0000-83ee-8000-000000000002", "acct1", "p.md")

    def test_unwritable_baseline_refuses_write_before_touching_registry(self):
        self._seed(["6afd3005-0000-83ee-8000-000000000001"])
        m._write_baseline(self.baseline, {"6afd3005-0000-83ee-8000-000000000001"})

        real_probe = m._probe_baseline_writable
        def boom(path):
            raise m.RegistryBaselineUnavailable("simulated unwritable baseline")
        m._probe_baseline_writable = boom
        try:
            with self.assertRaises(m.RegistryBaselineUnavailable):
                m.record_conv("6afd3006-0000-83ee-8000-000000000002", "acct1", "p.md")
        finally:
            m._probe_baseline_writable = real_probe
        self.assertEqual(len(m._load_json(self.path, {})), 1)

    # ---- D2-D 基准领先 ----
    def test_baseline_ahead_refuses_without_reconcile_flag(self):
        self._seed(["6afd4001-0000-83ee-8000-000000000001"])
        m._write_baseline(
            self.baseline,
            {"6afd4001-0000-83ee-8000-000000000001",
             "6afd4099-0000-83ee-8000-000000000099"},   # 磁盘上不存在
        )
        with self.assertRaises(m.RegistryShrinkRefused):
            m.record_conv("6afd4002-0000-83ee-8000-000000000002", "acct1", "p.md")
        self.assertEqual(len(m._load_json(self.path, {})), 1)

    def test_baseline_ahead_recovers_with_reconcile_flag(self):
        self._seed(["6afd4003-0000-83ee-8000-000000000001"])
        m._write_baseline(
            self.baseline,
            {"6afd4003-0000-83ee-8000-000000000001",
             "6afd4098-0000-83ee-8000-000000000098"},
        )
        os.environ["CHATGPT_REGISTRY_HIGHWATER_RECONCILE"] = "1"
        m.record_conv("6afd4004-0000-83ee-8000-000000000004", "acct1", "p.md")
        self.assertEqual(
            self._baseline_ids(),
            {"6afd4003-0000-83ee-8000-000000000001",
             "6afd4004-0000-83ee-8000-000000000004"},
        )

    # ---- 提交顺序：基准永不领先注册表 ----
    def test_baseline_never_leads_registry_on_write_failure(self):
        self._seed(["6afd5001-0000-83ee-8000-000000000001"])
        m._write_baseline(self.baseline, {"6afd5001-0000-83ee-8000-000000000001"})

        real_write = m._atomic_write
        def fail(path, text):
            raise OSError("simulated registry write failure")
        m._atomic_write = fail
        try:
            with self.assertRaises(OSError):
                m.record_conv("6afd5002-0000-83ee-8000-000000000002", "acct1", "p.md")
        finally:
            m._atomic_write = real_write

        # 注册表没写进去，基准也不能包含那条 id
        self.assertEqual(
            self._baseline_ids(), {"6afd5001-0000-83ee-8000-000000000001"}
        )
        self.assertEqual(len(m._load_json(self.path, {})), 1)


class TestRegistryAuditAndReconcileAudit(TmpBase):
    """DECISION-145 Q2/Q3：重建留痕 + 只读 --registry-audit。"""

    def setUp(self):
        super().setUp()
        self._conv = m.CONV_MAP_FILE
        m.CONV_MAP_FILE = os.path.join(self.tmp, "conv-accounts.json")
        self.path = m.CONV_MAP_FILE
        self.baseline = self.path + ".highwater.json"
        self.audit = self.path + ".audit.jsonl"
        self._env = os.environ.pop("CHATGPT_REGISTRY_HIGHWATER_RECONCILE", None)
        self._reason = os.environ.pop("CHATGPT_REGISTRY_RECONCILE_REASON", None)
        m._RECONCILE_CONSUMED.clear()

    def tearDown(self):
        m.CONV_MAP_FILE = self._conv
        for k, v in (("CHATGPT_REGISTRY_HIGHWATER_RECONCILE", self._env),
                     ("CHATGPT_REGISTRY_RECONCILE_REASON", self._reason)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        m._RECONCILE_CONSUMED.clear()
        super().tearDown()

    def _seed(self, ids):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({i: {"account": "acct1", "prompt": None, "ts": "t"} for i in ids}, f)

    def _audit_rows(self):
        with open(self.audit, "r", encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    # ---- Q2：重建必须留痕 ----
    def test_reconcile_writes_persistent_audit_with_full_diff(self):
        self._seed(["6afd6001-0000-83ee-8000-000000000001",
                    "6afd6002-0000-83ee-8000-000000000002"])
        m._write_baseline(self.baseline, {"6afd6001-0000-83ee-8000-000000000001",
                                          "6afd6002-0000-83ee-8000-000000000002",
                                          "6afd6003-0000-83ee-8000-000000000003"})
        os.environ["CHATGPT_REGISTRY_HIGHWATER_RECONCILE"] = "1"
        os.environ["CHATGPT_REGISTRY_RECONCILE_REASON"] = "人工核验：确认该 id 属已删会话"

        m.record_conv("6afd6004-0000-83ee-8000-000000000004", "acct1", "p.md")

        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["op"], "HIGHWATER_RECONCILE")
        self.assertEqual(r["basis"], "人工核验：确认该 id 属已删会话")
        self.assertEqual(r["lost_ids"], ["6afd6003-0000-83ee-8000-000000000003"])
        self.assertEqual(r["baseline_before"]["count"], 3)
        self.assertTrue(r["baseline_before"]["sha256"])
        # registry 记录的是「重建后将被写入的注册表」：2 条既有 + 本次新增 1 条
        self.assertEqual(r["registry"]["count"], 3)
        self.assertIn("ts_utc", r)
        self.assertIn("git_sha", r)
        self.assertEqual(r["result"], "attempted")

    def test_reconcile_without_reason_still_records_placeholder(self):
        self._seed(["6afd6101-0000-83ee-8000-000000000001"])
        m._write_baseline(self.baseline, {"6afd6101-0000-83ee-8000-000000000001",
                                          "6afd6102-0000-83ee-8000-000000000002"})
        os.environ["CHATGPT_REGISTRY_HIGHWATER_RECONCILE"] = "1"

        m.record_conv("6afd6103-0000-83ee-8000-000000000003", "acct1", "p.md")

        r = self._audit_rows()[0]
        self.assertIn("unspecified", r["basis"])

    def test_reconcile_env_is_one_shot_per_process(self):
        """DECISION-145：不得成为永久宽松模式。"""
        self._seed(["6afd6201-0000-83ee-8000-000000000001"])
        m._write_baseline(self.baseline, {"6afd6201-0000-83ee-8000-000000000001",
                                          "6afd6202-0000-83ee-8000-000000000002"})
        os.environ["CHATGPT_REGISTRY_HIGHWATER_RECONCILE"] = "1"
        m.record_conv("6afd6203-0000-83ee-8000-000000000003", "acct1", "p.md")

        # 第二次再遇到基准领先，env 仍在但授权已消费 → 必须拒绝
        m._write_baseline(self.baseline, {"6afd6201-0000-83ee-8000-000000000001",
                                          "6afd6202-0000-83ee-8000-000000000002",
                                          "6afd6209-0000-83ee-8000-000000000009"})
        with self.assertRaises(m.RegistryShrinkRefused):
            m.record_conv("6afd6204-0000-83ee-8000-000000000004", "acct1", "p.md")

    def test_no_audit_written_when_reconcile_refused(self):
        self._seed(["6afd6301-0000-83ee-8000-000000000001"])
        m._write_baseline(self.baseline, {"6afd6301-0000-83ee-8000-000000000001",
                                          "6afd6302-0000-83ee-8000-000000000002"})
        with self.assertRaises(m.RegistryShrinkRefused):
            m.record_conv("6afd6303-0000-83ee-8000-000000000003", "acct1", "p.md")
        self.assertFalse(os.path.exists(self.audit))

    # ---- Q3：只读审计 ----
    def test_audit_reports_consistent_and_mutates_nothing(self):
        self._seed(["6afd6401-0000-83ee-8000-000000000001"])
        m._write_baseline(self.baseline, {"6afd6401-0000-83ee-8000-000000000001"})
        before = open(self.path, "rb").read()
        b_before = open(self.baseline, "rb").read()

        res = m.registry_audit(self.path)

        self.assertEqual(res["consistency"], "CONSISTENT")
        self.assertEqual(res["mutation"], "NONE")
        self.assertEqual(res["missing_from_registry"], [])
        self.assertEqual(open(self.path, "rb").read(), before)
        self.assertEqual(open(self.baseline, "rb").read(), b_before)

    def test_audit_reports_baseline_ahead_with_full_id_list(self):
        self._seed(["6afd6501-0000-83ee-8000-000000000001"])
        m._write_baseline(self.baseline, {"6afd6501-0000-83ee-8000-000000000001",
                                          "6afd6502-0000-83ee-8000-000000000002",
                                          "6afd6503-0000-83ee-8000-000000000003"})
        res = m.registry_audit(self.path)
        self.assertEqual(res["consistency"], "BASELINE_AHEAD")
        self.assertEqual(res["recovery"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(res["missing_from_registry"],
                         ["6afd6502-0000-83ee-8000-000000000002",
                          "6afd6503-0000-83ee-8000-000000000003"])
        # 审计不得自动恢复
        self.assertEqual(len(m._load_json(self.path, {})), 1)
        self.assertEqual(res["mutation"], "NONE")

    def test_audit_reports_missing_baseline(self):
        self._seed(["6afd6601-0000-83ee-8000-000000000001"])
        res = m.registry_audit(self.path)
        self.assertEqual(res["consistency"], "BASELINE_MISSING")
        self.assertFalse(os.path.exists(self.baseline))

    def test_audit_reports_corrupt_baseline(self):
        self._seed(["6afd6701-0000-83ee-8000-000000000001"])
        with open(self.baseline, "w", encoding="utf-8") as f:
            f.write("{not json")
        res = m.registry_audit(self.path)
        self.assertEqual(res["consistency"], "BASELINE_UNREADABLE")
        self.assertTrue(res["baseline_error"])

    def test_audit_reports_unreadable_registry(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[]")
        res = m.registry_audit(self.path)
        self.assertEqual(res["consistency"], "REGISTRY_UNREADABLE")


class TestAccountExpansionAcct6(unittest.TestCase):
    """DECISION-147：acct6 进轮转，但身份白名单仍是显式枚举（不做目录扫描）。"""

    def test_acct6_in_rotation_constants(self):
        self.assertIn("acct6", m.ROTATION_ORDER)
        self.assertIn("acct6", m.ROTATION_PRIMARY)
        # nologin 仍只能是兜底，不能进 PRIMARY
        self.assertNotIn("nologin", m.ROTATION_PRIMARY)
        self.assertLess(
            m.ROTATION_ORDER.index("acct6"),
            m.ROTATION_ORDER.index("nologin"),
        )

    def test_acct6_is_a_valid_identity(self):
        self.assertIn("acct6", m._VALID_ACCOUNT_IDENTITIES)
        self.assertEqual(m.normalize_account_identity("acct6"), "acct6")

    def test_unknown_identity_still_rejected(self):
        """扩表不等于放开：磁盘上放个 acct9 目录也不该被接受。"""
        for bad in ("acct7", "acct9", "../etc", "acct6/../acct1", ""):
            with self.assertRaises((ValueError, Exception)):
                m.normalize_account_identity(bad)

    def test_acct6_rotation_participates_and_shares_cursor(self):
        """cursor 推进必须覆盖 acct6，而不是在 4 个老账号里打转。"""
        seen = []
        for start in range(len(m.ROTATION_PRIMARY)):
            st = m._normalize_rotation({"cursor": start})
            plan = [m.ROTATION_PRIMARY[(st["cursor"] + i) % len(m.ROTATION_PRIMARY)]
                    for i in range(len(m.ROTATION_PRIMARY))]
            seen.extend(plan)
        self.assertIn("acct6", seen)
        self.assertEqual(
            len([a for a in seen if a == "acct6"]),
            len(m.ROTATION_PRIMARY),
            "每个 cursor 起点下 acct6 都应恰好出现一次",
        )

    def test_acct6_failure_state_is_isolated(self):
        """acct6 的失败计数不得污染其他账号。"""
        st = m._normalize_rotation({})
        for name in m.ROTATION_PRIMARY:
            self.assertIn(name, st["failures"])
            self.assertIn(name, st["created"])


class TestAccountRecoveryAcct5(unittest.TestCase):
    """DECISION-154：acct5 凭据被新登录覆盖后重新验证通过，恢复为第六身份。

    源文件名与内容不符（`acct6-export (1).json` 里其实是 acct5），
    因此身份判定必须以凭据内容（sub/email）为准，不得按文件名。
    """

    def test_acct5_in_rotation_constants(self):
        self.assertIn("acct5", m.ROTATION_ORDER)
        self.assertIn("acct5", m.ROTATION_PRIMARY)
        self.assertNotIn("nologin", m.ROTATION_PRIMARY)
        self.assertLess(
            m.ROTATION_ORDER.index("acct5"),
            m.ROTATION_ORDER.index("nologin"),
        )

    def test_acct5_keeps_natural_number_order(self):
        """DECISION-154 Q2=A：按编号自然序，acct5 夹在 acct4 与 acct6 之间。"""
        self.assertLess(m.ROTATION_ORDER.index("acct4"), m.ROTATION_ORDER.index("acct5"))
        self.assertLess(m.ROTATION_ORDER.index("acct5"), m.ROTATION_ORDER.index("acct6"))
        self.assertEqual(
            list(m.ROTATION_PRIMARY),
            sorted(m.ROTATION_PRIMARY),
            "PRIMARY 必须是编号升序",
        )

    def test_acct5_is_a_valid_identity(self):
        self.assertIn("acct5", m._VALID_ACCOUNT_IDENTITIES)
        self.assertEqual(m.normalize_account_identity("acct5"), "acct5")

    def test_recovery_does_not_open_the_whitelist(self):
        """恢复一个身份不等于放开枚举：相邻编号仍须被拒。"""
        for bad in ("acct7", "acct0", "acct55", "acct5/../acct1", "ACCT5"):
            with self.assertRaises((ValueError, Exception)):
                m.normalize_account_identity(bad)

    def test_acct5_rotation_participates_and_shares_cursor(self):
        seen = []
        for start in range(len(m.ROTATION_PRIMARY)):
            st = m._normalize_rotation({"cursor": start})
            plan = [m.ROTATION_PRIMARY[(st["cursor"] + i) % len(m.ROTATION_PRIMARY)]
                    for i in range(len(m.ROTATION_PRIMARY))]
            seen.extend(plan)
        self.assertEqual(
            len([a for a in seen if a == "acct5"]),
            len(m.ROTATION_PRIMARY),
            "每个 cursor 起点下 acct5 都应恰好出现一次",
        )

    def test_acct5_failure_state_is_isolated(self):
        st = m._normalize_rotation({})
        for name in m.ROTATION_PRIMARY:
            self.assertIn(name, st["failures"])
            self.assertIn(name, st["created"])

class TestCookiePersistenceBoundary(TmpBase):
    """DECISION-156：Cookie 持久化边界。

    实测事故：acct1 的 cookies.json 里混进整包 Google 会话（50 条，覆盖
    accounts.google.com / .google.com / .youtube.com / .google.co.jp /
    .google.com.hk）。请求头过滤让它们发不出去，但 warm_cookies() 会把整个
    列表反复回写磁盘——凭据存储边界失守。这里锁死「哪些凭据有资格被保存」。
    """

    def test_chatgpt_domain_allowed(self):
        for d in ("chatgpt.com", ".chatgpt.com", "ws.chatgpt.com", "CHATGPT.COM"):
            with self.subTest(domain=d):
                self.assertTrue(m._cookie_domain_allowed(d))

    def test_openai_domain_allowed(self):
        for d in ("openai.com", ".openai.com", "auth.openai.com",
                  ".auth.openai.com", "files.openai.com", ".bzr.openai.com"):
            with self.subTest(domain=d):
                self.assertTrue(m._cookie_domain_allowed(d))

    def test_lookalike_domains_rejected(self):
        """精确边界：包含不等于属于。"""
        for d in ("notchatgpt.com", "evil-openai.com", "chatgpt.com.evil.io",
                  "openai.com.evil.io", "fakechatgpt.com", "chatgpt.co",
                  "myopenai.com", "google.com", ".google.com",
                  "accounts.google.com", ".youtube.com", ".google.co.jp",
                  ".google.com.hk", ".oaistatic.com"):
            with self.subTest(domain=d):
                self.assertFalse(m._cookie_domain_allowed(d))

    def test_empty_and_malformed_domain_rejected(self):
        for d in ("", None, "   ", ".", ".."):
            with self.subTest(domain=repr(d)):
                self.assertFalse(m._cookie_domain_allowed(d))

    def test_filter_keeps_only_openai(self):
        cookies = [
            {"name": "oai-did", "value": "x", "domain": ".chatgpt.com"},
            {"name": "__Secure-next-auth.session-token.0", "value": "y", "domain": "chatgpt.com"},
            {"name": "__cf_bm", "value": "z", "domain": ".openai.com"},
            {"name": "SID", "value": "g1", "domain": ".google.com"},
            {"name": "HSID", "value": "g2", "domain": ".google.com"},
            {"name": "LSID", "value": "g3", "domain": "accounts.google.com"},
            {"name": "__Secure-1PSID", "value": "g4", "domain": ".youtube.com"},
            {"name": "NID", "value": "g5", "domain": ".google.co.jp"},
        ]
        out = m._filter_openai_cookies(cookies)
        self.assertEqual(len(out), 3)
        self.assertEqual(
            sorted(c["name"] for c in out),
            ["__Secure-next-auth.session-token.0", "__cf_bm", "oai-did"],
        )

    def test_filter_preserves_all_fields(self):
        """过滤不得重构字段——只挑不选。"""
        c = {"name": "oai-did", "value": "v", "domain": ".chatgpt.com",
             "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax",
             "expires": 1796110471}
        out = m._filter_openai_cookies([c])
        self.assertEqual(out, [c])
        self.assertIs(out[0], c)

    def test_filter_handles_non_list_and_non_dict(self):
        self.assertEqual(m._filter_openai_cookies(None), [])
        self.assertEqual(m._filter_openai_cookies("x"), [])
        self.assertEqual(m._filter_openai_cookies([]), [])
        self.assertEqual(
            m._filter_openai_cookies(["junk", None, 5,
                                      {"name": "oai-did", "value": "v",
                                       "domain": ".chatgpt.com"}]),
            [{"name": "oai-did", "value": "v", "domain": ".chatgpt.com"}],
        )

    def test_extract_auth_shares_the_same_domain_semantics(self):
        """两份独立实现必须由同一组向量约束（DECISION-156 验收要求）。

        `extract_auth.py` 故意不 import 主脚本（避免脚本互相依赖），于是
        `_cookie_domain_allowed` 存在两份实现。两份实现最危险的失败模式不是
        一起错，而是**漂移**：有人只改了一处，边界就在另一条路径上裂开。
        这里把同一组向量同时喂给两份实现，任一处语义变化立刻红。
        """
        src = os.path.join(HERE, "..", "scripts", "extract_auth.py")
        spec = importlib.util.spec_from_file_location("extract_auth_under_test", src)
        ea = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ea)

        allow = ("chatgpt.com", ".chatgpt.com", "ws.chatgpt.com",
                 "openai.com", ".openai.com", "auth.openai.com", "CHATGPT.COM")
        deny = ("notchatgpt.com", "evil-openai.com", "chatgpt.com.evil.io",
                "openai.com.evil.io", "chatgpt.co", "myopenai.com",
                "google.com", ".google.com", "accounts.google.com",
                ".youtube.com", ".oaistatic.com", "", None, "  ", ".")

        for d in allow:
            with self.subTest(domain=d, kind="allow"):
                self.assertTrue(m._cookie_domain_allowed(d), "主脚本应允许 %r" % (d,))
                self.assertTrue(ea._cookie_domain_allowed(d), "extract_auth 应允许 %r" % (d,))
        for d in deny:
            with self.subTest(domain=d, kind="deny"):
                self.assertFalse(m._cookie_domain_allowed(d), "主脚本应拒绝 %r" % (d,))
                self.assertFalse(ea._cookie_domain_allowed(d), "extract_auth 应拒绝 %r" % (d,))

    def test_cookie_header_built_at_init_excludes_foreign_domains(self):
        """构造请求头时就必须过滤——不能只在回写磁盘时过滤。

        第三处同类绕过：`__init__` 与 `warm_cookies()` 末尾都在拼 Cookie 头，
        原实现用 `"chatgpt.com" in domain` 字符串包含，`notchatgpt.com` 会被
        当自己人发出去。这里直接构造实例，断言头里只剩 OpenAI 域。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "cookies.json"), "w", encoding="utf-8") as f:
                json.dump([
                    {"name": "oai-did", "value": "d", "domain": ".chatgpt.com"},
                    {"name": "__cf_bm", "value": "c", "domain": ".openai.com"},
                    {"name": "SID", "value": "g", "domain": ".google.com"},
                    {"name": "evil", "value": "x", "domain": "notchatgpt.com"},
                    {"name": "evil2", "value": "y", "domain": "chatgpt.com.evil.io"},
                ], f)
            with open(os.path.join(tmp, "access_token.txt"), "w", encoding="utf-8") as f:
                f.write("tok")

            with mock.patch.object(m, "resolve_proxy_info",
                                   return_value={"source": "t", "url": "http://127.0.0.1:7000"}):
                api = m.ChatGPTAPI(base_dir=tmp, account="acct1")

            self.assertEqual(api.cookie, "oai-did=d; __cf_bm=c")
            self.assertNotIn("SID", api.cookie)
            self.assertNotIn("notchatgpt", api.cookie)

    def test_warm_cookies_drops_foreign_cookies_on_writeback(self):
        """回归：真实走 warm_cookies() 的回写路径。

        输入 50 条 Google + 3 条 OpenAI，断言内存与磁盘最终都只剩 OpenAI 域。
        网络层 mock，持久化与过滤走真实代码。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            foreign = [{"name": "SID%d" % i, "value": "g", "domain": ".google.com"}
                       for i in range(50)]
            keep = [{"name": "oai-did", "value": "d", "domain": ".chatgpt.com"},
                    {"name": "__cf_bm", "value": "c", "domain": ".openai.com"},
                    {"name": "_account", "value": "a", "domain": "chatgpt.com"}]

            api = m.ChatGPTAPI.__new__(m.ChatGPTAPI)
            api.base_dir = tmp
            api.cookies = foreign + keep
            api.cookie = ""
            api.is_anon = False
            api.proxy = "http://127.0.0.1:7000"

            with mock.patch.object(m, "build_opener_for", side_effect=_fake_build_opener):
                api.warm_cookies()

            self.assertEqual(len(api.cookies), 4, "内存里不该留 Google cookie")
            self.assertTrue(all(m._cookie_domain_allowed(c["domain"]) for c in api.cookies))

            on_disk = json.load(open(os.path.join(tmp, "cookies.json"), encoding="utf-8"))
            self.assertEqual(len(on_disk), 4, "磁盘上不该留 Google cookie")
            self.assertTrue(all(m._cookie_domain_allowed(c["domain"]) for c in on_disk))

    def test_save_device_cookie_drops_foreign_domain(self):
        """落盘信任边界：`save_device_cookie` 必须自己 fail-closed。

        调用方今天恰好都先过 `cookie_jar()` 的过滤，但这是「恰好」而不是契约。
        直接喂一个混了 evil.com 的 jar，断言非 OpenAI 域既不进 .txt 也不进 .json。
        """
        jar = [{"name": "oai-did", "value": DEV_A, "domain": ".chatgpt.com", "path": "/"},
               {"name": "sid", "value": "x", "domain": "evil.com", "path": "/"}]
        self.assertEqual(m.save_device_cookie(jar), DEV_A)

        ddir = m.anon_device_dir()
        txt = _read(os.path.join(ddir, DEV_A + ".txt"))
        js = json.loads(_read(os.path.join(ddir, DEV_A + ".json")))
        self.assertNotIn("sid=x", txt)
        self.assertEqual([c["name"] for c in js], ["oai-did"])

        device_txt = os.path.join(m.BASE, "accounts", "nologin", "device-cookie.txt")
        self.assertNotIn("sid=x", _read(device_txt))

    def test_save_device_cookie_all_foreign_yields_nothing(self):
        """整包都是外人域 → 拿不到合法 oai-did → 返回 "" 且不落任何文件。"""
        jar = [{"name": "oai-did", "value": DEV_A, "domain": "notchatgpt.com"},
               {"name": "SID", "value": "g", "domain": ".google.com"}]
        self.assertEqual(m.save_device_cookie(jar), "")
        self.assertFalse(os.path.exists(os.path.join(m.anon_device_dir(), DEV_A + ".json")))
        self.assertFalse(os.path.exists(os.path.join(m.anon_device_dir(), DEV_A + ".txt")))
        self.assertFalse(
            os.path.exists(os.path.join(m.BASE, "accounts", "nologin", "device-cookie.txt")))

    def test_save_device_cookie_legit_jar_unaffected(self):
        """收紧不得误伤：纯 .chatgpt.com jar 照常落盘并返回 did。"""
        jar = [{"name": "oai-did", "value": DEV_A, "domain": ".chatgpt.com", "path": "/"},
               {"name": "__cf_bm", "value": "cf", "domain": ".chatgpt.com", "path": "/"}]
        self.assertEqual(m.save_device_cookie(jar), DEV_A)
        self.assertEqual(len(m.load_device_jar(DEV_A)), 2)
        self.assertIn("oai-did=" + DEV_A, m.load_device_cookie(DEV_A))

    def test_save_device_cookie_str_path_still_works(self):
        """老快照兼容：str 输入被合成 .chatgpt.com，必须仍通过域判定。"""
        self.assertEqual(m.save_device_cookie("oai-did=%s; __cf_bm=x" % DEV_A), DEV_A)
        self.assertIn("oai-did=" + DEV_A, m.load_device_cookie(DEV_A))


def _fake_build_opener(proxy, context=None, extra=()):
    return _NullOpener(extra)


class _NullOpener:
    """warm_cookies 里 opener.open(...) 的替身：不触网，只往 jar 里塞一颗新 cookie。

    warm_cookies 自己 new 一个 CookieJar，通过 `extra=[HTTPCookieProcessor(jar)]`
    交给 build_opener_for。替身必须把 cookie 写进**那个** jar，否则 jar 里只有
    原有 cookie，`changed` 为空会提前 return，回写分支压根不会被执行 —— 那样
    测试就只是「跑了一遍但什么也没验证」。
    """

    def __init__(self, extra=()):
        self._extra = list(extra)

    def open(self, req, timeout=None):
        for h in self._extra:
            jar = getattr(h, "cookiejar", None)
            if jar is None:
                continue
            import http.cookiejar as _cj
            jar.set_cookie(_cj.Cookie(
                0, "__cf_bm_fresh", "fresh", None, False, ".openai.com",
                False, False, "/", True, False, None, False, None, None, {}))

        class _R:
            def read(self_inner):
                return b""

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return _R()

class TestBatchAddedCountSemantics(TmpBase):
    """DECISION-151：`registered` != `added_count`，幂等性判据只能是后者。"""

    def setUp(self):
        super().setUp()
        self._conv = m.CONV_MAP_FILE
        m.CONV_MAP_FILE = os.path.join(self.tmp, "conv-accounts.json")
        self.path = m.CONV_MAP_FILE

    def tearDown(self):
        m.CONV_MAP_FILE = self._conv
        super().tearDown()

    def _seed(self, ids):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({i: {"account": "acct1", "prompt": None, "ts": "t"} for i in ids}, f)

    def test_existing_candidates_report_zero_added(self):
        self._seed(["6afd7001-0000-83ee-8000-000000000001",
                    "6afd7002-0000-83ee-8000-000000000002"])
        before = open(self.path, "rb").read()

        out = m.register_conversations_batch([
            {"id": "6afd7001-0000-83ee-8000-000000000001", "account": "acct1"},
            {"id": "6afd7002-0000-83ee-8000-000000000002", "account": "acct1"},
        ])

        # 已验证归属的候选照常返回全部……
        self.assertEqual(len(out), 2)
        # ……但真正新增为 0，且磁盘一字未动
        self.assertEqual(m.register_conversations_batch.last_added_count, 0)
        self.assertEqual(open(self.path, "rb").read(), before)

    def test_new_candidates_report_matching_added(self):
        self._seed(["6afd7101-0000-83ee-8000-000000000001"])
        out = m.register_conversations_batch([
            {"id": "6afd7102-0000-83ee-8000-000000000002", "account": "acct2"},
            {"id": "6afd7103-0000-83ee-8000-000000000003", "account": "acct2"},
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual(m.register_conversations_batch.last_added_count, 2)

    def test_mixed_batch_reports_only_new(self):
        self._seed(["6afd7201-0000-83ee-8000-000000000001"])
        out = m.register_conversations_batch([
            {"id": "6afd7201-0000-83ee-8000-000000000001", "account": "acct1"},
            {"id": "6afd7202-0000-83ee-8000-000000000002", "account": "acct3"},
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual(m.register_conversations_batch.last_added_count, 1)


def _load_p1():
    """加载 continue_full.py（它 import cdp，测试里用桩顶掉）。"""
    import types
    fake_cdp = types.ModuleType("cdp")
    class _C:
        def __init__(self, *a, **k):
            pass
    fake_cdp.CDPClient = _C
    spec = importlib.util.spec_from_file_location(
        "continue_full_under_test", os.path.join(HERE, "..", "scripts", "continue_full.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("cdp", fake_cdp)
    saved = sys.modules.get("cdp")
    sys.modules["cdp"] = fake_cdp
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["cdp"] = saved
    return mod


class _FakeCdp:
    """按顺序吐出 reply_snapshot 的返回值，用于驱动 reply_after 的判定分支。

    R375-01 之后 captured 需要连续两次"正文一致且 streaming=False"，
    所以默认把同一帧重复吐（stable_repeat>=2），单次断言不被误伤。
    """

    def __init__(self, counts, last="未完成的正文", streaming=True,
                 has_error=False, frames=None, alert_text=""):
        self.counts = list(counts)
        self.last = last
        self.streaming = streaming
        self.has_error = has_error
        self.alert_text = alert_text
        self.frames = list(frames) if frames else None

    def evaluate(self, js):
        if self.frames:
            f = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
            return {"value": json.dumps(f)}
        n = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        alert = self.alert_text
        if self.has_error and not alert:
            alert = "Something went wrong"   # 注入结构错误（等价于页面有 alert）
        return {"value": json.dumps({"count": n, "last": self.last,
                                     "streaming": self.streaming,
                                     "alert_text": alert})}


class _FakeCdpForMain:
    """驱动 continue_full.main() 走"无需续接、成功发送"分支。"""

    def ensure_page(self, **k):
        return None

    def navigate(self, url, timeout=30):
        return None

    def send(self, *a, **k):
        return None

    def evaluate(self, js):
        import json as _j
        s = str(js)
        if "data-message-author-role" in s:
            return {"value": _j.dumps({"count": 4, "last": "P1 回复",
                                       "streaming": False, "alert_text": ""})}
        if "composerLen" in s:
            return {"value": _j.dumps({"url": "https://chatgpt.com/c/"
                                       "9f8e7d6c-1111-4222-8333-444455556666",
                                       "composerLen": 0, "ctxLimit": False,
                                       "hasFup": False, "streaming": False})}
        return {"value": "OK"}


class TestConversationLog(TmpBase):
    """会话日志：提示词与回复全文按会话拆分留存，并可检索。

    R373 返修后钉住的是**契约**，不是实现细节：
      - 提示词必须在发送前落盘，落不了盘就不许发（R373-02 A）
      - 回复状态必须区分 captured/empty/unavailable/partial/quarantined/unrecorded
      - 未知身份不得合并成一个桶（R373-03）
      - 同会话并发追加不得交错（R373-04）
      - 日志正文必须是**实际发送**的那份，不是事后重读文件（R373-07）
    """

    def _prompt(self, body, name="p.md"):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    def _sse(self, conv_id, reply):
        return "\n".join([
            'data: {"conversation_id": "%s", "message": {"author": {"role": "assistant"},'
            ' "content": {"parts": ["%s"]}}}' % (conv_id, reply),
            "data: [DONE]",
        ])

    # ---------- 基本留存 ----------

    def test_prompt_and_reply_are_logged_per_conversation(self):
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "回复正文ABC"))
        body = "提示词正文XYZ"
        status, path, conv = m.send_prompt(api, cid, self._prompt(body))
        self.assertEqual(status, 200)

        recs = list(m.iter_turns(cid))
        self.assertEqual(len(recs), 1, "一轮 = 一行合并结果")
        r = recs[0]
        self.assertEqual(r["conv_id"], cid)
        self.assertEqual(r["prompt"], body, "提示词全文必须留存")
        self.assertEqual(r["reply"], "回复正文ABC", "回复全文必须留存")
        self.assertEqual(r["prompt_sha256"],
                         hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(r["reply_sha256"],
                         hashlib.sha256("回复正文ABC".encode("utf-8")).hexdigest())
        self.assertEqual(r["reply_state"], "captured")
        self.assertEqual(r["status"], 200)

    def test_new_chat_migrates_from_pending_to_real_id(self):
        """新会话先落 pending，拿到真实 id 后整轮迁移，pending 不留残骸（R373-03）。"""
        real = "9f8e7d6c-1111-4222-8333-444455556666"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(real, "hi"))
        status, path, conv = m.send_prompt(api, None, self._prompt("新会话提示词"),
                                          new_chat=True)
        self.assertEqual(status, 200)
        recs = list(m.iter_turns(real))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["prompt"], "新会话提示词")
        self.assertEqual(recs[0]["reply"], "hi")
        # pending 里必须已经没有这一轮
        pending = []
        try:
            pending = [n for n in os.listdir(m.log_pending_dir()) if n.endswith(".jsonl")]
        except OSError:
            pass
        self.assertEqual(pending, [], "迁移后 pending 不得留残骸")

    # ---------- R373-02：提示词先落盘 ----------

    def test_send_refused_when_prompt_cannot_be_persisted(self):
        """提示词落不了盘时**不许发送**——否则内容已被使用却无任何记录。"""
        cid = "22222222-3333-4444-8555-666666666666"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "不该发生"))
        blocker = os.path.join(self.tmp, "logs")
        with open(blocker, "w", encoding="utf-8") as f:   # 用文件占住 logs 目录
            f.write("x")
        with self.assertRaises(Exception):
            m.send_prompt(api, cid, self._prompt("这条不许发出去"))
        self.assertEqual(api.sent, [], "提示词没落盘就绝不能发送")

    def test_reply_log_failure_is_visible_not_silent(self):
        """回复补写失败时：投递结果照常返回，但该轮必须表现为未完成。"""
        cid = "33333333-4444-4555-8666-777777777777"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "有回复"))
        status, path, conv = m.send_prompt(api, cid, self._prompt("提示词"))

        # 删掉 begin 里的 reply_state，并把 end 行抹掉，模拟「end 没写成」
        log = os.path.join(m.log_dir(), cid + ".jsonl")
        lines = [l for l in open(log, encoding="utf-8") if l.strip()]
        self.assertEqual(len(lines), 2, "应当是 begin + end 两行")
        with open(log, "w", encoding="utf-8") as f:
            f.write(lines[0])          # 只留 begin
        recs = list(m.iter_turns(cid))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["reply_state"], "unrecorded",
                         "只有 begin 时必须显式标记未完成")
        self.assertIsNone(recs[0]["status"], "未完成轮次不得有成功状态")
        self.assertEqual(recs[0]["prompt"], "提示词", "提示词仍在，可追溯")

    # ---------- R373-05：回复状态 ----------

    def test_reply_state_distinguishes_empty_from_unavailable(self):
        """200 但空正文 = empty；413 = unavailable。两者绝不能都记成空串。"""
        cid_ok = "44444444-5555-4666-8777-888888888888"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid_ok, ""))
        m.send_prompt(api, cid_ok, self._prompt("空回复场景"))
        self.assertEqual(list(m.iter_turns(cid_ok))[0]["reply_state"], "empty")

        api2 = FakeAPI(os.path.join(self.tmp, "auth"), send_status=413)
        st, _p, _c = m.send_prompt(api2, cid_ok, self._prompt("413 场景", name="q.md"))
        self.assertEqual(st, 413)
        recs = list(m.iter_turns(cid_ok))
        self.assertEqual(recs[0]["reply_state"], "unavailable", "413 不得记成 empty")
        self.assertEqual(recs[0]["status"], 413)

    def test_partial_reply_is_marked_partial(self):
        """收割不完整不得冒充 captured——否则下游会把残缺当定稿。"""
        cid = "55555555-6666-4777-8888-999999999999"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "半截"))
        real = api.extract_reply_sse_checked
        api.extract_reply_sse_checked = lambda raw: (real(raw)[0], False, "no_done")
        api.get_status = 500      # 让 GET 补全走不通，保持不完整
        m.send_prompt(api, cid, self._prompt("部分流"))
        self.assertEqual(list(m.iter_turns(cid))[0]["reply_state"], "partial")

    # ---------- R373-03：未知身份不合并 ----------

    def test_invalid_conv_id_goes_to_its_own_file(self):
        """两个不同的非法 id 不能被合并成同一个桶，原始标识要保留。"""
        with self.assertRaises(ValueError):
            m.conversation_log_path("../../evil")
        p1 = m.unassigned_log_path("../../evil-a")
        p2 = m.unassigned_log_path("../../evil-b")
        self.assertNotEqual(p1, p2, "不同原始标识必须落到不同文件")
        for p in (p1, p2):
            self.assertTrue(p.startswith(m.log_unassigned_dir()), "不得逃出日志目录")
            self.assertNotIn("..", os.path.basename(p))

    # ---------- R373-04：并发追加 ----------

    def test_concurrent_appends_are_not_interleaved(self):
        """同会话并发追加：每条都是完整可解析的 JSON 行，不交错不丢。"""
        cid = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        path = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        big = "长" * 2000          # 单行远超一次 write 的典型缓冲
        def worker(n):
            for i in range(10):
                m._log_write(path, {"turn_id": "%s-%d" % (n, i), "phase": "begin",
                                    "conv_id": cid, "prompt": big, "seq": i})
        ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        recs = m._read_jsonl(path)
        self.assertEqual(len(recs), 40, "40 条不得丢")
        self.assertEqual(len({r["turn_id"] for r in recs}), 40, "不得重复")
        for r in recs:
            self.assertEqual(r["prompt"], big, "正文不得被截断或交错")

    # ---------- R373-07：提示词快照 ----------

    def test_logged_prompt_is_the_sent_text_not_reread(self):
        """日志正文必须是**发送那一刻**的快照，不是事后重读文件。"""
        cid = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "ok"))
        pf = self._prompt("原始正文AAA")

        # 关键：在**发送过程中**改写并删除原文件，模拟长耗时投递期间被改。
        # 若实现是「发送后重新读文件补日志」，这里就会记到 B 内容或记空。
        real_send = api.send
        def send_and_mutate(*a, **k):
            with open(pf, "w", encoding="utf-8") as f:
                f.write("发送途中被改成了BBB")
            os.remove(pf)
            return real_send(*a, **k)
        api.send = send_and_mutate

        m.send_prompt(api, cid, pf)
        rec = list(m.iter_turns(cid))[0]
        self.assertEqual(rec["prompt"], "原始正文AAA", "日志必须是发送时的快照")
        self.assertEqual(rec["prompt_chars"], len("原始正文AAA"))
        self.assertEqual(api.sent[0]["text"], "原始正文AAA")

    # ---------- 异常留痕 ----------

    def test_exception_still_leaves_a_trace(self):
        cid = "88888888-9999-4aaa-8bbb-cccccccccccc"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "y"))
        api.requirements = lambda: (None, None)
        with self.assertRaises(Exception):
            m.send_prompt(api, cid, self._prompt("异常前提示词"))
        recs = list(m.iter_turns(cid))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["prompt"], "异常前提示词")
        self.assertIsNone(recs[0]["status"])
        self.assertEqual(recs[0]["reply_state"], "unavailable")
        self.assertTrue(recs[0]["error"])

    # ---------- 检索 ----------

    def test_search_scoped_and_global(self):
        cid1 = "aaaaaaaa-1111-4111-8111-111111111111"
        cid2 = "bbbbbbbb-2222-4222-8222-222222222222"
        for cid, ptext, rtext in ((cid1, "关键词在提示词里", "无关回复"),
                                  (cid2, "别的提示词", "关键词在回复里")):
            api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, rtext))
            m.send_prompt(api, cid, self._prompt(ptext, name=cid + ".md"))

        all_hits = m.search_logs("关键词")
        self.assertEqual({h["conv_id"] for h in all_hits}, {cid1, cid2},
                         "全局检索要同时命中提示词侧与回复侧")
        only = m.search_logs("关键词", conv_id=cid1)
        self.assertEqual([h["conv_id"] for h in only], [cid1], "--conv 必须限定作用域")

    def test_search_is_case_insensitive_and_limits(self):
        cid = "cccccccc-3333-4333-8333-333333333333"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "TokenAlpha"))
        m.send_prompt(api, cid, self._prompt("tokenalpha 在提示词"))
        self.assertEqual(len(m.search_logs("TOKENALPHA")), 1)
        self.assertEqual(len(m.search_logs("tokenalpha", limit=0)), 1)

    def test_legacy_single_line_record_still_readable(self):
        """升级前写下的单行记录（无 phase/turn_id）必须仍能读出，且状态不误报。"""
        cid = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
        d = m.log_dir()
        os.makedirs(d, exist_ok=True)
        legacy = {"ts": "2026-11-23T21:57:02", "conv_id": cid, "target_conv": cid,
                  "account": "acct6", "prompt_file": "old.md",
                  "prompt": "旧格式提示词", "prompt_chars": 7, "prompt_sha256": "",
                  "reply": "旧格式回复", "reply_chars": 6, "reply_sha256": "",
                  "reply_file": "replies/x.md", "status": 200, "error": ""}
        with open(os.path.join(d, cid + ".jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy, ensure_ascii=False) + "\n")
        recs = list(m.iter_turns(cid))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["prompt"], "旧格式提示词")
        self.assertEqual(recs[0]["reply"], "旧格式回复")
        self.assertEqual(recs[0]["reply_state"], "captured",
                         "历史成功轮次不得被误标成 unrecorded")

    def test_corrupt_log_line_does_not_break_retrieval(self):
        cid = "dddddddd-4444-4444-8444-444444444444"
        d = m.log_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, cid + ".jsonl"), "w", encoding="utf-8") as f:
            f.write('{"turn_id": "t1", "phase": "begin", "conv_id": "%s", "prompt": "好行"}\n' % cid)
            f.write("{半截坏行\n")
            f.write("\n")
        recs = list(m.iter_turns(cid))
        self.assertEqual(len(recs), 1, "坏行必须被跳过")
        self.assertEqual(recs[0]["prompt"], "好行")

    # ---------- P1 通道 ----------

    def test_p1_channel_has_logging_hooks(self):
        """P1（continue_full.py）必须接入同一套日志——它曾是零留存的漏记通道。"""
        p1 = os.path.join(HERE, "..", "scripts", "continue_full.py")
        src = _read(p1)
        self.assertIn("log_begin", src, "P1 发送前必须固化提示词")
        self.assertIn("log_end", src, "P1 必须补写回复与终态")
        self.assertNotIn("lines = [l for l in prompt.splitlines()", src,
                         "P1 不得再按注释行删正文（会静默截断）")

    # ---------- R374-01：P1 DOM 证据的归属与完整性 ----------

    def test_p1_old_reply_cannot_pass_as_this_turn(self):
        """旧回复留在 DOM 里、本轮没有新增时，不得把旧回复记成本轮的 captured。"""
        p1 = _load_p1()
        # 基线 3 条；发送后仍是 3 条（本轮没产生新消息）
        c = _FakeCdp([3, 3])
        text, state = p1.reply_after(c, {"count": 3, "last": "旧回复", "streaming": False,
                                        "hasError": False}, settle_tries=2, settle_gap=0)
        self.assertIsNone(text, "不得取旧回复")
        self.assertEqual(state, "unavailable", "无法证明属于本轮 → unavailable，绝不是 captured")

    def test_p1_streaming_reply_is_partial_not_captured(self):
        """本轮回复已出现但仍在流式生成：必须记 partial，不能冒充完整。"""
        p1 = _load_p1()
        c = _FakeCdp([4])          # 新增了 1 条，但 streaming=True 一直不结束
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                        "hasError": False}, settle_tries=2, settle_gap=0)
        self.assertEqual(state, "partial")
        self.assertEqual(text, "未完成的正文")

    def test_p1_empty_reply_confirmed_is_empty(self):
        """确认属于本轮、已完成、且正文为空 → empty（不是 unavailable）。"""
        p1 = _load_p1()
        c = _FakeCdp([4], last="", streaming=False)
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                        "hasError": False}, settle_tries=2, settle_gap=0)
        self.assertIsNone(text)
        self.assertEqual(state, "empty")

    def test_p1_confirmed_new_reply_is_captured(self):
        """新增且已完成 → captured。"""
        p1 = _load_p1()
        c = _FakeCdp([4], last="本轮回复", streaming=False)
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                        "hasError": False}, settle_tries=2, settle_gap=0)
        self.assertEqual((text, state), ("本轮回复", "captured"))

    def test_p1_no_baseline_means_unavailable(self):
        """取不到基线就无法证明归属，只能 unavailable。"""
        p1 = _load_p1()
        text, state = p1.reply_after(_FakeCdp([1]), None, settle_tries=1, settle_gap=0)
        self.assertIsNone(text)
        self.assertEqual(state, "unavailable")

    def test_p1_main_flow_takes_baseline_before_send(self):
        """P1 主流程必须在 fill/发送**之前**取基线，且判定要经过 reply_after。

        否则「取最后一条 assistant」的归属错误会在真实流程里复活——
        单测 reply_after 挡不住，因为漏的是调用点。
        """
        src = _read(os.path.join(HERE, "..", "scripts", "continue_full.py"))
        i_base = src.index("baseline = reply_snapshot(c)")
        i_send = src.index('print("send:", c.evaluate(JS_SEND)')
        self.assertLess(i_base, i_send, "基线必须在发送之前取")

        # 不能再有无基线的直接判定（旧写法：captured if rp else empty）
        self.assertNotIn('"captured" if rp else "empty"', src,
                         "必须用 reply_after 判定，不能按非空就 captured")
        self.assertIn("reply_after(c, baseline)", src, "判定必须传入基线")
        self.assertNotIn("last_reply_text(c)", src, "旧的取最后一条写法必须消失")

    # ---------- 独立审计发现的三项（P1-1 / P1-2 / P2-7）----------

    def test_pending_never_deleted_when_unreadable(self):
        """pending 读不出来时绝不能删——那是唯一副本。"""
        cid = "11112222-3333-4444-8555-666677778888"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-read-fail")
        with open(begin, "wb") as f:
            f.write(bytes([255, 254]) + b" not utf8 at all")   # 非法 UTF-8，读必失败
        m._migrate_pending("t-read-fail", begin, cid)
        self.assertTrue(os.path.exists(begin), "读失败必须保留 pending")

    def test_migration_conflict_returns_none(self):
        """冲突时返回 None，让调用方知道迁移未完成（不能返回看似成功的 target）。"""
        cid = "22223333-4444-4555-8666-777788889999"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-conflict")
        a = {"turn_id": "t-conflict", "phase": "begin", "conv_id": "",
             "prompt": "A 版本", "prompt_chars": 4, "status": None, "reply": ""}
        b = dict(a, prompt="B 版本")
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
            f.write(json.dumps(b, ensure_ascii=False) + "\n")
        self.assertIsNone(m._migrate_pending("t-conflict", begin, cid))
        self.assertTrue(os.path.exists(begin))

    def test_begin_migrated_before_end_written(self):
        """新会话：必须先迁 begin，再写 end，避免崩溃后同一轮被拆成两半。"""
        real = "33334444-5555-4666-8777-888899990000"
        api = FakeAPI(os.path.join(self.tmp, "auth"),
                      sse=self._sse(real, "回复内容"))
        m.send_prompt(api, None, self._prompt("新会话"), new_chat=True)
        path = m.conversation_log_path(real)
        phases = [r.get("phase") for r in m._read_jsonl(path)]
        self.assertEqual(phases, ["begin", "end"], "目标文件里 begin 必须在 end 之前")
        self.assertFalse(os.path.exists(m.pending_log_path("x")), "不应残留暂存")
        self.assertEqual(len(list(m.iter_turns(real))), 1)

    def test_migration_failure_never_writes_end_alone(self):
        """迁移失败时不得只写 end——那会造出「有 end 无 begin」的残轮。"""
        cid = "44445555-6666-4777-8888-999900001111"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-half")
        a = {"turn_id": "t-half", "phase": "begin", "conv_id": "",
             "prompt": "A", "prompt_chars": 1, "status": None, "reply": ""}
        b = dict(a, prompt="B")          # 同键冲突 → 迁移必然返回 None
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
            f.write(json.dumps(b, ensure_ascii=False) + "\n")

        class _Api:
            account = "acct1"
            _last_reply_text = None
            reply_source = "api"
        m.log_end(_Api(), "t-half", begin, None, cid, 200, None, "captured")

        target = m.conversation_log_path(cid)
        phases = [r.get("phase") for r in m._read_jsonl(target)]
        self.assertNotIn("end", phases, "迁移失败时目标里绝不能单独出现 end")
        self.assertTrue(os.path.exists(begin), "begin 与 end 都应留在 pending")

    # ---------- R376 崩溃注入（审查要求：不能只靠总量通过）----------

    def test_crash_after_begin_written_before_pending_deleted(self):
        """begin 已进目标、pending 还没删时崩溃：重试必须补齐且不重复。"""
        cid = "aaaabbbb-cccc-4ddd-8eee-ffff00001111"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-crash1")
        rows = [
            {"turn_id": "t-crash1", "phase": "begin", "conv_id": "", "prompt": "P",
             "prompt_chars": 1, "status": None, "reply": ""},
            {"turn_id": "t-crash1", "phase": "end", "conv_id": "", "reply": "R",
             "reply_chars": 1, "reply_state": "captured", "status": 200},
        ]
        with open(begin, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            # 真实迁移写入时会把 conv_id 改写成目标会话 id，预置数据必须一致，
            # 否则模拟的就不是"begin 已进目标"这个状态（R378-01 会正确拒绝）。
            f.write(json.dumps(dict(rows[0], conv_id=cid), ensure_ascii=False) + "\n")

        m._migrate_pending("t-crash1", begin, cid)   # 模拟恢复
        got = m._read_jsonl(target)
        self.assertEqual([r.get("phase") for r in got], ["begin", "end"])
        self.assertFalse(os.path.exists(begin), "补齐后才删 pending")

    def test_end_write_fails_pending_retained(self):
        """end 写入 pending 失败时不得删除任何东西，且不能留下半轮。"""
        cid = "bbbbcccc-dddd-4eee-8fff-000011112222"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-crash2")
        rec = {"turn_id": "t-crash2", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        real = m._log_write
        def boom(path, record):
            if record.get("phase") == "end":
                raise OSError("模拟 end 写失败")
            return real(path, record)
        m._log_write = boom
        try:
            class _A:
                account = "acct1"
                _last_reply_text = None
                reply_source = "api"
            out = m.log_end(_A(), "t-crash2", begin, None, cid, 200, None, "captured")
        finally:
            m._log_write = real
        self.assertIsNone(out, "end 写不进 pending 时必须报未完成")
        self.assertTrue(os.path.exists(begin), "pending 必须保留")
        self.assertFalse(os.path.exists(m.conversation_log_path(cid)),
                         "不得在目标里留下任何残缺行")

    def test_target_unicode_error_keeps_pending(self):
        """target 编码坏（读不出来）时必须中止迁移并保留 pending（R376-04）。"""
        cid = "ccccdddd-eeee-4fff-8000-111122223333"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-crash3")
        rec = {"turn_id": "t-crash3", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(bytes([255, 254]) + b"broken")
        self.assertIsNone(m._migrate_pending("t-crash3", begin, cid))
        self.assertTrue(os.path.exists(begin), "target 读不出来时 pending 必须保留")

    def test_pending_unicode_error_keeps_pending(self):
        """pending 编码坏时必须保留（已覆盖，这里钉住返回 None）。"""
        cid = "ddddeeee-ffff-4000-8111-222233334444"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-crash4")
        with open(begin, "wb") as f:
            f.write(bytes([255, 254]) + b"broken")
        self.assertIsNone(m._migrate_pending("t-crash4", begin, cid))
        self.assertTrue(os.path.exists(begin))

    def test_long_body_with_error_word_is_not_error(self):
        """500 字正文里提到"网络错误"（在讨论它）不算错误。"""
        p1 = _load_p1()
        body = ("网络错误" * 100) + "这一段是在讨论该错误，不是真的出错"
        c = _FakeCdp([], frames=[
            {"count": 4, "last": body, "streaming": False, "hasError": False},
            {"count": 4, "last": body, "streaming": False, "hasError": False},
        ])
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                       "hasError": False}, settle_tries=2, settle_gap=0)
        self.assertEqual(state, "captured", "长正文不应因含错误词被判错")

    def test_structural_error_with_long_body_is_error(self):
        """长正文 + 真实结构性错误：不得因正文长而漏判（R376-01 反例）。"""
        p1 = _load_p1()
        c = _FakeCdp([], frames=[
            {"count": 4, "last": "很长很长的正文" * 50, "streaming": False,
             "alert_text": "Something went wrong"},
        ])
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                       "hasError": False}, settle_tries=3, settle_gap=0)
        self.assertEqual(state, "partial", "结构性错误不得被长正文屏蔽")

    def test_no_new_message_ever_returns_unavailable(self):
        """连续采样始终没有新增消息：必须 unavailable，且不能 NameError（R376-02）。"""
        p1 = _load_p1()
        c = _FakeCdp([3])        # 条数一直是基线
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                       "hasError": False}, settle_tries=3, settle_gap=0)
        self.assertIsNone(text)
        self.assertEqual(state, "unavailable")

    def test_unreadable_target_gets_no_appended_bytes(self):
        """target 读不出来时绝不能往里追加——那会把坏文件进一步弄乱（R376-04）。"""
        cid = "11113333-2222-4444-8555-666677778888"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-ro")
        rec = {"turn_id": "t-ro", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        bad = bytes([255, 254]) + b"broken"
        with open(target, "wb") as f:
            f.write(bad)
        m._migrate_pending("t-ro", begin, cid)
        with open(target, "rb") as f:
            after = f.read()
        self.assertEqual(after, bad, "读不出来时不得追加任何字节")

    def test_silently_lost_write_keeps_pending(self):
        """end 写入静默丢失（不抛错但不落盘）：回读校验必须挡住，pending 保留。"""
        cid = "22224444-3333-4555-8666-777788889999"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.log_pending_dir() + "/t-lost"
        begin = m.pending_log_path("t-lost")
        rows = [
            {"turn_id": "t-lost", "phase": "begin", "conv_id": "", "prompt": "P",
             "prompt_chars": 1, "status": None, "reply": ""},
            {"turn_id": "t-lost", "phase": "end", "conv_id": "", "reply": "R",
             "reply_chars": 1, "reply_state": "captured", "status": 200},
        ]
        with open(begin, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        real = m._log_write
        def swallow(path, record):
            if record.get("phase") == "end" and path != begin:
                return path      # 静默不写：既不抛错也不落盘
            return real(path, record)
        m._log_write = swallow
        try:
            out = m._migrate_pending("t-lost", begin, cid)
        finally:
            m._log_write = real
        self.assertIsNone(out, "回读发现缺行时必须报未完成")
        self.assertTrue(os.path.exists(begin), "end 没真正落盘时 pending 必须保留")

    # ---------- R376-01：judge_error 是纯函数，直接单测 ----------

    def test_judge_error_structural_independent_of_length(self):
        """结构性错误不受正文长度影响（R376-01 的核心反例）。"""
        p1 = _load_p1()
        long_body = "很长正文" * 200
        has, struct, text = p1.judge_error(long_body, "Something went wrong")
        self.assertTrue(has, "长正文必须不能屏蔽结构性错误")
        self.assertTrue(struct)

    def test_judge_error_long_body_mentioning_error_is_not_error(self):
        """长正文里提到"网络错误"（在讨论它）不算错。"""
        p1 = _load_p1()
        body = ("网络错误" * 5) + "这一段是在讨论该错误，并不是真的出错，请继续"
        has, struct, _text = p1.judge_error(body, "")
        self.assertFalse(has, "长正文提及错误词不得判错")
        self.assertFalse(struct)

    def test_judge_error_short_error_sentence_is_error(self):
        """整条正文就是一句错误提示 → 判错。"""
        p1 = _load_p1()
        has, _s, text = p1.judge_error("Something went wrong.", "")
        self.assertTrue(has)
        self.assertTrue(text)

    def test_judge_error_clean_reply_is_clean(self):
        """正常回复不误判。"""
        p1 = _load_p1()
        has, struct, text = p1.judge_error("这是正常回复", "")
        self.assertFalse(has)
        self.assertFalse(struct)
        self.assertFalse(text)

    def test_verify_catches_silent_corruption_during_write(self):
        """回读必须能发现"写进去的内容被悄悄改坏"——这是校验层的独立职责。

        前置的冲突检查只比对迁移前的内容；若写入环节本身损坏，只有回读校验能抓。
        """
        cid = "88889999-aaaa-4bbb-8ccc-ddddeeeeffff"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-corrupt")
        rec = {"turn_id": "t-corrupt", "phase": "begin", "conv_id": "",
               "prompt": "原始正文", "prompt_chars": 4, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        real = m._log_write
        def corrupt(path, record):
            bad = dict(record)
            if bad.get("phase") == "begin" and "prompt" in bad:
                bad["prompt"] = "被写坏的正文"      # 静默篡改，不抛错
            return real(path, bad)
        m._log_write = corrupt
        try:
            out = m._migrate_pending("t-corrupt", begin, cid)
        finally:
            m._log_write = real
        self.assertIsNone(out, "回读必须发现内容不一致")
        self.assertTrue(os.path.exists(begin), "内容不一致时 pending 必须保留")

    def test_verify_catches_conflicting_duplicates_when_source_matches_one(self):
        """目标有同键冲突，且 source 正好等于其中一条 → 冲突检查放行，回读必须拦下。"""
        cid = "9999aaaa-bbbb-4ccc-8ddd-eeeeffff0000"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-dup3")
        rec = {"turn_id": "t-dup3", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            # 三行同键：首行与末行都等于 source，中间夹一条坏值。
            #  - 前置冲突检查取**末行**（dict 覆盖）= source → 放行
            #  - 回读的 payload 校验取**首行**（setdefault）= source → 放行
            #  - 只有"重复键内部一致性检查"能发现中间那条坏值
            f.write(json.dumps(dict(rec, conv_id=cid), ensure_ascii=False) + "\n")
            f.write(json.dumps(dict(rec, conv_id=cid, prompt="中间的坏值"), ensure_ascii=False) + "\n")
            f.write(json.dumps(dict(rec, conv_id=cid), ensure_ascii=False) + "\n")
        out = m._migrate_pending("t-dup3", begin, cid)
        self.assertIsNone(out, "目标存在互相冲突的重复键，不得删 pending")
        self.assertTrue(os.path.exists(begin))

    def test_same_key_same_payload_different_conv_id_rejected(self):
        """同 turn_id + 同 payload 但 conv_id 不同：不得当成一致（R378-01 核心）。"""
        cid = "aaaabbbb-1111-4222-8333-444455556666"
        other = "ccccdddd-2222-4333-8444-555566667777"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-idconf")
        rec = {"turn_id": "t-idconf", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # 目标里已有同键、同 payload，但归属另一个会话
        with open(target, "w", encoding="utf-8") as f:
            f.write(json.dumps(dict(rec, conv_id=other), ensure_ascii=False) + "\n")
        out = m._migrate_pending("t-idconf", begin, cid)
        self.assertIsNone(out, "conv_id 不符必须拒绝，不能凭 payload 相同就认作一致")
        self.assertTrue(os.path.exists(begin), "pending 必须保留")

    def test_target_conv_id_mismatch_rejected(self):
        """目标记录的 conv_id 必须是本次迁移的预期 id。"""
        cid = "bbbbcccc-3333-4444-8555-666677778888"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-idmis")
        rec = {"turn_id": "t-idmis", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            # 内容完全相同，但挂在错误的会话下
            f.write(json.dumps(dict(rec, conv_id="dddddddd-4444-4555-8666-777788889999"),
                               ensure_ascii=False) + "\n")
        self.assertIsNone(m._migrate_pending("t-idmis", begin, cid))
        self.assertTrue(os.path.exists(begin))

    def test_legacy_empty_conv_id_cannot_satisfy_migrated_source_identity(self):
        """空 conv_id 的历史记录不得作为本次迁移的凭据（R379 唯一阻断）。

        构造：target 同键、同 payload、但 conv_id 为空。
        内容完全一致，只有身份缺失——必须拒绝。
        """
        cid = "11112222-3333-4444-8555-666677778888"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-emptyid")
        rec = {"turn_id": "t-emptyid", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            # 同键、同 payload，但没有身份
            f.write(json.dumps(dict(rec, conv_id=""), ensure_ascii=False) + "\n")
        out = m._migrate_pending("t-emptyid", begin, cid)
        self.assertIsNone(out, "空 conv_id 不得冒充迁移凭据")
        self.assertTrue(os.path.exists(begin), "pending 必须保留")

    def test_empty_and_correct_id_both_present_prefers_correct(self):
        """同键既有空身份记录、也有带正确 cid 的记录 → 应选中正确的那条并成功。"""
        cid = "22223333-4444-4555-8666-777788889999"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-mixed")
        rec = {"turn_id": "t-mixed", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(json.dumps(dict(rec, conv_id=""), ensure_ascii=False) + "\n")   # 空身份在前
            f.write(json.dumps(dict(rec, conv_id=cid), ensure_ascii=False) + "\n")  # 正确身份在后
        out = m._migrate_pending("t-mixed", begin, cid)
        self.assertIsNotNone(out, "有正确 cid 的记录时应成功，不能被前面的空身份记录挡住")
        self.assertFalse(os.path.exists(begin), "成功后应清理 pending")

    def test_duplicate_order_reversal_consistent(self):
        """同键记录顺序反转，结果必须一致（不得依赖遍历顺序）。"""
        for order in ("empty_first", "correct_first"):
            cid = ("33334444-5555-4666-8777-88889999000%d" % (0 if order == "empty_first" else 1))
            os.makedirs(m.log_pending_dir(), exist_ok=True)
            os.makedirs(m.log_dir(), exist_ok=True)
            tid = "t-order-" + order
            begin = m.pending_log_path(tid)
            rec = {"turn_id": tid, "phase": "begin", "conv_id": "",
                   "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
            with open(begin, "w", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            target = m.conversation_log_path(cid)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            rows = [dict(rec, conv_id=""), dict(rec, conv_id=cid)]
            if order == "correct_first":
                rows.reverse()
            with open(target, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            out = m._migrate_pending(tid, begin, cid)
            self.assertIsNotNone(out, "顺序不应影响结果：%s" % order)

    # ---------- R381-01：新会话投递失败必须保住 prompt ----------

    def test_new_chat_live_tail_failure_preserves_prompt(self):
        """新会话投递异常时，begin+end 必须同留在 pending，prompt 原文可读。

        这是 R381 确认的生产缺陷：旧实现把 end 写进 unassigned、再删掉 begin
        （唯一一份 prompt），多轮提示词已因此永久丢失（见 docs/incidents/）。
        """
        prompt = "这段提示词绝不能丢"
        api = FakeAPI(os.path.join(self.tmp, "auth"))
        # 新会话路径不走 live_tail；用 requirements 失败注入真实的投递中断
        api.requirements = lambda: (_ for _ in ()).throw(
            RuntimeError("模拟 PoW/requirements 中断"))

        with self.assertRaises(RuntimeError):
            m.send_prompt(api, None, self._prompt(prompt), new_chat=True)

        pend = [n for n in os.listdir(m.log_pending_dir()) if n.endswith(".jsonl")]
        self.assertEqual(len(pend), 1, "整轮必须留在 pending")
        begin_path = os.path.join(m.log_pending_dir(), pend[0])
        rows = m._read_jsonl(begin_path)
        self.assertEqual([r["phase"] for r in rows], ["begin", "end"])
        self.assertEqual(rows[0]["prompt"], prompt, "prompt 必须原文完整")
        self.assertEqual(rows[1]["reply_state"], "unavailable")
        self.assertEqual(rows[1]["conv_id"], "", "无 cid 时不得编造身份")
        self.assertTrue(os.path.exists(begin_path), "begin 不得被删")
        un = []
        try:
            un = [n for n in os.listdir(m.log_unassigned_dir()) if n.endswith(".jsonl")]
        except OSError:
            pass
        self.assertEqual(un, [], "不得再产生只有 end 的 unassigned 空壳")

    def test_end_append_failure_keeps_prompt(self):
        """end 追加进 pending 失败时，原 begin 仍必须存在、prompt 可读。"""
        prompt = "追加失败也不许丢"
        api = FakeAPI(os.path.join(self.tmp, "auth"))
        api.requirements = lambda: (_ for _ in ()).throw(RuntimeError("中断"))

        real = m._log_write
        def fail_end(path, record):
            if record.get("phase") == "end":
                raise OSError("模拟 end 写失败")
            return real(path, record)
        m._log_write = fail_end
        try:
            with self.assertRaises(Exception):
                m.send_prompt(api, None, self._prompt(prompt), new_chat=True)
        finally:
            m._log_write = real

        pend = [n for n in os.listdir(m.log_pending_dir()) if n.endswith(".jsonl")]
        self.assertEqual(len(pend), 1)
        rows = m._read_jsonl(os.path.join(m.log_pending_dir(), pend[0]))
        self.assertEqual([r["phase"] for r in rows], ["begin"], "只有 begin 也合法")
        self.assertEqual(rows[0]["prompt"], prompt, "prompt 必须还在")

    def test_existing_conv_failure_keeps_identity_behavior(self):
        """已有会话投递异常：仍按目标 cid 归属，不进 pending（行为不变）。"""
        cid = "aaa11111-2222-4333-8444-555566667777"
        api = FakeAPI(os.path.join(self.tmp, "auth"))
        def boom(*a, **k):
            raise RuntimeError("中断")
        api.live_tail = boom
        with self.assertRaises(RuntimeError):
            m.send_prompt(api, cid, self._prompt("既有会话"))
        recs = list(m.iter_turns(cid))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["conv_id"], cid)
        self.assertEqual(recs[0]["reply_state"], "unavailable")
        self.assertEqual(recs[0]["prompt"], "既有会话")

    # ---------- R381-02：_safe_conv_id 三态 ----------

    def test_safe_conv_id_three_states(self):
        """""=历史缺失 / 原值=合法 / None=畸形，三者必须可区分（R382-01 严格 UUID）。"""
        self.assertEqual(m._safe_conv_id(None), "")
        self.assertEqual(m._safe_conv_id(""), "")
        good = "9f8e7d6c-1111-4222-8333-444455556666"
        self.assertEqual(m._safe_conv_id(good), good)
        # 必须是规范的小写、带连字符 UUID
        self.assertIsNone(m._safe_conv_id(good.upper()), "大写 UUID 不是可信身份")
        self.assertIsNone(m._safe_conv_id(good.replace("-", "")), "无连字符不是 UUID")
        self.assertIsNone(m._safe_conv_id("abc"), "宽松标签不是会话身份")
        for bad in ("abc/../x", "a" * 200, "   ", "../evil", "a b"):
            self.assertIsNone(m._safe_conv_id(bad), bad)
        for bad in (123, [], {}, True):
            self.assertIsNone(m._safe_conv_id(bad), bad)

    def test_log_files_malformed_id_has_no_path_traversal(self):
        """畸形 id 的只读查找必须落在 unassigned 目录内（R382-02）。"""
        base = os.path.abspath(m.log_unassigned_dir())
        os.makedirs(m.log_unassigned_dir(), exist_ok=True)
        pend = os.path.abspath(m.log_pending_dir())
        for bad in ("../../evil", "../x", "a/b", "..", "x" * 300):
            for path, root in ((m.unassigned_log_path(bad), base),
                               (m.pending_log_path(bad), pend)):
                actual = os.path.abspath(path)
                self.assertEqual(os.path.commonpath([root, actual]), root,
                                 "畸形 id 不得逃出日志目录：%r -> %r" % (bad, path))
                self.assertNotIn("..", actual)

    def test_malformed_conv_id_rejected_at_migration_entry(self):
        """畸形 cid 进迁移入口必须立即拒绝，不能当历史空值。"""
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-mal")
        rec = {"turn_id": "t-mal", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.assertIsNone(m._migrate_pending("t-mal", begin, "abc/../x"))
        self.assertTrue(os.path.exists(begin), "畸形 id 不许动 pending")

    def test_malformed_identity_in_target_record_rejected(self):
        """目标记录身份畸形 → 拒绝迁移（不能当历史空值放行）。"""
        cid = "bbb22222-3333-4444-8555-666677778888"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-mal2")
        rec = {"turn_id": "t-mal2", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(json.dumps(dict(rec, conv_id="abc/../x"), ensure_ascii=False) + "\n")
        self.assertIsNone(m._migrate_pending("t-mal2", begin, cid))
        self.assertTrue(os.path.exists(begin))

    def test_migration_entry_distinguishes_malformed_from_missing(self):
        """畸形与缺失必须给出不同诊断（Owner §3.3：两种错误必须区分）。"""
        import contextlib
        import io as _io
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-diag")
        rec = {"turn_id": "t-diag", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            m._migrate_pending("t-diag", begin, "abc/../x")
        malformed_msg = buf.getvalue()

        buf2 = _io.StringIO()
        with contextlib.redirect_stdout(buf2):
            m._migrate_pending("t-diag", begin, "")
        missing_msg = buf2.getvalue()

        self.assertIn("畸形", malformed_msg, "畸形身份必须报畸形")
        self.assertIn("缺失", missing_msg, "缺失身份必须报缺失")
        self.assertNotEqual(malformed_msg, missing_msg, "两种错误不得同一句文案")

    def test_log_end_never_deletes_begin_left_in_conversations(self):
        """即使 begin 落在 conversations（非 pending），无 cid 时也不得删它。

        当前调用方走不到这条路径，但「删掉含 prompt 的文件」是不该存在的兜底行为；
        一旦将来有新调用方，删的是整个会话文件。
        """
        cid = "ccc33333-4444-4555-8666-777788889999"
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.conversation_log_path(cid)
        rec = {"turn_id": "t-keep", "phase": "begin", "conv_id": cid,
               "prompt": "不许删我", "prompt_chars": 5, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        class _Api:
            account = "acct1"
            _last_reply_text = None
            reply_source = "api"
        # 目标与落点都为空 → 走无 cid 分支
        m.log_end(_Api(), "t-keep", begin, None, None, None, None, "unavailable")
        self.assertTrue(os.path.exists(begin), "不得删除含 prompt 的 begin 文件")
        rows = m._read_jsonl(begin)
        self.assertEqual(rows[0]["prompt"], "不许删我")

    # ---------- R381-06：Bidi 控制符按码点拒绝 ----------

    def test_bidi_embedding_codepoints_rejected_individually(self):
        """U+202A–U+202E 逐个拒绝（逐码点，不是整类 Cf）。"""
        for cp in range(0x202A, 0x202F):
            with self.assertRaises(RuntimeError, msg="U+%04X 应被拒" % cp):
                m.validate_prompt_text("正常正文" + chr(cp) + "后续", "p.md")

    def test_bidi_isolate_codepoints_rejected_individually(self):
        """U+2066–U+2069 逐个拒绝。"""
        for cp in range(0x2066, 0x206A):
            with self.assertRaises(RuntimeError, msg="U+%04X 应被拒" % cp):
                m.validate_prompt_text("正常正文" + chr(cp) + "后续", "p.md")

    def test_benign_format_chars_are_kept(self):
        """ZWSP/ZWNJ/ZWJ/WJ/LRM/RLM/ALM 是正常排版字符，必须放行。"""
        keep = (0x200B, 0x200C, 0x200D, 0x2060, 0x200E, 0x200F, 0x061C)
        for cp in keep:
            m.validate_prompt_text("正常正文" + chr(cp) + "后续", "p.md")

    def test_bidi_error_message_names_codepoint_and_position(self):
        """错误提示必须报出码点与位置，便于定位（Owner 要求错误提示测试）。"""
        text = "前缀" + chr(0x202E) + "后缀"
        try:
            m.validate_prompt_text(text, "p.md")
            self.fail("应当抛出")
        except RuntimeError as e:
            msg = str(e)
            self.assertIn("U+202E", msg, "必须报出具体码点")
            self.assertIn("2", msg, "必须报出位置（0-based 第 2 位）")
            self.assertIn("p.md", msg)

    def test_multilingual_text_with_legit_format_chars_passes(self):
        """多语言正常文本（含 ZWJ 等）必须通过，不能误杀。"""
        samples = [
            "中文正文，含标点。「引号」【方括号】",
            "English body with emoji \U0001F600 and ZWJ \u200d inside",
            "العربية نص عادي",                 # 阿拉伯语
            "עברית טקסט רגיל",                 # 希伯来语
            "ไทย ข้อความปกติ",                 # 泰语
            "日本語のテキスト、改行あり\r\n二行目",
            "# Markdown\n\n- 列表\t`code`\n",
        ]
        for s in samples:
            m.validate_prompt_text(s, "p.md")

    # ---------- R388-A1：断头行隔离与损坏行告警 ----------

    def test_a1_truncated_tail_without_newline_then_append(self):
        """末尾残缺 JSON（无换行）→ 后续追加记录必须可读。"""
        cid = "1111aaaa-bbbb-4ccc-8ddd-eeee00001111"
        p = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"turn_id": "cut", "phase": "end", "reply": "x"}')   # 故意无换行
        m._log_write(p, {"turn_id": "t2", "phase": "begin", "conv_id": cid,
                         "prompt": "关键提示词", "prompt_chars": 5})
        rows = m._read_jsonl(p)
        self.assertTrue(any(r.get("turn_id") == "t2" for r in rows),
                        "追加记录必须可读，不能被拼进残行")
        self.assertEqual([r["prompt"] for r in rows if r.get("turn_id") == "t2"],
                         ["关键提示词"])

    def test_a1_truncated_tail_with_newline_then_append(self):
        """末尾残缺 JSON（有换行）→ 后续追加记录必须可读。"""
        cid = "2222aaaa-bbbb-4ccc-8ddd-eeee00002222"
        p = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"turn_id": "cut2", "phase": "end"' + chr(10))
        m._log_write(p, {"turn_id": "t3", "phase": "begin", "conv_id": cid,
                         "prompt": "第二条", "prompt_chars": 3})
        rows = m._read_jsonl(p)
        self.assertTrue(any(r.get("turn_id") == "t3" for r in rows))

    def test_a1_middle_corrupt_line_does_not_hide_later_rows(self):
        """中间坏行不得让后续有效行消失。"""
        cid = "3333aaaa-bbbb-4ccc-8ddd-eeee00003333"
        p = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": "a", "phase": "begin", "prompt": "A"}) + chr(10))
            f.write("{坏行" + chr(10))
            f.write(json.dumps({"turn_id": "c", "phase": "begin", "prompt": "C"}) + chr(10))
        rows = m._read_jsonl(p)
        self.assertEqual(sorted(r.get("turn_id") for r in rows), ["a", "c"])

    def test_a1_truncated_tail_reported_to_stderr(self):
        """末尾未完成 JSON 必须在 stderr 明确报 truncated tail。"""
        import contextlib
        import io as _io
        cid = "4444aaaa-bbbb-4ccc-8ddd-eeee00004444"
        p = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": "ok", "phase": "begin"}) + chr(10))
            f.write('{"turn_id": "cut')
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            m._read_jsonl(p)
        self.assertIn("truncated tail", buf.getvalue())

    # ---------- R388-A2：跨文件按 turn_id 聚合 ----------

    def test_a2_pending_begin_not_masked_by_conversations_end(self):
        """conversations 只有 end、pending 有 begin+end → prompt 必须可见。"""
        cid = "5555aaaa-bbbb-4ccc-8ddd-eeee00005555"
        tid = "dupA2"
        os.makedirs(m.log_dir(), exist_ok=True)
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.conversation_log_path(cid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid,
                                "reply": "回复", "reply_state": "captured",
                                "status": 200}, ensure_ascii=False) + chr(10))
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "唯一副本的提示词", "prompt_chars": 8,
                                "status": None, "reply": ""}, ensure_ascii=False) + chr(10))
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": "",
                                "reply": "回复", "reply_state": "captured",
                                "status": 200}, ensure_ascii=False) + chr(10))
        turns = list(m.iter_turns())
        self.assertEqual(len(turns), 1, "同一 turn_id 只输出一轮")
        self.assertEqual(turns[0].get("prompt"), "唯一副本的提示词")
        self.assertEqual(turns[0].get("reply"), "回复")

    def test_a2_order_independent(self):
        """两文件顺序交换，聚合结果必须一致。"""
        for first in ("conv", "pending"):
            cid = ("6666aaaa-bbbb-4ccc-8ddd-eeee0000%s" %
                   ("6666" if first == "conv" else "7777"))
            tid = "dupA2" + first
            os.makedirs(m.log_dir(), exist_ok=True)
            os.makedirs(m.log_pending_dir(), exist_ok=True)
            conv = m.conversation_log_path(cid)
            pend = m.pending_log_path(tid)
            conv_rec = {"turn_id": tid, "phase": "end", "conv_id": cid,
                        "reply": "R", "reply_state": "captured", "status": 200}
            begin_rec = {"turn_id": tid, "phase": "begin", "conv_id": "",
                         "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
            for path, rec in ((conv, conv_rec), (pend, begin_rec)):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
            turns = [t for t in m.iter_turns() if t.get("turn_id") == tid]
            self.assertEqual(len(turns), 1, first)
            self.assertEqual(turns[0].get("prompt"), "P", first)
            self.assertEqual(turns[0].get("reply"), "R", first)

    def test_a2_pending_only_begin_is_unrecorded(self):
        """pending 只有 begin → reply_state 必须是 unrecorded。"""
        tid = "onlyBegin"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "P", "prompt_chars": 1, "status": None,
                                "reply": ""}, ensure_ascii=False) + chr(10))
        turns = [t for t in m.iter_turns() if t.get("turn_id") == tid]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["reply_state"], "unrecorded")
        self.assertIsNone(turns[0]["status"])

    def test_a2_three_places_same_turn_yields_one(self):
        """同一 turn_id 在三处重复 → 只输出一轮。"""
        cid = "7777aaaa-bbbb-4ccc-8ddd-eeee00007777"
        tid = "triple"
        for d in (m.log_dir(), m.log_pending_dir(), m.log_unassigned_dir()):
            os.makedirs(d, exist_ok=True)
        rec = {"turn_id": tid, "phase": "begin", "conv_id": cid,
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        for path in (m.conversation_log_path(cid), m.pending_log_path(tid),
                     m.unassigned_log_path(tid)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
        turns = [t for t in m.iter_turns() if t.get("turn_id") == tid]
        self.assertEqual(len(turns), 1)

    def test_a2_legacy_rows_without_turn_id_not_merged(self):
        """没有 turn_id 的历史记录不得被误合并。"""
        cid = "8888aaaa-bbbb-4ccc-8ddd-eeee00008888"
        p = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            # 真实旧格式单行记录是带 conv_id 的（只是没有 turn_id）
            f.write(json.dumps({"conv_id": cid, "prompt": "旧1", "reply": "r1",
                                "status": 200}, ensure_ascii=False) + chr(10))
            f.write(json.dumps({"conv_id": cid, "prompt": "旧2", "reply": "r2",
                                "status": 200}, ensure_ascii=False) + chr(10))
        turns = list(m.iter_turns(cid))
        self.assertEqual(len(turns), 2, "两条无 turn_id 记录必须各算一轮")
        self.assertEqual({t["prompt"] for t in turns}, {"旧1", "旧2"})

    # ---------- R388-A4：_turns_in 容错 ----------

    def test_a4_unreadable_file_does_not_crash_log_list(self):
        """不可读文件不得让 --log-list 崩溃（真正走 CLI 路径）。"""
        import contextlib
        import io as _io
        cid = "9999aaaa-bbbb-4ccc-8ddd-eeee00009999"
        p = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # 同时放一个正常文件，保证 _turns_in 会被调用到
        with open(m.conversation_log_path("aaaabbbb-cccc-4ddd-8eee-ffff11112222"),
                  "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": "ok1", "phase": "begin",
                                "conv_id": "aaaabbbb-cccc-4ddd-8eee-ffff11112222",
                                "prompt": "P"}, ensure_ascii=False) + chr(10))
        with open(p, "wb") as f:
            f.write(bytes([255, 254]) + b"broken")
        self.assertIsNone(m._read_jsonl(p), "读不出来必须是 None")

        old_argv = list(sys.argv)
        sys.argv = ["chatgpt_send.py", "--log-list"]
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = m.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 0, "--log-list 不得因不可读文件崩溃")
        self.assertTrue(buf.getvalue().strip(), "应有输出")

    def test_a4_empty_jsonl_returns_empty(self):
        """空 JSONL 正常返回空结果。"""
        cid = "aaaabbbb-cccc-4ddd-8eee-ffff00001111"
        p = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        self.assertEqual(m._read_jsonl(p), [])

    # ---------- R388-A6：按 turn_id 定位 pending ----------

    def test_a6_show_finds_pending_by_turn_id(self):
        tid = "locateMe"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "P", "prompt_chars": 1, "status": None,
                                "reply": ""}, ensure_ascii=False) + chr(10))
        turns = list(m.iter_turns(tid))
        self.assertEqual(len(turns), 1, "应能按 turn_id 定位 pending")
        self.assertEqual(turns[0]["prompt"], "P")

    def test_a6_show_finds_unassigned_by_turn_id(self):
        tid = "locateMe2"
        os.makedirs(m.log_unassigned_dir(), exist_ok=True)
        with open(m.unassigned_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": "",
                                "reply": "R", "reply_state": "unavailable",
                                "status": None}, ensure_ascii=False) + chr(10))
        turns = list(m.iter_turns(tid))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["reply_state"], "unavailable")

    def test_a6_unknown_turn_id_reports_empty(self):
        self.assertEqual(list(m.iter_turns("noSuchTurn")), [])

    def test_a6_malformed_turn_id_no_traversal(self):
        base = os.path.abspath(m.log_unassigned_dir())
        pend = os.path.abspath(m.log_pending_dir())
        for bad in ("../../evil", "../x", "..", "a/b"):
            for path, root in ((m.unassigned_log_path(bad), base),
                               (m.pending_log_path(bad), pend)):
                actual = os.path.abspath(path)
                self.assertEqual(os.path.commonpath([root, actual]), root,
                                 "%r -> %r 逃出日志目录" % (bad, path))


    # ---------- R388-A5：P1 终态写入失败必须影响退出码 ----------

    def _run_p1_main(self, log_end_impl, argv=None):
        """用假 CDP 跑 continue_full.main()，返回退出码。"""
        import contextlib
        import io as _io
        import types

        p1 = _load_p1()
        fake = _FakeCdpForMain()
        orig_client = p1.CDPClient
        orig_log_end = p1._cs.log_end
        orig_sleep = p1.time.sleep
        p1.CDPClient = lambda *a, **k: fake
        p1._cs.log_end = log_end_impl
        p1.time.sleep = lambda *_a, **_k: None
        old_argv = list(sys.argv)
        sys.argv = ["continue_full.py"] + (argv or [
            "9f8e7d6c-1111-4222-8333-444455556666", self._prompt("P1 正文")])
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = p1.main()
        finally:
            p1.CDPClient = orig_client
            p1._cs.log_end = orig_log_end
            p1.time.sleep = orig_sleep
            sys.argv = old_argv
        return code

    def test_a5_log_end_raises_returns_nonzero(self):
        """log_end 抛 OSError → main 必须非零退出。"""
        def boom(*a, **k):
            raise OSError("模拟终态写失败")
        code = self._run_p1_main(boom)
        self.assertNotEqual(code, 0, "终态留存失败不得报告成功")

    def test_a5_log_end_returns_none_returns_nonzero(self):
        """log_end 返回 None（未确认）→ main 必须非零退出。"""
        code = self._run_p1_main(lambda *a, **k: None)
        self.assertNotEqual(code, 0, "未确认终态不得报告成功")

    def test_a5_log_end_success_keeps_zero(self):
        """log_end 正常返回路径 → 既有成功路径保持 0。"""
        code = self._run_p1_main(lambda *a, **k: "/some/path.jsonl")
        self.assertEqual(code, 0)

    # ---------- R389-05：真正的文件顺序交换 ----------

    def test_r389_order_swap_compares_full_result(self):
        """真正交换 _log_files 的返回顺序，整体聚合结果必须完全一致。"""
        cid = "cccc1111-2222-4333-8444-555566667777"
        tid = "swapTurn"
        os.makedirs(m.log_dir(), exist_ok=True)
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        conv = m.conversation_log_path(cid)
        pend = m.pending_log_path(tid)
        with open(conv, "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid,
                                "reply": "R", "reply_chars": 1,
                                "reply_state": "captured", "status": 200},
                               ensure_ascii=False) + chr(10))
        with open(pend, "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "P", "prompt_chars": 1,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))

        real = m._all_log_files
        try:
            forward = list(m.iter_turns())
            m._all_log_files = lambda: list(reversed(real()))
            backward = list(m.iter_turns())
        finally:
            m._all_log_files = real

        def norm(turns):
            out = []
            for t in turns:
                d = dict(t)
                d.pop("_file", None)
                d.pop("_files", None)
                out.append(d)
            return sorted(out, key=lambda x: str(x.get("turn_id")))

        self.assertEqual(norm(forward), norm(backward),
                         "文件顺序不得影响聚合结果")
        self.assertEqual(len(forward), 1)
        self.assertEqual(forward[0]["prompt"], "P")
        self.assertEqual(forward[0]["reply"], "R")

    # ---------- R389-02/03：身份合并规则 ----------

    def test_r389_empty_conv_id_does_not_overwrite_valid(self):
        """begin 的 conv_id="" 不得覆盖 end 的有效 conv_id。"""
        cid = "dddd1111-2222-4333-8444-555566667777"
        tid = "idKeep"
        os.makedirs(m.log_dir(), exist_ok=True)
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.conversation_log_path(cid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid,
                                "reply": "R", "reply_state": "captured",
                                "status": 200}, ensure_ascii=False) + chr(10))
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "P", "prompt_chars": 1,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))
        turns = list(m.iter_turns(tid))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["conv_id"], cid, "空 conv_id 不能覆盖有效身份")
        self.assertNotIn("_conv_id_conflict", turns[0])

    def test_r389_conflicting_valid_conv_ids_is_visible(self):
        """两个不同有效 conv_id 必须打 WARN 标冲突，不静默择一。"""
        import contextlib
        import io as _io
        cid_a = "eeee1111-2222-4333-8444-555566667777"
        cid_b = "ffff1111-2222-4333-8444-555566667777"
        tid = "idClash"
        os.makedirs(m.log_dir(), exist_ok=True)
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.conversation_log_path(cid_a), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid_a,
                                "reply": "R", "reply_state": "captured",
                                "status": 200}, ensure_ascii=False) + chr(10))
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": cid_b,
                                "prompt": "P", "prompt_chars": 1,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            turns = list(m.iter_turns(tid))
        self.assertEqual(len(turns), 1)
        self.assertIn("不同 conv_id", buf.getvalue(), "冲突必须可见")
        self.assertTrue(turns[0].get("_conv_id_conflict"), "冲突需留在记录上")

    # ---------- R389-04：_file 兼容 + _files ----------

    def test_r389_files_records_all_sources(self):
        """_file 保留首个来源，_files 记录全部来源且不重复。"""
        cid = "1111ffff-2222-4333-8444-555566667777"
        tid = "srcs"
        os.makedirs(m.log_dir(), exist_ok=True)
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.conversation_log_path(cid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid,
                                "reply": "R", "reply_state": "captured",
                                "status": 200}, ensure_ascii=False) + chr(10))
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "P", "prompt_chars": 1,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "P", "prompt_chars": 1,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))
        t = list(m.iter_turns(tid))[0]
        self.assertIn("_file", t, "保留旧字段")
        self.assertIn("_files", t)
        self.assertEqual(len(t["_files"]), len(set(t["_files"])), "来源不重复")
        self.assertEqual(len(t["_files"]), 2, "两个来源文件都要记录")

    # ---------- R389-06：CLI 跨桶查询 ----------

    def _cli(self, *args):
        import contextlib
        import io as _io
        old_argv = list(sys.argv)
        sys.argv = ["chatgpt_send.py"] + list(args)
        out = _io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = m.main()
        finally:
            sys.argv = old_argv
        return code, out.getvalue()

    def test_r389_cli_show_turn_id_crosses_buckets(self):
        """conversations 有 end、pending 有 begin → --log-show <turn_id> 两者都要有。"""
        cid = "2222ffff-3333-4444-8555-666677778888"
        tid = "cliTurn"
        os.makedirs(m.log_dir(), exist_ok=True)
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.conversation_log_path(cid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid,
                                "reply": "跨桶回复", "reply_chars": 5,
                                "reply_state": "captured", "status": 200},
                               ensure_ascii=False) + chr(10))
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "跨桶提示词", "prompt_chars": 5,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))
        code, out = self._cli("--log-show", tid)
        self.assertEqual(code, 0)
        self.assertIn("跨桶提示词", out, "turn_id 入口必须能看到 pending 的 prompt")
        self.assertIn("跨桶回复", out, "turn_id 入口必须能看到 conversations 的 reply")

    def test_r389_cli_show_conv_id_crosses_buckets(self):
        """同上场景，--log-show <conv_id> 也要拿到完整一轮。"""
        cid = "3333ffff-4444-4555-8666-777788889999"
        tid = "cliConv"
        os.makedirs(m.log_dir(), exist_ok=True)
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.conversation_log_path(cid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid,
                                "reply": "回复B", "reply_chars": 3,
                                "reply_state": "captured", "status": 200},
                               ensure_ascii=False) + chr(10))
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": "",
                                "prompt": "提示词B", "prompt_chars": 4,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))
        code, out = self._cli("--log-show", cid)
        self.assertEqual(code, 0)
        self.assertIn("提示词B", out, "conv_id 入口必须能看到 pending 的 prompt")
        self.assertIn("回复B", out)

    # ---------- R390-07：冲突身份的反向查询 ----------

    def test_r390_conflict_visible_from_both_conv_queries(self):
        """冲突轮次从 cid_a / cid_b 任一入口查都不能被静默隐藏。"""
        cid_a = "aaaa2222-3333-4444-8555-666677778888"
        cid_b = "bbbb2222-3333-4444-8555-666677778888"
        tid = "conflictBoth"
        os.makedirs(m.log_dir(), exist_ok=True)
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        with open(m.conversation_log_path(cid_a), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid_a,
                                "reply": "R", "reply_state": "captured",
                                "status": 200}, ensure_ascii=False) + chr(10))
        with open(m.pending_log_path(tid), "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": cid_b,
                                "prompt": "P", "prompt_chars": 1,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))

        hits_a = list(m.iter_turns(cid_a))
        hits_b = list(m.iter_turns(cid_b))
        # 至少一个入口能查到，且查到的记录必须带冲突标记（不得静默归一）
        self.assertTrue(hits_a or hits_b, "冲突轮次不得在两个入口都查不到")
        self.assertEqual(len(hits_a) + len(hits_b), 1, "冲突轮次只应归属一处")
        only = (hits_a or hits_b)[0]
        self.assertTrue(only.get("_conv_id_conflict"), "命中时必须保留冲突标记")

    # ---------- R390-08：重复 end 的合并策略 ----------

    def test_r390_duplicate_end_field_strategy_documented(self):
        """同一轮出现两个 end：非身份字段按后到者，身份字段不覆盖有效值。

        这是当前 _merge_scalar 的既定策略，用测试钉住，避免维护者误以为
        所有字段都顺序无关。
        """
        cid = "cccc2222-3333-4444-8555-666677778888"
        tid = "dupEnd"
        os.makedirs(m.log_dir(), exist_ok=True)
        p = m.conversation_log_path(cid)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "phase": "begin", "conv_id": cid,
                                "prompt": "P", "prompt_chars": 1,
                                "status": None, "reply": ""},
                               ensure_ascii=False) + chr(10))
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid,
                                "reply": "第一版回复", "reply_chars": 5,
                                "reply_state": "captured", "status": 200},
                               ensure_ascii=False) + chr(10))
            f.write(json.dumps({"turn_id": tid, "phase": "end", "conv_id": cid,
                                "reply": "第二版回复", "reply_chars": 5,
                                "reply_state": "captured", "status": 200},
                               ensure_ascii=False) + chr(10))
        turns = list(m.iter_turns(tid))
        self.assertEqual(len(turns), 1, "同 turn 只输出一轮")
        t = turns[0]
        self.assertEqual(t["prompt"], "P", "begin 的 prompt 不受 end 影响")
        self.assertEqual(t["reply"], "第二版回复", "非身份字段按后到者")
        self.assertEqual(t["conv_id"], cid, "身份字段保持有效值")

    # ---------- R377-01：正文错误判定必须整条匹配 ----------

    def test_judge_error_multiline_error_with_extra_text_is_not_error(self):
        """反例：第一行是错误提示、但整条带了额外说明 → 不算错误提示。"""
        p1 = _load_p1()
        own = "Something went wrong\nPlease try again later."
        has, _struct, text_err = p1.judge_error(own, "")
        self.assertFalse(text_err, "不能只看第一行就判错")
        self.assertFalse(has)

    def test_judge_error_whole_body_error_is_error(self):
        """整条正文就是一句错误提示 → 判错。"""
        p1 = _load_p1()
        for own in ("Something went wrong.", "出现错误！", "  网络错误  ",
                    "Something went wrong?", "出现错误？"):
            has, _s, text_err = p1.judge_error(own, "")
            self.assertTrue(text_err, own)
            self.assertTrue(has)

    # ---------- R377-02：回读必须校验内容一致性 ----------

    def test_migration_verify_detects_payload_mismatch(self):
        """目标里同键但内容被写坏：即使键齐全也不得删 pending。"""
        cid = "55556666-7777-4888-8999-aaaabbbbcccc"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-payload")
        rows = [
            {"turn_id": "t-payload", "phase": "begin", "conv_id": "",
             "prompt": "正确提示词", "prompt_chars": 5, "status": None, "reply": ""},
            {"turn_id": "t-payload", "phase": "end", "conv_id": "", "reply": "正确回复",
             "reply_chars": 4, "reply_state": "captured", "status": 200},
        ]
        with open(begin, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        bad = [dict(rows[0], conv_id=cid, prompt="被写坏的提示词"),
               dict(rows[1], conv_id=cid, reply="被写坏的回复")]
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            for r in bad:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        out = m._migrate_pending("t-payload", begin, cid)
        self.assertIsNone(out, "内容不一致必须报未完成")
        self.assertTrue(os.path.exists(begin), "内容不一致时 pending 必须保留")

    def test_migration_verify_rejects_conflicting_duplicate_keys(self):
        """目标里同一键出现两条且内容不同：拒绝迁移、保留 pending。"""
        cid = "66667777-8888-4999-8aaa-bbbbccccdddd"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-dup2")
        rec = {"turn_id": "t-dup2", "phase": "begin", "conv_id": "",
               "prompt": "P", "prompt_chars": 1, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target = m.conversation_log_path(cid)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(json.dumps(dict(rec, conv_id=cid, prompt="A"), ensure_ascii=False) + "\n")
            f.write(json.dumps(dict(rec, conv_id=cid, prompt="B"), ensure_ascii=False) + "\n")
        self.assertIsNone(m._migrate_pending("t-dup2", begin, cid))
        self.assertTrue(os.path.exists(begin))

    # ---------- R377-03：提示词校验 ----------

    def test_validate_prompt_rejects_nul(self):
        """8141 个 NUL 必须被拒——这是真实事故（10:26 写入崩溃）。"""
        with self.assertRaises(RuntimeError):
            m.validate_prompt_text(chr(0) * 8141, "p.md")

    def test_validate_prompt_rejects_single_nul_in_body(self):
        """正常正文里混一个 NUL 同样可疑，必须拒。"""
        with self.assertRaises(RuntimeError):
            m.validate_prompt_text("正常正文" + chr(0) + "继续", "p.md")

    def test_validate_prompt_rejects_control_chars(self):
        """除 CR/LF/TAB 外的 Cc 控制字符一律拒。"""
        with self.assertRaises(RuntimeError):
            m.validate_prompt_text("正文" + chr(7) + "响铃", "p.md")

    def test_validate_prompt_rejects_whitespace_only(self):
        """纯空白/换行没有可见正文，必须拒。"""
        with self.assertRaises(RuntimeError):
            m.validate_prompt_text("   \n\n \t \n", "p.md")

    def test_validate_prompt_accepts_normal_text(self):
        """正常中文/英文 Markdown（含 Tab、CRLF）必须通过。"""
        m.validate_prompt_text("# 标题\n\n正文 with tab\tand CRLF\r\n", "p.md")
        m.validate_prompt_text("English markdown body", "p.md")

    def test_nul_prompt_creates_no_begin_record(self):
        """校验失败时不得留下 begin——那一轮根本没发生。"""
        cid = "77778888-9999-4aaa-8bbb-ccccddddeeee"
        pf = os.path.join(self.tmp, "nul.md")
        with open(pf, "wb") as f:
            f.write(bytes([0]) * 100)
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "x"))
        with self.assertRaises(RuntimeError):
            m.send_prompt(api, cid, pf)
        self.assertEqual(api.sent, [], "校验失败绝不能发送")
        self.assertEqual(list(m.iter_turns(cid)), [], "校验失败不得留下 begin")
        pend = []
        try:
            pend = [n for n in os.listdir(m.log_pending_dir()) if n.endswith(".jsonl")]
        except OSError:
            pass
        self.assertEqual(pend, [], "也不得留下 pending")

    # ---------- R375-01：DOM 完成状态的稳定性证据 ----------

    def test_p1_new_node_without_streaming_flag_not_captured(self):
        """新增节点刚出现、streaming 恰好为 false，但正文还在变 → 不得 captured。

        这是 R375-01 的核心：单次 streaming=False 不足以证明完成。
        """
        p1 = _load_p1()
        c = _FakeCdp([], frames=[
            {"count": 4, "last": "第一稿", "streaming": False, "hasError": False},
            {"count": 4, "last": "第一稿还在长", "streaming": False, "hasError": False},
        ])
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                       "hasError": False}, settle_tries=2, settle_gap=0)
        self.assertEqual(state, "partial", "正文仍在变，不能算完成")
        self.assertEqual(text, "第一稿还在长")

    def test_p1_stable_twice_is_captured(self):
        """连续两次采样正文一致且 streaming=false → 才允许 captured。"""
        p1 = _load_p1()
        c = _FakeCdp([], frames=[
            {"count": 4, "last": "定稿", "streaming": False, "hasError": False},
            {"count": 4, "last": "定稿", "streaming": False, "hasError": False},
        ])
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                       "hasError": False}, settle_tries=2, settle_gap=0)
        self.assertEqual((text, state), ("定稿", "captured"))

    def test_p1_timeout_without_stability_is_partial(self):
        """超时仍不稳定 → partial，绝不升级为 captured。"""
        p1 = _load_p1()
        frames = [{"count": 4, "last": "字数%d" % i, "streaming": False,
                   "hasError": False} for i in range(6)]
        c = _FakeCdp([], frames=frames)
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                       "hasError": False}, settle_tries=4, settle_gap=0)
        self.assertEqual(state, "partial")

    def test_p1_error_page_not_captured(self):
        """页面明确报错时，即使正文稳定也不得报 captured。"""
        p1 = _load_p1()
        c = _FakeCdp([], frames=[
            {"count": 4, "last": "部分内容", "streaming": False,
             "alert_text": "Something went wrong"},
            {"count": 4, "last": "部分内容", "streaming": False,
             "alert_text": "Something went wrong"},
        ])
        text, state = p1.reply_after(c, {"count": 3, "last": "旧", "streaming": False,
                                       "hasError": False}, settle_tries=2, settle_gap=0)
        self.assertEqual(state, "partial", "报错页面不得冒充正常完成")
        self.assertEqual(text, "部分内容")

    # ---------- R375-02：迁移批次内部幂等 ----------

    def test_migration_batch_internal_duplicate_not_written_twice(self):
        """源文件里同键记录出现两次，目标文件只能有一份。"""
        cid = "dddddddd-eeee-4fff-8000-111111111111"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-3")
        rec = {"turn_id": "t-3", "phase": "begin", "conv_id": "",
               "prompt": "同一条", "prompt_chars": 3, "status": None, "reply": ""}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")   # 重复
        m._migrate_pending("t-3", begin, cid)
        got = m._read_jsonl(m.conversation_log_path(cid))
        self.assertEqual(len(got), 1, "批次内部重复键必须去重")

    def test_migration_conflicting_duplicate_keeps_pending(self):
        """同键但内容冲突：不得静默挑一条，必须保留 pending。"""
        cid = "eeeeeeee-ffff-4000-8111-222222222222"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-4")
        a = {"turn_id": "t-4", "phase": "begin", "conv_id": "",
             "prompt": "版本A", "prompt_chars": 3, "status": None, "reply": ""}
        b = dict(a, prompt="版本B")
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
            f.write(json.dumps(b, ensure_ascii=False) + "\n")
        m._migrate_pending("t-4", begin, cid)
        self.assertTrue(os.path.exists(begin), "冲突时必须保留 pending")
        got = m._read_jsonl(m.conversation_log_path(cid))
        self.assertEqual(len(got), 1, "只写入第一条，不静默追加冲突版本")

    def test_migration_registers_after_successful_write(self):
        """写成功后必须登记，否则同批次后续同键仍会重复写。"""
        cid = "ffffffff-0000-4111-8222-333333333333"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-5")
        rows = [
            {"turn_id": "t-5", "phase": "begin", "conv_id": "", "prompt": "P",
             "prompt_chars": 1, "status": None, "reply": ""},
            {"turn_id": "t-5", "phase": "begin", "conv_id": "", "prompt": "P",
             "prompt_chars": 1, "status": None, "reply": ""},
            {"turn_id": "t-5", "phase": "end", "conv_id": "", "reply": "R",
             "reply_chars": 1, "reply_state": "captured", "status": 200},
        ]
        with open(begin, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        m._migrate_pending("t-5", begin, cid)
        got = m._read_jsonl(m.conversation_log_path(cid))
        phases = [r.get("phase") for r in got]
        self.assertEqual(phases, ["begin", "end"], "begin 只有一份，end 也在")

    # ---------- R374-02：pending 迁移幂等 ----------

    def test_pending_migration_is_idempotent(self):
        """迁移重复执行不得产生两份记录（崩溃后重试必须安全）。"""
        cid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        begin = m.pending_log_path("t-1")
        rec = {"turn_id": "t-1", "phase": "begin", "conv_id": "",
               "prompt": "提示词", "prompt_chars": 3, "status": None,
               "reply": "", "reply_state": "unrecorded"}
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        m._migrate_pending("t-1", begin, cid)
        self.assertEqual(len(m._read_jsonl(m.conversation_log_path(cid))), 1)

        # 模拟崩溃后重试：pending 若还在（或手工重建同一份），再迁一次
        with open(begin, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        m._migrate_pending("t-1", begin, cid)
        got = m._read_jsonl(m.conversation_log_path(cid))
        self.assertEqual(len(got), 1, "已有同 turn_id+phase 不得重复追加")

    def test_pending_kept_when_migration_incomplete(self):
        """迁移没搬完时必须保留 pending，供下次重试；读取端不得重复计数。"""
        cid = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        begin = m.pending_log_path("t-2")
        recs = [
            {"turn_id": "t-2", "phase": "begin", "conv_id": "", "prompt": "P",
             "prompt_chars": 1, "status": None, "reply": ""},
            {"turn_id": "t-2", "phase": "end", "conv_id": "", "reply": "R",
             "reply_chars": 1, "reply_state": "captured", "status": 200},
        ]
        with open(begin, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        real_write = m._log_write
        calls = {"n": 0}
        def flaky(path, record):
            calls["n"] += 1
            if calls["n"] == 2:          # 第二行（end）写失败
                raise OSError("模拟磁盘写失败")
            return real_write(path, record)
        m._log_write = flaky
        try:
            m._migrate_pending("t-2", begin, cid)
        finally:
            m._log_write = real_write
        self.assertTrue(os.path.exists(begin), "没搬完必须保留 pending")

        # 恢复后再迁一次 → 补齐，且不重复
        m._migrate_pending("t-2", begin, cid)
        got = m._read_jsonl(m.conversation_log_path(cid))
        self.assertEqual(len(got), 2, "begin+end 各一份")
        self.assertFalse(os.path.exists(begin), "搬完后才删 pending")

    def test_cross_file_same_turn_not_double_counted(self):
        """pending 未删时同一轮可能出现在两个文件，读取必须按 turn_id 去重。"""
        cid = "cccccccc-dddd-4eee-8fff-000000000000"
        os.makedirs(m.log_pending_dir(), exist_ok=True)
        os.makedirs(m.log_dir(), exist_ok=True)
        rec = {"turn_id": "dup-1", "phase": "begin", "conv_id": cid,
               "prompt": "同一轮", "prompt_chars": 3, "status": None, "reply": ""}
        for p in (m.conversation_log_path(cid), m.pending_log_path("dup-1")):
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        turns = list(m.iter_turns())
        self.assertEqual(len(turns), 1, "同一 turn_id 只能算一轮")


class ThinkingModelPreferenceTests(unittest.TestCase):
    """默认思考模型选择：清单里存在 gpt-6-t-mini 时必须优先选它。"""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    class _ModelsAPI:
        account = "acct1"

        def __init__(self, models, default="auto"):
            self.payload = {"default_model_slug": default, "models": models}
            self.calls = 0

        def req(self, method, url):
            self.calls += 1
            return 200, json.dumps(self.payload)

    @staticmethod
    def _models():
        return [
            {"slug": "gpt-5-6-t-mini", "title": "GPT-5.6 Luna", "max_tokens": 262144},
            {"slug": "gpt-5-6-t-mini-mini", "title": "GPT-5.6 Luna", "max_tokens": 262144},
            {"slug": "gpt-6-t-mini", "title": "GPT-6", "max_tokens": 262144},
            {"slug": "gpt-6-t-mini-mini", "title": "GPT-6", "max_tokens": 262144},
        ]

    def test_prefers_gpt_6_when_available(self):
        api = self._ModelsAPI(self._models())
        chosen = self.m.ChatGPTAPI.resolve_thinking_model(api, cache={})
        self.assertEqual(chosen, "gpt-6-t-mini")

    def test_falls_back_when_gpt_6_absent(self):
        api = self._ModelsAPI(self._models()[:2])
        chosen = self.m.ChatGPTAPI.resolve_thinking_model(api, cache={})
        self.assertEqual(chosen, "gpt-5-6-t-mini")

    def test_explicit_model_still_wins(self):
        api = self._ModelsAPI(self._models())
        with mock.patch.dict(os.environ, {"CHATGPT_MODEL": "gpt-5-6-t-mini"}):
            chosen = self.m.ChatGPTAPI.resolve_thinking_model(api, cache={})
        self.assertEqual(chosen, "gpt-5-6-t-mini")

    def test_cache_separates_account_and_explicit_model(self):
        """缓存键是 (account, CHATGPT_MODEL)：同键不重复请求清单，换显式模型必须重算。"""
        api = self._ModelsAPI(self._models())
        cache = {}
        first = self.m.ChatGPTAPI.resolve_thinking_model(api, cache=cache)
        second = self.m.ChatGPTAPI.resolve_thinking_model(api, cache=cache)
        self.assertEqual((first, second), ("gpt-6-t-mini", "gpt-6-t-mini"))
        self.assertEqual(api.calls, 1, "同一 (account, explicit) 必须命中缓存")
        with mock.patch.dict(os.environ, {"CHATGPT_MODEL": "gpt-5-6-t-mini"}):
            third = self.m.ChatGPTAPI.resolve_thinking_model(api, cache=cache)
        self.assertEqual(third, "gpt-5-6-t-mini")
        self.assertEqual(api.calls, 1, "显式指定不请求模型清单")

    def test_preferred_thinking_env_override_wins(self):
        """CHATGPT_PREFERRED_THINKING 覆盖默认 gpt-6-t-mini。"""
        api = self._ModelsAPI(self._models())
        with mock.patch.dict(os.environ):
            os.environ.pop("CHATGPT_MODEL", None)
            os.environ["CHATGPT_PREFERRED_THINKING"] = "gpt-5-6-t-mini"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                chosen = self.m.ChatGPTAPI.resolve_thinking_model(api, cache={})
        self.assertEqual(chosen, "gpt-5-6-t-mini")
        self.assertIn("优先命中 gpt-5-6-t-mini（source=env）", out.getvalue())

    def test_preferred_thinking_default_when_env_unset(self):
        """变量不存在时行为明确：默认 gpt-6-t-mini，日志标 source=default。"""
        api = self._ModelsAPI(self._models())
        with mock.patch.dict(os.environ):
            os.environ.pop("CHATGPT_MODEL", None)
            os.environ.pop("CHATGPT_PREFERRED_THINKING", None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                chosen = self.m.ChatGPTAPI.resolve_thinking_model(api, cache={})
        self.assertEqual(chosen, "gpt-6-t-mini")
        self.assertIn("优先命中 gpt-6-t-mini（source=default）", out.getvalue())

    def test_preferred_thinking_absent_from_list_prints_diagnostic(self):
        """优先候选不在清单里：必须留可 grep 的诊断，不得静默当成已选中。"""
        api = self._ModelsAPI(self._models())
        with mock.patch.dict(os.environ):
            os.environ.pop("CHATGPT_MODEL", None)
            os.environ["CHATGPT_PREFERRED_THINKING"] = "gpt-7-t-mini"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                chosen = self.m.ChatGPTAPI.resolve_thinking_model(api, cache={})
        self.assertEqual(chosen, "gpt-5-6-t-mini", "缺失时按后备顺序解析")
        self.assertIn(
            "优先候选 gpt-7-t-mini 不在清单里（source=env），按后备顺序解析",
            out.getvalue(),
        )

    def test_preferred_thinking_invalid_value_raises_before_request(self):
        """非法配置（无 -t- / 空白 / 带空格）直接抛 ValueError，且不发请求。"""
        for bad in ("gpt-4o", "", "   ", "gpt 6 t mini"):
            with self.subTest(value=bad):
                api = self._ModelsAPI(self._models())
                with mock.patch.dict(os.environ):
                    os.environ.pop("CHATGPT_MODEL", None)
                    os.environ["CHATGPT_PREFERRED_THINKING"] = bad
                    with self.assertRaises(ValueError):
                        self.m.ChatGPTAPI.resolve_thinking_model(api, cache={})
                self.assertEqual(api.calls, 0, "非法配置必须在请求清单之前失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
