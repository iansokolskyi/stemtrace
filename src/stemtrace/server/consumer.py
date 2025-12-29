"""Broker-agnostic event consumer."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from stemtrace.library.transports import get_transport

if TYPE_CHECKING:
    from stemtrace.core.ports import EventTransport
    from stemtrace.server.store import GraphStore

logger = logging.getLogger(__name__)


class EventConsumer:
    """Background consumer that reads events and updates the GraphStore."""

    def __init__(
        self,
        broker_url: str,
        store: GraphStore,
        *,
        prefix: str = "stemtrace",
        ttl: int = 86400,
    ) -> None:
        """Initialize consumer with broker URL and target store."""
        self._broker_url = broker_url
        self._store = store
        self._prefix = prefix
        self._ttl = ttl
        self._transport: EventTransport | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

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
                    self._store.add_event(event)
                    logger.debug("Consumed event: %s (%s)", event.task_id, event.state)
                except Exception:
                    logger.exception("Error processing event %s", event.task_id)
        except Exception:
            if not self._stop_event.is_set():
                logger.exception("Consumer loop error")


class AsyncEventConsumer:
    """Async context manager wrapper for EventConsumer."""

    def __init__(
        self,
        broker_url: str,
        store: GraphStore,
        *,
        prefix: str = "stemtrace",
        ttl: int = 86400,
    ) -> None:
        """Initialize async consumer wrapper with broker URL and target store."""
        self._consumer = EventConsumer(broker_url, store, prefix=prefix, ttl=ttl)

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
