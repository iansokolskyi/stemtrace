"""Tests for RabbitMQTransport.publish() failing fast during broker outages.

A fake kombu module stands in for the broker. Its Connection models the parts
of kombu's connect path the transport relies on: a ``;``-separated URL becomes
``alt`` (one entry per host), and ``ensure_connection`` walks the hosts
round-robin like kombu's ``retry_over_time`` does (raising once ``retries``
reaches ``max_retries`` *before* calling the errback, sleeping for the
interval only after a full round of hosts has failed).
"""

import logging
import socket
import sys
import types
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any, ClassVar

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.transports.rabbitmq import (
    CONNECT_TIMEOUT,
    PUBLISH_IO_TIMEOUT,
    RabbitMQTransport,
)

LOGGER_NAME = "stemtrace.library.transports.rabbitmq"
H1 = "amqp://guest:guest@h1:5672//"
H2 = "amqp://guest:guest@h2:5672//"


class FakeClock:
    """Monotonic clock the test advances by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBroker:
    """State shared by the fake kombu module: which hosts are down, what arrived."""

    def __init__(self) -> None:
        self.down: set[str] = set()
        self.publish_error: Exception | None = None
        self.connections: list[Any] = []
        self.published: list[tuple[str, Any, dict[str, Any]]] = []


def _install_fake_kombu_for_publish(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker
) -> None:
    """Install a fake kombu publish stack into sys.modules (no broker required)."""
    fake_kombu = types.ModuleType("kombu")

    class FakeAmqpTransport:
        def __init__(self) -> None:
            self.sock, self.peer = socket.socketpair()

    class FakeAmqpConnection:
        def __init__(self) -> None:
            self.transport = FakeAmqpTransport()

    class FakeChannel:
        def __init__(self, connection: "Connection") -> None:
            self.connection = connection

    class Connection:
        retry_limit: ClassVar[int] = 50

        def __init__(self, url: str, **kwargs: Any) -> None:
            self.url = url
            self.kwargs = kwargs
            self.alt = url.split(";") if ";" in url else []
            self.ensure_calls: list[dict[str, Any]] = []
            self.attempted: list[str] = []
            self.sleeps: list[float] = []
            self.host: str | None = None
            self.closed = False
            self.sock_timeout_at_channel: float | None = None
            self.connection = FakeAmqpConnection()
            broker.connections.append(self)

        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            del exc_type, exc, tb
            self.closed = True
            self.connection.transport.sock.close()
            self.connection.transport.peer.close()

        def ensure_connection(
            self,
            *,
            errback: Callable[[Exception, float], None] | None = None,
            max_retries: int | None = None,
            interval_start: float = 2,
            interval_step: float = 2,
            interval_max: float = 30,
            reraise_as_library_errors: bool = True,
        ) -> "Connection":
            self.ensure_calls.append(
                {
                    "errback": errback,
                    "max_retries": max_retries,
                    "interval_start": interval_start,
                    "interval_step": interval_step,
                    "interval_max": interval_max,
                    "reraise_as_library_errors": reraise_as_library_errors,
                }
            )
            del interval_step, interval_max
            hosts = self.alt or [self.url]
            retries = 0
            while True:
                host = hosts[retries % len(hosts)]
                self.attempted.append(host)
                if host not in broker.down:
                    self.host = host
                    return self
                exc = ConnectionRefusedError(f"{host} refused")
                if max_retries is not None and retries >= max_retries:
                    raise exc
                if retries >= self.retry_limit:
                    raise RuntimeError("runaway connect loop")
                completes_round = (retries + 1) % len(hosts) == 0
                interval = interval_start if completes_round else 0
                if errback is not None:
                    errback(exc, interval)
                if interval:
                    self.sleeps.append(interval)
                retries += 1

        def channel(self) -> FakeChannel:
            if self.host is None:
                raise AssertionError("channel() before ensure_connection()")
            self.sock_timeout_at_channel = self.connection.transport.sock.gettimeout()
            return FakeChannel(self)

    class Exchange:
        def __init__(self, name: str, *, type: str, durable: bool) -> None:
            self.name = name
            self.type = type
            self.durable = durable

    class Producer:
        def __init__(self, channel: FakeChannel) -> None:
            self.channel = channel

        def publish(self, body: Any, **kwargs: Any) -> None:
            if broker.publish_error is not None:
                raise broker.publish_error
            host = self.channel.connection.host
            broker.published.append((host, body, kwargs))

    fake_kombu.Connection = Connection
    fake_kombu.Exchange = Exchange
    fake_kombu.Producer = Producer
    monkeypatch.setitem(sys.modules, "kombu", fake_kombu)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBroker]:
    fake = FakeBroker()
    _install_fake_kombu_for_publish(monkeypatch, fake)
    yield fake


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _event(task_id: str = "t-1") -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.publish",
        state=TaskState.STARTED,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _transport(url: str, clock: FakeClock) -> RabbitMQTransport:
    return RabbitMQTransport(url, prefix="test", ttl=60, monotonic=clock)


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno == level]


class TestRabbitMQPublishFailFast:
    """publish() connects once per host, without kombu's retry sleeps."""

    def test_publish_single_host_connects_once_without_retry_sleep(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)

        transport.publish(_event("t-1"))

        assert len(broker.connections) == 1
        conn = broker.connections[0]
        assert conn.url == H1
        assert conn.kwargs == {"connect_timeout": CONNECT_TIMEOUT}
        assert len(conn.ensure_calls) == 1
        call = conn.ensure_calls[0]
        assert call["max_retries"] == 0
        assert call["interval_start"] == 0
        assert call["interval_step"] == 0
        assert call["interval_max"] == 0
        assert call["reraise_as_library_errors"] is False
        assert conn.closed is True
        assert len(broker.published) == 1
        host, body, kwargs = broker.published[0]
        assert host == H1
        assert body["task_id"] == "t-1"
        assert kwargs["retry"] is False
        assert kwargs["serializer"] == "json"
        assert kwargs["delivery_mode"] == 2
        assert kwargs["routing_key"] == ""
        assert kwargs["exchange"].name == "test.events"
        assert kwargs["declare"] == [kwargs["exchange"]]

    def test_publish_bounds_socket_io_before_opening_channel(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)

        transport.publish(_event())

        assert broker.connections[0].sock_timeout_at_channel == PUBLISH_IO_TIMEOUT

    def test_publish_single_host_down_fails_after_one_attempt(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        broker.down = {H1}
        transport = _transport(H1, clock)

        transport.publish(_event("t-down"))

        conn = broker.connections[0]
        assert conn.attempted == [H1]
        assert conn.sleeps == []
        assert broker.published == []
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert (
            warnings[0]
            .getMessage()
            .startswith(
                "Failed to publish event t-down to RabbitMQ; dropping events for 1.0s"
            )
        )
        assert warnings[0].exc_info is not None


class TestRabbitMQPublishFailover:
    """Multi-host (failover) URLs: every host is tried once per attempt."""

    def test_publish_two_hosts_first_down_publishes_via_second_without_sleep(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)

        transport.publish(_event("t-failover"))

        conn = broker.connections[0]
        assert conn.ensure_calls[0]["max_retries"] == 1
        assert conn.attempted == [H1, H2]
        assert conn.sleeps == []
        assert [(host, body["task_id"]) for host, body, _ in broker.published] == [
            (H2, "t-failover")
        ]
        # A failure of h1 inside a successful attempt never opens the breaker.
        assert _records(caplog, logging.WARNING) == []
        transport.publish(_event("t-next"))
        assert len(broker.published) == 2

    def test_publish_two_hosts_both_down_tries_each_once(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        broker.down = {H1, H2}
        transport = _transport(f"{H1};{H2}", clock)

        transport.publish(_event())

        conn = broker.connections[0]
        assert conn.attempted == [H1, H2]
        assert conn.sleeps == []
        assert broker.published == []
        assert len(_records(caplog, logging.WARNING)) == 1

    def test_publish_after_failover_starts_with_last_working_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)
        transport.publish(_event("t-1"))

        transport.publish(_event("t-2"))

        second = broker.connections[1]
        assert second.url == f"{H2};{H1}"
        assert second.attempted == [H2]
        assert [host for host, _, _ in broker.published] == [H2, H2]

    def test_publish_preferred_host_down_falls_back_to_other_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)
        transport.publish(_event("t-1"))
        broker.down = {H2}

        transport.publish(_event("t-2"))
        transport.publish(_event("t-3"))

        assert broker.connections[1].attempted == [H2, H1]
        assert broker.connections[2].url == f"{H1};{H2}"
        assert broker.connections[2].attempted == [H1]
        assert [host for host, _, _ in broker.published] == [H2, H1, H1]


class TestRabbitMQPublishBreaker:
    """During an outage publish() skips connecting and logs a bounded amount."""

    def test_publish_while_breaker_open_skips_connecting(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(H1, clock)
        transport.publish(_event("t-0"))

        for i in range(50):
            transport.publish(_event(f"t-{i + 1}"))

        assert len(broker.connections) == 1

    def test_publish_probes_after_cooldown_and_recovers(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        broker.down = {H1}
        transport = _transport(H1, clock)
        transport.publish(_event("t-0"))

        clock.advance(1.0)
        transport.publish(_event("t-probe-1"))
        assert len(broker.connections) == 2

        clock.advance(1.0)
        transport.publish(_event("t-skipped"))
        assert len(broker.connections) == 2

        broker.down = set()
        clock.advance(1.0)
        transport.publish(_event("t-probe-2"))
        transport.publish(_event("t-after"))

        assert len(broker.connections) == 4
        assert [body["task_id"] for _, body, _ in broker.published] == [
            "t-probe-2",
            "t-after",
        ]
        infos = _records(caplog, logging.INFO)
        assert [r.getMessage() for r in infos] == [
            "Publishing to RabbitMQ recovered after 3.0s; dropped 3 event(s) "
            "during the outage"
        ]

    def test_publish_failover_failure_does_not_open_breaker_for_other_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)

        for i in range(5):
            transport.publish(_event(f"t-{i}"))

        assert len(broker.connections) == 5
        assert len(broker.published) == 5

    def test_publish_unexpected_producer_error_is_swallowed_and_opens_breaker(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        broker.publish_error = RuntimeError("channel error")
        transport = _transport(H1, clock)

        transport.publish(_event("t-0"))
        transport.publish(_event("t-1"))

        assert len(broker.connections) == 1
        assert broker.connections[0].closed is True
        assert len(_records(caplog, logging.WARNING)) == 1

    def test_publish_long_outage_logs_bounded_lines(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        broker.down = {H1}
        transport = _transport(H1, clock)

        # Five minutes at 20 events per second.
        for i in range(300 * 20):
            transport.publish(_event(f"t-{i}"))
            clock.advance(0.05)

        warnings = _records(caplog, logging.WARNING)
        assert 1 + 3 <= len(warnings) <= 1 + 5
        # One connection per probe: 1, 2, 4, 8, 16, then every 30s.
        assert len(broker.connections) <= 6 + 300 // 30
