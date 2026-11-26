# -*- coding: utf-8 -*-
"""chatgpt_send.py 匿名会话 / CDP 通道的**错误守卫**回归测试（离线，不触网）。

背景（issue #134）：这些 raise 是匿名通道唯一的失败信号。它们此前没有任何
直接断言——把守卫体替换成 ``pass`` 后全量测试仍然全绿，等于「错误被吞掉」
这件事本身没有被任何测试发现。

覆盖的 15 个守卫（scripts/chatgpt_send.py，main=de92dc1）：

  * 5090  _cdp()             CDP 客户端不可用
  * 5101  client()           CDP 端口未就绪
  * 5219  egress_ip()        无法新建校验标签页（异常）
  * 5221  egress_ip()        无法新建校验标签页（无 targetId）
  * 5452  _new_tab()         新建标签页失败
  * 5469  send()             CDP 端口未运行，无法确认匿名会话标签页
  * 5475  send()             匿名会话标签页已不存在
  * 5489  send()             匿名页输入框未出现
  * 5512  send()             发送后未拿到匿名会话 ID
  * 5514  send()             会话 ID 不合规
  * 5518  send()             匿名会话续接失败（落点不一致）
  * 5669  send_anon()        匿名会话不可访问（HTTP 404/403）
  * 5673  send_anon()        匿名会话暂时读不到（HTTP 429/0/5xx）
  * 5976  conversation_log_path() 畸形 conv_id
  * 5978  conversation_log_path() 缺少 conv_id

断言口径：**精确异常类型 + 消息片段**。只断言「抛了异常」不够——
例如把「畸形」守卫换成 pass 后，代码会落进下一条「缺少」守卫，异常类型
不变；只有消息片段能区分两条守卫（反向变异证据见 PR）。

所有 CDP 交互都用本地假对象，绝不建立真实连接。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("CHATGPT_SEND_PY") or os.path.join(
    HERE, "..", "scripts", "chatgpt_send.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "chatgpt_send_cdp_guards", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load_module()

CID_A = "11111111-2222-4333-8444-555555555555"
CID_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"


class _FakePage(object):
    """冒充已附加的页面客户端：只回答 send() 流程真正会问的东西。"""

    target_id = "tab-under-test"

    def __init__(self, url="https://chatgpt.com/"):
        self.url = url
        self.sent = []
        self.navigated = []

    def page_url(self):
        return self.url

    def navigate(self, url, timeout=60):
        self.navigated.append(url)

    def evaluate(self, expr):
        return {"value": True}

    def send(self, method, params=None):
        self.sent.append((method, params))
        return {}


class _DeadClient(object):
    """is_alive() 恒 False 的 CDP 客户端。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def is_alive(self):
        return False


class _NoTargetClient(object):
    """CDP 浏览器级客户端：createTarget 不返回 targetId。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def connect_browser(self):
        return None

    def send(self, method, params=None):
        return {}

    def close(self):
        return None


class _AdaptiveClock(object):
    """前两次 time() 返回 0（发送循环至少进一次），之后每次跳 1000 秒。

    守卫在时 send() 只调用两次 time() 就抛；守卫被删除后流程会继续进入
    `_idle()`，时间巨跳让它立即返回 False —— 于是测试以「RuntimeError
    not raised」这条**守卫断言**失败，而不是靠 mock 序列耗尽抛
    StopIteration。后者与守卫语义无关：send() 内部 time.time() 次数一变，
    未变异的代码也会误红。
    """

    def __init__(self):
        self.calls = 0

    def time(self):
        self.calls += 1
        if self.calls <= 2:
            return 0.0
        return 1000.0 * (self.calls - 2)

    def sleep(self, _seconds):
        pass


class GuardsBase(unittest.TestCase):
    """公共隔离：临时目录 + sys.path 复原 + 模块级路径复原。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guards-cdp-")
        self._path = list(sys.path)
        self._base = m.BASE
        self._anon_sessions = m.ANON_SESSIONS_FILE
        m.BASE = self.tmp
        m.ANON_SESSIONS_FILE = os.path.join(self.tmp, "anon-sessions.json")

    def tearDown(self):
        sys.path[:] = self._path
        m.BASE = self._base
        m.ANON_SESSIONS_FILE = self._anon_sessions
        shutil.rmtree(self.tmp, ignore_errors=True)

    def anon(self, port=9333):
        """构造一个不跑 __init__（不校验代理）的 AnonBrowser。"""
        br = object.__new__(m.AnonBrowser)
        br.port = port
        br.cdp_dir = self.tmp
        br._client = None
        br.last_cid = None
        br.last_tab = None
        return br

    def assertGuard(self, exc_type, fragment, callable_, *args, **kwargs):
        with self.assertRaises(exc_type) as ctx:
            callable_(*args, **kwargs)
        msg = str(ctx.exception)
        self.assertIn(fragment, msg, "守卫消息变了：%r" % msg)
        return msg


class TestClientGuards(GuardsBase):
    """连接建立阶段：5090 / 5101。"""

    def test_cdp_client_unavailable(self):
        """5090：cdp 导入失败必须报「CDP 客户端不可用」，并带出目录与原因。"""
        br = self.anon()
        with mock.patch.dict(sys.modules, {"cdp": None}):
            msg = self.assertGuard(RuntimeError, "CDP 客户端不可用", br._cdp)
        self.assertIn(self.tmp, msg)
        self.assertIn("cdp", msg.lower())

    def test_cdp_port_not_ready(self):
        """5101：端口活着才行；死了必须报「未就绪」并给出端口与启动命令。"""
        br = self.anon(port=9444)
        with mock.patch.object(m.AnonBrowser, "_cdp", lambda self: _DeadClient), \
                mock.patch.object(m.AnonBrowser, "_autostart", lambda self: False):
            msg = self.assertGuard(RuntimeError, "未就绪", br.client)
        self.assertIn("9444", msg)
        self.assertIn("start_chrome.py", msg)


class TestEgressGuards(GuardsBase):
    """出口校验：5219 / 5221。"""

    def test_create_target_exception_is_wrapped(self):
        """5219：建 target 时底层异常必须被包成可读错误，不能裸抛。"""
        br = self.anon()
        br._client = _FakePage()

        class _Boom(object):
            def __init__(self, **kwargs):
                raise RuntimeError("connect boom")

        with mock.patch.object(m.AnonBrowser, "_cdp", lambda self: _Boom):
            msg = self.assertGuard(
                RuntimeError, "无法新建校验标签页", br.egress_ip)
        self.assertIn("connect boom", msg)

    def test_create_target_without_target_id(self):
        """5221：createTarget 返回空 targetId 必须报错，不能继续拿空 id 去连。"""
        br = self.anon()
        br._client = _FakePage()
        with mock.patch.object(m.AnonBrowser, "_cdp", lambda self: _NoTargetClient):
            msg = self.assertGuard(
                RuntimeError, "无法新建校验标签页", br.egress_ip)
        self.assertIn("{}", msg, "空响应必须原样带进消息，便于定位")


class TestNewTabGuard(GuardsBase):
    """另开标签页：5452。"""

    def test_new_tab_without_target_id(self):
        fake_cdp = types.ModuleType("cdp")
        fake_cdp.CDPClient = _NoTargetClient
        br = self.anon()
        with mock.patch.dict(sys.modules, {"cdp": fake_cdp}):
            msg = self.assertGuard(
                RuntimeError, "新建标签页失败", br._new_tab)
        self.assertIn("{}", msg)


class TestSendGuards(GuardsBase):
    """匿名发送主流程：5469 / 5475 / 5489 / 5512 / 5514 / 5518。"""

    def _br_client(self, url="https://chatgpt.com/"):
        page = _FakePage(url)
        br = self.anon()
        br.client = lambda: page
        return br, page

    def test_existing_session_but_browser_down(self):
        """5469：浏览器没起 ≠ 会话丢了——必须明确「无法确认」，不得归档。"""
        br = self.anon()
        br.find_tab = lambda cid: None
        br.alive = lambda: False
        archived = []
        with mock.patch.object(m, "mark_anon_archived",
                               lambda *a, **k: archived.append((a, k))):
            msg = self.assertGuard(
                RuntimeError, "未运行，无法确认匿名会话", br.send, "hi", CID_A)
        self.assertIn("9333", msg)
        self.assertIn(CID_A[:8], msg)
        self.assertEqual(archived, [], "无法确认时绝不能把会话标成 archived")

    def test_existing_session_tab_gone(self):
        """5475：端口活着但标签页没了：归档并给出可读全文的替代路径。"""
        br = self.anon()
        br.find_tab = lambda cid: None
        br.alive = lambda: True
        archived = []
        with mock.patch.object(m, "mark_anon_archived",
                               lambda *a, **k: archived.append((a, k))):
            msg = self.assertGuard(
                RuntimeError, "标签页已不存在", br.send, "hi", CID_A)
        self.assertIn(CID_A[:8], msg)
        self.assertIn("--anon-read " + CID_A, msg)
        self.assertEqual(len(archived), 1, "确认丢失后才允许归档")

    def test_composer_never_appears(self):
        """5489：输入框 45 秒不出现（Cloudflare）必须报错，不能盲发。"""
        br, _page = self._br_client()
        br._wait = lambda *a, **k: False
        self.assertGuard(
            RuntimeError, "匿名页输入框未出现", br.send, "hi", None)

    def _drive_send(self, br, cid=None, times=None, clock=None):
        br._wait = lambda *a, **k: True
        br._clear_composer = lambda: None
        br._send_button = lambda: {"x": 1, "y": 2}
        br._conv_id = lambda: cid
        if clock is None:
            clock = types.SimpleNamespace(
                time=mock.Mock(side_effect=list(times or (0, 1))),
                sleep=lambda s: None)
        return mock.patch.object(m, "time", clock)

    def test_no_conv_id_after_submit(self):
        """5512：点击发送后始终读不到 /uc/<id>：必须报「未拿到」，不能返回 None。"""
        br, _page = self._br_client()
        fake_time = self._drive_send(br, cid=None, times=(0, 200))
        with fake_time:
            self.assertGuard(
                RuntimeError, "发送后未拿到匿名会话 ID", br.send, "hi", None)

    def test_malformed_conv_id_after_submit(self):
        """5514：页面给出非法 id 时必须报「不合规」，不得写进对照表。"""
        br, _page = self._br_client()
        fake_time = self._drive_send(br, cid="bad/id", clock=_AdaptiveClock())
        with fake_time:
            msg = self.assertGuard(
                RuntimeError, "发送后拿到的会话 ID 不合规", br.send, "hi", None)
        self.assertIn("bad/id", msg)

    def test_resume_landed_on_other_session(self):
        """5518：续接必须落回原会话；落到别处就是串会话，必须报错。"""
        br, page = self._br_client()
        br.find_tab = lambda cid: {"id": "tab-1"}
        br._attach = lambda tid: page
        fake_time = self._drive_send(br, cid=CID_B, clock=_AdaptiveClock())
        with fake_time:
            msg = self.assertGuard(
                RuntimeError, "匿名会话续接失败", br.send, "hi", CID_A)
        self.assertIn(CID_A[:8], msg)
        self.assertIn(CID_B[:8], msg)


class _AnonBrowserStub(object):
    """send_anon 用：只需在当前守卫之前的方法。"""

    def __init__(self, did="cccccccc-3333-4333-8333-333333333333"):
        self.did = did
        self.last_cid = None
        self.last_tab = None

    def identity(self):
        return self.did

    def cookie_jar(self):
        return [{"name": "oai-did", "value": self.did,
                 "domain": ".chatgpt.com", "path": "/"}]

    def cookie(self):
        return "oai-did=" + self.did

    def send(self, text, conv_id=None):  # pragma: no cover - 守卫在它之前
        raise AssertionError("守卫失效：send 不该被调用")


class TestSendAnonProbeGuards(GuardsBase):
    """发送前探测：5669 / 5673。"""

    def _api(self):
        api = object.__new__(m.ChatGPTAPI)
        api.account = "nologin"
        api.is_anon = True
        api.cookie = ""
        api.anon = _AnonBrowserStub()
        return api

    def test_session_inaccessible(self):
        """5669：403/404 才是会话真失效，必须明说且给出重建建议。"""
        api = self._api()
        with mock.patch.object(m, "probe_anon",
                               lambda a, c: (404, "会话不可访问")):
            msg = self.assertGuard(
                RuntimeError, "不可访问（HTTP 404）", m.send_anon,
                api, CID_A, "p.md", "hi")
        self.assertIn(CID_A[:8], msg)
        self.assertIn("重建", msg)

    def test_session_temporarily_unreadable(self):
        """5673：429/5xx 是通道问题，必须明说「不代表会话已丢失」，避免误删。"""
        api = self._api()
        with mock.patch.object(m, "probe_anon",
                               lambda a, c: (429, "通道异常")):
            msg = self.assertGuard(
                RuntimeError, "暂时读不到（HTTP 429", m.send_anon,
                api, CID_A, "p.md", "hi")
        self.assertIn("不代表会话已丢失", msg)
        self.assertIn("--anon-read", msg)


class TestConversationLogIdGuards(GuardsBase):
    """会话日志 conv_id 校验：5976 / 5978。"""

    def test_malformed_conv_id(self):
        """5976：「畸形」与「缺失」是两种身份状态，消息必须可区分。"""
        for bad in ("../../evil", "abc", "space id", 123, [], {}, b"bytes"):
            with self.subTest(bad=bad):
                msg = self.assertGuard(
                    ValueError, "畸形 conv_id", m.conversation_log_path, bad)
                self.assertNotIn("缺少", msg, "畸形不得退化成缺失：%r" % msg)

    def test_missing_conv_id(self):
        """5978：None/空串是历史缺失，必须与畸形分开报。"""
        for empty in (None, ""):
            with self.subTest(empty=empty):
                msg = self.assertGuard(
                    ValueError, "缺少 conv_id", m.conversation_log_path, empty)
                self.assertNotIn("畸形", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
