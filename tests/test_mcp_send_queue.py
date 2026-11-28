import threading
import unittest

from src.session_ops.mcp_server import (
    _BoundedExecutor,
    _ResourceExhausted,
    _SEND_MAX_INFLIGHT,
    _SEND_WORKERS,
    _exception_code,
)


class BoundedSendQueueTests(unittest.TestCase):
    def test_production_send_queue_is_single_consumer_and_bounded(self):
        self.assertEqual(_SEND_WORKERS, 1)
        self.assertEqual(_SEND_MAX_INFLIGHT, 9)
        self.assertEqual(_exception_code(_ResourceExhausted("full"))[0], "RESOURCE_EXHAUSTED")

    def test_queue_serializes_and_rejects_over_capacity(self):
        executor = _BoundedExecutor(
            max_workers=1,
            max_inflight=2,
            thread_name_prefix="test-session-ops-send",
        )
        started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        max_active = 0

        def blocking_send():
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            started.set()
            if not release.wait(3):
                raise AssertionError("test did not release the first send")
            with lock:
                active -= 1
            return "first"

        def queued_send():
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            with lock:
                active -= 1
            return "second"

        try:
            first = executor.submit(blocking_send)
            self.assertTrue(started.wait(1))
            second = executor.submit(queued_send)
            with self.assertRaises(_ResourceExhausted):
                executor.submit(lambda: "must not be admitted")

            release.set()
            self.assertEqual(first.result(timeout=2), "first")
            self.assertEqual(second.result(timeout=2), "second")
            self.assertEqual(max_active, 1)

            # Completed work releases its admission slot for the next call.
            self.assertEqual(executor.submit(lambda: "third").result(timeout=2), "third")
        finally:
            release.set()
            executor.shutdown(wait=True)

    def test_failed_task_also_releases_admission_slot(self):
        executor = _BoundedExecutor(
            max_workers=1,
            max_inflight=1,
            thread_name_prefix="test-session-ops-send-failure",
        )
        try:
            failed = executor.submit(lambda: 1 / 0)
            with self.assertRaises(ZeroDivisionError):
                failed.result(timeout=2)
            self.assertEqual(executor.submit(lambda: "recovered").result(timeout=2), "recovered")
        finally:
            executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
