"""Regression tests for AID-2026-011.

These tests isolate rotation state in a temporary directory.
They never access real credentials, account directories, or the network.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("CHATGPT_SEND_PY") or os.path.join(
    HERE, "..", "scripts", "chatgpt_send.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "chatgpt_send_aid_2026_011_under_test",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load_module()

LEGACY_ROTATION_PRIMARY = ("acct1", "acct2", "acct3", "acct4", "acct5", "acct6")


def initial_rotation_state():
    return {
        "order": ["acct1", "acct2", "acct3", "acct4"],
        "cursor": 0,
        "created": {},
        "failures": {},
        "unavailable": {},
        "fallback_uses": 0,
        "last": {},
        "updated": 0,
    }


class StubAPI:
    """Credential-free ChatGPTAPI replacement."""

    def __init__(self, account):
        self.account = account
        self.is_anon = account == "nologin"


class SendPromptAPI:
    """Minimal API surface required by send_prompt(new_chat=True)."""

    def __init__(self, status, body):
        self.account = "acct1"
        self.is_anon = False
        self.status = status
        self.body = body

    def requirements(self):
        return "test-requirements-token", "test-proof"

    def send(
        self,
        conv_id,
        text,
        tail,
        req_token,
        proof,
        model=None,
        timeout=420,
    ):
        return self.status, self.body


class AID2026011Tests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aid-2026-011-")
        self._rot = m.ROTATION_FILE
        self._conv = m.CONV_MAP_FILE
        self._base = m.BASE

        # 会话日志按 BASE 落盘：不隔离就会把测试提示词写进真实运行目录
        m.BASE = self.tmp

        m.ROTATION_FILE = os.path.join(
            self.tmp, "account-rotation.json"
        )
        m.CONV_MAP_FILE = os.path.join(
            self.tmp, "conv-accounts.json"
        )

        self.write_rotation(initial_rotation_state())
        self._discovery = mock.patch.object(
            m, "_rotation_primary", return_value=list(LEGACY_ROTATION_PRIMARY)
        )
        self._discovery.start()
        self.addCleanup(self._discovery.stop)

    def tearDown(self):
        m.BASE = self._base
        m.ROTATION_FILE = self._rot
        m.CONV_MAP_FILE = self._conv

        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_rotation(self, state):
        with open(m.ROTATION_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

    def read_rotation(self):
        return m._load_json(m.ROTATION_FILE, {})

    def test_200_body_containing_historical_token_is_not_413(self):
        """Assert that response-body content cannot turn HTTP 200 into 413."""

        body = (
            "HTTP response body containing "
            "conversation_too_large"
        )

        prompt_file = os.path.join(self.tmp, "prompt.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write("test prompt\n")

        api = SendPromptAPI(200, body)

        with mock.patch.object(m.time, "sleep"):
            status, path, conv = m.send_prompt(
                api,
                None,
                prompt_file,
                new_chat=True,
                thinking=False,
            )

        self.assertNotEqual(
            status,
            413,
            "An HTTP 200 response must not be reclassified as HTTP 413",
        )

        self.assertEqual(
            status,
            1,
            "Without a structured conversation_id, the existing "
            "A11 boundary must reject the response",
        )

        self.assertIsNone(path)
        self.assertTrue(
            conv.mismatch,
            "Unresolved conversation identity must remain explicit",
        )

    def test_real_413_terminates_rotation_without_retry_or_fallback(self):
        """Assert one 413 attempt, no second account, and no nologin."""

        calls = []

        def fake_send_prompt(api, *args, **kwargs):
            calls.append(api.account)
            return 413, None, None

        with (
            mock.patch.object(m, "ChatGPTAPI", StubAPI),
            mock.patch.object(m, "send_prompt", side_effect=fake_send_prompt),
            mock.patch.object(m, "_attempt_create", wraps=m._attempt_create) as attempt,
            mock.patch("builtins.print"),
        ):
            result = m.create_chat_rotating("unused-prompt.txt")

        self.assertEqual(result, 1)
        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(calls, ["acct1"])

        self.assertNotIn(
            "nologin",
            calls,
            "HTTP 413 must never open anonymous fallback",
        )

    def test_real_413_does_not_increment_failure_or_add_cooldown(self):
        """Assert actual _attempt_create leaves account failure state unchanged."""

        with (
            mock.patch.object(m, "ChatGPTAPI", StubAPI),
            mock.patch.object(
                m,
                "send_prompt",
                return_value=(413, None, None),
            ),
            mock.patch("builtins.print"),
        ):
            st = m.load_rotation()
            attempts = []

            status, conv = m._attempt_create(
                st,
                "acct1",
                "unused-prompt.txt",
                attempts,
            )

        self.assertEqual(status, 413)
        self.assertIsNone(conv)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], 413)

        saved = self.read_rotation()

        self.assertEqual(
            saved["failures"].get("acct1", 0),
            0,
            "413 must not increment the account failure counter",
        )

        self.assertNotIn(
            "acct1",
            saved["unavailable"],
            "413 must not create an account cooldown entry",
        )

    def test_413_does_not_open_anonymous_fallback(self):
        """Assert 413 is terminal even when anonymous fallback is eligible."""

        calls = []

        def fake_attempt(st, name, prompt_file, attempts, thinking=None):
            calls.append(name)
            attempts.append({
                "account": name,
                "status": 413,
                "conv_id": None,
            })
            return 413, None

        with (
            mock.patch.object(m, "_attempt_create", side_effect=fake_attempt),
            mock.patch("builtins.print"),
        ):
            result = m.create_chat_rotating("unused-prompt.txt")

        self.assertEqual(result, 1)
        self.assertEqual(calls, ["acct1"])
        self.assertNotIn("nologin", calls)

    def test_unknown_send_outcome_stops_account_rotation(self):
        calls = []

        def fake_attempt(st, name, prompt_file, attempts, thinking=None):
            calls.append(name)
            attempts.append({"account": name, "status": "OUTCOME_UNKNOWN"})
            return "OUTCOME_UNKNOWN", None

        with (
            mock.patch.object(m, "_attempt_create", side_effect=fake_attempt),
            mock.patch("builtins.print"),
        ):
            result = m.create_chat_rotating("unused-prompt.txt")

        self.assertEqual(result, 2)
        self.assertEqual(calls, ["acct1"])
        self.assertNotIn("nologin", calls)

    def test_ambiguous_convref_stops_rotation_before_next_account(self):
        with tempfile.TemporaryDirectory(prefix="so-unknown-convref-") as directory:
            prompt = os.path.join(directory, "prompt.txt")
            with open(prompt, "w", encoding="utf-8") as stream:
                stream.write("hello")
            state = initial_rotation_state()
            attempts = []
            candidate = m.ConvRef(
                None, mismatch=True,
                reason="conversation_id_unresolved: no unique SSE id",
            )
            with (
                mock.patch.object(m, "ChatGPTAPI", StubAPI),
                mock.patch.object(m, "send_prompt", return_value=(1, None, candidate)),
            ):
                status, conversation_id = m._attempt_create(
                    state, "acct1", prompt, attempts
                )
            self.assertEqual(status, "OUTCOME_UNKNOWN")
            self.assertIsNone(conversation_id)
            self.assertEqual(attempts[0]["status"], "OUTCOME_UNKNOWN")

    def test_send_transport_error_is_unknown_not_safe_to_rotate(self):
        api = object.__new__(m.ChatGPTAPI)
        api.req = lambda *args, **kwargs: (0, "EXC connection reset")
        status, _body = api.send(None, "prompt", None, "token", "proof")
        self.assertEqual(status, 0)
        self.assertEqual(api.last_send_certainty, "UNKNOWN")

        api.req = lambda *args, **kwargs: (429, "rate limited")
        status, _body = api.send(None, "prompt", None, "token", "proof")
        self.assertEqual(status, 429)
        self.assertEqual(api.last_send_certainty, "NOT_SUBMITTED")

    def test_send_transport_error_raises_unknown_outcome(self):
        class AmbiguousAPI:
            account = "acct1"
            is_anon = False
            status = 0
            certainty = "UNKNOWN"

            def requirements(self):
                return "token", "proof"

            def send(self, *args, **kwargs):
                self.last_send_certainty = self.certainty
                return self.status, "ambiguous response"

        with tempfile.TemporaryDirectory(prefix="so-unknown-send-") as directory:
            prompt = os.path.join(directory, "prompt.txt")
            with open(prompt, "w", encoding="utf-8") as stream:
                stream.write("hello")
            with self.assertRaises(m.RemoteSendOutcomeUnknown):
                m._send_prompt_impl(
                    AmbiguousAPI(), None, prompt, new_chat=True, thinking=False
                )
            accepted = AmbiguousAPI()
            accepted.status = 202
            accepted.certainty = "SUBMITTED"
            with self.assertRaises(m.RemoteSendOutcomeUnknown):
                m._send_prompt_impl(
                    accepted, None, prompt, new_chat=True, thinking=False
                )

    def test_401_403_429_preserve_failure_and_cooldown_behavior(self):
        """Assert failure counts, unavailable reasons, and existing TTLs."""

        cases = (
            (401, None),
            (403, m.COOLDOWN_403),
            (429, m.COOLDOWN_429),
        )

        for status, expected_ttl in cases:
            with self.subTest(status=status):
                self.write_rotation(initial_rotation_state())

                mark_calls = []

                original_mark_unavailable = m.mark_unavailable

                def recording_mark_unavailable(
                    state,
                    name,
                    reason,
                    ttl=None,
                ):
                    mark_calls.append({
                        "account": name,
                        "reason": reason,
                        "ttl": ttl,
                    })
                    return original_mark_unavailable(
                        state,
                        name,
                        reason,
                        ttl,
                    )

                with (
                    mock.patch.object(m, "ChatGPTAPI", StubAPI),
                    mock.patch.object(
                        m,
                        "send_prompt",
                        return_value=(status, None, None),
                    ),
                    mock.patch.object(
                        m,
                        "mark_unavailable",
                        side_effect=recording_mark_unavailable,
                    ),
                    mock.patch("builtins.print"),
                ):
                    st = m.load_rotation()
                    attempts = []

                    actual_status, conv = m._attempt_create(
                        st,
                        "acct1",
                        "unused-prompt.txt",
                        attempts,
                    )

                self.assertEqual(actual_status, status)
                self.assertIsNone(conv)

                saved = self.read_rotation()

                self.assertEqual(
                    saved["failures"].get("acct1", 0),
                    1,
                    "401/403/429 must each increment failure count",
                )

                self.assertEqual(
                    len(mark_calls),
                    1,
                    "Each of these statuses must call mark_unavailable",
                )

                recorded = mark_calls[0]

                self.assertEqual(recorded["account"], "acct1")
                self.assertTrue(recorded["reason"])

                self.assertEqual(
                    recorded["ttl"],
                    expected_ttl,
                    "401 must have no TTL; 403/429 must preserve "
                    "their existing cooldown TTLs",
                )

                self.assertIn(
                    "acct1",
                    saved["unavailable"],
                    "401/403/429 must preserve unavailable-state recording",
                )

                self.assertEqual(
                    saved["unavailable"]["acct1"]["reason"],
                    recorded["reason"],
                )

    def test_all_logged_in_429_still_allows_anonymous_fallback(self):
        """Assert the existing all-429 fallback gate remains functional."""

        calls = []

        def fake_attempt(st, name, prompt_file, attempts, thinking=None):
            calls.append(name)

            if name in LEGACY_ROTATION_PRIMARY:
                attempts.append({
                    "account": name,
                    "status": 429,
                    "conv_id": None,
                })
                return 429, None

            attempts.append({
                "account": name,
                "status": 200,
                "conv_id": None,
            })
            return 200, None

        with (
            mock.patch.object(m, "_attempt_create", side_effect=fake_attempt),
            mock.patch("builtins.print"),
        ):
            result = m.create_chat_rotating("unused-prompt.txt")

        self.assertEqual(result, 0)

        self.assertEqual(len(calls), len(LEGACY_ROTATION_PRIMARY) + 1)
        self.assertEqual(set(calls[:len(LEGACY_ROTATION_PRIMARY)]), set(LEGACY_ROTATION_PRIMARY))
        self.assertEqual(calls[len(LEGACY_ROTATION_PRIMARY)], "nologin")

    def test_200_without_conv_id_stops_rotation(self):
        """Assert a 200 without identity does not cause another account attempt."""

        calls = []

        def fake_attempt(st, name, prompt_file, attempts, thinking=None):
            calls.append(name)
            attempts.append({
                "account": name,
                "status": 200,
                "conv_id": None,
            })
            return 200, None

        with (
            mock.patch.object(m, "_attempt_create", side_effect=fake_attempt),
            mock.patch("builtins.print"),
        ):
            result = m.create_chat_rotating("unused-prompt.txt")

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("nologin", calls)


if __name__ == "__main__":
    unittest.main()
