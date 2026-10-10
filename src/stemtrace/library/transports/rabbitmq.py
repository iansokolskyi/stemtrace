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
    import threading
    from collections.abc import Callable, Iterator

# Reconnect backoff after consume failures: doubles per failure, resets once a
# connection has been established and read from (a drain returned or timed out).
RECONNECT_INITIAL_BACKOFF = 1.0
RECONNECT_MAX_BACKOFF = 30.0

# Seconds a consumer connection attempt may take to open the socket (also the
# per-frame wait during the AMQP handshake). Kept short so a stalled broker
# can't hold up shutdown for long.
CONNECT_TIMEOUT = 2.0

# Longest single wait for broker frames. The consume loop checks the stop event
# between waits, so this bounds how long shutdown waits on an idle connection;
# it must stay well below the server consumer's 5s stop timeout.
DRAIN_TIMEOUT = 1.0


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

    def __init__(
        self,
        url: str,
        *,
        prefix: str,
        ttl: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize the RabbitMQ transport.

        Args:
            url: AMQP URL (amqp/amqps/pyamqp).
            prefix: Namespace for exchange/queue names.
            ttl: Event retention window in seconds (queue message TTL).
            sleep: Sleep function for reconnect backoff when no stop event is
                bound (injectable for tests).
        """
        self._url = url
        self._ttl = ttl
        self._sleep = sleep
        self._connected: bool | None = None
        self._stop_event: threading.Event | None = None
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
    def connected(self) -> bool | None:
        """Whether the last drain of the consumer connection reached RabbitMQ.

        None until the first drain of the current consume() completes, True
        after a drain returned or timed out on a live connection, False while
        connecting or draining is failing.
        """
        return self._connected

    def bind_stop_event(self, event: threading.Event) -> None:
        """Stop consume() and interrupt its reconnect backoff when ``event`` is set."""
        self._stop_event = event

    def _stopping(self) -> bool:
        """Whether a bound stop event has been set."""
        return self._stop_event is not None and self._stop_event.is_set()

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

        Connection and channel errors never end the iterator: the transport
        logs a warning (with a traceback only for the first failure in a row),
        backs off exponentially (capped at RECONNECT_MAX_BACKOFF seconds) and
        reconnects on a new connection, declaring the exchange and queue
        again. Each attempt connects once, with CONNECT_TIMEOUT. The backoff
        resets once a connection has been established and read from (a drain
        returned or timed out); connections that fail before that keep the
        delay growing. The iterator ends once a stop event bound with
        bind_stop_event() is set; that event also interrupts the backoff wait,
        and the stop is noticed within DRAIN_TIMEOUT on a live connection.

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

        # Unknown until this iterator's first drain completes.
        self._connected = None
        failures = 0
        backoff = RECONNECT_INITIAL_BACKOFF

        while not self._stopping():
            try:
                with Connection(self._url, connect_timeout=CONNECT_TIMEOUT) as conn:
                    # Connect exactly once: kombu's default connect path
                    # retries internally with sleeps that ignore the stop
                    # event. Retries are this loop's job (with backoff).
                    conn.ensure_connection(
                        max_retries=0, reraise_as_library_errors=False
                    )
                    channel = conn.channel()
                    # Bind fresh copies to this connection's channel. kombu
                    # binds an entity only once, so re-binding the shared
                    # templates would keep declaring on the first (dead)
                    # channel after a reconnect.
                    bound_exchange = exchange(channel)
                    bound_queue = queue(channel)
                    bound_exchange.declare()
                    bound_queue.declare()

                    with Consumer(
                        channel,
                        queues=[bound_queue],
                        callbacks=[on_message],
                        accept=["json"],
                    ):
                        while True:
                            # Periodic wakeup to allow outer loops to run.
                            with contextlib.suppress(TimeoutError, socket.timeout):
                                conn.drain_events(timeout=DRAIN_TIMEOUT)

                            # The connection is established and was read from
                            # (the drain returned or timed out), so the outage,
                            # if any, is over.
                            if failures:
                                logger.info(
                                    "RabbitMQ consume loop reconnected after "
                                    "%d failure(s)",
                                    failures,
                                )
                            failures = 0
                            backoff = RECONNECT_INITIAL_BACKOFF
                            self._connected = True

                            if self._stopping():
                                return

                            while pending:
                                yield pending.popleft()
            except Exception as exc:
                if self._stopping():
                    return
                failures += 1
                backoff = self._back_off(failures, backoff, exc)

    def _back_off(self, failures: int, backoff: float, exc: Exception) -> float:
        """Log a consume failure, wait, and return the next backoff delay.

        The full traceback is logged only on the first failure in a row; later
        attempts get a single warning line to avoid flooding the logs.
        """
        self._connected = False
        if failures == 1:
            logger.warning(
                "RabbitMQ consume loop error; retrying in %.1fs",
                backoff,
                exc_info=True,
            )
        else:
            logger.warning(
                "RabbitMQ consume loop still failing (attempt %d): %s; "
                "retrying in %.1fs",
                failures,
                exc,
                backoff,
            )
        if self._stop_event is not None:
            self._stop_event.wait(backoff)
        else:
            self._sleep(backoff)
        return min(backoff * 2, RECONNECT_MAX_BACKOFF)

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
