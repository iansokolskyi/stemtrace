"""Integration tests for RabbitMQ transport.

These tests require a running RabbitMQ instance.
Skip if RabbitMQ is not available.

Run with: pytest tests/integration/test_rabbitmq_transport.py -v
"""

import contextlib
import os
import queue
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.library.transports import get_transport
from stemtrace.library.transports.rabbitmq import RabbitMQTransport

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")


def rabbitmq_available() -> bool:
    """Check if RabbitMQ is available."""
    try:
        from kombu import Connection

        with Connection(RABBITMQ_URL, connect_timeout=1) as conn:
            conn.connect()
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
