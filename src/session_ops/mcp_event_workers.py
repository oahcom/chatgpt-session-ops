"""Supervised background loops for MCP event collection and webhook delivery."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any


class MCPEventDeliveryWorker:
    def __init__(
        self,
        events_store: Any,
        *,
        authorize: Any = None,
        poll_seconds: float = 0.5,
        batch_size: int = 100,
        cleanup_interval_seconds: float = 3600,
    ) -> None:
        if not 0.05 <= float(poll_seconds) <= 60:
            raise ValueError("poll_seconds must be in 0.05..60")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be in 1..500")
        if not 60 <= float(cleanup_interval_seconds) <= 86400:
            raise ValueError("cleanup_interval_seconds must be in 60..86400")
        self.events_store = events_store
        self.authorize = authorize
        self.poll_seconds = float(poll_seconds)
        self.batch_size = batch_size
        self.cleanup_interval_seconds = float(cleanup_interval_seconds)

    def run_forever(self, stop_event: threading.Event) -> None:
        last_cleanup = 0.0
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_cleanup >= self.cleanup_interval_seconds:
                try:
                    self.events_store.cleanup_expired()
                except Exception:
                    logging.exception("MCP event retention cleanup failed")
                    # Retry cleanup after a bounded delay, but do not block
                    # event delivery while the maintenance operation is down.
                    last_cleanup = now - self.cleanup_interval_seconds + min(60.0, self.cleanup_interval_seconds)
                else:
                    last_cleanup = now
            try:
                delivered = self.events_store.deliver_pending(
                    limit=self.batch_size,
                    authorize=self.authorize,
                )
                if delivered == 0:
                    stop_event.wait(self.poll_seconds)
            except Exception:
                logging.exception("MCP webhook delivery worker failed")
                stop_event.wait(self.poll_seconds)


class MCPEventWorkers:
    """Start/stop the sender collector and webhook dispatcher as one lifespan unit."""

    def __init__(self, collector: Any, delivery_worker: MCPEventDeliveryWorker) -> None:
        self.collector = collector
        self.delivery_worker = delivery_worker
        self._stop_event: threading.Event | None = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if any(thread.is_alive() for thread in self._threads):
                return
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._threads = [
                threading.Thread(
                    target=self.collector.run_forever,
                    args=(stop_event,),
                    kwargs={"poll_seconds": 1.0},
                    name="mcp-event-collector",
                    daemon=True,
                ),
                threading.Thread(
                    target=self.delivery_worker.run_forever,
                    args=(stop_event,),
                    name="mcp-event-delivery",
                    daemon=True,
                ),
            ]
            for thread in self._threads:
                thread.start()

    def stop(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            threads = list(self._threads)
            if stop_event is not None:
                stop_event.set()
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=10)
        with self._lock:
            # Keep references to timed-out workers. A later start must not
            # clear their stop signal or create a second live generation.
            self._threads = [thread for thread in threads if thread.is_alive()]
            if not self._threads:
                self._stop_event = None
