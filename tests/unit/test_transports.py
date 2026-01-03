"""Tests for transport implementations."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.core.exceptions import UnsupportedBrokerError
from stemtrace.library.transports import get_transport
from stemtrace.library.transports.memory import MemoryTransport
from stemtrace.library.transports.redis import RedisTransport


@pytest.fixture
def memory_transport() -> MemoryTransport:
    """Create a fresh MemoryTransport with cleared events."""
    MemoryTransport.clear()
    return MemoryTransport()


@pytest.fixture
def started_event() -> TaskEvent:
    """Create a STARTED event for testing."""
    return TaskEvent(
        task_id="task-001",
        name="tests.my_task",
        state=TaskState.STARTED,
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def success_event() -> TaskEvent:
    """Create a SUCCESS event for testing."""
    return TaskEvent(
        task_id="task-001",
        name="tests.my_task",
        state=TaskState.SUCCESS,
        timestamp=datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC),
    )


class TestMemoryTransport:
    """Tests for MemoryTransport."""

    def test_publish_adds_event(
        self,
        memory_transport: MemoryTransport,
        started_event: TaskEvent,
    ) -> None:
        """publish() stores event in events list."""
        memory_transport.publish(started_event)

        assert len(MemoryTransport.events) == 1
        assert MemoryTransport.events[0] == started_event

    def test_publish_worker_event_adds_event(
        self, memory_transport: MemoryTransport
    ) -> None:
        """publish() stores WorkerEvent instances as well."""
        worker_event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1",
            pid=12345,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
            registered_tasks=["tasks.add"],
        )

        memory_transport.publish(worker_event)

        assert len(MemoryTransport.events) == 1
        assert MemoryTransport.events[0] == worker_event

    def test_publish_multiple_events(
        self,
        memory_transport: MemoryTransport,
        started_event: TaskEvent,
        success_event: TaskEvent,
    ) -> None:
        """publish() stores multiple events in order."""
        memory_transport.publish(started_event)
        memory_transport.publish(success_event)

        assert len(MemoryTransport.events) == 2
        assert MemoryTransport.events[0] == started_event
        assert MemoryTransport.events[1] == success_event

    def test_consume_yields_all_events(
        self,
        memory_transport: MemoryTransport,
        started_event: TaskEvent,
        success_event: TaskEvent,
    ) -> None:
        """consume() yields all published events."""
        memory_transport.publish(started_event)
        memory_transport.publish(success_event)

        events = list(memory_transport.consume())

        assert events == [started_event, success_event]

    def test_clear_removes_all_events(
        self,
        memory_transport: MemoryTransport,
        started_event: TaskEvent,
    ) -> None:
        """clear() removes all stored events."""
        memory_transport.publish(started_event)
        MemoryTransport.clear()

        assert len(MemoryTransport.events) == 0

    def test_from_url_ignores_url(self) -> None:
        """from_url() creates transport regardless of URL."""
        transport = MemoryTransport.from_url("memory://ignored")

        assert isinstance(transport, MemoryTransport)

    def test_events_shared_across_instances(self, started_event: TaskEvent) -> None:
        """Events are shared across all MemoryTransport instances."""
        MemoryTransport.clear()
        transport1 = MemoryTransport()
        transport2 = MemoryTransport()

        transport1.publish(started_event)

        assert started_event in transport2.events


class TestGetTransport:
    """Tests for get_transport factory function."""

    def test_memory_scheme(self) -> None:
        """get_transport('memory://') returns MemoryTransport."""
        transport = get_transport("memory://")

        assert isinstance(transport, MemoryTransport)

    def test_unsupported_scheme_raises(self) -> None:
        """get_transport() raises for unknown schemes."""
        with pytest.raises(UnsupportedBrokerError) as exc_info:
            get_transport("unknown://localhost")

        assert exc_info.value.scheme == "unknown"

    def test_amqp_scheme_raises_for_now(self) -> None:
        """AMQP support is planned but not yet implemented."""
        with pytest.raises(UnsupportedBrokerError) as exc_info:
            get_transport("amqp://localhost")

        assert exc_info.value.scheme == "amqp"

    def test_redis_scheme_creates_transport(self) -> None:
        """get_transport('redis://...') returns RedisTransport.

        Note: This doesn't actually connect to Redis, it just creates the client.
        """
        transport = get_transport("redis://localhost:6379/0")

        assert isinstance(transport, RedisTransport)

    def test_rediss_scheme_normalized(self) -> None:
        """rediss:// (TLS) is normalized to redis transport."""
        transport = get_transport("rediss://localhost:6379/0")

        assert isinstance(transport, RedisTransport)

    def test_prefix_passed_to_transport(self) -> None:
        """Custom prefix is passed to transport."""
        transport = get_transport(
            "redis://localhost:6379/0",
            prefix="custom_prefix",
        )

        assert isinstance(transport, RedisTransport)
        assert transport.stream_key == "custom_prefix:events"

    def test_ttl_passed_to_transport(self) -> None:
        """Custom TTL is passed to transport."""
        transport = get_transport(
            "redis://localhost:6379/0",
            ttl=7200,
        )

        assert isinstance(transport, RedisTransport)
        assert transport.ttl == 7200


class TestRedisTransport:
    """Tests for RedisTransport with mocked Redis client."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create a mock Redis client."""
        return MagicMock()

    @pytest.fixture
    def transport(self, mock_client: MagicMock) -> RedisTransport:
        """Create a RedisTransport with mocked client."""
        return RedisTransport(client=mock_client, prefix="test", ttl=3600)

    @pytest.fixture
    def sample_event(self) -> TaskEvent:
        """Create a sample event for testing."""
        return TaskEvent(
            task_id="task-123",
            name="tests.sample_task",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        )

    @pytest.fixture
    def worker_event(self) -> WorkerEvent:
        """Create a sample WorkerEvent for testing."""
        return WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1",
            pid=12345,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
            registered_tasks=["tasks.add", "tasks.multiply"],
        )

    def test_client_property(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
    ) -> None:
        """client property returns the Redis client."""
        assert transport.client is mock_client

    def test_stream_key_property(self, transport: RedisTransport) -> None:
        """stream_key property returns prefixed key."""
        assert transport.stream_key == "test:events"

    def test_ttl_property(self, transport: RedisTransport) -> None:
        """ttl property returns the configured TTL."""
        assert transport.ttl == 3600

    def test_publish_calls_xadd(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """publish() calls xadd with serialized event."""
        transport.publish(sample_event)

        mock_client.xadd.assert_called_once()
        call_args = mock_client.xadd.call_args
        assert call_args[0][0] == "test:events"
        assert "data" in call_args[0][1]
        assert call_args[1]["maxlen"] == 10000  # max(3600, 10000)
        assert call_args[1]["approximate"] is True

    def test_publish_serializes_event_as_json(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """publish() serializes event to JSON."""
        transport.publish(sample_event)

        call_args = mock_client.xadd.call_args
        data = call_args[0][1]["data"]
        # Verify it's valid JSON that can be deserialized back
        restored = TaskEvent.model_validate_json(data)
        assert restored == sample_event

    def test_publish_logs_error_on_exception(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
        caplog: Any,
    ) -> None:
        """publish() logs warning on Redis errors, doesn't raise."""
        mock_client.xadd.side_effect = ConnectionError("Redis unavailable")

        # Should not raise
        transport.publish(sample_event)

        assert "Failed to publish event" in caplog.text
        assert "task-123" in caplog.text

    def test_publish_worker_event_logs_error_on_exception(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        worker_event: WorkerEvent,
        caplog: Any,
    ) -> None:
        """publish() logs warning on Redis errors for WorkerEvent, doesn't raise."""
        mock_client.xadd.side_effect = ConnectionError("Redis unavailable")

        # Should not raise
        transport.publish(worker_event)

        assert "Failed to publish event" in caplog.text
        assert "worker-1:12345" in caplog.text

    def test_consume_yields_worker_event(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        worker_event: WorkerEvent,
    ) -> None:
        """consume() yields WorkerEvent instances from stream when event_type present."""
        serialized = worker_event.model_dump_json().encode()
        mock_client.xread.return_value = [
            (
                b"test:events",
                [(b"1234567890-0", {b"data": serialized})],
            )
        ]

        events = []
        for event in transport.consume():
            events.append(event)
            break

        assert len(events) == 1
        assert events[0] == worker_event

    def test_consume_yields_events(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """consume() yields TaskEvent instances from stream."""
        serialized = sample_event.model_dump_json().encode()
        mock_client.xread.return_value = [
            (
                b"test:events",
                [(b"1234567890-0", {b"data": serialized})],
            )
        ]

        # Get first event only (consume() is infinite loop)
        events = []
        for event in transport.consume():
            events.append(event)
            break

        assert len(events) == 1
        assert events[0] == sample_event

    def test_consume_updates_last_id(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """consume() updates last_id after each message."""
        serialized = sample_event.model_dump_json().encode()

        # Return two messages, verify second xread uses updated ID
        mock_client.xread.return_value = [
            (
                b"test:events",
                [(b"1234567890-0", {b"data": serialized})],
            )
        ]

        # Consume first event
        gen = transport.consume()
        next(gen)

        # Now xread should have been called; check second call uses updated ID
        # Reset mock and call again
        mock_client.xread.reset_mock()
        mock_client.xread.return_value = [
            (
                b"test:events",
                [(b"1234567890-1", {b"data": serialized})],
            )
        ]

        next(gen)

        # Second call should use the updated ID from first message
        call_args = mock_client.xread.call_args
        assert call_args[0][0] == {"test:events": "1234567890-0"}

    def test_consume_handles_string_message_id(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """consume() handles string message IDs (not bytes)."""
        serialized = sample_event.model_dump_json().encode()
        # Message ID as string, not bytes
        mock_client.xread.return_value = [
            (
                "test:events",
                [("1234567890-0", {b"data": serialized})],
            )
        ]

        events = []
        for event in transport.consume():
            events.append(event)
            break

        assert len(events) == 1

    def test_consume_handles_string_data_field(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """consume() handles string 'data' key (not bytes)."""
        serialized = sample_event.model_dump_json()  # String, not bytes
        mock_client.xread.return_value = [
            (
                b"test:events",
                [(b"1234567890-0", {"data": serialized})],
            )
        ]

        events = []
        for event in transport.consume():
            events.append(event)
            break

        assert len(events) == 1
        assert events[0] == sample_event

    def test_consume_skips_messages_without_data(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """consume() skips messages without data, continues to next."""
        serialized = sample_event.model_dump_json().encode()
        call_count = 0

        def xread_side_effect(*args: Any, **kwargs: Any) -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First: message without data field
                return [
                    (
                        b"test:events",
                        [(b"1234567890-0", {b"other": b"value"})],
                    )
                ]
            # Second: message with data (to allow test to complete)
            return [
                (
                    b"test:events",
                    [(b"1234567890-1", {b"data": serialized})],
                )
            ]

        mock_client.xread.side_effect = xread_side_effect

        events = []
        for event in transport.consume():
            events.append(event)
            break

        # Should have skipped first message and got second
        assert len(events) == 1
        assert call_count == 2

    def test_consume_with_custom_last_id(
        self,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """consume() respects custom last_id parameter."""
        transport = RedisTransport(client=mock_client, prefix="test", ttl=3600)
        serialized = sample_event.model_dump_json().encode()

        def xread_side_effect(*args: Any, **kwargs: Any) -> list[Any]:
            # Verify first call uses custom ID
            assert args[0] == {"test:events": "9999-0"}
            return [
                (
                    b"test:events",
                    [(b"9999-1", {b"data": serialized})],
                )
            ]

        mock_client.xread.side_effect = xread_side_effect

        for _ in transport.consume(last_id="9999-0"):
            break

        mock_client.xread.assert_called_once()

    def test_parse_event_raises_for_unknown_payload(
        self, transport: RedisTransport
    ) -> None:
        """_parse_event should raise when payload lacks both event_type and task_id."""
        with pytest.raises(ValueError):
            transport._parse_event('{"unexpected": "value"}')

    def test_consume_skips_empty_reads_and_logs_parse_errors(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
        caplog: Any,
    ) -> None:
        """consume() should continue on empty reads and skip invalid JSON payloads."""
        serialized = sample_event.model_dump_json().encode()
        call_count = 0

        def xread_side_effect(*args: Any, **kwargs: Any) -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []
            if call_count == 2:
                return [
                    (
                        b"test:events",
                        [(b"1234567890-0", {b"data": b"not-json"})],
                    )
                ]
            return [
                (
                    b"test:events",
                    [(b"1234567890-1", {b"data": serialized})],
                )
            ]

        mock_client.xread.side_effect = xread_side_effect

        for _event in transport.consume():
            break

        assert call_count == 3
        assert "Failed to parse event from Redis stream" in caplog.text
