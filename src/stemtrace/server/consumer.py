"""Broker-agnostic event consumer."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from stemtrace.core.events import TaskEvent, WorkerEvent, WorkerEventType
from stemtrace.core.ports import ConnectionAwareTransport, StoppableTransport
from stemtrace.transports import get_transport

if TYPE_CHECKING:
    from stemtrace.core.ports import EventTransport
    from stemtrace.server.store import GraphStore, WorkerRegistry

logger = logging.getLogger(__name__)

# Default interval for stale worker checks (seconds)
STALE_CHECK_INTERVAL = 60

# Backoff for restarting the consume loop after an unexpected error (seconds).
# Doubles per consecutive failure and resets once an event is received.
RESTART_INITIAL_BACKOFF = 1.0
RESTART_MAX_BACKOFF = 30.0


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
        # Consecutive consume-loop restarts since the last received event.
        self._failures = 0
        # True once the transport's iterator ended on its own (not stopped).
        self._finished = False
        # True after stop() was called, until the next start().
        self._stopped = False

    @property
    def is_running(self) -> bool:
        """Whether the consumer thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_stopped(self) -> bool:
        """Whether the consumer was stopped on purpose with stop()."""
        return self._stopped

    @property
    def is_finished(self) -> bool:
        """Whether the transport's iterator ended normally (e.g. memory://).

        A finished consumer is not running but hasn't failed either.
        """
        return self._finished

    @property
    def is_connected(self) -> bool | None:
        """Whether the consumer can currently read from the broker.

        False while the consume loop keeps restarting after errors, until the
        transport reports a successful read again. Otherwise the transport's
        own report. None when not running or when nothing is known.
        """
        if not self.is_running:
            return None
        transport: object = self._transport
        connected: bool | None = None
        if isinstance(transport, ConnectionAwareTransport):
            reported = transport.connected
            connected = reported if isinstance(reported, bool) else None
        if self._failures and connected is not True:
            return False
        return connected

    def start(self) -> None:
        """Start consuming in background thread. Idempotent."""
        if self._thread is not None:
            return

        self._stop_event.clear()
        self._failures = 0
        self._finished = False
        self._stopped = False
        self._transport = get_transport(
            self._broker_url, prefix=self._prefix, ttl=self._ttl
        )
        transport: object = self._transport
        if isinstance(transport, StoppableTransport):
            transport.bind_stop_event(self._stop_event)
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

        self._stopped = True
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

        backoff = RESTART_INITIAL_BACKOFF
        while not self._stop_event.is_set():
            try:
                for event in self._transport.consume():
                    if self._stop_event.is_set():
                        return
                    if self._failures:
                        logger.info(
                            "Consumer recovered after %d restart(s)", self._failures
                        )
                        self._failures = 0
                        backoff = RESTART_INITIAL_BACKOFF
                    self._handle_event(event)
                self._on_iterator_end()
                return
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                self._failures += 1
                if self._failures == 1:
                    logger.warning(
                        "Consumer loop error; restarting in %.1fs",
                        backoff,
                        exc_info=True,
                    )
                else:
                    logger.warning(
                        "Consumer loop still failing (attempt %d): %s; "
                        "restarting in %.1fs",
                        self._failures,
                        exc,
                        backoff,
                    )
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, RESTART_MAX_BACKOFF)

    def _handle_event(self, event: TaskEvent | WorkerEvent) -> None:
        """Apply one event; an error is logged and skips only this event.

        A failure here must not restart the consume loop: that would
        reconnect to the broker over a bad event or a store bug.
        """
        try:
            self._process_event(event)
            self._maybe_check_stale_workers()
        except Exception:
            # Deliberately broad: a bad event or store bug must not stop
            # consumption; the traceback is logged.
            logger.exception("Error processing event")

    def _on_iterator_end(self) -> None:
        """Record that the transport's iterator ended without an error.

        Either stop() was called (the transport saw the bound stop event), or
        the event source ran out on its own (e.g. MemoryTransport), which
        leaves the consumer finished rather than failed.
        """
        if self._stop_event.is_set():
            return
        self._finished = True
        self._failures = 0
        logger.info("Event source exhausted; consumer finished")

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
            if self._worker_registry is not None and event.hostname is not None:
                # A busy worker's task events keep it online (no heartbeat).
                self._worker_registry.mark_seen(event.hostname, event.timestamp)

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
        else:
            # WORKER_SHUTDOWN, or WORKER_TERMINATING (a cold stop, which sends
            # no worker_shutdown): the process ended. A new event type must
            # get its own branch (test_worker_event_types_are_all_handled).
            self._worker_registry.mark_shutdown(event.hostname, event.pid)
            logger.info(
                "Worker %s: %s:%d", event.event_type.value, event.hostname, event.pid
            )


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

    @property
    def is_finished(self) -> bool:
        """Whether the event source ended normally (e.g. memory://)."""
        return self._consumer.is_finished

    @property
    def is_stopped(self) -> bool:
        """Whether the consumer was stopped on purpose with stop()."""
        return self._consumer.is_stopped

    @property
    def is_connected(self) -> bool | None:
        """Whether the transport can currently reach the broker (None if unknown)."""
        return self._consumer.is_connected

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
