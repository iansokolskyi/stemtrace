"""Tests for RedisTransport timeouts, reconnects and connectivity reporting."""

import logging
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports import redis as redis_transport_module
from stemtrace.transports.redis import (
    MIN_IDLE_READ_SECONDS,
    READ_TIMEOUT_WARN_AFTER,
    READ_TIMEOUT_WARN_INTERVAL,
    RECONNECT_MAX_BACKOFF,
    XREAD_BLOCK_MS,
    RedisTransport,
    effective_socket_timeout,
)

READ_TIMEOUT = RedisTimeoutError("Timeout reading from socket")
CONNECT_TIMEOUT = RedisTimeoutError("Timeout connecting to server")
CONNECTION_CLOSED = RedisConnectionError("Connection closed by server.")


class FakePool:
    """Minimal stand-in for redis.ConnectionPool exposing connection kwargs."""

    def __init__(self, socket_timeout: float | None) -> None:
        self.connection_kwargs: dict[str, Any] = {"socket_timeout": socket_timeout}


class FakeRedis:
    """Scripted Redis client: each xread() pops the next result or raises it.

    A read that comes back empty or times out has waited ``idle_read_s``
    first: redis-py 8.1.0 ``Connection.read_response`` raises ``TimeoutError``
    only after ``socket.timeout`` fires, i.e. after the socket timeout, and
    XREAD ``BLOCK`` answers an empty reply after its block window. The default
    is the window consume() uses with redis-py's 5s socket timeout; pass a
    small value to model a tiny ``socket_timeout`` in the URL. ``clock`` is
    advanced by that wait (reads that return entries or fail to connect take
    no time).
    """

    def __init__(
        self,
        script: list[Any],
        *,
        socket_timeout: float | None = None,
        clock: "FakeClock | None" = None,
        idle_read_s: float = XREAD_BLOCK_MS / 1000,
    ) -> None:
        self.script = list(script)
        self.calls: list[tuple[dict[str, str], int]] = []
        self.connection_pool = FakePool(socket_timeout)
        self._clock = clock
        self._idle_read_s = idle_read_s

    def xread(self, streams: dict[str, str], block: int, count: int) -> Any:
        del count
        self.calls.append((dict(streams), block))
        if not self.script:
            raise AssertionError("xread called more times than scripted")
        item = self.script.pop(0)
        idle = item == [] or item is None or item is READ_TIMEOUT
        if idle and self._clock is not None:
            self._clock.now += self._idle_read_s
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
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _entry(message_id: str, event: TaskEvent) -> list[Any]:
    return [
        (
            b"test:events",
            [(message_id.encode(), {b"data": event.model_dump_json().encode()})],
        )
    ]


def _make(
    script: list[Any],
    *,
    socket_timeout: float | None = None,
    idle_read_s: float = XREAD_BLOCK_MS / 1000,
) -> tuple[RedisTransport, FakeRedis, SleepRecorder, FakeClock]:
    clock = FakeClock()
    sleeper = SleepRecorder(clock)
    client = FakeRedis(
        script, socket_timeout=socket_timeout, clock=clock, idle_read_s=idle_read_s
    )
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


def _expected_block_ms(client: redis.Redis) -> int:
    """Block window derived from the timeout an actual connection would use."""
    timeout = client.connection_pool.make_connection().socket_timeout
    if timeout is None:
        return XREAD_BLOCK_MS
    return min(XREAD_BLOCK_MS, int(timeout * 1000 / 2))


class TestSocketTimeoutConfiguration:
    def test_from_url_client_timeouts_match_plain_redis_from_url(self) -> None:
        """The publish path keeps redis-py's own timeouts (unchanged from main)."""
        url = "redis://localhost:6379/0"
        plain = redis.Redis.from_url(url, decode_responses=False)

        transport = RedisTransport.from_url(url)

        def timeouts(client: redis.Redis) -> dict[str, Any]:
            kwargs = client.connection_pool.connection_kwargs
            return {k: v for k, v in kwargs.items() if "timeout" in k}

        assert timeouts(transport.client) == timeouts(plain)
        made = transport.client.connection_pool.make_connection()
        expected = plain.connection_pool.make_connection()
        assert made.socket_timeout == expected.socket_timeout
        assert made.socket_connect_timeout == expected.socket_connect_timeout
        assert effective_socket_timeout(transport.client) == expected.socket_timeout

    @pytest.mark.parametrize(
        "url",
        [
            "redis://localhost:6379/0",
            "rediss://localhost:6380/0",
            "unix:///tmp/redis.sock",
            "redis://localhost:6379/0?socket_timeout=30",
            "redis://localhost:6379/0?socket_timeout=3",
        ],
    )
    def test_block_window_for_client_is_below_effective_socket_timeout(
        self, url: str
    ) -> None:
        client = redis.Redis.from_url(url)

        transport = RedisTransport(client=client, prefix="test", ttl=60)

        assert transport._block_ms == _expected_block_ms(client)

    @pytest.mark.skipif(
        int(redis.__version__.split(".")[0]) < 8,
        reason="redis-py 8 changed the default socket_timeout to 5s",
    )
    def test_block_window_for_plain_from_url_on_redis_py_8_is_half_default(
        self,
    ) -> None:
        client = redis.Redis.from_url("redis://localhost:6379/0")

        assert effective_socket_timeout(client) == 5.0
        assert RedisTransport(client=client, prefix="t", ttl=60)._block_ms == 2500

    def test_block_window_with_explicit_unbounded_timeout_is_full(self) -> None:
        client = redis.Redis(host="localhost", socket_timeout=None)

        assert effective_socket_timeout(client) is None
        assert RedisTransport(client=client, prefix="t", ttl=60)._block_ms == (
            XREAD_BLOCK_MS
        )

    def test_consume_uses_block_window_below_client_socket_timeout(self) -> None:
        transport, client, _, _ = _make(
            [_entry("1-0", _event("a"))], socket_timeout=4.0
        )

        _take(transport.consume(), 1)

        assert client.calls[0][1] == 2000


class TestIdleReadPacing:
    """A tiny ``socket_timeout`` must not turn consume() into a busy loop.

    redis-py 8.1.0 ``Connection.read_response`` raises ``TimeoutError`` once
    the socket timeout passes, and consume() blocks XREAD for half of it
    (``_xread_block_ms``, at least 1ms), so with ``socket_timeout=0.001`` an
    idle read is over in about a millisecond. ``idle_read_s`` models that.
    """

    TINY = 0.001

    @pytest.mark.parametrize("idle", ["empty", "timeout"])
    def test_consume_with_tiny_socket_timeout_waits_out_the_idle_interval(
        self, idle: str
    ) -> None:
        reads = 20
        reply: Any = [] if idle == "empty" else READ_TIMEOUT
        event = _event("a")
        transport, client, sleeper, clock = _make(
            [reply] * reads + [_entry("1-0", event)],
            socket_timeout=self.TINY,
            idle_read_s=self.TINY,
        )

        assert _take(transport.consume(), 1) == [event]

        assert len(client.calls) == reads + 1
        assert sleeper.delays == pytest.approx(
            [MIN_IDLE_READ_SECONDS - self.TINY] * reads
        )
        # Bounded: no more than one read per MIN_IDLE_READ_SECONDS.
        assert clock.now == pytest.approx(reads * MIN_IDLE_READ_SECONDS)

    def test_consume_with_idle_reads_at_the_block_window_does_not_wait(self) -> None:
        event = _event("a")
        transport, _, sleeper, _ = _make(
            [[], READ_TIMEOUT, [], _entry("1-0", event)],
            socket_timeout=5.0,
        )

        assert _take(transport.consume(), 1) == [event]

        assert sleeper.delays == []

    def test_consume_with_events_flowing_does_not_wait(self) -> None:
        events = [_event(f"e{i}") for i in range(3)]
        transport, _, sleeper, _ = _make(
            [_entry(f"{i}-0", e) for i, e in enumerate(events)],
            idle_read_s=self.TINY,
        )

        assert _take(transport.consume(), 3) == events

        assert sleeper.delays == []

    def test_consume_idle_wait_uses_the_stop_event_and_ends_when_it_is_set(
        self,
    ) -> None:
        class StopsDuringTheWait(threading.Event):
            def __init__(self) -> None:
                super().__init__()
                self.waits: list[float | None] = []

            def wait(self, timeout: float | None = None) -> bool:
                self.waits.append(timeout)
                self.set()  # the stop arrives while consume() is waiting
                return True

        transport, client, sleeper, _ = _make(
            [[]] * 5, socket_timeout=self.TINY, idle_read_s=self.TINY
        )
        stop = StopsDuringTheWait()
        transport.bind_stop_event(stop)

        assert list(transport.consume()) == []

        # One read, one wait on the stop event (not the injected sleep), done.
        assert len(client.calls) == 1
        assert stop.waits == pytest.approx([MIN_IDLE_READ_SECONDS - self.TINY])
        assert sleeper.delays == []


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
        # All the timeouts fell within one warning interval.
        assert clock.now < READ_TIMEOUT_WARN_INTERVAL

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


class TestOtherErrorsAndStopping:
    def test_consume_with_response_error_marks_disconnected_and_raises(self) -> None:
        transport, _, sleeper, _ = _make(
            [_entry("1-0", _event("a")), ResponseError("WRONGTYPE wrong kind")]
        )
        iterator = transport.consume()
        _take(iterator, 1)
        assert transport.connected is True

        with pytest.raises(ResponseError):
            next(iterator)

        assert transport.connected is False
        assert sleeper.delays == []

    def test_consume_called_again_resets_connected_until_first_read(self) -> None:
        transport, client, _, _ = _make(
            [ResponseError("NOPERM"), _entry("1-0", _event("a"))]
        )
        with pytest.raises(ResponseError):
            next(transport.consume())
        observed: list[bool | None] = []
        original_xread = client.xread

        def xread(*args: Any, **kwargs: Any) -> Any:
            observed.append(transport.connected)
            return original_xread(*args, **kwargs)

        client.xread = xread  # type: ignore[method-assign]

        _take(transport.consume(), 1)

        assert observed == [None]
        assert transport.connected is True

    def test_consume_with_bound_stop_event_ends_iterator(self) -> None:
        transport, client, _, _ = _make([_entry("1-0", _event("a"))])
        stop = threading.Event()
        transport.bind_stop_event(stop)
        iterator = transport.consume()
        _take(iterator, 1)

        stop.set()

        assert list(iterator) == []
        assert len(client.calls) == 1

    def test_stop_event_interrupts_reconnect_backoff(self) -> None:
        transport, _, sleeper, _ = _make([CONNECTION_CLOSED] * 3)
        stop = threading.Event()
        transport.bind_stop_event(stop)
        finished = threading.Event()

        def run() -> None:
            list(transport.consume())
            finished.set()

        thread = threading.Thread(target=run, daemon=True)
        started = time.monotonic()
        thread.start()
        time.sleep(0.1)  # Now waiting in the first 1s backoff.
        stop.set()

        assert finished.wait(timeout=0.5)
        assert time.monotonic() - started < 0.9
        # The bound event replaces the injected sleep entirely.
        assert sleeper.delays == []


class TestFailureBookkeeping:
    """How connection failures, read timeouts and successful reads reset each other."""

    @pytest.mark.parametrize(
        "connect_error", [CONNECTION_CLOSED, CONNECT_TIMEOUT], ids=["closed", "timeout"]
    )
    def test_consume_with_connect_failure_between_read_timeouts_restarts_count(
        self, connect_error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        streak = READ_TIMEOUT_WARN_AFTER - 1
        script: list[Any] = [READ_TIMEOUT] * streak
        script += [connect_error]
        script += [READ_TIMEOUT] * streak
        script.append(_entry("1-0", _event("a")))
        transport, _, sleeper, _ = _make(script)

        with caplog.at_level(logging.WARNING):
            _take(transport.consume(), 1)

        warnings = [r.getMessage() for r in caplog.records]
        assert len(warnings) == 1
        assert warnings[0].startswith("Redis consumer on test:events cannot reach")
        assert sleeper.delays == [1.0]

    def test_consume_with_read_timeouts_between_failures_keeps_backoff_growing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, _, sleeper, _ = _make(
            [
                CONNECTION_CLOSED,
                READ_TIMEOUT,
                CONNECTION_CLOSED,
                _entry("1-0", _event("a")),
            ]
        )

        with caplog.at_level(logging.WARNING):
            _take(transport.consume(), 1)

        assert sleeper.delays == [1.0, 2.0]
        assert "still failing (attempt 2)" in caplog.records[-1].getMessage()

    def test_consume_after_successful_read_warns_on_next_timeout_streak_at_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        streak: list[Any] = [READ_TIMEOUT] * READ_TIMEOUT_WARN_AFTER
        transport, _, _, clock = _make(
            [*streak, [], *streak, _entry("1-0", _event("a"))]
        )

        with caplog.at_level(logging.WARNING):
            _take(transport.consume(), 1)

        # Within one warning interval, but the empty read in between ended the
        # first streak.
        assert clock.now < READ_TIMEOUT_WARN_INTERVAL
        assert [r.getMessage() for r in caplog.records] == [
            "XREAD on test:events timed out 3 times in a row; check the network "
            "and the socket_timeout setting"
        ] * 2

    def test_consume_after_reconnect_logs_recovery_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, _, _, _ = _make(
            [CONNECTION_CLOSED, CONNECTION_CLOSED, _entry("1-0", _event("a"))]
        )

        with caplog.at_level(logging.INFO):
            _take(transport.consume(), 1)

        recovery = caplog.records[-1]
        assert recovery.levelno == logging.INFO
        assert recovery.getMessage() == (
            "Redis consumer on test:events reconnected after 2 failure(s)"
        )
        assert recovery.exc_info is None


def test_consume_first_backoff_reads_initial_constant_at_consume_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redis_transport_module, "RECONNECT_INITIAL_BACKOFF", 0.25)
    transport, _, sleeper, _ = _make([CONNECTION_CLOSED, _entry("1-0", _event("a"))])

    _take(transport.consume(), 1)

    assert sleeper.delays == [0.25]
