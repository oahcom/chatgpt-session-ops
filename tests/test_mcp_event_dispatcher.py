import threading
import unittest

from src.session_ops.mcp_event_dispatcher import MCPEventDispatcher


class Collector:
    def __init__(self):
        self.called = threading.Event()

    def run_once(self):
        self.called.set()
        return 0


class Store:
    def __init__(self):
        self.cleanup_calls = 0
        self.delivery_calls = []
        self.delivered = threading.Event()

    def cleanup_expired(self):
        self.cleanup_calls += 1
        raise RuntimeError("temporary sqlite maintenance failure")

    def deliver_pending(self, *, limit, authorize=None):
        self.delivery_calls.append((limit, authorize))
        self.delivered.set()
        return 0


class MCPEventDispatcherTests(unittest.TestCase):
    def test_single_dispatcher_collects_and_delivers_without_extra_threads(self):
        collector = Collector()
        store = Store()
        authorize = lambda **_kwargs: True
        dispatcher = MCPEventDispatcher(
            collector,
            store,
            authorize=authorize,
            poll_seconds=0.05,
            delivery_batch_size=1,
            cleanup_interval_seconds=60,
        )
        dispatcher.start()
        try:
            self.assertTrue(collector.called.wait(1))
            self.assertTrue(store.delivered.wait(1))
            self.assertEqual(dispatcher._thread.name, "mcp-events")
            self.assertEqual(store.cleanup_calls, 1)
            self.assertEqual(store.delivery_calls[0], (1, authorize))
        finally:
            dispatcher.stop()
        self.assertIsNone(dispatcher._thread)

    def test_start_is_idempotent_and_stop_joins_the_only_thread(self):
        collector = Collector()
        store = Store()
        dispatcher = MCPEventDispatcher(collector, store, poll_seconds=0.05)
        dispatcher.start()
        first_thread = dispatcher._thread
        dispatcher.start()
        self.assertIs(dispatcher._thread, first_thread)
        dispatcher.stop()
        self.assertFalse(first_thread.is_alive())
        self.assertIsNone(dispatcher._thread)
