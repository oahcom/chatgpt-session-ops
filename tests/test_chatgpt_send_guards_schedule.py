"""schedule / prompt / credential guards in scripts/chatgpt_send.py (issue #132).

These guards are all reducible to ``pass`` without turning the existing suite
red: the older tests only assert ``assertRaises``, so when a guard disappears
the *next* guard raises a different exception and the assertion still passes.

Every test here therefore pins the exception TYPE and the FULL message, and
pairs each rejection with an acceptance case (a guard that always raised would
fail the acceptance half).

Pinned guards (line numbers on main @ de92dc1):

    check_schedule      568 rrule 为空          570 rrule 含空白字符
                        587 rrule 缺 FREQ       589 FREQ 必须 HOURLY
                        595 INTERVAL 过长        597 INTERVAL 非十进制整数
    check_oneshot       640 scheduledAt 非 ISO  642 scheduledAt 缺时区
    validate_prompt_text 6508 空文件            6510 含 NUL
    _send_prompt_impl   6609 resolver 返回无效思考模型
    resolve_thinking_model 4810 ThinkingModelUnavailable
    ThinkingModelUnavailable.__init__ 4274 非法 reason_code
    ChatGPTAPI.__init__ 4314 非法 account       4333 cookies 非数组
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from scripts import chatgpt_send as sender


class ScheduleRruleGuardTests(unittest.TestCase):
    """check_schedule(): scripts/chatgpt_send.py:568,570,587,589,595,597."""

    def test_empty_rrule_rejected(self):
        """568: `if not raw` -> "rrule 为空"."""
        for bad in ("", "   ", "\t \t"):
            with self.subTest(rrule=bad):
                with self.assertRaises(sender.ScheduleError) as ctx:
                    sender.check_schedule(bad)
                self.assertEqual(str(ctx.exception), "rrule 为空")

    def test_accepts_minimal_hourly_rrule(self):
        """Acceptance pair for 568/570/587/589/595/597."""
        result = sender.check_schedule("FREQ=HOURLY;INTERVAL=1")
        self.assertEqual(result["rrule"], "FREQ=HOURLY;INTERVAL=1")
        self.assertEqual(result["executable_field"], "rrule")

    def test_internal_whitespace_rejected(self):
        """570: whitespace must not be silently patched away."""
        raw = "FREQ=HO URLY"
        expected = ("rrule 含空白字符（内部空格会让 'FREQ=HO URLY' 被悄悄"
                    "修补成合法输入，等于替用户改了输入）：%r" % raw)
        with self.assertRaises(sender.ScheduleError) as ctx:
            sender.check_schedule(raw)
        self.assertEqual(str(ctx.exception), expected)

    def test_missing_freq_rejected(self):
        """587: `freq is None` -> "rrule 缺 FREQ" (not an AttributeError)."""
        with self.assertRaises(sender.ScheduleError) as ctx:
            sender.check_schedule("INTERVAL=1")
        self.assertEqual(str(ctx.exception), "rrule 缺 FREQ")

    def test_non_hourly_freq_rejected(self):
        """589: only HOURLY is executable; the received value is echoed."""
        with self.assertRaises(sender.ScheduleError) as ctx:
            sender.check_schedule("FREQ=DAILY")
        self.assertEqual(
            str(ctx.exception),
            "FREQ 必须 HOURLY（周期固定 1 小时），收到 'DAILY'",
        )

    def test_overlong_interval_rejected(self):
        """595: >32 digits is rejected before the decimal check."""
        iv = "1" * 40
        with self.assertRaises(sender.ScheduleError) as ctx:
            sender.check_schedule("FREQ=HOURLY;INTERVAL=" + iv)
        self.assertEqual(
            str(ctx.exception),
            "INTERVAL 过长（40 位）：%r" % (iv[:32],),
        )

    def test_non_decimal_interval_rejected(self):
        """597: RFC 5545 decimal integer only — '1_0' is not a number here."""
        with self.assertRaises(sender.ScheduleError) as ctx:
            sender.check_schedule("FREQ=HOURLY;INTERVAL=1_0")
        self.assertEqual(
            str(ctx.exception),
            "INTERVAL 不是 RFC 十进制整数：'1_0'",
        )


class ScheduleOneshotGuardTests(unittest.TestCase):
    """check_oneshot(): scripts/chatgpt_send.py:640,642."""

    def test_non_iso_scheduled_at_rejected(self):
        """640: fromisoformat ValueError becomes ScheduleError, never escapes."""
        with self.assertRaises(sender.ScheduleError) as ctx:
            sender.check_oneshot("not-a-date")
        self.assertEqual(
            str(ctx.exception), "scheduledAt 不是 ISO 8601：'not-a-date'"
        )

    def test_scheduled_at_without_offset_rejected(self):
        """642: a naive timestamp cannot be scheduled."""
        raw = "2026-11-19T16:00:00"
        with self.assertRaises(sender.ScheduleError) as ctx:
            sender.check_oneshot(raw)
        self.assertEqual(
            str(ctx.exception),
            "scheduledAt 缺时区偏移（必须形如 2026-11-19T16:00:00+08:00）：%r"
            % raw,
        )

    def test_accepts_future_offset_timestamp(self):
        """Acceptance pair for 640/642."""
        now = sender.datetime(2026, 11, 19, 15, 0, 0,
                              tzinfo=timezone.utc)
        result = sender.check_oneshot("2026-11-19T16:00:00+00:00", now=now)
        self.assertEqual(result["scheduled_at"], "2026-11-19T16:00:00+00:00")
        self.assertEqual(result["seconds_from_now"], 3600)


class PromptTextGuardTests(unittest.TestCase):
    """validate_prompt_text() via load_prompt_text(): lines 6508,6510."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _prompt(self, name, payload):
        path = os.path.join(self.root, name)
        if isinstance(payload, bytes):
            with open(path, "wb") as f:
                f.write(payload)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(payload)
        return path

    def test_empty_prompt_file_rejected(self):
        """6508: an empty file must not become an empty prompt."""
        path = self._prompt("empty.md", "")
        with self.assertRaises(RuntimeError) as ctx:
            sender.load_prompt_text(path)
        self.assertEqual(str(ctx.exception), "提示词文件为空：%s" % path)

    def test_nul_prompt_file_rejected(self):
        """6510: NUL survives .strip(), so it needs its own guard."""
        path = self._prompt("nul.md", "正文" + chr(0) + "继续")
        with self.assertRaises(RuntimeError) as ctx:
            sender.load_prompt_text(path)
        self.assertEqual(
            str(ctx.exception),
            "提示词文件含 NUL 字符，疑似损坏：%s" % path,
        )

    def test_accepts_normal_prompt_file(self):
        """Acceptance pair for 6508/6510."""
        path = self._prompt("ok.md", "# 标题\n\n正文 with tab\tand CRLF\r\n")
        self.assertEqual(
            sender.load_prompt_text(path),
            "# 标题\n\n正文 with tab\tand CRLF",
        )


class _InvalidThinkingResolverAPI(object):
    """Minimal fake for _send_prompt_impl: resolver returns a non-thinking slug."""

    account = "acct1"
    is_anon = False
    last_status = 0
    last_body = ""
    last_truncated = False

    def __init__(self, model):
        self._model = model

    def resolve_thinking_model(self, api):
        return self._model

    def requirements(self):
        return "", ""

    def classify_failure(self, status, body):
        return "proxy"

    def repair_proxy(self):
        pass


class SendPromptThinkingGuardTests(unittest.TestCase):
    """_send_prompt_impl(): line 6609 — final line of defence for -t- slugs."""

    def test_resolver_returning_non_thinking_model_is_rejected(self):
        with mock.patch.object(sender.time, "sleep", lambda *_: None):
            with self.assertRaises(sender.ThinkingModelUnavailable) as ctx:
                sender._send_prompt_impl(
                    _InvalidThinkingResolverAPI("auto"),
                    None,
                    "p.md",
                    new_chat=True,
                    thinking=True,
                    text="正文",
                )
        exc = ctx.exception
        self.assertEqual(exc.reason_code, "THINKING_MODEL_UNAVAILABLE")
        self.assertEqual(exc.account, "acct1")
        self.assertEqual(exc.model_slug, "auto")
        self.assertIn("resolver 返回了无效思考模型", exc.detail)
        self.assertEqual(
            str(exc),
            "THINKING_MODEL_UNAVAILABLE: account=acct1: "
            "resolver 返回了无效思考模型：'auto'",
        )

    def test_falsy_resolver_model_is_rejected(self):
        """The guard is `not model or "-t-" not in str(model)` — falsy dies too."""
        with mock.patch.object(sender.time, "sleep", lambda *_: None):
            with self.assertRaises(sender.ThinkingModelUnavailable) as ctx:
                sender._send_prompt_impl(
                    _InvalidThinkingResolverAPI(""),
                    None,
                    "p.md",
                    new_chat=True,
                    thinking=True,
                    text="正文",
                )
        self.assertEqual(ctx.exception.reason_code,
                         "THINKING_MODEL_UNAVAILABLE")

    def test_valid_thinking_model_passes_the_guard(self):
        """Acceptance pair: a -t- slug is allowed through (later failure only)."""
        with mock.patch.object(sender.time, "sleep", lambda *_: None):
            try:
                result = sender._send_prompt_impl(
                    _InvalidThinkingResolverAPI("gpt-6-t-mini"),
                    None,
                    "p.md",
                    new_chat=True,
                    thinking=True,
                    text="正文",
                )
            except sender.ThinkingModelUnavailable as exc:
                self.fail("valid -t- model rejected: %s" % exc)
        # requirements() is empty in the fake, so the attempt loop bails out.
        self.assertEqual(result, (0, None, None))


class ThinkingModelUnavailableGuardTests(unittest.TestCase):
    """ThinkingModelUnavailable + resolve_thinking_model: lines 4274,4810."""

    def test_invalid_reason_code_rejected(self):
        """4274: only the three documented reason codes are constructible."""
        with self.assertRaises(ValueError) as ctx:
            sender.ThinkingModelUnavailable("NOPE", "acct1", "detail")
        self.assertEqual(str(ctx.exception),
                         "invalid thinking failure reason: 'NOPE'")

    def test_valid_reason_code_accepted(self):
        """Acceptance pair for 4274."""
        exc = sender.ThinkingModelUnavailable(
            "AUTH_INVALID", "acct1", "detail"
        )
        self.assertEqual(exc.reason_code, "AUTH_INVALID")
        self.assertEqual(str(exc), "AUTH_INVALID: account=acct1: detail")

    def test_explicit_non_thinking_model_raises(self):
        """4810: explicit CHATGPT_MODEL without -t- must not be cached as auto."""
        with mock.patch.dict(os.environ, {"CHATGPT_MODEL": "gpt-4o"}):
            with self.assertRaises(sender.ThinkingModelUnavailable) as ctx:
                sender.ChatGPTAPI.resolve_thinking_model(
                    _InvalidThinkingResolverAPI("auto"), cache={}
                )
        exc = ctx.exception
        self.assertEqual(exc.reason_code, "THINKING_MODEL_UNAVAILABLE")
        self.assertEqual(exc.account, "acct1")
        self.assertEqual(
            str(exc),
            "THINKING_MODEL_UNAVAILABLE: account=acct1: "
            "CHATGPT_MODEL='gpt-4o' 不是思考模型 slug",
        )

    def test_explicit_thinking_model_returned_and_cached(self):
        """Acceptance pair for 4810."""
        cache = {}
        with mock.patch.dict(os.environ, {"CHATGPT_MODEL": "gpt-6-t-mini"}):
            slug = sender.ChatGPTAPI.resolve_thinking_model(
                _InvalidThinkingResolverAPI("auto"), cache=cache
            )
        self.assertEqual(slug, "gpt-6-t-mini")
        self.assertEqual(cache[("acct1", "gpt-6-t-mini")], "gpt-6-t-mini")


class CredentialIdentityGuardTests(unittest.TestCase):
    """ChatGPTAPI.__init__: lines 4314 (account) and 4333 (cookies shape)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        patcher = mock.patch.multiple(
            sender,
            resolve_proxy_info=mock.Mock(return_value={"source": "test"}),
            require_proxy=mock.Mock(return_value="http://127.0.0.1:9"),
            build_opener_for=mock.Mock(return_value=object()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_account(self, account, cookies):
        base = os.path.join(self.root, "accounts", account)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "cookies.json"), "w", encoding="utf-8") as f:
            json.dump(cookies, f)
        with open(os.path.join(base, "access_token.txt"), "w", encoding="utf-8") as f:
            f.write("token\n")
        return base

    def test_unknown_account_rejected(self):
        """4314: account must be a configured identity, not a path fragment."""
        with self.assertRaises(ValueError) as ctx:
            sender.ChatGPTAPI(base_dir=self.root, account="acct7")
        message = str(ctx.exception)
        self.assertTrue(message.startswith("account must be one of "),
                        message)
        self.assertIn("acct1", message)
        self.assertIn("nologin", message)

    def test_accepts_configured_account(self):
        """Acceptance pair for 4314: a known identity constructs normally."""
        self._write_account("acct2", [
            {"name": "a", "value": "b", "domain": ".chatgpt.com"},
        ])
        api = sender.ChatGPTAPI(base_dir=self.root, account="acct2")
        self.assertEqual(api.account, "acct2")
        self.assertEqual(api.cookie, "a=b")

    def test_non_list_cookies_rejected(self):
        """4333: cookies.json must be an array, not an object."""
        base = self._write_account("acct3", {"not": "a list"})
        path = os.path.join(base, "cookies.json")
        with self.assertRaises(ValueError) as ctx:
            sender.ChatGPTAPI(base_dir=self.root, account="acct3")
        self.assertEqual(
            str(ctx.exception), "%s 格式不对：应为 cookie 数组" % path
        )

    def test_nologin_skips_cookie_files(self):
        """Acceptance pair for 4333: the anon identity needs no cookie array."""
        api = sender.ChatGPTAPI(base_dir=self.root, account="nologin")
        self.assertEqual(api.cookies, [])
        self.assertEqual(api.token, "")


if __name__ == "__main__":
    unittest.main()
