# -*- coding: utf-8 -*-
"""#151：钉住 req() 不把 IncompleteRead.partial 追加两次。

缺陷：`_read_stream()` 在 except 里把 partial 追加进 sink 后裸 raise；旧版 `req()`
捕获同一异常后又 append 一次 → 13 字节 partial 变成 26 字节，正文重复、落盘件翻倍。

本用例只走异常路径，不触网、不需要凭据：假 opener 的 read(16384) 第一次抛
IncompleteRead 携带 13 字节，之后 EOF。
"""
import http.client
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "chatgpt_send.py")

SPEC = importlib.util.spec_from_file_location(
    "chatgpt_send_151_partial",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PARTIAL = b"partial-bytes"  # 13 字节


class _FakeResp(object):
    """上下文管理器式假响应：首次 read 抛 IncompleteRead，之后 EOF。"""

    def __init__(self, partial):
        self._partial = partial
        self._raised = False
        self.status = 200
        self.headers = {}

    def read(self, amt=None):
        if not self._raised:
            self._raised = True
            raise http.client.IncompleteRead(self._partial, 100)
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener(object):
    def __init__(self, resp):
        self._resp = resp

    def open(self, rq, timeout=None):
        return self._resp


class PartialNotDuplicatedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cgpt-151-")

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                try:
                    os.unlink(os.path.join(root, name))
                except OSError:
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(root, name))
                except OSError:
                    pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _make_api(self):
        api = object.__new__(MODULE.ChatGPTAPI)
        api.account = "default"
        api.base_dir = self.tmp
        api.is_anon = False
        api.cookie = ""
        api.token = ""
        api._opener = _FakeOpener(_FakeResp(PARTIAL))
        api.last_headers = {}
        api.last_body = ""
        api.last_status = 0
        api.quota = {}
        api.last_truncated = False
        api.last_partial_path = None
        api.last_partial_bytes = 0
        return api

    def test_partial_is_appended_once(self):
        api = self._make_api()
        buf = io.StringIO()
        with redirect_stdout(buf):
            status, text = api.req("POST", "https://chatgpt.com/backend-api/conversation")

        out = buf.getvalue()
        self.assertEqual(status, 200)
        self.assertIs(api.last_truncated, True)
        # 13 而不是 26：partial 只应保留一份
        self.assertEqual(api.last_partial_bytes, 13)
        self.assertEqual(text, PARTIAL.decode())
        self.assertEqual(text.count("partial-bytes"), 1)

        path = api.last_partial_path
        self.assertIsNotNone(path)
        with open(path, "rb") as f:
            saved = f.read()
        self.assertEqual(len(saved), 13)
        self.assertEqual(saved, PARTIAL)

        self.assertIn("PARTIAL after", out)
        self.assertIn("响应流被截断", out)


if __name__ == "__main__":
    unittest.main()
