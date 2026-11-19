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

    def test_atomic_write_uses_unique_tmp_names(self):
        # 事故：固定 `.tmp` 时同进程两个线程写同一个临时文件，Windows 上 os.replace
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
                          "model": model})
        return self.send_status, self.sse or ""

    def warm_cookies(self):
        return False

    def extract_reply_sse(self, raw):
        return m.ChatGPTAPI.extract_reply_sse(raw)

    def harvest_mapping(self, raw):
        return m.ChatGPTAPI.harvest_mapping(raw)

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
        real_alive, real_start = m._proc_alive, m._proc_start_time
        try:
            m._proc_alive = lambda pid: True
            m._proc_start_time = lambda pid: None
            self._write_lock(lock, {"pid": 4242, "process_start_time": 7,
                                    "created_at": time.time() - 999})
            self.assertFalse(m._lock_holder_stale(lock))
        finally:
            m._proc_alive, m._proc_start_time = real_alive, real_start

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
        alive, start = m._proc_alive, m._proc_start_time
        try:
            m._proc_alive = lambda pid: True
            m._proc_start_time = lambda pid: None
            ok, why = m._lease_reclaimable({
                "run_id": "r1", "lease_until": self._now() - 1,
                "pid": 4242, "process_start_time": 7})
        finally:
            m._proc_alive, m._proc_start_time = alive, start
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

    def test_no_row_is_evidence_free(self):
        for clause, cells in self._rows().items():
            joined = " ".join(cells)
            with self.subTest(clause=clause):
                self.assertRegex(joined, r"(test|code|live|human):",
                                 "%s 没有任何证据标注" % clause)


if __name__ == "__main__":
    unittest.main(verbosity=2)
