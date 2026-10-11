"""Tests for RedisTransport.publish(): bounded client and circuit breaker."""

import logging
from datetime import datetime, timezone
from typing import Any

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.transports.redis import (
    PUBLISH_CONNECT_TIMEOUT,
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_SLOW_ATTEMPT,
    PUBLISH_SOCKET_TIMEOUT,
    RECONNECT_INITIAL_BACKOFF,
    RedisTransport,
)

LOGGER_NAME = "stemtrace.transports.redis"
REFUSED = RedisConnectionError("Error 111 connecting to 127.0.0.1:6379.")
TIMED_OUT = RedisTimeoutError("Timeout connecting to server")


class FakeClock:
    """Manual monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakePublishRedis:
    """Scripted XADD: each call takes ``delay`` seconds, then fails or succeeds.

    A script item is ``(delay, error_or_None)``; once the script is used up,
    calls succeed instantly.
    """

    def __init__(self, clock: FakeClock, script: list[tuple[float, Exception | None]]):
        self.clock = clock
        self.script = list(script)
        self.calls: list[str] = []

    def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> bytes:
        del fields, kwargs
        self.calls.append(key)
        delay, error = self.script.pop(0) if self.script else (0.0, None)
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
    script: list[tuple[float, Exception | None]],
) -> tuple[RedisTransport, FakePublishRedis, FakeClock]:
    clock = FakeClock()
    publisher = FakePublishRedis(clock, script)
    transport = RedisTransport(
        client=UnusedRedis(),  # type: ignore[arg-type]  # structural fake for redis.Redis
        prefix="test",
        ttl=60,
        publish_client=publisher,  # type: ignore[arg-type]  # structural fake
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

    def test_publish_timeout_opens_breaker_and_skips_redis(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, publisher, _ = _make([(PUBLISH_CONNECT_TIMEOUT, TIMED_OUT)])

        for i in range(50):
            transport.publish(_event(f"t-{i}"))

        assert len(publisher.calls) == 1
        records = _warnings(caplog)
        assert len(records) == 1
        assert records[0].getMessage() == (
            "Failed to publish event t-0 to Redis; dropping events for 1.0s "
            "before trying again (1 event(s) dropped since the last report)"
        )

    def test_publish_fast_refusals_open_after_threshold(self) -> None:
        transport, publisher, _ = _make([(0.001, REFUSED)] * 10)

        for i in range(10):
            transport.publish(_event(f"t-{i}"))

        assert len(publisher.calls) == PUBLISH_FAILURES_TO_OPEN

    def test_publish_slow_successes_open_breaker(self) -> None:
        transport, publisher, _ = _make([(0.9, None)] * 10)

        for i in range(10):
            transport.publish(_event(f"t-{i}"))

        assert len(publisher.calls) == PUBLISH_FAILURES_TO_OPEN

    def test_publish_probes_after_cooldown_and_recovers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, publisher, clock = _make([(PUBLISH_SLOW_ATTEMPT, TIMED_OUT)])
        transport.publish(_event("t-0"))
        transport.publish(_event("t-1"))
        assert len(publisher.calls) == 1

        clock.now += RECONNECT_INITIAL_BACKOFF
        transport.publish(_event("t-2"))
        transport.publish(_event("t-3"))

        assert len(publisher.calls) == 3
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

        message = _warnings(caplog)[0].getMessage()
        assert message.startswith("Failed to publish event w1:42:")
        assert message.endswith(
            "to Redis; publishing continues (1 event(s) dropped since the last report)"
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
