# -*- coding: utf-8 -*-
"""issue #149：四条「删掉也不红」的超时 / 截断守卫。

每个用例都钉住**精确异常类型** + **完整消息**（FileNotFoundError 则钉 args[0] 是路径，
不是 errno 2），因为这几条守卫被替换成 ``pass`` 后，代码往往落进下一条更深的守卫：
异常类型看起来一样，只有消息（或 args 形状）能证明是哪一条守卫在兜底。

覆盖的四条守卫（按消息 / 行为定位，不按行号；base = origin/main 1a3b757）：

  * _extract_timeout_flag()  ``"--timeout 重复指定"``
        ``["--timeout","600","--timeout","900"]``：删守卫后后者静默覆盖前者，
        调用方以为用了 900s，实际生效值取决于位置——不可观测的配置漂移。
  * _extract_timeout_flag()  ``"--timeout 需要秒数（形如 --timeout 600）"``
        末尾孤立 ``--timeout``：删守卫后 ``argv[i + 1]`` 越界 -> IndexError，
        用户看到的是崩溃而不是「用法错误」。
  * judge_send_log()         ``raise FileNotFoundError(log_path)``
        删守卫后落到 ``open()`` 的 ``FileNotFoundError(2, "No such file...")``，
        ``args[0]`` 是 errno 而不是路径——判定器把「日志没生成」和「判定器自己
        读错了文件」混成同一种错误，排障时拿到的是 errno 2。
  * ChatGPTAPI._read_stream()  ``except IncompleteRead: sink.append(partial); raise``
        partial 必须**先落进 sink 再重抛**：删 raise 会吞掉判定（循环继续、正常返回），
        删 append 则调用方拿到的已收字节少了最后一块。调用方 ``req()`` 依赖这次重抛
        把 sink 折成 PARTIAL 落盘，而 ``last_truncated=True`` 正是
        ``_send_prompt_impl`` 打 ``SEND_TRUNCATED``（并走 exit 3）的唯一条件。

全部用例离线运行：不建连接、不读凭据，假 resp / 假 opener 都是本地对象。
"""
import http.client
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from scripts import chatgpt_send as sender


def _make_tmp(test, prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, path, True)
    return path


class ExtractTimeoutFlagDuplicateTests(unittest.TestCase):
    """_extract_timeout_flag(): 重复指定 ``--timeout`` 必须报错，不许后者静默覆盖。"""

    def test_duplicate_timeout_rejected_with_specific_message(self):
        argv = ["--timeout", "600", "--timeout", "900"]

        with self.assertRaises(ValueError) as raised:
            sender._extract_timeout_flag(argv)

        self.assertIs(type(raised.exception), ValueError)
        self.assertEqual(str(raised.exception), "--timeout 重复指定")


class ExtractTimeoutFlagMissingValueTests(unittest.TestCase):
    """_extract_timeout_flag(): 末尾孤立 ``--timeout`` 必须报「需要秒数」，不许越界崩。"""

    def test_trailing_timeout_without_value_rejected_with_specific_message(self):
        argv = ["conv-1", "p.md", "--timeout"]

        with self.assertRaises(ValueError) as raised:
            sender._extract_timeout_flag(argv)

        self.assertIs(type(raised.exception), ValueError)
        self.assertEqual(str(raised.exception), "--timeout 需要秒数（形如 --timeout 600）")


class JudgeSendLogMissingFileTests(unittest.TestCase):
    """judge_send_log(): 日志不存在时抛 FileNotFoundError(路径)，不是 errno 2。"""

    def setUp(self):
        self.tmp = _make_tmp(self, "guards-149-judge-")
        self.missing = os.path.join(self.tmp, "definitely-absent-send.log")

    def test_missing_log_raises_with_path_as_first_arg(self):
        self.assertFalse(os.path.exists(self.missing))

        with self.assertRaises(FileNotFoundError) as raised:
            sender.judge_send_log(self.missing, prompt_file=None, now=0, live_pids=[])

        self.assertIs(type(raised.exception), FileNotFoundError)
        self.assertEqual(raised.exception.args, (self.missing,))
        # 删守卫后 open() 抛的是 (2, "No such file or directory")——args 形状必须可区分。
        self.assertNotEqual(raised.exception.args, (2, "No such file or directory"))


class _OneShotTruncatedResponse:
    """假 resp：首次 ``read()`` 抛 IncompleteRead(partial)，之后返回 EOF。

    之后返回 ``b""`` 是关键：守卫被替换成 ``pass`` 时循环会继续跑一轮并「正常」
    返回，于是「必须重抛」这条断言直接失败，而不是靠越界/死循环碰巧变红。
    """

    status = 200
    headers = {}

    def __init__(self, partial=b"partial-bytes", expected=100):
        self._partial = partial
        self._expected = expected
        self.read_calls = 0

    def read(self, _size=-1):
        self.read_calls += 1
        if self.read_calls == 1:
            raise http.client.IncompleteRead(self._partial, self._expected)
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeOpener:
    """只实现 ``open(request, timeout=...)``：让 ``req()`` 走真实代码路径。"""

    def __init__(self, response):
        self._response = response
        self.calls = 0

    def open(self, _request, timeout=None):
        self.calls += 1
        return self._response


def _bare_api(base_dir, response):
    """构造一个不做网络/凭据初始化的 ChatGPTAPI 实例，只填 ``req()`` 需要的属性。"""
    api = sender.ChatGPTAPI.__new__(sender.ChatGPTAPI)
    api.account = "acct1"
    api.base_dir = base_dir
    api.is_anon = False
    api.cookie = ""
    api.token = ""
    api._opener = _FakeOpener(response)
    api.last_headers = {}
    api.last_body = ""
    api.last_status = 0
    api.quota = {}
    api.last_truncated = False
    api.last_partial_path = None
    api.last_partial_bytes = 0
    return api


class ReadStreamIncompleteReadTests(unittest.TestCase):
    """_read_stream(): partial 先 append 进 sink 再原样重抛；调用方据此落盘并标记截断。"""

    PARTIAL = b"partial-bytes"

    def test_partial_bytes_buffered_before_reraise(self):
        resp = _OneShotTruncatedResponse(self.PARTIAL)
        sink = []

        with self.assertRaises(http.client.IncompleteRead) as raised:
            sender.ChatGPTAPI._read_stream(resp, sink)

        self.assertIs(type(raised.exception), http.client.IncompleteRead)
        self.assertEqual(raised.exception.partial, self.PARTIAL)
        # 顺序断言：重抛之前 partial 必须已经进了 sink（否则调用方拿到 0 字节）。
        self.assertEqual(sink, [self.PARTIAL])
        self.assertEqual(resp.read_calls, 1)

        # 调用方链路：req() 把已收字节落盘，并置 last_truncated —— _send_prompt_impl
        # 只在这个标记为真时打 `SEND_TRUNCATED pfile=... st=... bytes=...` 并走 exit 3。
        tmp = _make_tmp(self, "guards-149-partial-")
        api = _bare_api(tmp, _OneShotTruncatedResponse(self.PARTIAL))
        out = io.StringIO()
        with redirect_stdout(out):
            status, text = api.req("GET",
                                   "https://chatgpt.com/backend-api/conversation/x",
                                   timeout=5)
        printed = out.getvalue()

        self.assertEqual(status, 200)
        # 已收字节不得丢：req() 的 IncompleteRead 分支会把 e.partial 再 append 一次
        # （现有实现如此，见其 except 分支），所以这里不钉总长度，只钉「partial 确实
        # 到了返回值与落盘件」——真实字节数由 last_partial_bytes 与文件长度自证一致。
        self.assertIn(self.PARTIAL.decode("utf-8"), text)
        self.assertIs(api.last_truncated, True)
        self.assertGreaterEqual(api.last_partial_bytes, len(self.PARTIAL))
        self.assertTrue(api.last_partial_path)
        self.assertTrue(api.last_partial_path.startswith(
            os.path.join(tmp, "timeouts") + os.sep))
        with open(api.last_partial_path, "rb") as f:
            saved = f.read()
        self.assertIn(self.PARTIAL, saved)
        self.assertEqual(len(saved), api.last_partial_bytes)
        # 标记必须可机读：MARKER + 字节数 + 落盘路径同时出现在输出里。
        self.assertIn("PARTIAL after", printed)
        self.assertIn("%d bytes buffered, saved to %s"
                      % (api.last_partial_bytes, api.last_partial_path), printed)
        self.assertIn("响应流被截断", printed)


if __name__ == "__main__":
    unittest.main()
