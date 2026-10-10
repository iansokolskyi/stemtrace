"""Tests for the publish circuit breaker (no I/O, injectable clock)."""

import logging
import os
import threading

import pytest

from stemtrace.library.transports.breaker import PublishBreaker

LOGGER_NAME = "tests.publish_breaker"


class FakeClock:
    """Monotonic clock the test advances by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_breaker(clock: FakeClock, *, probe_timeout: float = 60.0) -> PublishBreaker:
    return PublishBreaker(
        "TestBroker",
        logger=logging.getLogger(LOGGER_NAME),
        initial_cooldown=1.0,
        max_cooldown=30.0,
        log_interval=60.0,
        probe_timeout=probe_timeout,
        monotonic=clock,
    )


def _error() -> ConnectionRefusedError:
    return ConnectionRefusedError("broker down")


def _take(breaker: PublishBreaker) -> int:
    """Start an attempt that the breaker must allow."""
    ticket = breaker.allow()
    assert ticket is not None
    return ticket


def _attempt_fails(breaker: PublishBreaker, event_id: str = "evt") -> None:
    """Start an attempt and report that it failed, like publish() does."""
    breaker.record_failure(_take(breaker), event_id, _error())


def _attempt_succeeds(breaker: PublishBreaker) -> None:
    breaker.record_success(_take(breaker))


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


class TestPublishBreaker:
    """Open/skip/probe/reset behaviour of PublishBreaker."""

    def test_allow_when_closed_returns_ticket(self) -> None:
        breaker = _make_breaker(FakeClock())

        assert breaker.allow() is not None
        assert breaker.is_open is False
        assert breaker.dropped == 0

    def test_record_failure_opens_and_logs_traceback_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        breaker = _make_breaker(FakeClock())

        _attempt_fails(breaker, "task-1")

        assert breaker.is_open is True
        assert breaker.dropped == 1
        assert breaker.cooldown == 1.0
        records = _records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].exc_info is not None
        assert records[0].getMessage() == (
            "Failed to publish event task-1 to TestBroker; dropping events "
            "for 1.0s before trying again"
        )

    def test_allow_while_open_skips_and_counts_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)

        clock.advance(0.5)
        results = [breaker.allow() for _ in range(100)]

        assert results == [None] * 100
        assert breaker.dropped == 101
        assert len(_records(caplog)) == 1

    def test_allow_after_cooldown_lets_exactly_one_probe_through(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)

        clock.advance(1.0)

        assert breaker.allow() is not None
        # The probe claimed the slot: concurrent callers keep dropping.
        assert breaker.allow() is None
        assert breaker.allow() is None
        assert breaker.dropped == 3

    def test_failed_probe_doubles_cooldown_up_to_cap(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        cooldowns = [breaker.cooldown]

        for _ in range(7):
            clock.advance(breaker.cooldown)
            _attempt_fails(breaker)
            cooldowns.append(breaker.cooldown)

        assert cooldowns == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]

    def test_failed_probe_reopens_for_new_cooldown_from_now(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        clock.advance(1.0)
        _attempt_fails(breaker)

        clock.advance(1.9)
        assert breaker.allow() is None
        clock.advance(0.1)
        assert breaker.allow() is not None

    def test_record_success_after_outage_resets_and_logs_summary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        for _ in range(4):
            breaker.allow()
        clock.advance(1.0)

        _attempt_succeeds(breaker)

        assert breaker.is_open is False
        assert breaker.dropped == 0
        assert breaker.cooldown == 1.0
        assert breaker.allow() is not None
        records = _records(caplog)
        assert len(records) == 2
        # WARNING so it shows at Celery's default worker log level.
        assert records[1].levelno == logging.WARNING
        assert records[1].getMessage() == (
            "Publishing to TestBroker recovered after 1.0s; dropped 5 event(s) "
            "during the outage"
        )

    def test_record_success_when_closed_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        breaker = _make_breaker(FakeClock())

        _attempt_succeeds(breaker)

        assert _records(caplog) == []

    def test_reopen_after_recovery_starts_from_initial_cooldown(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        clock.advance(1.0)
        _attempt_fails(breaker)
        assert breaker.cooldown == 2.0
        clock.advance(2.0)
        _attempt_succeeds(breaker)

        _attempt_fails(breaker)

        assert breaker.cooldown == 1.0

    def test_long_outage_logs_bounded_summaries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)

        # Ten minutes of outage, 100 events per second, every probe failing.
        for _ in range(600 * 100):
            clock.advance(0.01)
            ticket = breaker.allow()
            if ticket is not None:
                breaker.record_failure(ticket, "evt", _error())

        records = _records(caplog)
        # One traceback when it opened, then at most one line per minute.
        assert len(records) <= 1 + 10
        assert len(records) >= 1 + 9
        assert all(r.exc_info is None for r in records[1:])
        assert records[1].levelno == logging.WARNING
        assert "TestBroker still unavailable" in records[1].getMessage()
        assert "next attempt in 30.0s" in records[-1].getMessage()
        assert breaker.dropped == 600 * 100 + 1

    def test_summary_reports_dropped_count_and_last_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        for _ in range(9):
            breaker.allow()

        clock.advance(60.0)
        _attempt_fails(breaker)

        message = _records(caplog)[-1].getMessage()
        assert message == (
            "TestBroker still unavailable after 60.0s; dropped 11 event(s) so far "
            "(last error: ConnectionRefusedError: broker down); next attempt in 2.0s"
        )


class TestPublishBreakerInFlightAttempts:
    """Only the claimed probe changes an open breaker's cooldown."""

    def test_concurrent_failures_open_once_with_initial_cooldown(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        # Eight threads were mid-publish when the broker went away.
        tickets = [_take(breaker) for _ in range(8)]

        for i, ticket in enumerate(tickets):
            breaker.record_failure(ticket, f"t-{i}", _error())

        assert breaker.cooldown == 1.0
        assert breaker.dropped == 8
        records = _records(caplog)
        assert len(records) == 1
        assert "dropping events for 1.0s" in records[0].getMessage()
        clock.advance(0.9)
        assert breaker.allow() is None
        clock.advance(0.1)
        assert breaker.allow() is not None

    def test_concurrent_failures_from_threads_do_not_escalate(self) -> None:
        breaker = _make_breaker(FakeClock())
        tickets = [_take(breaker) for _ in range(16)]
        start = threading.Barrier(16)

        def worker(ticket: int) -> None:
            start.wait()
            breaker.record_failure(ticket, "evt", _error())

        threads = [threading.Thread(target=worker, args=(t,)) for t in tickets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        assert breaker.cooldown == 1.0
        assert breaker.dropped == 16

    def test_probe_slower_than_cooldown_admits_no_second_probe(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        clock.advance(1.0)
        probe = _take(breaker)

        # The probe's connect takes 2s, twice the 1s cooldown.
        clock.advance(2.0)
        assert breaker.allow() is None
        breaker.record_failure(probe, "probe", _error())

        assert breaker.cooldown == 2.0
        clock.advance(1.9)
        assert breaker.allow() is None
        clock.advance(0.1)
        assert breaker.allow() is not None

    def test_in_flight_failure_while_probe_outstanding_counts_only_as_drop(
        self,
    ) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        stale = _take(breaker)
        _attempt_fails(breaker)
        clock.advance(1.0)
        probe = _take(breaker)

        breaker.record_failure(stale, "stale", _error())

        assert breaker.cooldown == 1.0
        assert breaker.dropped == 2
        breaker.record_success(probe)
        assert breaker.is_open is False

    def test_in_flight_success_while_open_keeps_breaker_open(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        stale = _take(breaker)
        _attempt_fails(breaker)

        breaker.record_success(stale)

        assert breaker.is_open is True
        assert breaker.allow() is None

    def test_stale_failure_after_recovery_is_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        stale = _take(breaker)
        _attempt_fails(breaker)
        clock.advance(1.0)
        _attempt_succeeds(breaker)

        breaker.record_failure(stale, "stale", _error())

        assert breaker.is_open is False
        assert breaker.dropped == 0
        assert len(_records(caplog)) == 2

    def test_probe_that_never_reports_is_replaced_after_probe_timeout(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock, probe_timeout=10.0)
        _attempt_fails(breaker)
        clock.advance(1.0)
        lost = _take(breaker)

        clock.advance(9.9)
        assert breaker.allow() is None
        clock.advance(0.1)
        replacement = _take(breaker)

        # The abandoned probe reporting late changes nothing.
        breaker.record_failure(lost, "lost", _error())
        assert breaker.cooldown == 1.0
        breaker.record_success(replacement)
        assert breaker.is_open is False

    def test_concurrent_allow_after_cooldown_admits_single_probe(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        clock.advance(1.0)
        start = threading.Barrier(16)
        results: list[int | None] = []
        results_lock = threading.Lock()

        def worker() -> None:
            start.wait()
            for _ in range(50):
                ticket = breaker.allow()
                with results_lock:
                    results.append(ticket)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        assert len(results) == 16 * 50
        assert sum(ticket is not None for ticket in results) == 1
        assert breaker.dropped == 1 + 16 * 50 - 1


class TestPublishBreakerFork:
    """A forked child starts with a closed breaker and a fresh lock."""

    def test_allow_in_forked_child_resets_inherited_open_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        assert breaker.allow() is None
        parent_pid = os.getpid()

        # Simulate running in a prefork child: same object, different pid.
        monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)

        assert breaker.allow() is not None
        assert breaker.is_open is False
        assert breaker.dropped == 0

    def test_allow_in_forked_child_replaces_lock_held_at_fork(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        breaker = _make_breaker(FakeClock())
        _attempt_fails(breaker)
        parent_pid = os.getpid()
        # A thread in the parent held the lock when the process forked.
        breaker._lock.acquire()

        monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)
        result: list[int | None] = []
        worker = threading.Thread(target=lambda: result.append(breaker.allow()))
        worker.start()
        worker.join(timeout=2.0)

        assert worker.is_alive() is False
        assert len(result) == 1
        assert result[0] is not None
