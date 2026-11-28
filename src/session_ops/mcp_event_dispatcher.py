"""One in-process dispatcher for Sender completion events and MCP webhooks."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any


class MCPEventDispatcher:
    """Bridge Sender outbox events to subscribed ChatGPT callbacks.

    This is one lightweight thread inside the existing MCP service, not a
    separate service or job-execution worker. Sender jobs continue to run in
    the existing Sender worker.
    """

    def __init__(
        self,
        collector: Any,
        events_store: Any,
        *,
        authorize: Any = None,
        poll_seconds: float = 1.0,
        delivery_batch_size: int = 1,
        cleanup_interval_seconds: float = 3600,
    ) -> None:
        if not 0.05 <= float(poll_seconds) <= 60:
            raise ValueError("poll_seconds must be in 0.05..60")
        if isinstance(delivery_batch_size, bool) or not isinstance(delivery_batch_size, int) or not 1 <= delivery_batch_size <= 2:
            raise ValueError("delivery_batch_size must be in 1..2")
        if not 60 <= float(cleanup_interval_seconds) <= 86400:
            raise ValueError("cleanup_interval_seconds must be in 60..86400")
        self.collector = collector
        self.events_store = events_store
        self.authorize = authorize
        self.poll_seconds = float(poll_seconds)
        self.delivery_batch_size = delivery_batch_size
        self.cleanup_interval_seconds = float(cleanup_interval_seconds)
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _run(self, stop_event: threading.Event) -> None:
        last_cleanup = float("-inf")
        while not stop_event.is_set():
            did_work = False
            try:
                did_work = self.collector.run_once() > 0 or did_work
            except Exception:
                logging.exception("MCP Sender event collection failed")

            # Shutdown may be requested while the Sender request is in flight.
            # Finish that claim/ack cycle, but do not start cleanup or a new
            # webhook request after the stop signal has arrived.
            if stop_event.is_set():
                break

            now = time.monotonic()
            if now - last_cleanup >= self.cleanup_interval_seconds:
                try:
                    self.events_store.cleanup_expired()
                except Exception:
                    logging.exception("MCP event retention cleanup failed")
                    last_cleanup = now - self.cleanup_interval_seconds + min(60.0, self.cleanup_interval_seconds)
                else:
                    last_cleanup = now

            if stop_event.is_set():
                break

            try:
                delivered = self.events_store.deliver_pending(
                    limit=self.delivery_batch_size,
                    authorize=self.authorize,
                )
                did_work = delivered > 0 or did_work
            except Exception:
                logging.exception("MCP webhook delivery failed")

            if not did_work:
                stop_event.wait(self.poll_seconds)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(stop_event,),
                name="mcp-events",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
            if stop_event is not None:
                stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=12)
        with self._lock:
            if thread is None or not thread.is_alive():
                self._thread = None
                self._stop_event = None
