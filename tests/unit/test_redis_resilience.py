"""Tests for RedisTransport timeouts, reconnects and connectivity reporting."""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.transports.redis import (
    DEFAULT_SOCKET_TIMEOUT,
    READ_TIMEOUT_WARN_AFTER,
    READ_TIMEOUT_WARN_INTERVAL,
    RECONNECT_MAX_BACKOFF,
    XREAD_BLOCK_MS,
    RedisTransport,
)

READ_TIMEOUT = RedisTimeoutError("Timeout reading from socket")
CONNECT_TIMEOUT = RedisTimeoutError("Timeout connecting to server")
CONNECTION_CLOSED = RedisConnectionError("Connection closed by server.")


class FakePool:
    """Minimal stand-in for redis.ConnectionPool exposing connection kwargs."""

    def __init__(self, socket_timeout: float | None) -> None:
        self.connection_kwargs: dict[str, Any] = {"socket_timeout": socket_timeout}


class FakeRedis:
    """Scripted Redis client: each xread() pops the next result or raises it."""

    def __init__(
        self, script: list[Any], *, socket_timeout: float | None = None
    ) -> None:
        self.script = list(script)
        self.calls: list[tuple[dict[str, str], int]] = []
        self.connection_pool = FakePool(socket_timeout)

    def xread(self, streams: dict[str, str], block: int, count: int) -> Any:
        del count
        self.calls.append((dict(streams), block))
        if not self.script:
            raise AssertionError("xread called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    """Manual monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SleepRecorder:
    """Records requested sleeps and advances a FakeClock instead of sleeping."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.clock.now += seconds


def _event(task_id: str) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.task",
        state=TaskState.STARTED,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _entry(message_id: str, event: TaskEvent) -> list[Any]:
    return [
        (
            b"test:events",
            [(message_id.encode(), {b"data": event.model_dump_json().encode()})],
        )
    ]


def _make(
    script: list[Any], *, socket_timeout: float | None = None
) -> tuple[RedisTransport, FakeRedis, SleepRecorder, FakeClock]:
    clock = FakeClock()
    sleeper = SleepRecorder(clock)
    client = FakeRedis(script, socket_timeout=socket_timeout)
    transport = RedisTransport(
        client=client,  # type: ignore[arg-type]  # structural fake for redis.Redis
        prefix="test",
        ttl=60,
        sleep=sleeper,
        monotonic=clock,
    )
    return transport, client, sleeper, clock


def _take(iterator: Iterator[Any], n: int) -> list[Any]:
    return [next(iterator) for _ in range(n)]


class TestSocketTimeoutConfiguration:
    def test_from_url_without_socket_timeout_sets_timeout_above_block_window(
        self,
    ) -> None:
        transport = RedisTransport.from_url("redis://localhost:6379/0")

        kwargs = transport.client.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == DEFAULT_SOCKET_TIMEOUT
        assert DEFAULT_SOCKET_TIMEOUT > XREAD_BLOCK_MS / 1000
        assert transport._block_ms == XREAD_BLOCK_MS

    def test_from_url_with_socket_timeout_in_url_keeps_url_value(self) -> None:
        transport = RedisTransport.from_url(
            "redis://localhost:6379/0?socket_timeout=30"
        )

        kwargs = transport.client.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == 30.0
        assert transport._block_ms == XREAD_BLOCK_MS

    def test_from_url_with_short_socket_timeout_shrinks_block_window(self) -> None:
        transport = RedisTransport.from_url("redis://localhost:6379/0?socket_timeout=2")

        assert transport._block_ms == 1000

    def test_consume_uses_block_window_below_client_socket_timeout(self) -> None:
        transport, client, _, _ = _make(
            [_entry("1-0", _event("a"))], socket_timeout=4.0
        )

        _take(transport.consume(), 1)

        assert client.calls[0][1] == 2000


class TestReadTimeouts:
    def test_consume_with_single_read_timeout_retries_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        event = _event("a")
        transport, client, sleeper, _ = _make([READ_TIMEOUT, _entry("1-0", event)])

        with caplog.at_level(logging.DEBUG):
            assert _take(transport.consume(), 1) == [event]

        assert len(client.calls) == 2
        assert sleeper.delays == []
        assert transport.connected is True
        assert [r.levelno for r in caplog.records] == [logging.DEBUG]

    def test_consume_with_repeated_read_timeouts_warns_rate_limited(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        event = _event("a")
        timeouts = READ_TIMEOUT_WARN_AFTER + 3
        script: list[Any] = [READ_TIMEOUT] * timeouts
        script.append(_entry("1-0", event))
        transport, client, _, clock = _make(script)
        states: list[bool | None] = []
        original_xread = client.xread

        def xread(*args: Any, **kwargs: Any) -> Any:
            states.append(transport.connected)
            return original_xread(*args, **kwargs)

        client.xread = xread  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            iterator = transport.consume()
            assert _take(iterator, 1) == [event]

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert (
            f"timed out {READ_TIMEOUT_WARN_AFTER} times in a row" in warnings[0].message
        )
        # Disconnected once the warning threshold is reached, reconnected after.
        assert states[READ_TIMEOUT_WARN_AFTER] is False
        assert transport.connected is True
        assert clock.now == 0.0

    def test_consume_with_read_timeouts_after_interval_warns_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        event = _event("a")
        script: list[Any] = [READ_TIMEOUT] * (READ_TIMEOUT_WARN_AFTER + 1)
        script.append(_entry("1-0", event))
        transport, client, _, clock = _make(script)
        original_xread = client.xread

        def xread(*args: Any, **kwargs: Any) -> Any:
            # Each read timeout takes "socket_timeout" seconds of wall time.
            clock.now += READ_TIMEOUT_WARN_INTERVAL
            return original_xread(*args, **kwargs)

        client.xread = xread  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            _take(transport.consume(), 1)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2


class TestReconnect:
    def test_consume_with_connect_timeout_warns_and_backs_off(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        event = _event("a")
        transport, _, sleeper, _ = _make([CONNECT_TIMEOUT, _entry("1-0", event)])
        seen: list[bool | None] = []

        with caplog.at_level(logging.INFO):
            for item in transport.consume():
                seen.append(transport.connected)
                assert item == event
                break

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        assert sleeper.delays == [1.0]
        assert seen == [True]
        assert "reconnected after 1 failure(s)" in caplog.text

    def test_consume_with_connection_errors_backs_off_exponentially_to_cap(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        event = _event("a")
        transport, _, sleeper, _ = _make(
            [CONNECTION_CLOSED] * 7 + [_entry("1-0", event)]
        )

        with caplog.at_level(logging.WARNING):
            _take(transport.consume(), 1)

        assert sleeper.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        assert max(sleeper.delays) == RECONNECT_MAX_BACKOFF
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 7
        assert warnings[0].exc_info is not None
        assert all(w.exc_info is None for w in warnings[1:])
        assert "still failing (attempt 7)" in warnings[-1].message

    def test_consume_after_success_resets_backoff(self) -> None:
        a, b = _event("a"), _event("b")
        transport, _, sleeper, _ = _make(
            [
                CONNECTION_CLOSED,
                CONNECTION_CLOSED,
                _entry("1-0", a),
                CONNECTION_CLOSED,
                _entry("2-0", b),
            ]
        )

        assert _take(transport.consume(), 2) == [a, b]
        assert sleeper.delays == [1.0, 2.0, 1.0]

    def test_consume_after_connection_error_resumes_from_last_id(self) -> None:
        a, b = _event("a"), _event("b")
        transport, client, _, _ = _make(
            [_entry("5-0", a), CONNECTION_CLOSED, _entry("6-0", b)]
        )

        assert _take(transport.consume(), 2) == [a, b]
        assert [call[0] for call in client.calls] == [
            {"test:events": "0"},
            {"test:events": "5-0"},
            {"test:events": "5-0"},
        ]

    def test_consume_called_again_resumes_from_last_consumed_id(self) -> None:
        a, b = _event("a"), _event("b")
        transport, client, _, _ = _make([_entry("7-0", a), _entry("8-0", b)])

        assert _take(transport.consume(), 1) == [a]
        assert _take(transport.consume(), 1) == [b]
        assert client.calls[1][0] == {"test:events": "7-0"}

    def test_connected_while_failing_is_false(self) -> None:
        transport, client, _, _ = _make([CONNECTION_CLOSED, _entry("1-0", _event("a"))])
        observed: list[bool | None] = []
        original_xread = client.xread

        def xread(*args: Any, **kwargs: Any) -> Any:
            observed.append(transport.connected)
            return original_xread(*args, **kwargs)

        client.xread = xread  # type: ignore[method-assign]

        assert transport.connected is None
        _take(transport.consume(), 1)

        assert observed == [None, False]
        assert transport.connected is True
