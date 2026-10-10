"""Tests for transport implementations."""

import copy
import itertools
import logging
import socket
import sys
import threading
import types
from datetime import datetime, timezone
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.core.exceptions import UnsupportedBrokerError
from stemtrace.core.ports import ConnectionAwareTransport, StoppableTransport
from stemtrace.library.transports import get_transport
from stemtrace.library.transports.memory import MemoryTransport
from stemtrace.library.transports.rabbitmq import (
    CONNECT_TIMEOUT,
    DRAIN_TIMEOUT,
    RECONNECT_INITIAL_BACKOFF,
    RECONNECT_MAX_BACKOFF,
    RabbitMQTransport,
)
from stemtrace.library.transports.redis import (
    RECONNECT_INITIAL_BACKOFF as REDIS_RECONNECT_INITIAL_BACKOFF,
)
from stemtrace.library.transports.redis import (
    RECONNECT_MAX_BACKOFF as REDIS_RECONNECT_MAX_BACKOFF,
)
from stemtrace.library.transports.redis import (
    RedisTransport,
    _normalize_redis_ssl_params,
)


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
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def success_event() -> TaskEvent:
    """Create a SUCCESS event for testing."""
    return TaskEvent(
        task_id="task-001",
        name="tests.my_task",
        state=TaskState.SUCCESS,
        timestamp=datetime(2024, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
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
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
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


class TestNormalizeRedisSslParams:
    """Tests for _normalize_redis_ssl_params URL normalization."""

    def test_cert_required_normalized(self) -> None:
        url = "rediss://:pass@host:6379/1?ssl_cert_reqs=CERT_REQUIRED"
        assert "ssl_cert_reqs=required" in _normalize_redis_ssl_params(url)

    def test_cert_none_normalized(self) -> None:
        url = "rediss://:pass@host:6379/0?ssl_cert_reqs=CERT_NONE"
        assert "ssl_cert_reqs=none" in _normalize_redis_ssl_params(url)

    def test_cert_optional_normalized(self) -> None:
        url = "rediss://:pass@host:6379/0?ssl_cert_reqs=CERT_OPTIONAL"
        assert "ssl_cert_reqs=optional" in _normalize_redis_ssl_params(url)

    def test_already_lowercase_unchanged(self) -> None:
        url = "rediss://:pass@host:6379/0?ssl_cert_reqs=required"
        assert "ssl_cert_reqs=required" in _normalize_redis_ssl_params(url)

    def test_no_query_params_unchanged(self) -> None:
        url = "rediss://:pass@host:6379/0"
        assert _normalize_redis_ssl_params(url) == url

    def test_no_ssl_cert_reqs_unchanged(self) -> None:
        url = "rediss://:pass@host:6379/0?timeout=5"
        assert _normalize_redis_ssl_params(url) == url

    def test_other_params_preserved(self) -> None:
        url = "rediss://:pass@host:6379/1?ssl_cert_reqs=CERT_REQUIRED&timeout=5"
        result = _normalize_redis_ssl_params(url)
        assert "ssl_cert_reqs=required" in result
        assert "timeout=5" in result


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
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )

    @pytest.fixture
    def worker_event(self) -> WorkerEvent:
        """Create a sample WorkerEvent for testing."""
        return WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1",
            pid=12345,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
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

    def test_consume_when_xread_times_out_retries_and_yields_event(
        self,
        transport: RedisTransport,
        mock_client: MagicMock,
        sample_event: TaskEvent,
    ) -> None:
        """consume() treats a socket read timeout as an empty read and keeps going."""
        serialized = sample_event.model_dump_json().encode()
        call_count = 0

        def xread_side_effect(*args: Any, **kwargs: Any) -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RedisTimeoutError("Timeout reading from socket")
            return [
                (
                    b"test:events",
                    [(b"1234567890-0", {b"data": serialized})],
                )
            ]

        mock_client.xread.side_effect = xread_side_effect

        events = []
        for event in transport.consume():
            events.append(event)
            break

        assert call_count == 2
        assert events == [sample_event]

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


def _install_fake_kombu_for_consume(
    monkeypatch: Any, *, drain_handler: Any
) -> type[Any]:
    """Install a fake kombu consumer stack into sys.modules (no broker required).

    The fake models the kombu behaviour the transport depends on: an entity
    (Exchange/Queue) binds to a channel only once (``maybe_bind`` is a no-op
    when already bound, and ``entity(channel)`` copies the entity, keeping an
    existing binding), declaring an unbound entity fails, and declaring on a
    channel whose connection has been closed fails like kombu does.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        drain_handler: Callable invoked from Connection.drain_events with signature:
            (drain_count: int, callbacks: list[Any]) -> None

    Returns:
        The fake Connection class. ``Connection.instances`` lists every
        connection opened, so tests can inspect their arguments and calls.
    """
    fake_kombu = types.ModuleType("kombu")
    fake_kombu_messaging = types.ModuleType("kombu.messaging")

    class FakeMessage:
        def __init__(self, *, reject_raises: bool = False) -> None:
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
        def __init__(self, connection: "Connection") -> None:
            self.connection = connection
            self.callbacks: list[Any] = []
            self.on_decode_error: Any = None

    class Connection:
        instances: ClassVar[list["Connection"]] = []
        # Hosts that refuse connections; tests mutate this to simulate outages.
        down_hosts: ClassVar[set[str]] = set()
        # Optional hook called with each host as it is tried.
        on_attempt: ClassVar[Any] = None

        def __init__(self, url: str, **kwargs: Any) -> None:
            self.url = url
            self.kwargs = kwargs
            # kombu splits ";"-separated URLs into failover alternates.
            self.alt: list[str] = url.split(";") if ";" in url else []
            self.attempted_hosts: list[str] = []
            self.connected_host: str | None = None
            self.closed = False
            self.released = False
            self.collected = False
            self.ensure_calls: list[dict[str, Any]] = []
            self.drain_timeouts: list[float] = []
            self._channel = FakeChannel(self)
            self._drain_count = 0
            Connection.instances.append(self)

        @property
        def connected(self) -> bool:
            return self.connected_host is not None and not self.closed

        def ensure_connection(self, **kwargs: Any) -> "Connection":
            """Mimic kombu's retry_over_time: raise before switching hosts."""
            self.ensure_calls.append(kwargs)
            max_retries = kwargs.get("max_retries")
            hosts = self.alt or [self.url]
            for retries in itertools.count():
                host = hosts[retries % len(hosts)]
                self.attempted_hosts.append(host)
                if Connection.on_attempt is not None:
                    Connection.on_attempt(host)
                if host not in Connection.down_hosts:
                    self.connected_host = host
                    return self
                if max_retries is not None and retries >= max_retries:
                    raise ConnectionRefusedError(f"{host} is down")
                # kombu calls `callback` before moving on to the next host.
                callback = kwargs.get("callback")
                if callback is not None:
                    callback()
            raise AssertionError("unreachable")

        def channel(self) -> FakeChannel:
            if not self.connected:
                raise AssertionError("channel() before ensure_connection()")
            return self._channel

        def release(self) -> None:
            self.released = True
            self.closed = True

        def collect(self) -> None:
            self.collected = True
            self.closed = True

        def drain_events(self, *, timeout: float) -> None:
            self.drain_timeouts.append(timeout)
            self._drain_count += 1
            drain_handler(self._drain_count, self._channel.callbacks)

    class _Entity:
        def __init__(self) -> None:
            self.channel: FakeChannel | None = None

        def maybe_bind(self, channel: FakeChannel) -> Any:
            # kombu binds only once: a bound entity ignores later channels.
            if self.channel is None and channel:
                self.channel = channel
            return self

        def __call__(self, channel: FakeChannel) -> Any:
            # kombu's bind(): copy, then maybe_bind (keeps an existing binding).
            return copy.copy(self).maybe_bind(channel)

        def declare(self) -> None:
            if self.channel is None:
                raise RuntimeError("entity is not bound to a channel")
            if self.channel.connection.closed:
                raise ConnectionError("connection already closed")

    class Exchange(_Entity):
        def __init__(self, name: str, *, type: str, durable: bool) -> None:
            super().__init__()
            self.name = name
            del type, durable

    class Queue(_Entity):
        def __init__(
            self,
            *,
            name: str,
            exchange: Exchange,
            routing_key: str,
            durable: bool,
            queue_arguments: dict[str, Any],
        ) -> None:
            super().__init__()
            self.name = name
            del exchange, routing_key, durable, queue_arguments

    class Consumer:
        def __init__(
            self,
            channel: FakeChannel,
            *,
            queues: list[Queue],
            callbacks: list[Any],
            on_decode_error: Any = None,
            accept: list[str],
        ) -> None:
            del queues, accept
            self._channel = channel
            self._callbacks = callbacks
            self._on_decode_error = on_decode_error

        def consume(self) -> None:
            self._channel.callbacks = self._callbacks
            self._channel.on_decode_error = self._on_decode_error

    fake_kombu.Connection = Connection
    fake_kombu.Exchange = Exchange
    fake_kombu.Queue = Queue
    fake_kombu.FakeMessage = FakeMessage
    fake_kombu_messaging.Consumer = Consumer

    monkeypatch.setitem(sys.modules, "kombu", fake_kombu)
    monkeypatch.setitem(sys.modules, "kombu.messaging", fake_kombu_messaging)
    return Connection


class _RunawayLoopError(BaseException):
    """Raised by _BoundedSleep to break a reconnect loop that never recovers."""


class _BoundedSleep:
    """Injectable sleep that records delays and aborts a runaway reconnect loop."""

    def __init__(self, limit: int = 20) -> None:
        self.delays: list[float] = []
        self._limit = limit

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self._limit:
            raise _RunawayLoopError(f"still reconnecting after {self._limit} sleeps")


class _RecordingPublishChannel:
    def __init__(self, published: dict[str, Any]) -> None:
        self.published = published


class _RecordingPublishAmqpConnection:
    """py-amqp connection that records close() and collect() calls."""

    def __init__(self, published: dict[str, Any]) -> None:
        self._published = published

    def close(self) -> None:
        self._published["closed"] = True

    def collect(self) -> None:
        self._published["collected"] = True


class _RecordingPublishConnection:
    """kombu Connection that always connects; a subclass binds ``published``."""

    published: ClassVar[dict[str, Any]]

    def __init__(self, url: str, **_: Any) -> None:
        self.url = url
        self.alt: list[str] = []
        self.connection = _RecordingPublishAmqpConnection(self.published)
        self._channel = _RecordingPublishChannel(self.published)

    def __enter__(self) -> "_RecordingPublishConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb

    def ensure_connection(self, **_: Any) -> "_RecordingPublishConnection":
        return self

    def channel(self) -> _RecordingPublishChannel:
        return self._channel


class _RecordingPublishExchange:
    def __init__(self, name: str, *, type: str, durable: bool) -> None:
        self.name = name
        del type, durable


class _RecordingPublishProducer:
    def __init__(self, channel: _RecordingPublishChannel) -> None:
        self.channel = channel

    def publish(self, body: Any, **kwargs: Any) -> None:
        self.channel.published["body"] = body
        self.channel.published.update(kwargs)


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
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
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
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
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
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
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
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        transport.publish(event)
        assert seen["task_id"] == "t2"

    def test_consume_yields_event_using_fake_kombu(self, monkeypatch: Any) -> None:
        """consume() yields parsed events (exercise kombu control flow without I/O)."""
        seen: dict[str, Any] = {}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            if drain_count > 1:
                raise TimeoutError

            body = TaskEvent(
                task_id="consume-1",
                name="tests.consume",
                state=TaskState.RECEIVED,
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ).model_dump(mode="json")
            # Use the fake message type we installed on the kombu module.
            msg = sys.modules["kombu"].FakeMessage()  # type: ignore[attr-defined]
            seen["msg"] = msg
            for cb in callbacks:
                cb(body, msg)

        _install_fake_kombu_for_consume(monkeypatch, drain_handler=drain_handler)

        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert isinstance(received, TaskEvent)
        assert received.task_id == "consume-1"
        assert seen["msg"].acked is True

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

        seen: dict[str, Any] = {}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            # Use the fake message type we installed on the kombu module.
            FakeMessage = sys.modules["kombu"].FakeMessage  # type: ignore[attr-defined]
            if drain_count == 1:
                body = {"unexpected": "value"}
                seen["invalid_msg"] = FakeMessage(reject_raises=True)
                for cb in callbacks:
                    cb(body, seen["invalid_msg"])
                return
            if drain_count == 2:
                body = TaskEvent(
                    task_id="consume-2",
                    name="tests.consume",
                    state=TaskState.RECEIVED,
                    timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
                ).model_dump(mode="json")
                seen["valid_msg"] = FakeMessage(reject_raises=False)
                for cb in callbacks:
                    cb(body, seen["valid_msg"])
                return
            raise TimeoutError

        _install_fake_kombu_for_consume(monkeypatch, drain_handler=drain_handler)

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
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
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
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            registered_tasks=[],
        )

        transport.publish(event)
        assert "Failed to publish event worker-1:12345" in caplog.text

    def test_publish_uses_kombu_producer_without_network(
        self, monkeypatch: Any
    ) -> None:
        """_publish_payload runs against a fake kombu module (no broker required)."""
        published: dict[str, Any] = {}
        fake_kombu = types.ModuleType("kombu")
        fake_kombu.Connection = type(
            "Connection", (_RecordingPublishConnection,), {"published": published}
        )
        fake_kombu.Exchange = _RecordingPublishExchange
        fake_kombu.Producer = _RecordingPublishProducer

        monkeypatch.setitem(sys.modules, "kombu", fake_kombu)

        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        event = TaskEvent(
            task_id="t4",
            name="tests.t4",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        transport.publish(event)

        assert published["body"]["task_id"] == "t4"
        assert published["serializer"] == "json"
        assert published["delivery_mode"] == 2
        assert published["closed"] is True
        assert "collected" not in published


class _RecordingStopEvent(threading.Event):
    """Stop event that records wait() timeouts and sets itself on the first wait."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        self.set()
        return True


class TestRabbitMQConsumeBackoff:
    """Reconnect backoff, stop handling and connectivity of RabbitMQTransport.consume()."""

    @staticmethod
    def _event_body() -> dict[str, Any]:
        return TaskEvent(
            task_id="backoff-1",
            name="tests.backoff",
            state=TaskState.RECEIVED,
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ).model_dump(mode="json")

    @classmethod
    def _deliver(cls, callbacks: list[Any]) -> None:
        msg = sys.modules["kombu"].FakeMessage()  # type: ignore[attr-defined]
        for cb in callbacks:
            cb(cls._event_body(), msg)

    def test_backoff_constants_match_redis_transport(self) -> None:
        """Both transports back off with the same initial delay and cap."""
        assert RECONNECT_INITIAL_BACKOFF == REDIS_RECONNECT_INITIAL_BACKOFF
        assert RECONNECT_MAX_BACKOFF == REDIS_RECONNECT_MAX_BACKOFF

    def test_transport_implements_stoppable_and_connection_aware(self) -> None:
        """The consumer can bind a stop event and read connectivity."""
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        assert isinstance(transport, StoppableTransport)
        assert isinstance(transport, ConnectionAwareTransport)
        assert transport.connected is None

    def test_consume_when_broker_down_backs_off_exponentially_and_logs_once(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failures sleep 1,2,4,...,30s; only the first failure logs a traceback."""
        failures_before_success = 7
        attempts = {"n": 0}
        sleeper = _BoundedSleep()

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        original_ensure = conn_cls.ensure_connection

        def flaky_ensure(conn: Any, **kwargs: Any) -> Any:
            attempts["n"] += 1
            if attempts["n"] <= failures_before_success:
                raise ConnectionRefusedError("broker down")
            return original_ensure(conn, **kwargs)

        monkeypatch.setattr(conn_cls, "ensure_connection", flaky_ensure)

        transport = RabbitMQTransport(
            "amqp://localhost", prefix="test", ttl=60, sleep=sleeper
        )
        with caplog.at_level(logging.INFO):
            gen = transport.consume()
            received = next(gen)
            gen.close()

        assert received.task_id == "backoff-1"
        assert sleeper.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        # One per failure, plus the recovery line.
        assert len(warnings) == failures_before_success + 1
        assert warnings[0].exc_info is not None
        assert all(r.exc_info is None for r in warnings[1:])
        assert "still failing (attempt 7): broker down" in warnings[-2].getMessage()
        assert "reconnected after 7 failure(s)" in warnings[-1].getMessage()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_consume_when_connection_drops_before_first_read_keeps_backing_off(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Connections dropped before their first drain completes don't reset backoff."""
        sleeper = _BoundedSleep()
        drains = {"n": 0}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            drains["n"] += 1
            if drains["n"] <= 3:
                raise ConnectionResetError("dropped")
            self._deliver(callbacks)

        _install_fake_kombu_for_consume(monkeypatch, drain_handler=drain_handler)
        transport = RabbitMQTransport(
            "amqp://localhost", prefix="test", ttl=60, sleep=sleeper
        )
        with caplog.at_level(logging.INFO):
            gen = transport.consume()
            received = next(gen)
            gen.close()

        assert received.task_id == "backoff-1"
        assert sleeper.delays == [1.0, 2.0, 4.0]
        assert "reconnected after 3 failure(s)" in caplog.text

    def test_consume_after_drain_timeout_resets_backoff(self, monkeypatch: Any) -> None:
        """A drain that times out proves the connection is alive and resets backoff."""
        sleeper = _BoundedSleep()
        attempts = {"n": 0}
        drains = {"n": 0}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            drains["n"] += 1
            if drains["n"] == 1:
                raise TimeoutError
            if drains["n"] == 2:
                raise ConnectionResetError("dropped")
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        original_channel = conn_cls.channel

        def flaky_channel(conn: Any) -> Any:
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise ConnectionRefusedError("broker down")
            return original_channel(conn)

        monkeypatch.setattr(conn_cls, "channel", flaky_channel)
        transport = RabbitMQTransport(
            "amqp://localhost", prefix="test", ttl=60, sleep=sleeper
        )
        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert received.task_id == "backoff-1"
        assert sleeper.delays == [1.0, 2.0, 1.0]

    def test_consume_when_stop_event_set_during_backoff_wakes_and_ends(
        self, monkeypatch: Any
    ) -> None:
        """The backoff waits on the bound stop event, not sleep, and then ends."""
        sleeper = _BoundedSleep()

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count, callbacks
            raise AssertionError("must not connect")

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )

        def broken_channel(conn: Any) -> Any:
            del conn
            raise ConnectionRefusedError("broker down")

        monkeypatch.setattr(conn_cls, "channel", broken_channel)
        stop = _RecordingStopEvent()
        transport = RabbitMQTransport(
            "amqp://localhost", prefix="test", ttl=60, sleep=sleeper
        )
        transport.bind_stop_event(stop)

        assert list(transport.consume()) == []
        assert stop.waits == [1.0]
        assert sleeper.delays == []
        assert transport.connected is False

    def test_consume_when_stop_event_set_while_connected_ends(
        self, monkeypatch: Any
    ) -> None:
        """Setting the stop event ends the drain loop without reconnecting."""
        stop = threading.Event()
        drains = {"n": 0}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count, callbacks
            drains["n"] += 1
            stop.set()
            raise TimeoutError

        _install_fake_kombu_for_consume(monkeypatch, drain_handler=drain_handler)
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        transport.bind_stop_event(stop)

        assert list(transport.consume()) == []
        assert drains["n"] == 1

    def test_consume_with_stop_event_already_set_never_connects(
        self, monkeypatch: Any
    ) -> None:
        """consume() ends immediately when the stop event is already set."""
        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=lambda _n, _cb: None
        )

        def unexpected_channel(conn: Any) -> Any:
            del conn
            raise AssertionError("must not connect")

        monkeypatch.setattr(conn_cls, "channel", unexpected_channel)
        stop = threading.Event()
        stop.set()
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        transport.bind_stop_event(stop)

        assert list(transport.consume()) == []

    def test_connected_tracks_failures_and_successful_drains(
        self, monkeypatch: Any
    ) -> None:
        """connected is False while failing and True after a drain (timeouts count)."""
        observed: list[bool | None] = []
        attempts = {"n": 0}
        transport_ref: dict[str, RabbitMQTransport] = {}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            observed.append(transport_ref["t"].connected)
            if drain_count == 1:
                raise TimeoutError
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        original_channel = conn_cls.channel

        def flaky_channel(conn: Any) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionRefusedError("broker down")
            return original_channel(conn)

        monkeypatch.setattr(conn_cls, "channel", flaky_channel)
        transport = RabbitMQTransport(
            "amqp://localhost",
            prefix="test",
            ttl=60,
            sleep=lambda _s: observed.append(transport.connected),
        )
        transport_ref["t"] = transport

        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert received.task_id == "backoff-1"
        # sleep after the failure, first drain (still failing), second drain
        # (after a timed-out drain proved the connection alive).
        assert observed == [False, False, True]
        assert transport.connected is True

    def test_consume_called_again_resets_connected_until_first_drain(
        self, monkeypatch: Any
    ) -> None:
        """A new consume() reports unknown connectivity until its first drain."""
        observed: list[bool | None] = []
        transport_ref: dict[str, RabbitMQTransport] = {}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            observed.append(transport_ref["t"].connected)
            self._deliver(callbacks)

        _install_fake_kombu_for_consume(monkeypatch, drain_handler=drain_handler)
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        transport_ref["t"] = transport

        for _ in range(2):
            gen = transport.consume()
            next(gen)
            gen.close()

        assert observed == [None, None]
        assert transport.connected is True

    def test_consume_when_error_follows_stop_ends_without_backoff(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A connection error raised after stop was requested ends quietly."""
        sleeper = _BoundedSleep()
        stop = threading.Event()

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count, callbacks
            stop.set()
            raise ConnectionResetError("closed during shutdown")

        _install_fake_kombu_for_consume(monkeypatch, drain_handler=drain_handler)
        transport = RabbitMQTransport(
            "amqp://localhost", prefix="test", ttl=60, sleep=sleeper
        )
        transport.bind_stop_event(stop)

        with caplog.at_level(logging.WARNING):
            assert list(transport.consume()) == []

        assert sleeper.delays == []
        assert caplog.records == []

    def test_consume_after_connection_drop_declares_on_new_channel(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After a drop the exchange and queue are declared on the new connection."""
        sleeper = _BoundedSleep()
        drains = {"n": 0}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            drains["n"] += 1
            if drains["n"] == 2:
                raise ConnectionResetError("broker restarted")
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        transport = RabbitMQTransport(
            "amqp://localhost", prefix="test", ttl=60, sleep=sleeper
        )
        with caplog.at_level(logging.INFO):
            gen = transport.consume()
            first = next(gen)
            second = next(gen)
            gen.close()

        assert [first.task_id, second.task_id] == ["backoff-1", "backoff-1"]
        assert len(conn_cls.instances) == 2
        assert conn_cls.instances[0].released is True
        assert sleeper.delays == [1.0]
        assert "reconnected after 1 failure(s)" in caplog.text

    def test_consume_connects_without_kombu_retry_and_with_bounded_timeouts(
        self, monkeypatch: Any
    ) -> None:
        """Each attempt connects once (no hidden kombu retry) with short timeouts."""

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            if drain_count == 1:
                raise TimeoutError
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        gen = transport.consume()
        next(gen)
        gen.close()

        assert CONNECT_TIMEOUT <= 2.0
        assert DRAIN_TIMEOUT <= 1.0
        (conn,) = conn_cls.instances
        assert conn.kwargs == {"connect_timeout": CONNECT_TIMEOUT}
        assert conn.ensure_calls == [
            {
                "max_retries": 0,
                "reraise_as_library_errors": False,
                "callback": transport._raise_if_stopping,
            }
        ]
        assert conn.drain_timeouts == [DRAIN_TIMEOUT, DRAIN_TIMEOUT]

    def test_consume_with_failover_url_uses_next_host_without_backoff(
        self, monkeypatch: Any
    ) -> None:
        """With h1 down, one attempt tries h1 then h2 (no sleep) and consumes from h2."""
        sleeper = _BoundedSleep()

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        conn_cls.down_hosts.add("amqp://h1//")
        transport = RabbitMQTransport(
            "amqp://h1//;amqp://h2//", prefix="test", ttl=60, sleep=sleeper
        )
        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert received.task_id == "backoff-1"
        assert sleeper.delays == []
        (conn,) = conn_cls.instances
        assert conn.ensure_calls == [
            {
                "max_retries": 1,
                "reraise_as_library_errors": False,
                "callback": transport._raise_if_stopping,
            }
        ]
        assert conn.attempted_hosts == ["amqp://h1//", "amqp://h2//"]
        assert conn.connected_host == "amqp://h2//"

    def test_consume_with_failover_url_all_hosts_down_backs_off_per_round(
        self, monkeypatch: Any
    ) -> None:
        """When every host is down, each attempt tries all of them, then backs off."""

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        conn_cls.down_hosts.update({"amqp://h1//", "amqp://h2//"})
        delays: list[float] = []

        def sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 2:
                conn_cls.down_hosts.discard("amqp://h2//")

        transport = RabbitMQTransport(
            "amqp://h1//;amqp://h2//", prefix="test", ttl=60, sleep=sleep
        )
        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert received.task_id == "backoff-1"
        assert delays == [1.0, 2.0]
        assert [c.attempted_hosts for c in conn_cls.instances] == [
            ["amqp://h1//", "amqp://h2//"]
        ] * 3

    def test_consume_when_stopped_abandons_connection_without_close_handshake(
        self, monkeypatch: Any
    ) -> None:
        """On stop the socket is dropped (collect), not closed gracefully."""
        stop = threading.Event()

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count, callbacks
            stop.set()
            raise TimeoutError

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        transport.bind_stop_event(stop)

        assert list(transport.consume()) == []
        (conn,) = conn_cls.instances
        assert conn.collected is True
        assert conn.released is False

    def test_consume_closed_by_caller_releases_connection(
        self, monkeypatch: Any
    ) -> None:
        """Closing the iterator without a stop closes the connection gracefully."""

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        transport = RabbitMQTransport.from_url(
            "amqp://localhost", prefix="test", ttl=60
        )
        gen = transport.consume()
        next(gen)
        gen.close()

        (conn,) = conn_cls.instances
        assert conn.released is True
        assert conn.collected is False

    def test_consume_drops_undecodable_message_and_stays_connected(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A message kombu can't decode is rejected once, not retried as an outage."""
        caplog.set_level(logging.DEBUG, logger="stemtrace.library.transports.rabbitmq")
        sleeper = _BoundedSleep()
        bad: dict[str, Any] = {}
        conn_ref: dict[str, Any] = {}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            if drain_count == 1:
                msg = sys.modules["kombu"].FakeMessage(  # type: ignore[attr-defined]
                    reject_raises=True
                )
                bad["msg"] = msg
                channel = conn_ref["cls"].instances[-1].channel()
                channel.on_decode_error(
                    msg,
                    ValueError("Refusing to deserialize content of type text/plain"),
                )
                return
            self._deliver(callbacks)

        conn_ref["cls"] = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )
        transport = RabbitMQTransport(
            "amqp://localhost", prefix="test", ttl=60, sleep=sleeper
        )
        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert received.task_id == "backoff-1"
        assert bad["msg"].rejected is True
        assert sleeper.delays == []
        assert transport.connected is True
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "Dropping undecodable message" in warnings[0].getMessage()
        assert "text/plain" in warnings[0].getMessage()
        assert "Failed to reject undecodable RabbitMQ message" in caplog.text

    def test_consume_when_closing_connection_fails_logs_debug_and_continues(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing close never escapes; the consumer reconnects as usual."""
        caplog.set_level(logging.DEBUG, logger="stemtrace.library.transports.rabbitmq")
        sleeper = _BoundedSleep()
        drains = {"n": 0}

        def drain_handler(drain_count: int, callbacks: list[Any]) -> None:
            del drain_count
            drains["n"] += 1
            if drains["n"] == 1:
                raise ConnectionResetError("dropped")
            self._deliver(callbacks)

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=drain_handler
        )

        def broken_release(conn: Any) -> None:
            conn.closed = True
            raise OSError("socket already closed")

        monkeypatch.setattr(conn_cls, "release", broken_release)
        transport = RabbitMQTransport(
            "amqp://localhost", prefix="test", ttl=60, sleep=sleeper
        )
        gen = transport.consume()
        received = next(gen)
        gen.close()

        assert received.task_id == "backoff-1"
        assert sleeper.delays == [1.0]
        assert "Failed to close RabbitMQ consumer connection" in caplog.text

    def test_consume_when_stopped_during_failover_pass_skips_remaining_hosts(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stop requested while trying one host ends before trying the next."""
        sleeper = _BoundedSleep()
        stop = threading.Event()
        hosts = ["amqp://h1//", "amqp://h2//", "amqp://h3//"]

        conn_cls = _install_fake_kombu_for_consume(
            monkeypatch, drain_handler=lambda _n, _cb: None
        )
        conn_cls.down_hosts.update(hosts)
        monkeypatch.setattr(
            conn_cls,
            "on_attempt",
            lambda host: stop.set() if host == hosts[0] else None,
        )
        transport = RabbitMQTransport(
            ";".join(hosts), prefix="test", ttl=60, sleep=sleeper
        )
        transport.bind_stop_event(stop)

        with caplog.at_level(logging.WARNING):
            assert list(transport.consume()) == []

        (conn,) = conn_cls.instances
        assert conn.attempted_hosts == [hosts[0]]
        assert conn.ensure_calls[0]["max_retries"] == 2
        assert sleeper.delays == []
        assert caplog.records == []
