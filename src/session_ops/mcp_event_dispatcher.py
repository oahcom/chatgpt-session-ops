"""One in-process dispatcher for locally persisted MCP completion events."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any


class MCPEventDispatcher:
    """Deliver completion events; ChatGPT execution stays in this repository."""

    def __init__(self, events_store: Any, *, authorize: Any = None,
                 poll_seconds: float = 1.0, delivery_batch_size: int = 1,
                 cleanup_interval_seconds: float = 3600) -> None:
        if not 0.05 <= float(poll_seconds) <= 60:
            raise ValueError("poll_seconds must be in 0.05..60")
        if isinstance(delivery_batch_size, bool) or not isinstance(delivery_batch_size, int) or not 1 <= delivery_batch_size <= 2:
            raise ValueError("delivery_batch_size must be in 1..2")
        if not 60 <= float(cleanup_interval_seconds) <= 86400:
            raise ValueError("cleanup_interval_seconds must be in 60..86400")
        self.events_store = events_store
        self.authorize = authorize
        self.poll_seconds = float(poll_seconds)
        self.delivery_batch_size = delivery_batch_size
        self.cleanup_interval_seconds = float(cleanup_interval_seconds)
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._operation_lock = threading.Lock()

    def _run(self, stop_event: threading.Event) -> None:
        last_cleanup = float("-inf")
        while not stop_event.is_set():
            did_work = False
            now = time.monotonic()
            if now - last_cleanup >= self.cleanup_interval_seconds:
                try:
                    with self._operation_lock:
                        if stop_event.is_set():
                            break
                        self.events_store.cleanup_expired()
                except Exception:
                    logging.exception("MCP event retention cleanup failed")
                    last_cleanup = now - self.cleanup_interval_seconds + min(60.0, self.cleanup_interval_seconds)
                else:
                    last_cleanup = now
            if stop_event.is_set():
                break
            try:
                with self._operation_lock:
                    if stop_event.is_set():
                        break
                    delivered = self.events_store.deliver_pending(
                        limit=self.delivery_batch_size, authorize=self.authorize
                    )
                did_work = delivered > 0
            except Exception:
                logging.exception("MCP webhook delivery failed")
            if not did_work:
                stop_event.wait(self.poll_seconds)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            stop_event = threading.Event()
            thread = threading.Thread(target=self._run, args=(stop_event,),
                                      name="mcp-events", daemon=False)
            self._stop_event = stop_event
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        # Serialize the complete stop/join/clear sequence with start(). If the
        # lock were released before join, a concurrent start could replace the
        # thread reference and then have that new reference cleared by this
        # older stop call.
        with self._lock:
            stop_event, thread = self._stop_event, self._thread
            if stop_event is not None:
                stop_event.set()
                # The current operation is bounded by SQLite's 5s busy timeout
                # or the callback's 5s network timeout. Leave margin for cleanup.
                if not self._operation_lock.acquire(timeout=30):
                    raise TimeoutError("MCP event dispatcher did not leave its in-flight operation")
                self._operation_lock.release()
            if thread is not None and thread.is_alive():
                thread.join(timeout=3)
                if thread.is_alive():
                    raise RuntimeError("MCP event dispatcher thread did not terminate")
            if self._thread is thread:
                self._thread = None
                self._stop_event = None
