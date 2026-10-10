"""Integration tests for RabbitMQ transport.

These tests require a running RabbitMQ instance.
Skip if RabbitMQ is not available.

Run with: pytest tests/integration/test_rabbitmq_transport.py -v
"""

import base64
import contextlib
import json
import logging
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.library.transports import get_transport
from stemtrace.library.transports.rabbitmq import RabbitMQTransport

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")
RABBITMQ_MANAGEMENT_URL = os.environ.get(
    "RABBITMQ_MANAGEMENT_URL", "http://guest:guest@localhost:15672"
)


def rabbitmq_available() -> bool:
    """Check if RabbitMQ is available."""
    try:
        from kombu import Connection

        with Connection(RABBITMQ_URL, connect_timeout=1) as conn:
            conn.connect()
        return True
    except Exception:
        return False


def _management_request(method: str, path: str) -> Any:
    """Call the RabbitMQ management HTTP API and return the decoded JSON body."""
    parsed = urllib.parse.urlsplit(RABBITMQ_MANAGEMENT_URL)
    base = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 15672}"
    credentials = f"{parsed.username or 'guest'}:{parsed.password or 'guest'}"
    request = urllib.request.Request(f"{base}/api/{path}", method=method)
    request.add_header(
        "Authorization", "Basic " + base64.b64encode(credentials.encode()).decode()
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read()
    return json.loads(body) if body else None


def management_api_available() -> bool:
    """Check if the RabbitMQ management API is reachable."""
    try:
        _management_request("GET", "overview")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not rabbitmq_available(),
    reason="RabbitMQ not available",
)


def _consume_one(
    transport: RabbitMQTransport, timeout: float = 10.0
) -> TaskEvent | WorkerEvent:
    """Consume exactly one event from transport with a wall-clock timeout.

    RabbitMQTransport.consume() is an infinite iterator by design; this helper
    prevents tests from hanging forever if the broker or routing is misconfigured.
    """
    out: queue.Queue[TaskEvent | WorkerEvent] = queue.Queue(maxsize=1)
    errors: queue.Queue[BaseException] = queue.Queue(maxsize=1)

    def run() -> None:
        gen = transport.consume()
        try:
            out.put(next(gen))
        except Exception as exc:
            errors.put(exc)
        finally:
            gen.close()

    thread = threading.Thread(target=run, name="rabbitmq-consume-once", daemon=True)
    thread.start()

    try:
        return out.get(timeout=timeout)
    except queue.Empty as err:
        if not errors.empty():
            raise errors.get() from None
        raise TimeoutError(
            f"Timed out waiting for RabbitMQ event after {timeout}s"
        ) from err


@pytest.fixture
def rabbitmq_transport() -> Iterator[RabbitMQTransport]:
    """Create a RabbitMQ transport with unique prefix for test isolation."""
    prefix = f"test_{int(time.time() * 1000)}"
    transport = get_transport(RABBITMQ_URL, prefix=prefix, ttl=60)
    assert isinstance(transport, RabbitMQTransport)

    yield transport

    # Cleanup: delete the queue/exchange created by the transport.
    with contextlib.suppress(Exception):
        from kombu import Connection

        with Connection(RABBITMQ_URL, connect_timeout=1) as conn:
            channel = conn.channel()
            # Best-effort cleanup; ignore failures.
            channel.queue_delete(transport.queue_name)
            channel.exchange_delete(transport.exchange_name)


class TestRabbitMQTransportIntegration:
    """Integration tests for RabbitMQTransport with real RabbitMQ."""

    def test_publish_and_consume_task_event(
        self, rabbitmq_transport: RabbitMQTransport
    ) -> None:
        """Publish a TaskEvent and consume it back."""
        event = TaskEvent(
            task_id="rabbitmq-test-123",
            name="tests.rabbitmq_task",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )
        # Ensure the consumer queue exists before publishing; RabbitMQ does not
        # retain messages in the exchange.
        rabbitmq_transport._declare_exchange_and_queue()
        rabbitmq_transport.publish(event)

        received = _consume_one(rabbitmq_transport, timeout=10.0)
        assert isinstance(received, TaskEvent)
        assert received.task_id == event.task_id
        assert received.name == event.name
        assert received.state == event.state

    def test_publish_and_consume_worker_event(
        self, rabbitmq_transport: RabbitMQTransport
    ) -> None:
        """Publish a WorkerEvent and consume it back."""
        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1",
            pid=12345,
            timestamp=datetime.now(timezone.utc),
            registered_tasks=["tests.task_a"],
        )
        rabbitmq_transport._declare_exchange_and_queue()
        rabbitmq_transport.publish(event)

        received = _consume_one(rabbitmq_transport, timeout=10.0)
        assert isinstance(received, WorkerEvent)
        assert received.event_type == event.event_type
        assert received.hostname == event.hostname
        assert received.pid == event.pid


@pytest.mark.skipif(
    not management_api_available(),
    reason="RabbitMQ management API not available",
)
class TestRabbitMQTransportReconnect:
    """Recovery after the broker closes the consumer connection."""

    @staticmethod
    def _event(task_id: str) -> TaskEvent:
        return TaskEvent(
            task_id=task_id,
            name="tests.rabbitmq_reconnect",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )

    @staticmethod
    def _consumer_connection_name(queue_name: str, timeout: float) -> str:
        """Wait for the management API to report the queue's consumer connection."""
        path = f"queues/{urllib.parse.quote('/', safe='')}/{queue_name}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            details = _management_request("GET", path).get("consumer_details") or []
            if details:
                name: str = details[0]["channel_details"]["connection_name"]
                return name
            time.sleep(0.5)
        raise TimeoutError(f"No consumer reported on {queue_name} after {timeout}s")

    def test_consume_after_broker_closes_connection_reconnects_and_resumes(
        self,
        rabbitmq_transport: RabbitMQTransport,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Events published after a forced disconnect still arrive."""
        caplog.set_level(logging.INFO, logger="stemtrace.library.transports.rabbitmq")
        rabbitmq_transport._declare_exchange_and_queue()
        stop = threading.Event()
        rabbitmq_transport.bind_stop_event(stop)
        received: queue.Queue[TaskEvent | WorkerEvent] = queue.Queue()

        def run() -> None:
            for event in rabbitmq_transport.consume():
                received.put(event)

        thread = threading.Thread(target=run, name="rabbitmq-reconnect", daemon=True)
        thread.start()
        try:
            rabbitmq_transport.publish(self._event("before-disconnect"))
            first = received.get(timeout=10)
            assert isinstance(first, TaskEvent)
            assert first.task_id == "before-disconnect"

            connection = self._consumer_connection_name(
                rabbitmq_transport.queue_name, timeout=20
            )
            _management_request(
                "DELETE",
                f"connections/{urllib.parse.quote(connection, safe='')}",
            )
            rabbitmq_transport.publish(self._event("after-disconnect"))

            second = received.get(timeout=20)
            assert isinstance(second, TaskEvent)
            assert second.task_id == "after-disconnect"
            assert "reconnected after 1 failure(s)" in caplog.text
            assert rabbitmq_transport.connected is True
        finally:
            stopped_at = time.monotonic()
            stop.set()
            thread.join(timeout=5)
        assert not thread.is_alive()
        # The idle drain loop notices the stop well within the consumer's 5s join.
        assert time.monotonic() - stopped_at < 3


def _start_consumer(
    transport: RabbitMQTransport,
) -> tuple[queue.Queue[TaskEvent | WorkerEvent], threading.Event, threading.Thread]:
    """Consume in a background thread; returns (events, stop event, thread)."""
    stop = threading.Event()
    transport.bind_stop_event(stop)
    events: queue.Queue[TaskEvent | WorkerEvent] = queue.Queue()

    def run() -> None:
        for event in transport.consume():
            events.put(event)

    thread = threading.Thread(target=run, name="rabbitmq-consumer", daemon=True)
    thread.start()
    return events, stop, thread


def _stop_consumer(stop: threading.Event, thread: threading.Thread) -> None:
    """Stop a consumer started with _start_consumer and check it ends promptly."""
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


class TestRabbitMQTransportResilience:
    """Failover URLs against a real broker."""

    @staticmethod
    def _event(task_id: str) -> TaskEvent:
        return TaskEvent(
            task_id=task_id,
            name="tests.rabbitmq_resilience",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )

    def test_consume_with_failover_url_skips_unreachable_first_host(
        self, rabbitmq_transport: RabbitMQTransport
    ) -> None:
        """A closed port listed first doesn't keep the consumer from the broker."""
        rabbitmq_transport._declare_exchange_and_queue()
        failover = RabbitMQTransport(
            f"amqp://guest:guest@127.0.0.1:1//;{RABBITMQ_URL}",
            prefix=rabbitmq_transport.exchange_name.removesuffix(".events"),
            ttl=60,
        )
        assert failover.queue_name == rabbitmq_transport.queue_name
        events, stop, thread = _start_consumer(failover)
        try:
            rabbitmq_transport.publish(self._event("via-failover"))
            received = events.get(timeout=15)
            assert isinstance(received, TaskEvent)
            assert received.task_id == "via-failover"
            assert failover.connected is True
        finally:
            _stop_consumer(stop, thread)
