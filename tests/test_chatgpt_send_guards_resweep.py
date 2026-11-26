# -*- coding: utf-8 -*-
"""issue #147：五个在上一次全量重扫后仍然「删掉也不红」的守卫。

每个用例都钉住**精确异常类型**和**完整消息**（或至少消息里的守卫专属片段）。
只写 ``assertRaises`` 不够：守卫被替换成 ``pass`` 后，代码往往落进下一条守卫，
异常类型不变，只有消息能区分是哪一条守卫在兜底。

覆盖的五个守卫（按消息字符串定位，不按行号；main=06a5181）：

  * extract_deliverable()   "--fence 需要正整数：%r"
        旧用例只喂 0/-1/"-2"，``int()`` 能成功，走的是「从 1 开始」分支；
        非数字串（"abc"/"1.5"）才命中本题的守卫。删守卫 -> n 未定义。
  * check_schedule()        "rrule 含空 rule part（分号多写）：%r"
        test_hardening.py 只 assertRaises，不断言消息；删守卫后空 part 会落到
        「必须是 NAME=VALUE」，异常类型不变但消息变了。
  * check_oneshot()         "scheduledAt 为空"
        空串/None 在删守卫后会落进 fromisoformat 的「不是 ISO 8601」分支。
  * register_conversations_batch()
                            "batch registration account verification failed: %s"
        回读记录是 dict 但 account 无效（None）：与「verification failed」
        （记录不是 dict）是两条不同守卫，消息必须可区分。
  * AnonBrowser.send()      "发送按钮不可用（输入可能未进 React 状态）"
        _send_button() 恒返回 None：删守卫后立刻 ``btn["x"]`` -> TypeError。

所有用例离线运行，CDP 全部用本地假对象，不建立真实连接。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from scripts import chatgpt_send as sender


CID_A = "6afe889e-abf8-83e8-8e65-20ad6f7129e3"


def _make_tmp(test, prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, path, True)
    return path


class ExtractDeliverableFenceGuardTests(unittest.TestCase):
    """extract_deliverable(): ``int(fence)`` 失败时的 CustodyError。"""

    GUARD = "--fence 需要正整数"

    def setUp(self):
        self.tmp = _make_tmp(self, "guards-resweep-fence-")
        self.src = os.path.join(self.tmp, "src.md")
        with open(self.src, "wb") as f:
            f.write(b"```\nbody\n```\n")
        self.out = os.path.join(self.tmp, "body.md")

    def test_non_integer_fence_rejected_with_specific_message(self):
        """fence 不是十进制整数时必须报「需要正整数」，不能漏成 UnboundLocalError。"""
        for bad in ("abc", "1.5"):
            with self.subTest(fence=bad):
                with self.assertRaises(sender.CustodyError) as ctx:
                    sender.extract_deliverable(self.src, fence=bad, out=self.out)
                self.assertIs(type(ctx.exception), sender.CustodyError)
                self.assertEqual(
                    str(ctx.exception),
                    "%s：%r" % (self.GUARD, bad),
                )
                # 与「从 1 开始」守卫区分：非数字串不该走到那条分支。
                self.assertNotIn("从 1 开始", str(ctx.exception))


class CheckScheduleEmptyPartGuardTests(unittest.TestCase):
    """check_schedule(): 分号多写产生的空 rule part。"""

    def test_empty_rule_part_rejected_with_specific_message(self):
        raw = "FREQ=HOURLY;;INTERVAL=1"
        with self.assertRaises(sender.ScheduleError) as ctx:
            sender.check_schedule(raw)
        self.assertIs(type(ctx.exception), sender.ScheduleError)
        self.assertEqual(
            str(ctx.exception),
            "rrule 含空 rule part（分号多写）：%r" % raw,
        )
        # 删守卫后会退化成「必须是 NAME=VALUE」；两者必须可区分。
        self.assertIn("空 rule part", str(ctx.exception))


class CheckOneshotEmptyGuardTests(unittest.TestCase):
    """check_oneshot(): 空 scheduledAt 的专属消息。"""

    def test_empty_scheduled_at_rejected_with_specific_message(self):
        for bad in ("", None, "   "):
            with self.subTest(scheduled_at=bad):
                with self.assertRaises(sender.ScheduleError) as ctx:
                    sender.check_oneshot(bad)
                self.assertIs(type(ctx.exception), sender.ScheduleError)
                self.assertEqual(str(ctx.exception), "scheduledAt 为空")


class BatchRegistrationAccountVerificationGuardTests(unittest.TestCase):
    """register_conversations_batch(): 回读记录的 account 无效。"""

    def setUp(self):
        self.tmp = _make_tmp(self, "guards-resweep-batch-")
        self.registry_path = os.path.join(self.tmp, "conv-accounts.json")
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        patcher = mock.patch.object(
            sender, "CONV_MAP_FILE", self.registry_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_invalid_readback_account_rejected_with_specific_message(self):
        """回读记录存在但不是 dict 是另一条守卫；这里必须是 account 校验那条。"""
        with mock.patch.object(
            sender,
            "_read_conv_map_for_migration",
            side_effect=[{}, {CID_A: {"account": None}}],
        ):
            with self.assertRaises(RuntimeError) as ctx:
                sender.register_conversations_batch([
                    {"id": CID_A, "account": "acct1"},
                ])
        self.assertIs(type(ctx.exception), RuntimeError)
        self.assertEqual(
            str(ctx.exception),
            "batch registration account verification failed: %s" % CID_A,
        )


class _FakePage(object):
    """只回答 AnonBrowser.send() 在守卫之前会问的东西。"""

    target_id = "tab-under-test"

    def __init__(self):
        self.sent = []
        self.navigated = []

    def page_url(self):
        return "https://chatgpt.com/"

    def navigate(self, url, timeout=60):
        self.navigated.append(url)

    def evaluate(self, expr):
        return {"value": True}

    def send(self, method, params=None):
        self.sent.append((method, params))
        return {}


class AnonSendButtonGuardTests(unittest.TestCase):
    """AnonBrowser.send(): 输入进不去 React 状态时发送按钮始终不可用。"""

    def setUp(self):
        self.tmp = _make_tmp(self, "guards-resweep-cdp-")
        self._base = sender.BASE
        self._anon_sessions = sender.ANON_SESSIONS_FILE
        sender.BASE = self.tmp
        sender.ANON_SESSIONS_FILE = os.path.join(self.tmp, "anon-sessions.json")
        self.addCleanup(self._restore_module_paths)

    def _restore_module_paths(self):
        sender.BASE = self._base
        sender.ANON_SESSIONS_FILE = self._anon_sessions

    def _anon(self):
        br = object.__new__(sender.AnonBrowser)
        br.port = 9333
        br.cdp_dir = self.tmp
        br._client = None
        br.last_cid = None
        br.last_tab = None
        return br

    def test_send_button_unavailable_rejected_with_specific_message(self):
        br = self._anon()
        br.client = lambda: _FakePage()
        br._wait = lambda *a, **k: True
        br._clear_composer = lambda: None
        br._send_button = lambda: None
        br._conv_id = lambda: None
        with mock.patch.object(sender.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError) as ctx:
                br.send("hi", None)
        self.assertIs(type(ctx.exception), RuntimeError)
        self.assertEqual(
            str(ctx.exception),
            "发送按钮不可用（输入可能未进 React 状态）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
