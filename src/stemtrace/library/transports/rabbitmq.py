"""RabbitMQ transport using kombu.

This adapter publishes stemtrace events to a durable fanout exchange and consumes
from a durable queue bound to that exchange.

Design goals:
- Broker-agnostic event schema (same JSON shape as Redis transport).
- Fire-and-forget publishing: log errors, never raise (Celery tasks must not block).
- Best-effort replay: messages are retained in the queue for up to `ttl` seconds.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from typing_extensions import Self

from stemtrace.core.events import TaskEvent, WorkerEvent

logger = logging.getLogger(__name__)

StreamEvent = TaskEvent | WorkerEvent

if TYPE_CHECKING:
    from collections.abc import Iterator


def _normalize_prefix(prefix: str) -> str:
    """Normalize an arbitrary prefix into an AMQP-safe name fragment.

    stemtrace uses `prefix` for Redis keys and for URL mount paths. For RabbitMQ,
    we keep it human-readable but avoid characters that commonly cause problems
    in exchange/queue names (notably `/`).
    """
    normalized = prefix.strip()
    normalized = normalized.strip("/")
    normalized = normalized.replace("/", ".")
    return normalized or "stemtrace"


class RabbitMQTransport:
    """RabbitMQ (AMQP) transport using kombu."""

    def __init__(self, url: str, *, prefix: str, ttl: int) -> None:
        """Initialize the RabbitMQ transport.

        Args:
            url: AMQP URL (amqp/amqps/pyamqp).
            prefix: Namespace for exchange/queue names.
            ttl: Event retention window in seconds (queue message TTL).
        """
        self._url = url
        self._ttl = ttl
        self._prefix = _normalize_prefix(prefix)

        # Fanout exchange to broadcast events.
        self._exchange_name = f"{self._prefix}.events"

        # Per-consumer queue name. We intentionally avoid new env vars/flags and
        # derive a stable-ish id from the hostname.
        self._consumer_id = socket.gethostname()
        self._queue_name = f"{self._exchange_name}.{self._consumer_id}"

        # RabbitMQ expects TTL/expiry in milliseconds.
        ttl_ms = max(ttl, 1) * 1000
        # Make queues self-cleaning if a consumer disappears permanently.
        # Keep it slightly longer than message TTL to allow catching up.
        queue_expires_ms = ttl_ms + 60_000
        self._queue_arguments: dict[str, Any] = {
            "x-message-ttl": ttl_ms,
            "x-expires": queue_expires_ms,
        }

    @property
    def ttl(self) -> int:
        """TTL in seconds."""
        return self._ttl

    @property
    def exchange_name(self) -> str:
        """The exchange name used for publishing."""
        return self._exchange_name

    @property
    def queue_name(self) -> str:
        """The queue name used for consumption."""
        return self._queue_name

    def publish(self, event: StreamEvent) -> None:
        """Publish an event to RabbitMQ.

        Fire-and-forget: logs errors, never raises.

        Args:
            event: Event to publish (TaskEvent or WorkerEvent).
        """
        try:
            self._publish_payload(event.model_dump(mode="json"))
        except Exception:
            event_id = self._event_identifier(event)
            logger.warning(
                "Failed to publish event %s to RabbitMQ", event_id, exc_info=True
            )

    def _publish_payload(self, payload: dict[str, Any]) -> None:
        """Publish a JSON-serializable payload via kombu."""
        # kombu is imported lazily to keep import-time side effects minimal.
        from kombu import Connection, Exchange, Producer

        exchange = Exchange(self._exchange_name, type="fanout", durable=True)
        # Publish persistent messages so the broker can retain them in queues.
        delivery_mode = 2

        with Connection(self._url) as conn:
            channel = conn.channel()
            producer = Producer(channel)
            producer.publish(
                payload,
                exchange=exchange,
                routing_key="",
                declare=[exchange],
                serializer="json",
                delivery_mode=delivery_mode,
                retry=False,
            )

    def _declare_exchange_and_queue(self) -> None:
        """Declare the fanout exchange and this transport's durable consumer queue.

        This is primarily useful for tests and for ensuring the consumer queue exists
        before publishing events (RabbitMQ does not buffer messages in exchanges).
        """
        from kombu import Connection, Exchange, Queue

        exchange = Exchange(self._exchange_name, type="fanout", durable=True)
        queue = Queue(
            name=self._queue_name,
            exchange=exchange,
            routing_key="",
            durable=True,
            queue_arguments=self._queue_arguments,
        )

        with Connection(self._url) as conn:
            channel = conn.channel()
            exchange.maybe_bind(channel)
            queue.maybe_bind(channel)
            exchange.declare()
            queue.declare()

    @staticmethod
    def _event_identifier(event: StreamEvent) -> str:
        """Create a stable identifier for logging."""
        if isinstance(event, TaskEvent):
            return event.task_id
        return f"{event.hostname}:{event.pid}:{event.event_type}"

    def consume(self) -> Iterator[StreamEvent]:
        """Blocking iterator that yields events as they arrive.

        Yields:
            Parsed TaskEvent or WorkerEvent instances.
        """
        from kombu import Connection, Exchange, Queue
        from kombu.messaging import Consumer

        exchange = Exchange(self._exchange_name, type="fanout", durable=True)
        queue = Queue(
            name=self._queue_name,
            exchange=exchange,
            routing_key="",
            durable=True,
            queue_arguments=self._queue_arguments,
        )

        # Buffered handoff from kombu callback -> generator.
        pending: deque[StreamEvent] = deque()

        def on_message(body: Any, message: Any) -> None:
            """Convert broker payload to events, acknowledge/reject messages."""
            try:
                pending.append(self._parse_event(body))
                message.ack()
            except Exception:
                logger.warning(
                    "Failed to parse event from RabbitMQ queue %s",
                    self._queue_name,
                    exc_info=True,
                )
                # Drop malformed messages to avoid poison-pill loops.
                try:
                    message.reject(requeue=False)
                except Exception:
                    logger.debug("Failed to reject malformed RabbitMQ message")

        while True:
            try:
                with Connection(self._url) as conn:
                    channel = conn.channel()
                    exchange.maybe_bind(channel)
                    queue.maybe_bind(channel)
                    exchange.declare()
                    queue.declare()

                    with Consumer(
                        channel,
                        queues=[queue],
                        callbacks=[on_message],
                        accept=["json"],
                    ):
                        while True:
                            # Periodic wakeup to allow outer loops to run.
                            with contextlib.suppress(TimeoutError, socket.timeout):
                                conn.drain_events(timeout=5)

                            while pending:
                                yield pending.popleft()
            except Exception:
                logger.exception("RabbitMQ consume loop error")
                time.sleep(1)

    @staticmethod
    def _parse_event(payload: Any) -> StreamEvent:
        """Parse payload into appropriate event type."""
        data: Any = payload
        if isinstance(payload, (bytes, bytearray)):
            data = payload.decode()

        if isinstance(data, str):
            parsed: Any = json.loads(data)
        else:
            parsed = data

        if isinstance(parsed, dict):
            if "event_type" in parsed:
                return WorkerEvent.model_validate(parsed)
            if "task_id" in parsed:
                return TaskEvent.model_validate(parsed)
        raise ValueError("Unknown event payload: missing event_type and task_id")

    @classmethod
    def from_url(cls, url: str, *, prefix: str = "stemtrace", ttl: int = 86400) -> Self:
        """Create transport from AMQP URL."""
        return cls(url, prefix=prefix, ttl=ttl)
