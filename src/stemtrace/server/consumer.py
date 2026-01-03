"""Broker-agnostic event consumer."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from stemtrace.core.events import TaskEvent, WorkerEvent, WorkerEventType
from stemtrace.library.transports import get_transport

if TYPE_CHECKING:
    from stemtrace.core.ports import EventTransport
    from stemtrace.server.store import GraphStore, WorkerRegistry

logger = logging.getLogger(__name__)

# Default interval for stale worker checks (seconds)
STALE_CHECK_INTERVAL = 60


class EventConsumer:
    """Background consumer that reads events and updates the GraphStore."""

    def __init__(
        self,
        broker_url: str,
        store: GraphStore,
        *,
        prefix: str = "stemtrace",
        ttl: int = 86400,
        worker_registry: WorkerRegistry | None = None,
        stale_check_interval: int = STALE_CHECK_INTERVAL,
    ) -> None:
        """Initialize consumer with broker URL and target store."""
        self._broker_url = broker_url
        self._store = store
        self._prefix = prefix
        self._ttl = ttl
        self._worker_registry = worker_registry
        self._stale_check_interval = stale_check_interval
        self._transport: EventTransport | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_stale_check: float = 0.0

    @property
    def is_running(self) -> bool:
        """Whether the consumer thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start consuming in background thread. Idempotent."""
        if self._thread is not None:
            return

        self._stop_event.clear()
        self._transport = get_transport(
            self._broker_url, prefix=self._prefix, ttl=self._ttl
        )
        self._thread = threading.Thread(
            target=self._consume_loop,
            name="stemtrace-consumer",
            daemon=True,
        )
        self._thread.start()
        logger.info("Event consumer started")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the consumer gracefully."""
        if self._thread is None:
            return

        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("Consumer thread did not stop gracefully")
        self._thread = None
        self._transport = None
        logger.info("Event consumer stopped")

    def _consume_loop(self) -> None:
        if self._transport is None:
            return

        logger.debug("Consumer loop starting, reading from %s", self._broker_url)

        try:
            for event in self._transport.consume():
                if self._stop_event.is_set():
                    break

                try:
                    self._process_event(event)
                    self._maybe_check_stale_workers()
                except Exception:
                    logger.exception("Error processing event")
        except Exception:
            if not self._stop_event.is_set():
                logger.exception("Consumer loop error")

    def _maybe_check_stale_workers(self) -> None:
        """Periodically check for and mark stale workers as offline.

        Called during event processing to detect workers that crashed
        without sending a WORKER_SHUTDOWN event.
        """
        if self._worker_registry is None:
            return

        now = time.monotonic()
        if now - self._last_stale_check >= self._stale_check_interval:
            self._last_stale_check = now
            self._worker_registry.remove_stale_workers()
            logger.debug("Checked for stale workers")

    def _process_event(self, event: TaskEvent | WorkerEvent) -> None:
        """Route event to appropriate handler based on type."""
        if isinstance(event, WorkerEvent):
            self._handle_worker_event(event)
        else:
            self._store.add_event(event)
            logger.debug("Consumed task event: %s (%s)", event.task_id, event.state)

    def _handle_worker_event(self, event: WorkerEvent) -> None:
        """Handle worker lifecycle events.

        Processes ALL worker events from Redis to rebuild registry state.
        When server restarts but workers are still running, historical
        WORKER_READY events are valid and should register workers.
        If a worker has shut down, the WORKER_SHUTDOWN event will mark it
        offline. Stream ordering ensures correct final state.
        """
        if self._worker_registry is None:
            logger.debug("No worker registry, skipping worker event")
            return

        if event.event_type == WorkerEventType.WORKER_READY:
            self._worker_registry.register_worker(
                hostname=event.hostname,
                pid=event.pid,
                tasks=event.registered_tasks,
                task_definitions=event.task_definitions,
                event_timestamp=event.timestamp,
            )
            logger.info(
                "Worker registered: %s:%d (%d tasks)",
                event.hostname,
                event.pid,
                len(event.registered_tasks),
            )
        elif event.event_type == WorkerEventType.WORKER_SHUTDOWN:
            self._worker_registry.mark_shutdown(event.hostname, event.pid)
            logger.info("Worker shutdown: %s:%d", event.hostname, event.pid)


class AsyncEventConsumer:
    """Async context manager wrapper for EventConsumer."""

    def __init__(
        self,
        broker_url: str,
        store: GraphStore,
        *,
        prefix: str = "stemtrace",
        ttl: int = 86400,
        worker_registry: WorkerRegistry | None = None,
        stale_check_interval: int = STALE_CHECK_INTERVAL,
    ) -> None:
        """Initialize async consumer wrapper with broker URL and target store."""
        self._consumer = EventConsumer(
            broker_url,
            store,
            prefix=prefix,
            ttl=ttl,
            worker_registry=worker_registry,
            stale_check_interval=stale_check_interval,
        )

    @property
    def is_running(self) -> bool:
        """Whether the consumer thread is alive."""
        return self._consumer.is_running

    async def __aenter__(self) -> AsyncEventConsumer:
        """Start consumer on context enter."""
        self._consumer.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Stop consumer on context exit."""
        self._consumer.stop()

    def start(self) -> None:
        """Start the consumer."""
        self._consumer.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the consumer."""
        self._consumer.stop(timeout=timeout)
