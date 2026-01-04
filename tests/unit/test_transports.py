"""Tests for transport implementations."""

import logging
import socket
import sys
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.core.exceptions import UnsupportedBrokerError
from stemtrace.library.transports import get_transport
from stemtrace.library.transports.memory import MemoryTransport
from stemtrace.library.transports.rabbitmq import RabbitMQTransport
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

    def test_amqp_scheme_creates_transport(self) -> None:
        """get_transport('amqp://...') returns RabbitMQTransport."""
        transport = get_transport("amqp://localhost")
        assert isinstance(transport, RabbitMQTransport)

    def test_amqps_scheme_normalized(self) -> None:
        """amqps:// (TLS) is normalized to amqp transport."""
        transport = get_transport("amqps://localhost")
        assert isinstance(transport, RabbitMQTransport)

    def test_pyamqp_scheme_normalized(self) -> None:
        """pyamqp:// is normalized to amqp transport (Celery alias)."""
        transport = get_transport("pyamqp://localhost")
        assert isinstance(transport, RabbitMQTransport)

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


class TestRabbitMQTransport:
    """Tests for RabbitMQTransport without a broker."""

    def test_publish_logs_error_on_exception(
        self, caplog: Any, monkeypatch: Any
    ) -> None:
        """publish() logs warning on kombu errors, doesn't raise."""
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )

        def _boom(_self: RabbitMQTransport, _payload: dict[str, Any]) -> None:
            raise ConnectionError("RabbitMQ unavailable")

        # Patch the internal publisher to avoid depending on kombu in this test.
        monkeypatch.setattr(RabbitMQTransport, "_publish_payload", _boom)

        event = TaskEvent(
            task_id="task-123",
            name="tests.sample_task",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        )

        transport.publish(event)
        assert "Failed to publish event task-123 to RabbitMQ" in caplog.text

    def test_exchange_and_queue_names_derived_from_prefix(self) -> None:
        """Exchange/queue names derive from prefix and hostname."""
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="/stemtrace", ttl=60
        )
        assert transport.exchange_name == "stemtrace.events"
        assert transport.queue_name.startswith("stemtrace.events.")

    def test_parse_event_detects_task_event(self) -> None:
        """_parse_event returns TaskEvent for task_id payload."""
        payload = TaskEvent(
            task_id="t1",
            name="tests.t1",
            state=TaskState.SUCCESS,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        ).model_dump(mode="json")
        parsed = RabbitMQTransport._parse_event(payload)
        assert isinstance(parsed, TaskEvent)
        assert parsed.task_id == "t1"

    def test_parse_event_detects_worker_event(self) -> None:
        """_parse_event returns WorkerEvent for event_type payload."""
        payload = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="h",
            pid=1,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            registered_tasks=[],
        ).model_dump(mode="json")
        parsed = RabbitMQTransport._parse_event(payload)
        assert isinstance(parsed, WorkerEvent)
        assert parsed.hostname == "h"

    def test_publish_calls_internal_publisher_with_json_payload(
        self, monkeypatch: Any
    ) -> None:
        """publish() passes JSON-mode dict payload to internal publisher."""
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        seen: dict[str, Any] = {}

        def _capture(self: RabbitMQTransport, payload: dict[str, Any]) -> None:
            seen.update(payload)

        monkeypatch.setattr(RabbitMQTransport, "_publish_payload", _capture)

        event = TaskEvent(
            task_id="t2",
            name="tests.t2",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        transport.publish(event)
        assert seen["task_id"] == "t2"

    def test_consume_yields_event_using_fake_kombu(self, monkeypatch: Any) -> None:
        """consume() yields parsed events (exercise kombu control flow without I/O)."""
        fake_kombu = types.ModuleType("kombu")
        fake_kombu_messaging = types.ModuleType("kombu.messaging")

        class FakeMessage:
            def __init__(self) -> None:
                self.acked = False
                self.rejected = False

            def ack(self) -> None:
                self.acked = True

            def reject(self, *, requeue: bool) -> None:
                del requeue
                self.rejected = True

        class FakeChannel:
            def __init__(self) -> None:
                self.callbacks: list[Any] = []

        class Connection:
            def __init__(self, url: str, **_: Any) -> None:
                self.url = url
                self._channel = FakeChannel()
                self._drained = False

            def __enter__(self) -> "Connection":
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                del exc_type, exc, tb

            def channel(self) -> FakeChannel:
                return self._channel

            def drain_events(self, *, timeout: int) -> None:
                del timeout
                if self._drained:
                    raise TimeoutError
                self._drained = True

                body = TaskEvent(
                    task_id="consume-1",
                    name="tests.consume",
                    state=TaskState.RECEIVED,
                    timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                ).model_dump(mode="json")
                msg = FakeMessage()
                for cb in self._channel.callbacks:
                    cb(body, msg)

        class Exchange:
            def __init__(self, name: str, *, type: str, durable: bool) -> None:
                self.name = name
                del type, durable

            def maybe_bind(self, channel: FakeChannel) -> None:
                del channel

            def declare(self) -> None:
                return None

        class Queue:
            def __init__(
                self,
                *,
                name: str,
                exchange: Exchange,
                routing_key: str,
                durable: bool,
                queue_arguments: dict[str, Any],
            ) -> None:
                self.name = name
                del exchange, routing_key, durable, queue_arguments

            def maybe_bind(self, channel: FakeChannel) -> None:
                del channel

            def declare(self) -> None:
                return None

        class Consumer:
            def __init__(
                self,
                channel: FakeChannel,
                *,
                queues: list[Queue],
                callbacks: list[Any],
                accept: list[str],
            ) -> None:
                del queues, accept
                self._channel = channel
                self._callbacks = callbacks

            def __enter__(self) -> "Consumer":
                self._channel.callbacks = self._callbacks
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                del exc_type, exc, tb
                self._channel.callbacks = []

        fake_kombu.Connection = Connection
        fake_kombu.Exchange = Exchange
        fake_kombu.Queue = Queue
        fake_kombu_messaging.Consumer = Consumer

        monkeypatch.setitem(sys.modules, "kombu", fake_kombu)
        monkeypatch.setitem(sys.modules, "kombu.messaging", fake_kombu_messaging)

        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert isinstance(received, TaskEvent)
        assert received.task_id == "consume-1"

    def test_declare_exchange_and_queue_uses_queue_arguments(
        self, monkeypatch: Any
    ) -> None:
        """_declare_exchange_and_queue declares durable exchange + queue with TTL args."""
        fake_kombu = types.ModuleType("kombu")

        declared: dict[str, Any] = {}

        class FakeChannel:
            pass

        class Connection:
            def __init__(self, url: str, **_: Any) -> None:
                self.url = url
                self._channel = FakeChannel()

            def __enter__(self) -> "Connection":
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                del exc_type, exc, tb

            def channel(self) -> FakeChannel:
                return self._channel

        class Exchange:
            def __init__(self, name: str, *, type: str, durable: bool) -> None:
                declared["exchange_name"] = name
                declared["exchange_type"] = type
                declared["exchange_durable"] = durable

            def maybe_bind(self, channel: FakeChannel) -> None:
                del channel

            def declare(self) -> None:
                declared["exchange_declared"] = True

        class Queue:
            def __init__(
                self,
                *,
                name: str,
                exchange: Exchange,
                routing_key: str,
                durable: bool,
                queue_arguments: dict[str, Any],
            ) -> None:
                declared["queue_name"] = name
                declared["queue_routing_key"] = routing_key
                declared["queue_durable"] = durable
                declared["queue_arguments"] = dict(queue_arguments)
                del exchange

            def maybe_bind(self, channel: FakeChannel) -> None:
                del channel

            def declare(self) -> None:
                declared["queue_declared"] = True

        fake_kombu.Connection = Connection
        fake_kombu.Exchange = Exchange
        fake_kombu.Queue = Queue

        monkeypatch.setitem(sys.modules, "kombu", fake_kombu)

        # Stable consumer id for deterministic names.
        monkeypatch.setattr(socket, "gethostname", lambda: "host-1")

        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        transport._declare_exchange_and_queue()

        assert declared["exchange_name"] == "test.events"
        assert declared["exchange_type"] == "fanout"
        assert declared["exchange_durable"] is True
        assert declared["exchange_declared"] is True

        assert declared["queue_name"] == "test.events.host-1"
        assert declared["queue_routing_key"] == ""
        assert declared["queue_durable"] is True
        assert declared["queue_declared"] is True
        assert declared["queue_arguments"]["x-message-ttl"] == 60 * 1000
        assert declared["queue_arguments"]["x-expires"] > 60 * 1000

    def test_consume_rejects_malformed_message_and_continues(
        self, caplog: Any, monkeypatch: Any
    ) -> None:
        """consume() rejects malformed messages and still yields subsequent valid ones."""
        caplog.set_level(logging.DEBUG, logger="stemtrace.library.transports.rabbitmq")

        fake_kombu = types.ModuleType("kombu")
        fake_kombu_messaging = types.ModuleType("kombu.messaging")

        seen: dict[str, Any] = {}

        class FakeMessage:
            def __init__(self, *, reject_raises: bool) -> None:
                self.acked = False
                self.rejected = False
                self._reject_raises = reject_raises

            def ack(self) -> None:
                self.acked = True

            def reject(self, *, requeue: bool) -> None:
                del requeue
                self.rejected = True
                if self._reject_raises:
                    raise RuntimeError("reject failed")

        class FakeChannel:
            def __init__(self) -> None:
                self.callbacks: list[Any] = []

        class Connection:
            def __init__(self, url: str, **_: Any) -> None:
                self.url = url
                self._channel = FakeChannel()
                self._drain_count = 0

            def __enter__(self) -> "Connection":
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                del exc_type, exc, tb

            def channel(self) -> FakeChannel:
                return self._channel

            def drain_events(self, *, timeout: int) -> None:
                del timeout
                self._drain_count += 1

                if self._drain_count == 1:
                    # Malformed payload triggers reject path.
                    body = {"unexpected": "value"}
                    seen["invalid_msg"] = FakeMessage(reject_raises=True)
                    for cb in self._channel.callbacks:
                        cb(body, seen["invalid_msg"])
                    return

                if self._drain_count == 2:
                    body = TaskEvent(
                        task_id="consume-2",
                        name="tests.consume",
                        state=TaskState.RECEIVED,
                        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                    ).model_dump(mode="json")
                    seen["valid_msg"] = FakeMessage(reject_raises=False)
                    for cb in self._channel.callbacks:
                        cb(body, seen["valid_msg"])
                    return

                raise TimeoutError

        class Exchange:
            def __init__(self, name: str, *, type: str, durable: bool) -> None:
                self.name = name
                del type, durable

            def maybe_bind(self, channel: FakeChannel) -> None:
                del channel

            def declare(self) -> None:
                return None

        class Queue:
            def __init__(
                self,
                *,
                name: str,
                exchange: Exchange,
                routing_key: str,
                durable: bool,
                queue_arguments: dict[str, Any],
            ) -> None:
                self.name = name
                del exchange, routing_key, durable, queue_arguments

            def maybe_bind(self, channel: FakeChannel) -> None:
                del channel

            def declare(self) -> None:
                return None

        class Consumer:
            def __init__(
                self,
                channel: FakeChannel,
                *,
                queues: list[Queue],
                callbacks: list[Any],
                accept: list[str],
            ) -> None:
                del queues, accept
                self._channel = channel
                self._callbacks = callbacks

            def __enter__(self) -> "Consumer":
                self._channel.callbacks = self._callbacks
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                del exc_type, exc, tb
                self._channel.callbacks = []

        fake_kombu.Connection = Connection
        fake_kombu.Exchange = Exchange
        fake_kombu.Queue = Queue
        fake_kombu_messaging.Consumer = Consumer

        monkeypatch.setitem(sys.modules, "kombu", fake_kombu)
        monkeypatch.setitem(sys.modules, "kombu.messaging", fake_kombu_messaging)

        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert isinstance(received, TaskEvent)
        assert received.task_id == "consume-2"
        assert seen["invalid_msg"].rejected is True
        assert seen["valid_msg"].acked is True
        assert "Failed to parse event from RabbitMQ queue" in caplog.text
        assert "Failed to reject malformed RabbitMQ message" in caplog.text

    def test_ttl_property(self) -> None:
        """ttl property returns the configured TTL."""
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        assert transport.ttl == 60

    def test_parse_event_accepts_json_string_and_bytes(self) -> None:
        """_parse_event accepts JSON str and bytes payloads."""
        event = TaskEvent(
            task_id="t3",
            name="tests.t3",
            state=TaskState.SUCCESS,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        json_str = event.model_dump_json()
        parsed_str = RabbitMQTransport._parse_event(json_str)
        assert isinstance(parsed_str, TaskEvent)
        assert parsed_str.task_id == "t3"

        parsed_bytes = RabbitMQTransport._parse_event(json_str.encode())
        assert isinstance(parsed_bytes, TaskEvent)
        assert parsed_bytes.task_id == "t3"

    def test_parse_event_raises_for_unknown_payload(self) -> None:
        """_parse_event raises for payloads missing task_id and event_type."""
        with pytest.raises(ValueError):
            RabbitMQTransport._parse_event({"unexpected": "value"})

    def test_publish_logs_worker_event_identifier_on_exception(
        self, caplog: Any, monkeypatch: Any
    ) -> None:
        """publish() includes worker identifier in logs for WorkerEvent failures."""
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )

        def _boom(_self: RabbitMQTransport, _payload: dict[str, Any]) -> None:
            raise ConnectionError("RabbitMQ unavailable")

        monkeypatch.setattr(RabbitMQTransport, "_publish_payload", _boom)

        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1",
            pid=12345,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            registered_tasks=[],
        )

        transport.publish(event)
        assert "Failed to publish event worker-1:12345" in caplog.text

    def test_publish_uses_kombu_producer_without_network(
        self, monkeypatch: Any
    ) -> None:
        """_publish_payload runs against a fake kombu module (no broker required)."""
        fake_kombu = types.ModuleType("kombu")

        published: dict[str, Any] = {}

        class FakeChannel:
            pass

        class Connection:
            def __init__(self, url: str) -> None:
                self.url = url
                self._channel = FakeChannel()

            def __enter__(self) -> "Connection":
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                del exc_type, exc, tb

            def channel(self) -> FakeChannel:
                return self._channel

        class Exchange:
            def __init__(self, name: str, *, type: str, durable: bool) -> None:
                self.name = name
                del type, durable

        class Producer:
            def __init__(self, channel: FakeChannel) -> None:
                self.channel = channel

            def publish(self, body: Any, **kwargs: Any) -> None:
                published["body"] = body
                published.update(kwargs)

        fake_kombu.Connection = Connection
        fake_kombu.Exchange = Exchange
        fake_kombu.Producer = Producer

        monkeypatch.setitem(sys.modules, "kombu", fake_kombu)

        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        event = TaskEvent(
            task_id="t4",
            name="tests.t4",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        transport.publish(event)

        assert published["body"]["task_id"] == "t4"
        assert published["serializer"] == "json"
        assert published["delivery_mode"] == 2
