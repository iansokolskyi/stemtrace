"""Tests for RabbitMQTransport.publish(): reuse, failing fast, failover, breaker.

``tests/fake_kombu.py`` stands in for kombu and py-amqp (see its docstring for
the real behavior it models and where that was read).
"""

import logging
import threading
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from kombu import Connection as KombuConnection

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.transports.rabbitmq import (
    CONNECT_TIMEOUT,
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_IO_TIMEOUT,
    RabbitMQTransport,
)
from stemtrace.transports.rabbitmq_publisher import connection_name
from tests import fake_kombu
from tests.fake_kombu import FakeBroker, FakeClock

LOGGER_NAME = "stemtrace.transports.rabbitmq"
H1 = "amqp://guest:guest@h1:5672//"
H2 = "amqp://guest:guest@h2:5672//"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> Iterator[FakeBroker]:
    fake = FakeBroker(clock)
    fake_kombu.install(monkeypatch, fake)
    yield fake
    fake.release_sockets()


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
        # The heartbeat is forced off: nothing sends heartbeats between publishes.
        assert conn.kwargs == {
            "connect_timeout": CONNECT_TIMEOUT,
            "transport_options": {
                "heartbeat": 0,
                "client_properties": {"connection_name": connection_name()},
            },
        }
        assert len(conn.ensure_calls) == 1
        call = conn.ensure_calls[0]
        assert call["max_retries"] == 0
        assert call["interval_start"] == 0
        assert call["interval_step"] == 0
        assert call["interval_max"] == 0
        assert call["reraise_as_library_errors"] is False
        # The connection stays open for the next publish.
        assert conn.connection.calls == []
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
        assert warnings[0].getMessage() == (
            "Failed to publish event t-down to RabbitMQ; publishing continues "
            "(1 event(s) dropped since the last report)"
        )
        assert warnings[0].exc_info is not None


class TestRabbitMQPublishConnectionReuse:
    """One connection per process serves every publish."""

    def test_publish_many_events_reuses_one_connection_and_declares_once(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)

        for i in range(50):
            transport.publish(_event(f"t-{i}"))

        assert len(broker.connections) == 1
        conn = broker.connections[0]
        assert len(conn.ensure_calls) == 1
        assert conn.connection.calls == []
        assert conn.declare_calls == 1
        assert [body["task_id"] for _, body, _ in broker.published] == [
            f"t-{i}" for i in range(50)
        ]

    def test_publish_failure_after_connect_drops_socket_without_close_wait(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.publish_error = TimeoutError("timed out")
        transport = _transport(H1, clock)

        transport.publish(_event())

        # No close handshake that would wait out another timeout.
        assert broker.connections[0].connection.calls == ["collect"]

    def test_publish_after_failure_connects_again_on_next_attempt(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        broker.publish_error = TimeoutError("timed out")
        transport = _transport(H1, clock)
        transport.publish(_event("t-0"))
        broker.publish_error = None

        transport.publish(_event("t-1"))
        transport.publish(_event("t-2"))

        assert len(broker.connections) == 2
        assert broker.connections[0].connection.calls == ["collect"]
        assert broker.connections[1].connection.calls == []
        assert [body["task_id"] for _, body, _ in broker.published] == ["t-1", "t-2"]
        assert len(_records(caplog, logging.WARNING)) == 1

    def test_publish_dropped_event_logs_debug_line(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        broker.down = {H1}
        transport = _transport(H1, clock)
        for i in range(3):
            transport.publish(_event(f"t-{i}"))

        transport.publish(_event("t-dropped"))

        debug = [r.getMessage() for r in _records(caplog, logging.DEBUG)]
        assert debug == [
            "RabbitMQ publishing paused after failures; dropped event t-dropped"
        ]


class TestRabbitMQTransportClose:
    def test_close_closes_publisher_connection_and_next_publish_reconnects(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))

        transport.close()
        transport.publish(_event("t-2"))

        assert broker.connections[0].connection.calls == ["close", "collect"]
        assert len(broker.connections) == 2
        assert [body["task_id"] for _, body, _ in broker.published] == ["t-1", "t-2"]


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

    def test_publish_after_failover_keeps_publishing_on_the_working_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)
        transport.publish(_event("t-1"))

        transport.publish(_event("t-2"))

        assert len(broker.connections) == 1
        assert [host for host, _, _ in broker.published] == [H2, H2]

    def test_publish_reconnect_after_failover_starts_with_last_working_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)
        transport.publish(_event("t-1"))
        broker.close_idle_connections()

        transport.publish(_event("t-2"))

        second = broker.connections[1]
        # Same URL; kombu's own failover switch moves to the second host.
        assert second.url == f"{H1};{H2}"
        assert second.switches == 1
        assert second.attempted == [H2]
        assert [host for host, _, _ in broker.published] == [H2, H2]

    def test_publish_preferred_host_down_falls_back_to_other_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)
        transport.publish(_event("t-1"))
        broker.down = {H2}
        broker.close_idle_connections()

        transport.publish(_event("t-2"))
        broker.close_idle_connections()
        transport.publish(_event("t-3"))

        assert broker.connections[1].attempted == [H2, H1]
        assert broker.connections[2].switches == 0
        assert broker.connections[2].attempted == [H1]
        assert [host for host, _, _ in broker.published] == [H2, H1, H1]


class TestRabbitMQPublishFailoverResolution:
    """Starting from a later host keeps the parameters kombu resolves for it."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "amqp://app:secret@rabbit1:5673/prod;rabbit2",
                [
                    ("rabbit1", 5673, "app", "secret", "prod"),
                    ("rabbit2", 5673, "app", "secret", "prod"),
                ],
            ),
            (
                "amqp://app:secret@rabbit1:5673/prod;amqp://u:p@h2:5674",
                [
                    ("rabbit1", 5673, "app", "secret", "prod"),
                    ("h2", 5674, "u", "p", "prod"),
                ],
            ),
            (
                # A URL-form entry doesn't inherit credentials (pyamqp then
                # uses guest/guest); port and vhost come from the previous
                # entry when omitted, and "//" means vhost "/".
                "amqp://app:secret@rabbit1:5673/prod;amqp://h2;amqp://h3:5674//",
                [
                    ("rabbit1", 5673, "app", "secret", "prod"),
                    ("h2", 5673, None, None, "prod"),
                    ("h3", 5674, None, None, "/"),
                ],
            ),
            (
                "amqp://app:secret@rabbit1:5673/prod;rabbit2;amqp://u:p@h3/other",
                [
                    ("rabbit1", 5673, "app", "secret", "prod"),
                    ("rabbit2", 5673, "app", "secret", "prod"),
                    ("h3", 5673, "u", "p", "other"),
                ],
            ),
        ],
    )
    def test_publish_connection_resolves_each_entry_like_kombu_failover(
        self, url: str, expected: list[tuple[str, int, str | None, str | None, str]]
    ) -> None:
        publisher = RabbitMQTransport(url, prefix="test", ttl=60)._publisher

        resolved = []
        for first in range(len(expected)):
            conn = publisher._connection(KombuConnection, first)
            resolved.append(
                (
                    conn.hostname,
                    conn.port,
                    conn.userid,
                    conn.password,
                    conn.virtual_host,
                )
            )
            assert conn.connect_timeout == CONNECT_TIMEOUT
            assert conn.transport_options["heartbeat"] == 0

        assert resolved == expected

    def test_publish_auth_failure_on_later_host_returns_to_url_order(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)
        transport.publish(_event("t-1"))
        assert broker.connections[0].attempted == [H1, H2]
        # h1 comes back; h2 now refuses the login, which kombu doesn't retry
        # on another host.
        broker.down = set()
        broker.auth_refused = {H2}
        broker.close_idle_connections()

        transport.publish(_event("t-2"))
        transport.publish(_event("t-3"))

        assert broker.connections[1].attempted == [H2]
        assert broker.connections[2].switches == 0
        assert broker.connections[2].attempted == [H1]
        assert [(host, body["task_id"]) for host, body, _ in broker.published] == [
            (H2, "t-1"),
            (H1, "t-3"),
        ]


class TestRabbitMQPublishBreaker:
    """During an outage publish() skips connecting and logs a bounded amount."""

    def test_publish_fast_failures_open_breaker_on_third_in_a_row(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(H1, clock)
        for i in range(3):
            transport.publish(_event(f"t-{i}"))

        for i in range(50):
            transport.publish(_event(f"t-{i + 3}"))

        assert len(broker.connections) == 3

    def test_publish_timeouts_open_breaker_after_three(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The publish step timed out after PUBLISH_IO_TIMEOUT, every time.
        broker.publish_error = TimeoutError("timed out")
        broker.publish_delay = PUBLISH_IO_TIMEOUT
        transport = _transport(H1, clock)

        transport.publish(_event("t-slow"))
        assert transport._publish_breaker.is_open is False
        for i in range(50):
            transport.publish(_event(f"t-{i}"))

        assert len(broker.connections) == PUBLISH_FAILURES_TO_OPEN
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 2
        assert "dropping events for 1.0s" in warnings[1].getMessage()

    def test_publish_probes_after_cooldown_and_recovers(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        broker.down = {H1}
        transport = _transport(H1, clock)
        for i in range(3):
            transport.publish(_event(f"t-{i}"))

        clock.advance(1.0)
        transport.publish(_event("t-probe-1"))
        assert len(broker.connections) == 4

        clock.advance(1.0)
        transport.publish(_event("t-skipped"))
        assert len(broker.connections) == 4

        broker.down = set()
        clock.advance(1.0)
        transport.publish(_event("t-probe-2"))
        transport.publish(_event("t-after"))

        # The probe's connection is kept for the event after it.
        assert len(broker.connections) == 5
        assert [body["task_id"] for _, body, _ in broker.published] == [
            "t-probe-2",
            "t-after",
        ]
        # Warning level, so it shows at Celery's default worker log level.
        warnings = _records(caplog, logging.WARNING)
        assert [w.getMessage().split(";")[0] for w in warnings[:2]] == [
            "Failed to publish event t-0 to RabbitMQ",
            "Failed to publish event t-2 to RabbitMQ",
        ]
        assert "publishing continues" in warnings[0].getMessage()
        assert "dropping events for 1.0s" in warnings[1].getMessage()
        assert warnings[2].getMessage() == (
            "Publishing to RabbitMQ recovered after 3.0s "
            "(2 event(s) dropped since the last report)"
        )
        assert len(warnings) == 3

    def test_publish_failover_failure_does_not_open_breaker_for_other_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.down = {H1}
        transport = _transport(f"{H1};{H2}", clock)

        for i in range(5):
            transport.publish(_event(f"t-{i}"))

        assert len(broker.connections) == 1
        assert len(broker.published) == 5

    def test_publish_sporadic_fast_error_drops_only_that_event(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        broker.publish_error = RuntimeError("channel error")
        transport = _transport(H1, clock)
        transport.publish(_event("t-0"))
        broker.publish_error = None

        transport.publish(_event("t-1"))

        assert len(broker.connections) == 2
        assert broker.connections[0].connection.calls == ["collect"]
        assert [body["task_id"] for _, body, _ in broker.published] == ["t-1"]
        assert len(_records(caplog, logging.WARNING)) == 1

    def test_publish_failure_after_connect_rotates_to_next_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        # h1 accepts connections but its publishes fail (a node-local fault).
        broker.publish_error = RuntimeError("channel error")
        broker.publish_error_hosts = {H1}
        transport = _transport(f"{H1};{H2}", clock)

        transport.publish(_event("t-0"))
        transport.publish(_event("t-1"))

        assert broker.connections[1].attempted == [H2]
        assert [(host, body["task_id"]) for host, body, _ in broker.published] == [
            (H2, "t-1")
        ]

    def test_publish_concurrent_failures_open_breaker_once_for_initial_cooldown(
        self,
        broker: FakeBroker,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Eight task threads are in flight (past the breaker) when the broker
        # stops answering. They share one connection, so they publish one at
        # a time: the third failure opens the breaker, and the five still
        # waiting skip their attempt instead of each waiting out a timeout.
        broker.publish_error = TimeoutError("timed out")
        broker.publish_delay = PUBLISH_IO_TIMEOUT
        transport = _transport(H1, clock)
        in_flight = threading.Barrier(8)
        allow = transport._publish_breaker.allow

        def allow_then_wait_for_the_others() -> int | None:
            ticket = allow()
            in_flight.wait(timeout=5.0)
            return ticket

        monkeypatch.setattr(
            transport._publish_breaker, "allow", allow_then_wait_for_the_others
        )
        threads = [
            threading.Thread(target=transport.publish, args=(_event(f"t-{i}"),))
            for i in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        broker.publish_error = None
        broker.publish_delay = 0.0

        assert [thread.is_alive() for thread in threads] == [False] * 8
        assert len(broker.connections) == PUBLISH_FAILURES_TO_OPEN
        warnings = _records(caplog, logging.WARNING)
        # The first failure's line, then the third opens the breaker; the
        # other five were in flight and don't change the state.
        assert len(warnings) == 2
        assert "dropping events for 1.0s" in warnings[1].getMessage()
        # The cooldown wasn't escalated: one second later a probe goes out
        # and, with the broker healthy again, publishing resumes.
        monkeypatch.setattr(transport._publish_breaker, "allow", allow)
        clock.advance(1.0)
        transport.publish(_event("t-probe"))
        assert len(broker.connections) == PUBLISH_FAILURES_TO_OPEN + 1
        assert [body["task_id"] for _, body, _ in broker.published] == ["t-probe"]

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
        # Three fast failures, then one connection per probe: after 1, 2, 4,
        # 8, 16, then every 30s.
        assert len(broker.connections) <= 3 + 5 + 300 // 30


def test_event_identifier_for_worker_event_uses_event_type_value() -> None:
    event = WorkerEvent(
        event_type=WorkerEventType.WORKER_READY,
        hostname="w1",
        pid=42,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    assert RabbitMQTransport._event_identifier(event) == "w1:42:worker_ready"
