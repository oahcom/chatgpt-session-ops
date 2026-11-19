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


if __name__ == "__main__":
    unittest.main(verbosity=2)
