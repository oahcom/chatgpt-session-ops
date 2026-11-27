import unittest

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
            batch_size=17,
            cleanup_interval_seconds=60,
        )
        stop = StopAfterWait()
        worker.run_forever(stop)
        self.assertEqual(store.cleanup_calls, 1)
        self.assertEqual(store.delivery_calls, [(17, authorize)])
        self.assertEqual(stop.waits, 1)


if __name__ == "__main__":
    unittest.main()
