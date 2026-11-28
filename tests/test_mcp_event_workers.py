import threading
import unittest
from unittest.mock import patch

from src.session_ops.mcp_event_workers import MCPEventDeliveryWorker


class StopAfterWait:
    def __init__(self):
        self.stopped = False
        self.waits = 0

    def is_set(self):
        return self.stopped

    def wait(self, _seconds):
        self.waits += 1
        self.stopped = True


class Store:
    def __init__(self):
        self.cleanup_calls = 0
        self.delivery_calls = []

    def cleanup_expired(self):
        self.cleanup_calls += 1
        raise RuntimeError("temporary sqlite maintenance failure")

    def deliver_pending(self, *, limit, authorize):
        self.delivery_calls.append((limit, authorize))
        return 0


class MCPEventDeliveryWorkerTests(unittest.TestCase):
    def test_cleanup_failure_does_not_block_delivery_and_authorizer_is_forwarded(self):
        store = Store()
        authorize = lambda **_kwargs: True
        worker = MCPEventDeliveryWorker(
            store,
            authorize=authorize,
            poll_seconds=0.05,
            batch_size=2,
            cleanup_interval_seconds=60,
        )
        stop = StopAfterWait()
        with patch("src.session_ops.mcp_event_workers.time.monotonic", return_value=1.0):
            worker.run_forever(stop)
        self.assertEqual(store.cleanup_calls, 1)
        self.assertEqual(store.delivery_calls, [(2, authorize)])
        self.assertEqual(stop.waits, 1)


    def test_timed_out_worker_generation_is_retained_and_not_restarted(self):
        class BlockingRole:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()
                self.calls = 0
                self.stop_events = []

            def run_forever(self, stop_event, **_kwargs):
                self.calls += 1
                self.stop_events.append(stop_event)
                self.started.set()
                self.release.wait(timeout=3)

        from src.session_ops.mcp_event_workers import MCPEventWorkers
        collector = BlockingRole()
        delivery = BlockingRole()
        manager = MCPEventWorkers(collector, delivery)
        manager.start()
        self.assertTrue(collector.started.wait(timeout=1))
        self.assertTrue(delivery.started.wait(timeout=1))
        first_generation = list(manager._threads)
        first_stop = manager._stop_event

        # Simulate the bounded join expiring while the worker is still alive.
        with patch("threading.Thread.join", lambda self, timeout=None: None):
            manager.stop()
        self.assertEqual(len(manager._threads), 2)
        self.assertTrue(all(thread.is_alive() for thread in manager._threads))
        manager.start()
        self.assertEqual(collector.calls, 1)
        self.assertEqual(delivery.calls, 1)
        self.assertTrue(first_stop.is_set())

        collector.release.set()
        delivery.release.set()
        for thread in first_generation:
            thread.join(timeout=1)
        manager.start()
        self.assertEqual(collector.calls, 2)
        self.assertEqual(delivery.calls, 2)
        self.assertIsNot(manager._stop_event, first_stop)
        manager.stop()



if __name__ == "__main__":
    unittest.main()
