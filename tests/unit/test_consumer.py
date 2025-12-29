"""Tests for EventConsumer and AsyncEventConsumer."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.consumer import AsyncEventConsumer, EventConsumer
from stemtrace.server.store import GraphStore


class FakeTransport:
    """Fake transport for testing."""

    def __init__(self, events: list[TaskEvent] | None = None) -> None:
        self._events = events or []
        self._publish_count = 0
        self._consume_started = False
        self._stop = False

    def publish(self, event: TaskEvent) -> None:
        self._events.append(event)
        self._publish_count += 1

    def consume(self) -> Iterator[TaskEvent]:
        self._consume_started = True
        for event in self._events:
            if self._stop:
                break
            yield event
        # Block until stopped
        while not self._stop:
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop = True


@pytest.fixture
def store() -> GraphStore:
    """Create a fresh GraphStore for each test."""
    return GraphStore()


@pytest.fixture
def sample_events() -> list[TaskEvent]:
    """Create sample events for testing."""
    base_time = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        TaskEvent(
            task_id=f"task-{i}",
            name="tests.sample",
            state=TaskState.STARTED,
            timestamp=base_time,
        )
        for i in range(5)
    ]


class TestEventConsumer:
    def test_initial_state(self, store: GraphStore) -> None:
        consumer = EventConsumer("memory://", store)
        assert not consumer.is_running

    def test_start_stops_gracefully(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()

            assert consumer.is_running
            time.sleep(0.05)  # Let thread start

            # Stop should work
            fake.stop()
            consumer.stop(timeout=1.0)
            assert not consumer.is_running

    def test_start_idempotent(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()
            consumer.start()  # Second start should be no-op

            assert consumer.is_running

            fake.stop()
            consumer.stop(timeout=1.0)

    def test_stop_when_not_running(self, store: GraphStore) -> None:
        consumer = EventConsumer("memory://", store)
        # Should not raise
        consumer.stop()

    def test_consumes_events_into_store(
        self, store: GraphStore, sample_events: list[TaskEvent]
    ) -> None:
        fake = FakeTransport(sample_events.copy())

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()

            # Wait for events to be consumed
            time.sleep(0.1)

            fake.stop()
            consumer.stop(timeout=1.0)

            assert store.node_count == 5

    def test_config_passed_to_transport(self, store: GraphStore) -> None:
        mock_get_transport = MagicMock(return_value=FakeTransport())

        with patch("stemtrace.server.consumer.get_transport", mock_get_transport):
            consumer = EventConsumer(
                "redis://localhost:6379",
                store,
                prefix="custom_prefix",
                ttl=3600,
            )
            consumer.start()
            time.sleep(0.05)

            mock_get_transport.assert_called_once_with(
                "redis://localhost:6379",
                prefix="custom_prefix",
                ttl=3600,
            )

            mock_get_transport.return_value.stop()
            consumer.stop(timeout=1.0)


class TestAsyncEventConsumer:
    def test_initial_state(self, store: GraphStore) -> None:
        consumer = AsyncEventConsumer("memory://", store)
        assert not consumer.is_running

    @pytest.mark.asyncio
    async def test_async_context_manager(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            async with AsyncEventConsumer("memory://", store) as consumer:
                assert consumer.is_running
                fake.stop()

            assert not consumer.is_running

    def test_manual_start_stop(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = AsyncEventConsumer("memory://", store)
            consumer.start()
            assert consumer.is_running

            fake.stop()
            consumer.stop(timeout=1.0)
            assert not consumer.is_running
