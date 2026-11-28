import asyncio
import threading
import time
import unittest

from src.session_ops.authority import Principal
from src.session_ops.mcp_auth import AuthContext
from src.session_ops.mcp_server import (
    _CHATGPT_READ_INFLIGHT_KEYS,
    _CHATGPT_READ_INFLIGHT_LOCK,
    _ResourceExhausted,
    _chatgpt_read_key,
    _run_chatgpt_read,
    _submit_chatgpt_read,
)


class ChatGPTReadAdmissionTests(unittest.TestCase):
    @staticmethod
    def _wait_key_released(key, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with _CHATGPT_READ_INFLIGHT_LOCK:
                if key not in _CHATGPT_READ_INFLIGHT_KEYS:
                    return
            time.sleep(0.005)
        raise AssertionError("read admission key was not released after worker exit")

    def test_read_key_is_scoped_to_principal_and_conversation(self):
        auth = AuthContext(
            Principal("tenant-a", "principal-a"),
            (),
            "https://issuer.example",
            "subject-a",
            "realm-a",
        )
        same = _chatgpt_read_key(
            "chatgpt.session.read", auth, {"conversation_id": "conv-1"}, "rpc-1"
        )
        replay = _chatgpt_read_key(
            "chatgpt.session.read", auth, {"conversation_id": "conv-1"}, "rpc-2"
        )
        other_conversation = _chatgpt_read_key(
            "chatgpt.session.read", auth, {"conversation_id": "conv-2"}, "rpc-1"
        )
        self.assertEqual(same, replay)
        self.assertNotEqual(same, other_conversation)

    def test_duplicate_read_is_rejected_until_worker_finishes(self):
        started = threading.Event()
        release = threading.Event()
        key = ("chatgpt.session.read", "issuer", "realm", "subject", "tenant", "principal", "conv")

        def worker():
            started.set()
            if not release.wait(2):
                raise TimeoutError("test worker release timed out")
            return "done"

        future = _submit_chatgpt_read(worker, key=key)
        self.assertTrue(started.wait(1))
        with self.assertRaises(_ResourceExhausted):
            _submit_chatgpt_read(lambda: "duplicate", key=key)
        release.set()
        self.assertEqual(future.result(timeout=2), "done")
        self._wait_key_released(key)
        next_future = _submit_chatgpt_read(lambda: "next", key=key)
        self.assertEqual(next_future.result(timeout=2), "next")
        self._wait_key_released(key)

    def test_waiter_timeout_keeps_key_until_underlying_worker_exits(self):
        started = threading.Event()
        release = threading.Event()
        key = ("chatgpt.session.read", "issuer", "realm", "subject", "tenant", "principal", "slow-conv")

        def worker():
            started.set()
            if not release.wait(2):
                raise TimeoutError("test worker release timed out")
            return "done"

        async def scenario():
            waiter = asyncio.create_task(
                _run_chatgpt_read(worker, timeout=0.02, key=key)
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            with self.assertRaises(asyncio.TimeoutError):
                await waiter
            with _CHATGPT_READ_INFLIGHT_LOCK:
                self.assertIn(key, _CHATGPT_READ_INFLIGHT_KEYS)
            with self.assertRaises(_ResourceExhausted):
                _submit_chatgpt_read(lambda: "duplicate", key=key)
            release.set()
            await asyncio.to_thread(self._wait_key_released, key)
            self.assertEqual(
                await _run_chatgpt_read(lambda: "after-release", timeout=1, key=key),
                "after-release",
            )

        asyncio.run(scenario())

    def test_cancelled_waiter_keeps_key_until_underlying_worker_exits(self):
        started = threading.Event()
        release = threading.Event()
        key = ("chatgpt.session.read", "issuer", "realm", "subject", "tenant", "principal", "cancelled-conv")

        def worker():
            started.set()
            if not release.wait(2):
                raise TimeoutError("test worker release timed out")
            return "done"

        async def scenario():
            waiter = asyncio.create_task(
                _run_chatgpt_read(worker, timeout=1, key=key)
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            with _CHATGPT_READ_INFLIGHT_LOCK:
                self.assertIn(key, _CHATGPT_READ_INFLIGHT_KEYS)
            with self.assertRaises(_ResourceExhausted):
                _submit_chatgpt_read(lambda: "duplicate", key=key)
            release.set()
            await asyncio.to_thread(self._wait_key_released, key)

        asyncio.run(scenario())

    def test_distinct_reads_can_use_bounded_capacity_concurrently(self):
        started = threading.Barrier(2)
        release = threading.Event()
        keys = (
            ("chatgpt.session.read", "issuer", "realm", "subject", "tenant", "principal", "conv-a"),
            ("chatgpt.session.read", "issuer", "realm", "subject", "tenant", "principal", "conv-b"),
        )

        def worker():
            started.wait(timeout=2)
            if not release.wait(2):
                raise TimeoutError("test worker release timed out")
            return "done"

        futures = [_submit_chatgpt_read(worker, key=key) for key in keys]
        with self.assertRaises(_ResourceExhausted):
            _submit_chatgpt_read(lambda: "overflow", key=("other",))
        release.set()
        self.assertEqual([future.result(timeout=2) for future in futures], ["done", "done"])
        for key in keys:
            self._wait_key_released(key)
