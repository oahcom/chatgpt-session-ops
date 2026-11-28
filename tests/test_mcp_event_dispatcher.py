import threading
import unittest

from src.session_ops.mcp_event_dispatcher import MCPEventDispatcher


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
    def test_single_dispatcher_delivers_local_events_without_collector(self):
        store = Store()
        authorize = lambda **_kwargs: True
        dispatcher = MCPEventDispatcher(
            store,
            authorize=authorize,
            poll_seconds=0.05,
            delivery_batch_size=1,
            cleanup_interval_seconds=60,
        )
        dispatcher.start()
        try:
            self.assertTrue(store.delivered.wait(1))
            self.assertEqual(dispatcher._thread.name, "mcp-events")
            self.assertFalse(dispatcher._thread.daemon)
            self.assertEqual(store.cleanup_calls, 1)
            self.assertEqual(store.delivery_calls[0], (1, authorize))
        finally:
            dispatcher.stop()
        self.assertIsNone(dispatcher._thread)

    def test_start_is_idempotent_and_stop_joins_the_only_thread(self):
        store = Store()
        dispatcher = MCPEventDispatcher(store, poll_seconds=0.05)
        dispatcher.start()
        first_thread = dispatcher._thread
        dispatcher.start()
        self.assertIs(dispatcher._thread, first_thread)
        dispatcher.stop()
        self.assertFalse(first_thread.is_alive())
        self.assertIsNone(dispatcher._thread)

    def test_stop_fails_closed_when_inflight_operation_does_not_finish(self):
        class BusyLock:
            def acquire(self, timeout=None):
                return False
            def release(self):
                raise AssertionError("release must not be called")

        class LiveThread:
            def is_alive(self):
                return True
            def join(self, timeout=None):
                raise AssertionError("join must not run while operation lock is busy")

        dispatcher = MCPEventDispatcher(Store())
        dispatcher._operation_lock = BusyLock()
        dispatcher._stop_event = threading.Event()
        dispatcher._thread = LiveThread()
        with self.assertRaises(TimeoutError):
            dispatcher.stop()

    def test_stop_fails_if_thread_remains_alive_after_join(self):
        class AvailableLock:
            def acquire(self, timeout=None):
                return True
            def release(self):
                return None

        class StuckThread:
            def is_alive(self):
                return True
            def join(self, timeout=None):
                return None

        dispatcher = MCPEventDispatcher(Store())
        dispatcher._operation_lock = AvailableLock()
        dispatcher._stop_event = threading.Event()
        dispatcher._thread = StuckThread()
        with self.assertRaises(RuntimeError):
            dispatcher.stop()
