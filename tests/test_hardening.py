# -*- coding: utf-8 -*-
"""chatgpt_send.py 的离线回归测试（不触网、不需要凭据）。

    python -m unittest discover -s tests -v
    python tests/test_hardening.py          # 直接跑也支持

覆盖的都是「出过事或一想就疼」的点：id 路径穿越、状态文件损坏被静默覆盖、
归档写半截、回复被静默截断、锁重入与 stale 回收、轮转兜底判定。
"""
import importlib.util
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

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
        for raw, want in (("1", 5), ("30", 30), ("99999", 180), (None, 42), ("junk", 42)):
            api.last_headers = {} if raw is None else {"retry-after": raw}
            self.assertEqual(api._retry_after(42), want)

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

    def live_tail(self, conv_id, tries=2):
        return "parent-msg-1", '{"mapping": {}}'

    def cached_tail(self, conv_id):
        return None

    def requirements(self):
        return "req-token", "proof-token"

    def send(self, conv_id, text, parent_message_id, req_token, proof, timeout=420):
        self.sent.append({"conv_id": conv_id, "parent": parent_message_id, "text": text})
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

    def test_new_chat_has_no_parent(self):
        cid = "11111111-2222-4333-8444-555555555555"
        api = FakeAPI(os.path.join(self.tmp, "auth"), sse=self._sse(cid, "owner ruling"))
        status, _p, conv = m.send_prompt(api, None, self._prompt("x"), new_chat=True)
        self.assertEqual(status, 200)
        self.assertEqual(conv, cid, "新会话必须从 SSE 里认出 id")
        self.assertIsNone(api.sent[0]["parent"], "新会话不该带 parent_message_id")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
