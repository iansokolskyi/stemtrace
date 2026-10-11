"""Tests for RedisTransport.publish(): bounded client and circuit breaker."""

import logging
import os
from datetime import datetime, timezone
from typing import Any

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.transports import redis as redis_transport
from stemtrace.transports.redis import (
    PUBLISH_CONNECT_TIMEOUT,
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_SOCKET_TIMEOUT,
    RECONNECT_INITIAL_BACKOFF,
    RedisTransport,
)

LOGGER_NAME = "stemtrace.transports.redis"
REFUSED = RedisConnectionError("Error 111 connecting to 127.0.0.1:6379.")
TIMED_OUT = RedisTimeoutError("Timeout connecting to server")
READ_TIMED_OUT = RedisTimeoutError("Timeout reading from socket")


class FakeClock:
    """Manual monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


Step = tuple[float, Exception | None] | tuple[float, Exception | None, bool]


class FakePublishRedis:
    """Scripted XADD: each call takes ``delay`` seconds, then fails or succeeds.

    A script item is ``(delay, error_or_None)`` or ``(delay, error, sent)``;
    ``sent`` marks the XADD as written to Redis before the error, like the
    publish client's connection does. Once the script is used up, calls
    succeed instantly.
    """

    def __init__(self, clock: FakeClock, script: list[Step]):
        self.clock = clock
        self.script = list(script)
        self.calls: list[str] = []

    def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> bytes:
        del fields, kwargs
        self.calls.append(key)
        step = self.script.pop(0) if self.script else (0.0, None)
        delay, error = step[0], step[1]
        if len(step) == 3 and step[2]:
            redis_transport._XADD_SENT.set(True)
        self.clock.now += delay
        if error is not None:
            raise error
        return b"1-0"


class UnusedRedis:
    """Consume client that publish() must never touch."""

    def xadd(self, *args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("publish() used the consume client")


def _event(task_id: str = "t-1") -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.task",
        state=TaskState.STARTED,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _make(
    script: list[Step],
) -> tuple[RedisTransport, FakePublishRedis, FakeClock]:
    clock = FakeClock()
    publisher = FakePublishRedis(clock, script)
    transport = RedisTransport(
        client=UnusedRedis(),  # type: ignore[arg-type]  # structural fake for redis.Redis
        prefix="test",
        ttl=60,
        publish_client_factory=lambda: publisher,  # type: ignore[arg-type,return-value]  # structural fake
        monotonic=clock,
    )
    return transport, publisher, clock


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == LOGGER_NAME and r.levelno == logging.WARNING
    ]


class TestRedisPublishBreaker:
    def test_publish_uses_publish_client_not_consume_client(self) -> None:
        transport, publisher, _ = _make([])

        transport.publish(_event())

        assert publisher.calls == ["test:events"]

    def test_publish_single_timeout_drops_only_its_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One timeout may come from a healthy Redis (e.g. the thread waited
        for the GIL past its deadline): it must not drop a cooldown of events."""
        transport, publisher, _ = _make([(PUBLISH_CONNECT_TIMEOUT, TIMED_OUT)])

        for i in range(10):
            transport.publish(_event(f"t-{i}"))

        assert len(publisher.calls) == 10
        assert transport._publish_breaker.is_open is False
        assert [r.getMessage() for r in _warnings(caplog)] == [
            "Failed to publish event t-0 to Redis; publishing continues "
            "(1 event(s) dropped since the last report)"
        ]

    def test_publish_hung_redis_opens_breaker_after_three_timeouts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, publisher, _ = _make([(PUBLISH_SOCKET_TIMEOUT, TIMED_OUT)] * 50)

        for i in range(50):
            transport.publish(_event(f"t-{i}"))

        assert len(publisher.calls) == PUBLISH_FAILURES_TO_OPEN
        assert [r.getMessage() for r in _warnings(caplog)][-1] == (
            "Failed to publish event t-2 to Redis; dropping events for 1.0s "
            "before trying again (2 event(s) dropped since the last report)"
        )

    @pytest.mark.parametrize("error", [READ_TIMED_OUT, RedisConnectionError("reset")])
    def test_publish_reply_lost_after_xadd_sent_counts_as_unconfirmed(
        self, error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Redis may still apply an XADD whose reply timed out (e.g. a stall)."""
        transport, _, _ = _make([(PUBLISH_SOCKET_TIMEOUT, error, True)])

        transport.publish(_event("t-0"))

        assert [r.getMessage() for r in _warnings(caplog)] == [
            "Failed to publish event t-0 to Redis; publishing continues "
            "(0 event(s) dropped, 1 sent but unconfirmed since the last report)"
        ]

    def test_publish_error_reply_after_xadd_sent_counts_as_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An error reply means Redis rejected the XADD: the event is lost."""
        transport, _, _ = _make([(0.001, ResponseError("OOM"), True)])

        transport.publish(_event("t-0"))

        assert (
            _warnings(caplog)[0]
            .getMessage()
            .endswith("(1 event(s) dropped since the last report)")
        )

    def test_publish_timeout_before_xadd_sent_counts_as_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, _, _ = _make([(PUBLISH_SOCKET_TIMEOUT, READ_TIMED_OUT, False)])

        transport.publish(_event("t-0"))

        assert (
            _warnings(caplog)[0]
            .getMessage()
            .endswith("(1 event(s) dropped since the last report)")
        )

    def test_publish_fast_refusals_open_after_threshold(self) -> None:
        transport, publisher, _ = _make([(0.001, REFUSED)] * 10)

        for i in range(10):
            transport.publish(_event(f"t-{i}"))

        assert len(publisher.calls) == PUBLISH_FAILURES_TO_OPEN

    def test_publish_slow_successes_never_open_breaker(self) -> None:
        """Wall time is not evidence of a slow Redis (e.g. another thread may
        hold the GIL), so only failures open the breaker."""
        transport, publisher, _ = _make([(2.0, None)] * 10)

        for i in range(10):
            transport.publish(_event(f"t-{i}"))

        assert len(publisher.calls) == 10
        assert transport._publish_breaker.is_open is False

    def test_publish_probes_after_cooldown_and_recovers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, publisher, clock = _make([(PUBLISH_SOCKET_TIMEOUT, TIMED_OUT)] * 3)
        for i in range(4):
            transport.publish(_event(f"t-{i}"))
        assert len(publisher.calls) == 3

        clock.now += RECONNECT_INITIAL_BACKOFF
        transport.publish(_event("t-4"))
        transport.publish(_event("t-5"))

        assert len(publisher.calls) == 5
        assert _warnings(caplog)[-1].getMessage() == (
            "Publishing to Redis recovered after 1.0s "
            "(1 event(s) dropped since the last report)"
        )

    def test_publish_worker_event_failure_logs_worker_identifier(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, _, _ = _make([(0.001, REFUSED)])
        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="w1",
            pid=42,
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

        transport.publish(event)

        assert _warnings(caplog)[0].getMessage() == (
            "Failed to publish event w1:42:worker_ready to Redis; publishing "
            "continues (1 event(s) dropped since the last report)"
        )


class TestRedisPublishClient:
    @pytest.mark.parametrize(
        "url",
        [
            "redis://localhost:6379/0",
            "rediss://localhost:6380/0",
            "unix:///tmp/redis.sock",
            "redis://localhost:6379/0?socket_timeout=30&socket_connect_timeout=30",
            "redis://localhost:6379/0?retry_on_timeout=True",
        ],
    )
    def test_from_url_publish_client_is_bounded_without_retries(self, url: str) -> None:
        transport = RedisTransport.from_url(url)

        connection = transport.publish_client.connection_pool.make_connection()

        assert connection.socket_timeout == PUBLISH_SOCKET_TIMEOUT
        assert connection.socket_connect_timeout == PUBLISH_CONNECT_TIMEOUT
        assert connection.retry.get_retries() == 0

    def test_from_url_keeps_consume_client_timeouts_and_other_options(self) -> None:
        url = "redis://localhost:6379/3?socket_timeout=30&health_check_interval=7"
        plain = redis.Redis.from_url(url).connection_pool.make_connection()

        transport = RedisTransport.from_url(url)

        consume = transport.client.connection_pool.make_connection()
        publish = transport.publish_client.connection_pool.make_connection()
        assert consume.socket_timeout == plain.socket_timeout == 30.0
        assert transport.publish_client is not transport.client
        assert publish.db == 3
        assert publish.health_check_interval == 7

    def test_constructor_without_publish_client_publishes_with_client(self) -> None:
        clock = FakeClock()
        client = FakePublishRedis(clock, [])

        transport = RedisTransport(
            client=client,  # type: ignore[arg-type]  # structural fake
            prefix="test",
            ttl=60,
        )
        transport.publish(_event())

        assert transport.publish_client is client
        assert client.calls == ["test:events"]


class TestRedisPublishClientPerProcess:
    """Each process builds its own publish client, so a forked child never
    touches a connection pool (or its lock) inherited from the parent."""

    def _make_counting(
        self,
    ) -> tuple[RedisTransport, list[FakePublishRedis]]:
        clock = FakeClock()
        built: list[FakePublishRedis] = []

        def factory() -> FakePublishRedis:
            built.append(FakePublishRedis(clock, []))
            return built[-1]

        transport = RedisTransport(
            client=UnusedRedis(),  # type: ignore[arg-type]  # structural fake
            prefix="test",
            ttl=60,
            publish_client_factory=factory,  # type: ignore[arg-type]  # structural fake
            monotonic=clock,
        )
        return transport, built

    def test_publish_client_is_built_lazily_once_per_process(self) -> None:
        transport, built = self._make_counting()
        assert built == []

        transport.publish(_event("a"))
        transport.publish(_event("b"))

        assert len(built) == 1
        assert built[0].calls == ["test:events", "test:events"]

    def test_publish_in_forked_child_builds_new_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport, built = self._make_counting()
        transport.publish(_event("in-parent"))
        parent_pid = os.getpid()

        monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)
        transport.publish(_event("in-child"))
        transport.publish(_event("in-child-again"))

        assert len(built) == 2
        assert built[0].calls == ["test:events"]
        assert built[1].calls == ["test:events", "test:events"]
        assert transport.publish_client is built[1]

    def test_from_url_publish_client_does_not_close_its_pool_when_dropped(
        self,
    ) -> None:
        """A child drops the parent's client; closing it would take the
        inherited pool's lock (redis-py 8.1 ConnectionPool.reset)."""
        transport = RedisTransport.from_url("redis://localhost:6379/0")

        assert transport.publish_client.auto_close_connection_pool is False


class TestRedisPublishConnectionTracksXadd:
    """The publish client's connections note when XADD reached the socket."""

    @pytest.mark.parametrize(
        "url", ["redis://localhost:6379/0", "rediss://localhost:6380/0"]
    )
    def test_send_command_marks_only_xadd_as_sent(
        self, url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = RedisTransport.from_url(url)
        pool = transport.publish_client.connection_pool
        connection = pool.make_connection()
        sent: list[Any] = []
        monkeypatch.setattr(
            connection, "send_packed_command", lambda command, **kw: sent.append(1)
        )
        token = redis_transport._XADD_SENT.set(False)
        try:
            connection.send_command("CLIENT", "SETINFO", "LIB-NAME", "x")
            assert redis_transport._XADD_SENT.get() is False
            connection.send_command("XADD", "k", "*", "data", "x")
            assert redis_transport._XADD_SENT.get() is True
        finally:
            redis_transport._XADD_SENT.reset(token)
        assert len(sent) == 2
        assert isinstance(
            connection,
            type(redis.Redis.from_url(url).connection_pool.make_connection()),
        )
