"""Integration tests for Redis transport.

These tests require a running Redis instance.
Skip if Redis is not available.

Run with: pytest tests/integration/test_redis_transport.py -v
"""

import contextlib
import os
import queue
import threading
import time
from datetime import datetime, timezone

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports import get_transport
from stemtrace.transports.redis import MIN_IDLE_READ_SECONDS, RedisTransport

# Skip all tests if Redis is not available
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")


def redis_available() -> bool:
    """Check if Redis is available."""
    try:
        import redis

        client = redis.from_url(REDIS_URL)
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not redis_available(),
    reason="Redis not available",
)


@pytest.fixture
def redis_transport() -> RedisTransport:
    """Create a Redis transport with unique prefix for test isolation."""
    prefix = f"test_{int(time.time() * 1000)}"
    transport = get_transport(REDIS_URL, prefix=prefix, ttl=60)
    assert isinstance(transport, RedisTransport)

    yield transport

    # Cleanup: delete the stream
    with contextlib.suppress(Exception):
        transport._client.delete(transport.stream_key)


@pytest.fixture
def sample_event() -> TaskEvent:
    """Create a sample event for testing."""
    return TaskEvent(
        task_id="redis-test-123",
        name="tests.redis_task",
        state=TaskState.STARTED,
        timestamp=datetime.now(timezone.utc),
    )


class TestRedisTransportIntegration:
    """Integration tests for RedisTransport with real Redis."""

    def test_publish_and_consume_single_event(
        self, redis_transport: RedisTransport, sample_event: TaskEvent
    ) -> None:
        """Publish an event and consume it back."""
        redis_transport.publish(sample_event)

        # Consume should yield the event
        events = []
        for event in redis_transport.consume():
            events.append(event)
            break  # Only get one

        assert len(events) == 1
        assert events[0].task_id == sample_event.task_id
        assert events[0].name == sample_event.name
        assert events[0].state == sample_event.state

    def test_publish_multiple_events(self, redis_transport: RedisTransport) -> None:
        """Publish multiple events and consume them in order."""
        events_to_publish = [
            TaskEvent(
                task_id=f"task-{i}",
                name="tests.multi",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
            for i in range(5)
        ]

        for event in events_to_publish:
            redis_transport.publish(event)

        consumed = []
        for event in redis_transport.consume():
            consumed.append(event)
            if len(consumed) >= 5:
                break

        assert len(consumed) == 5
        for i, event in enumerate(consumed):
            assert event.task_id == f"task-{i}"

    def test_event_roundtrip_preserves_all_fields(
        self, redis_transport: RedisTransport
    ) -> None:
        """All event fields are preserved through publish/consume."""
        original = TaskEvent(
            task_id="full-event-123",
            name="tests.full_task",
            state=TaskState.RETRY,
            timestamp=datetime(2024, 6, 15, 12, 30, 45, tzinfo=timezone.utc),
            parent_id="parent-456",
            root_id="root-789",
            group_id="group-xyz",
            trace_id="trace-abc",
            retries=3,
        )

        redis_transport.publish(original)

        for event in redis_transport.consume():
            assert event.task_id == original.task_id
            assert event.name == original.name
            assert event.state == original.state
            assert event.parent_id == original.parent_id
            assert event.root_id == original.root_id
            assert event.group_id == original.group_id
            assert event.trace_id == original.trace_id
            assert event.retries == original.retries
            break

    def test_group_id_roundtrip(self, redis_transport: RedisTransport) -> None:
        """group_id field is preserved through publish/consume."""
        group_id = "test-group-abc-123"
        events = [
            TaskEvent(
                task_id=f"group-task-{i}",
                name="tests.group_member",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
            for i in range(3)
        ]

        for event in events:
            redis_transport.publish(event)

        consumed = []
        for event in redis_transport.consume():
            consumed.append(event)
            if len(consumed) >= 3:
                break

        # All events should have the same group_id preserved
        assert len(consumed) == 3
        for event in consumed:
            assert event.group_id == group_id

    def test_stream_key_uses_prefix(self, redis_transport: RedisTransport) -> None:
        """Stream key is based on configured prefix."""
        assert ":events" in redis_transport.stream_key
        assert redis_transport.stream_key.startswith("test_")

    def test_get_transport_creates_redis_transport(self) -> None:
        """get_transport with redis:// URL creates RedisTransport."""
        transport = get_transport(REDIS_URL, prefix="test_factory", ttl=60)
        assert isinstance(transport, RedisTransport)
        assert transport.ttl == 60


class TestRedisStreamOperations:
    """Tests for Redis stream low-level operations."""

    def test_stream_created_on_publish(self, redis_transport: RedisTransport) -> None:
        """Publishing creates the stream if it doesn't exist."""
        redis_transport.publish(
            TaskEvent(
                task_id="create-stream",
                name="tests.create",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )

        exists = redis_transport._client.exists(redis_transport.stream_key)
        assert exists == 1

    def test_stream_length_increases(self, redis_transport: RedisTransport) -> None:
        """Each publish adds an entry to the stream."""
        for i in range(3):
            redis_transport.publish(
                TaskEvent(
                    task_id=f"len-test-{i}",
                    name="tests.length",
                    state=TaskState.SUCCESS,
                    timestamp=datetime.now(timezone.utc),
                )
            )

        length = redis_transport._client.xlen(redis_transport.stream_key)
        assert length == 3


class TestIdleConsumeWithTinySocketTimeout:
    """A tiny ``socket_timeout`` in the broker URL must not busy-loop consume()."""

    IDLE_SECONDS = 2.0

    def test_consume_idle_with_tiny_socket_timeout_reads_at_a_bounded_rate(
        self,
    ) -> None:
        separator = "&" if "?" in REDIS_URL else "?"
        url = f"{REDIS_URL}{separator}socket_timeout=0.001"
        prefix = f"test_tiny_{int(time.time() * 1000)}"
        transport = RedisTransport.from_url(url, prefix=prefix, ttl=60)
        reads = 0
        real_xread = transport.client.xread

        def counting_xread(*args: object, **kwargs: object) -> object:
            nonlocal reads
            reads += 1
            return real_xread(*args, **kwargs)

        transport.client.xread = counting_xread  # type: ignore[method-assign]
        stop = threading.Event()
        transport.bind_stop_event(stop)
        received: queue.Queue[object] = queue.Queue()

        def run() -> None:
            for event in transport.consume():
                received.put(event)

        thread = threading.Thread(target=run, name="redis-idle", daemon=True)
        thread.start()
        try:
            time.sleep(self.IDLE_SECONDS)
            idle_reads = reads
            publisher = RedisTransport.from_url(REDIS_URL, prefix=prefix, ttl=60)
            publisher.publish(
                TaskEvent(
                    task_id="after-idle",
                    name="tests.idle",
                    state=TaskState.STARTED,
                    timestamp=datetime.now(timezone.utc),
                )
            )
            delivered = received.get(timeout=5)
        finally:
            stop.set()
            thread.join(timeout=5)
            with contextlib.suppress(Exception):
                transport.client.delete(transport.stream_key)

        assert not thread.is_alive()
        # One read per MIN_IDLE_READ_SECONDS at most (+2 for the edges); an
        # unpaced loop made over 200 in these two seconds.
        assert idle_reads <= self.IDLE_SECONDS / MIN_IDLE_READ_SECONDS + 2
        assert isinstance(delivered, TaskEvent)
        assert delivered.task_id == "after-idle"
