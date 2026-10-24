"""Integration tests for RabbitMQ transport.

These tests require a running RabbitMQ instance.
Skip if RabbitMQ is not available.

Run with: pytest tests/integration/test_rabbitmq_transport.py -v
"""

import base64
import contextlib
import http.client
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
from amqp.exceptions import AMQPError
from kombu import Connection, Exchange, Producer
from kombu.exceptions import KombuError

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.transports import get_transport
from stemtrace.transports.rabbitmq import RabbitMQTransport

_WORKER = os.environ.get(
    "PYTEST_XDIST_WORKER", "gw"
)  # keeps names unique across xdist workers
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")
RABBITMQ_MANAGEMENT_URL = os.environ.get(
    "RABBITMQ_MANAGEMENT_URL", "http://guest:guest@localhost:15672"
)


def rabbitmq_available() -> bool:
    """Check if RabbitMQ is available."""
    try:
        with Connection(RABBITMQ_URL, connect_timeout=1) as conn:
            conn.connect()
        return True
    except (OSError, AMQPError, KombuError):
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
    except (OSError, ValueError, http.client.HTTPException):
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
        except Exception as exc:  # noqa: BLE001  forwarded to the test thread
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
    prefix = f"test_{_WORKER}_{int(time.time() * 1000)}"
    transport = get_transport(RABBITMQ_URL, prefix=prefix, ttl=60)
    assert isinstance(transport, RabbitMQTransport)

    yield transport

    # Cleanup: delete the queue/exchange created by the transport.
    with (
        contextlib.suppress(Exception),
        Connection(RABBITMQ_URL, connect_timeout=1) as conn,
    ):
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
        """Wait for the management API to report the queue's consumer connection.

        A consumer can be listed before its channel details are: the
        management stats arrive asynchronously, so ``channel_details`` may
        still be empty. Keep polling until the connection name shows up.
        """
        path = f"queues/{urllib.parse.quote('/', safe='')}/{queue_name}"
        deadline = time.monotonic() + timeout
        details: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            details = _management_request("GET", path).get("consumer_details") or []
            for consumer in details:
                name = (consumer.get("channel_details") or {}).get("connection_name")
                if isinstance(name, str) and name:
                    return name
            time.sleep(0.2)
        raise TimeoutError(
            f"No consumer connection reported on {queue_name} after {timeout}s "
            f"(last consumer_details: {details!r})"
        )

    def test_consume_after_broker_closes_connection_reconnects_and_resumes(
        self,
        rabbitmq_transport: RabbitMQTransport,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Events published after a forced disconnect still arrive."""
        caplog.set_level(logging.INFO, logger="stemtrace.transports.rabbitmq")
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


class TestRabbitMQConsumerHeartbeats:
    """An idle consumer on a ``?heartbeat=N`` URL keeps its connection."""

    HEARTBEAT_S = 1
    # Several intervals: RabbitMQ closes a connection after two silent ones.
    IDLE_S = 5.0

    def test_consume_idle_with_heartbeat_url_keeps_the_same_connection(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="stemtrace.transports.rabbitmq")
        separator = "&" if "?" in RABBITMQ_URL else "?"
        url = f"{RABBITMQ_URL}{separator}heartbeat={self.HEARTBEAT_S}"
        transport = RabbitMQTransport.from_url(
            url,
            prefix=f"test_hb_{_WORKER}_{int(time.time() * 1000)}",
            ttl=60,
        )
        transport._declare_exchange_and_queue()
        events, stop, thread = _start_consumer(transport)
        try:
            time.sleep(self.IDLE_S)
            transport.publish(
                TaskEvent(
                    task_id="after-idle",
                    name="tests.rabbitmq_heartbeat",
                    state=TaskState.STARTED,
                    timestamp=datetime.now(timezone.utc),
                )
            )
            delivered = events.get(timeout=10)
            connected = transport.connected
        finally:
            _stop_consumer(stop, thread)
            with (
                contextlib.suppress(Exception),
                Connection(RABBITMQ_URL, connect_timeout=1) as conn,
            ):
                channel = conn.channel()
                channel.queue_delete(transport.queue_name)
                channel.exchange_delete(transport.exchange_name)

        # The broker did not drop the consumer: no error, no reconnect.
        assert "consume loop" not in caplog.text
        assert connected is True
        assert isinstance(delivered, TaskEvent)
        assert delivered.task_id == "after-idle"


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
    """Failover URLs and undecodable messages against a real broker."""

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

    def test_consume_drops_non_json_message_and_stays_connected(
        self,
        rabbitmq_transport: RabbitMQTransport,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A text/plain message is dropped once; the next event still arrives."""
        caplog.set_level(logging.WARNING, logger="stemtrace.transports.rabbitmq")
        rabbitmq_transport._declare_exchange_and_queue()
        with Connection(RABBITMQ_URL, connect_timeout=2) as conn:
            Producer(conn.channel()).publish(
                "not json",
                exchange=Exchange(
                    rabbitmq_transport.exchange_name, type="fanout", durable=True
                ),
                routing_key="",
                content_type="text/plain",
                content_encoding="utf-8",
                retry=False,
            )
        rabbitmq_transport.publish(self._event("after-bad-message"))

        events, stop, thread = _start_consumer(rabbitmq_transport)
        try:
            received = events.get(timeout=10)
            assert isinstance(received, TaskEvent)
            assert received.task_id == "after-bad-message"
            assert rabbitmq_transport.connected is True
        finally:
            _stop_consumer(stop, thread)
        warnings = [r.getMessage() for r in caplog.records]
        assert len(warnings) == 1
        assert "Dropping undecodable message" in warnings[0]
        assert "text/plain" in warnings[0]
