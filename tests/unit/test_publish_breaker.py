"""Tests for the publish circuit breaker (no I/O, injectable clock)."""

import logging
import os
import random
import re
import threading
from collections.abc import Callable

import pytest

from stemtrace.transports.breaker import PublishBreaker

LOGGER_NAME = "tests.publish_breaker"
SLOW = 2.0  # elapsed seconds of a failed attempt that timed out
FAST = 0.001  # elapsed seconds of e.g. a refused connection


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
        slow_attempt=0.5,
        failures_to_open=3,
        monotonic=clock,
    )


def _error() -> ConnectionRefusedError:
    return ConnectionRefusedError("broker down")


def _take(breaker: PublishBreaker) -> int:
    """Start an attempt that the breaker must allow."""
    ticket = breaker.allow()
    assert ticket is not None
    return ticket


def _attempt_fails(
    breaker: PublishBreaker,
    event_id: str = "evt",
    *,
    elapsed: float = SLOW,
    unconfirmed: bool = False,
) -> None:
    """Start an attempt and report that it failed, like publish() does."""
    breaker.record_failure(
        _take(breaker), event_id, _error(), elapsed=elapsed, unconfirmed=unconfirmed
    )


def _attempt_succeeds(breaker: PublishBreaker, *, elapsed: float = FAST) -> None:
    """Start an attempt and report that it succeeded after ``elapsed`` seconds."""
    breaker.record_success(_take(breaker), elapsed=elapsed)


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def _simulate(
    breaker: PublishBreaker,
    clock: FakeClock,
    *,
    seconds: float,
    rate: float,
    fails: Callable[[int], bool],
    elapsed: float = FAST,
) -> int:
    """Publish ``rate`` events per second; return how many were delivered."""
    delivered = 0
    for i in range(int(seconds * rate)):
        clock.advance(1 / rate)
        ticket = breaker.allow()
        if ticket is None:
            continue
        if fails(i):
            breaker.record_failure(ticket, f"e{i}", _error(), elapsed=elapsed)
        else:
            breaker.record_success(ticket, elapsed=FAST)
            delivered += 1
    return delivered


class TestPublishBreakerOpening:
    """When a failure opens the breaker, and what it logs."""

    def test_allow_when_closed_returns_ticket(self) -> None:
        breaker = _make_breaker(FakeClock())

        assert breaker.allow() is not None
        assert breaker.is_open is False
        assert breaker.dropped == 0

    def test_slow_failure_opens_and_logs_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        breaker = _make_breaker(FakeClock())

        _attempt_fails(breaker, "task-1", elapsed=0.5)

        assert breaker.is_open is True
        assert breaker.cooldown == 1.0
        records = _records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].exc_info is not None
        assert records[0].getMessage() == (
            "Failed to publish event task-1 to TestBroker; dropping events for "
            "1.0s before trying again (1 event(s) dropped since the last report)"
        )

    def test_single_fast_failure_drops_only_that_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        breaker = _make_breaker(FakeClock())

        _attempt_fails(breaker, "task-1", elapsed=FAST)

        assert breaker.is_open is False
        assert breaker.allow() is not None
        records = _records(caplog)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].getMessage() == (
            "Failed to publish event task-1 to TestBroker; publishing continues "
            "(1 event(s) dropped since the last report)"
        )

    def test_consecutive_fast_failures_open_on_third(self) -> None:
        breaker = _make_breaker(FakeClock())

        _attempt_fails(breaker, elapsed=FAST)
        _attempt_fails(breaker, elapsed=FAST)
        assert breaker.is_open is False
        _attempt_fails(breaker, elapsed=FAST)

        assert breaker.is_open is True
        assert breaker.cooldown == 1.0

    def test_success_resets_consecutive_fast_failures(self) -> None:
        breaker = _make_breaker(FakeClock())

        for _ in range(10):
            _attempt_fails(breaker, elapsed=FAST)
            _attempt_fails(breaker, elapsed=FAST)
            _attempt_succeeds(breaker)

        assert breaker.is_open is False

    def test_two_unconfirmed_failures_in_a_row_set_max_cooldown(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)

        _attempt_fails(breaker, unconfirmed=True)
        assert breaker.is_open is True
        assert breaker.cooldown == 1.0
        clock.advance(1.0)
        _attempt_fails(breaker, unconfirmed=True)

        assert breaker.cooldown == 30.0
        clock.advance(29.9)
        assert breaker.allow() is None
        clock.advance(0.1)
        _attempt_fails(breaker, unconfirmed=True)
        assert breaker.cooldown == 30.0
        # The opening line reported the first one.
        assert breaker.unconfirmed == 2

    def test_single_unconfirmed_failure_then_stall_recovers_quickly(self) -> None:
        # A brief broker pause: an in-flight close times out (unconfirmed),
        # the probe's connect times out, the next probe succeeds.
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker, unconfirmed=True)
        clock.advance(1.0)
        _attempt_fails(breaker)
        assert breaker.cooldown == 2.0
        clock.advance(2.0)

        _attempt_succeeds(breaker)

        assert breaker.is_open is False

    def test_unconfirmed_probe_after_plain_failure_only_doubles(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        clock.advance(1.0)

        _attempt_fails(breaker, unconfirmed=True)

        assert breaker.cooldown == 2.0


class TestPublishBreakerOpen:
    """Open/skip/probe/reset behaviour once the breaker is open."""

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
        assert breaker.dropped == 100
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

    def test_failed_probe_doubles_cooldown_up_to_cap(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        cooldowns = [breaker.cooldown]

        for _ in range(7):
            clock.advance(breaker.cooldown)
            # Even a fast probe failure counts: the breaker is already open.
            _attempt_fails(breaker, elapsed=FAST)
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

    def test_record_success_after_outage_resets_and_logs_recovery(
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
            "Publishing to TestBroker recovered after 1.0s "
            "(4 event(s) dropped since the last report)"
        )

    def test_unconfirmed_events_are_reported_separately(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker, "t-blocked", unconfirmed=True)
        for _ in range(3):
            breaker.allow()
        clock.advance(30.0)
        _attempt_fails(breaker, unconfirmed=True)
        clock.advance(30.0)

        _attempt_succeeds(breaker)

        messages = [r.getMessage() for r in _records(caplog)]
        assert messages[0] == (
            "Failed to publish event t-blocked to TestBroker; dropping events for "
            "1.0s before trying again (0 event(s) dropped, 1 sent but "
            "unconfirmed since the last report)"
        )
        assert messages[-1] == (
            "Publishing to TestBroker recovered after 60.0s "
            "(3 event(s) dropped, 1 sent but unconfirmed since the last report)"
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

    def test_summary_reports_losses_since_last_report_and_last_error(
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

        record = _records(caplog)[-1]
        assert record.getMessage() == (
            "TestBroker still unavailable after 60.0s (10 event(s) dropped since "
            "the last report; last error: ConnectionRefusedError: broker down); "
            "next attempt in 2.0s"
        )
        assert record.exc_info is None


class TestPublishBreakerFlakyBroker:
    """Sporadic failures on a healthy broker cost little and log little."""

    @pytest.mark.parametrize(
        ("failure_rate", "min_delivered"), [(0.02, 0.97), (0.10, 0.87)]
    )
    def test_random_fast_failures_deliver_close_to_failure_free_rate(
        self, failure_rate: float, min_delivered: float
    ) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        rng = random.Random(1234)
        seconds, rate = 600.0, 20.0

        delivered = _simulate(
            breaker,
            clock,
            seconds=seconds,
            rate=rate,
            fails=lambda _i: rng.random() < failure_rate,
        )

        assert delivered / (seconds * rate) >= min_delivered

    def test_alternating_fast_failures_log_at_most_once_per_interval(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)

        delivered = _simulate(
            breaker, clock, seconds=120.0, rate=20.0, fails=lambda i: i % 2 == 1
        )

        assert delivered == 1200
        records = _records(caplog)
        assert 2 <= len(records) <= 3
        assert records[0].exc_info is not None
        # The next line (a failure line or a loss report, whichever comes due
        # first) reports everything since the first one: about 60s at 10
        # failures per second.
        match = re.search(r"\((\d+) event\(s\) dropped", records[1].getMessage())
        assert match is not None
        assert 599 <= int(match.group(1)) <= 601

    def test_alternating_slow_failures_log_bounded_lines(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)

        _simulate(
            breaker,
            clock,
            seconds=120.0,
            rate=20.0,
            fails=lambda i: i % 2 == 1,
            elapsed=SLOW,
        )

        records = _records(caplog)
        # Failure lines at most once a minute, each followed by at most one
        # recovery line.
        assert len(records) <= 2 * 3
        assert sum(r.exc_info is not None for r in records) <= 3

    def test_long_outage_logs_bounded_summaries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)

        # Ten minutes of outage, 100 events per second, every probe failing.
        _simulate(breaker, clock, seconds=600.0, rate=100.0, fails=lambda _i: True)

        records = _records(caplog)
        # One traceback when it opened, then at most one line per minute.
        assert 1 + 9 <= len(records) <= 1 + 10
        assert all(r.exc_info is None for r in records[1:])
        assert "TestBroker still unavailable" in records[1].getMessage()
        assert "next attempt in 30.0s" in records[-1].getMessage()


class TestPublishBreakerLossReporting:
    """Every loss is reported within one interval, however the broker flaps."""

    def test_second_brief_outage_is_reported_within_one_interval(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        clock.advance(1.0)
        _attempt_succeeds(breaker)
        assert len(_records(caplog)) == 2
        # A second outage 10s later: its opening line is rate-limited, and
        # so is the line when it recovers.
        clock.advance(9.0)
        _attempt_fails(breaker, "second")
        for _ in range(5):
            breaker.allow()
        clock.advance(1.0)
        _attempt_succeeds(breaker)
        assert breaker.dropped == 6
        assert len(_records(caplog)) == 2

        # Healthy publishing goes on; once an interval has passed since the
        # last report, the pending losses are reported.
        for _ in range(60):
            clock.advance(1.0)
            _attempt_succeeds(breaker)

        assert breaker.dropped == 0
        messages = [r.getMessage() for r in _records(caplog)]
        assert messages[2:] == [
            "Publishing to TestBroker continues "
            "(6 event(s) dropped since the last report)"
        ]

    def test_suppressed_recovery_of_logged_outage_is_written_later(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        clock.advance(30.0)
        _attempt_succeeds(breaker)
        assert len(_records(caplog)) == 2
        # A second outage a minute after the first: its opening line is
        # written, but its recovery a second later is rate-limited, and no
        # losses are pending to trigger a report.
        clock.advance(30.0)
        _attempt_fails(breaker, "second")
        assert len(_records(caplog)) == 3
        clock.advance(1.0)
        _attempt_succeeds(breaker)
        assert len(_records(caplog)) == 3

        for _ in range(60):
            clock.advance(1.0)
            _attempt_succeeds(breaker)

        messages = [r.getMessage() for r in _records(caplog)]
        assert messages[3:] == [
            "Publishing to TestBroker recovered after 1.0s "
            "(0 event(s) dropped since the last report)"
        ]

    def test_nothing_pending_writes_no_report(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)

        for _ in range(200):
            clock.advance(1.0)
            _attempt_succeeds(breaker)

        assert _records(caplog) == []

    @pytest.mark.parametrize("elapsed", [FAST, SLOW])
    def test_flapping_broker_writes_at_most_four_lines_per_interval(
        self, elapsed: float
    ) -> None:
        clock = FakeClock()
        logger = logging.getLogger(f"{LOGGER_NAME}.flapping")
        logger.propagate = False
        stamps: list[float] = []

        class Stamp(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                stamps.append(clock.now)

        handler = Stamp()
        logger.addHandler(handler)
        try:
            breaker = PublishBreaker(
                "TestBroker",
                logger=logger,
                initial_cooldown=1.0,
                max_cooldown=30.0,
                log_interval=60.0,
                probe_timeout=60.0,
                slow_attempt=0.5,
                failures_to_open=3,
                monotonic=clock,
            )
            rng = random.Random(99)
            # Bursts of failures and successes in random lengths.
            pattern = [rng.random() < 0.5 for _ in range(12000)]
            _simulate(
                breaker,
                clock,
                seconds=600.0,
                rate=20.0,
                fails=lambda i: pattern[i],
                elapsed=elapsed,
            )
        finally:
            logger.removeHandler(handler)

        start = 1000.0
        for window in range(10):
            lo = start + window * 60.0
            assert sum(lo <= t < lo + 60.0 for t in stamps) <= 4
        # Losses never stay unreported for long.
        assert breaker.dropped < 20 * 60


class TestPublishBreakerInFlightAttempts:
    """Only the claimed probe changes an open breaker's cooldown."""

    def test_concurrent_slow_failures_open_once_with_initial_cooldown(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        # Eight threads were mid-publish when the broker went away.
        tickets = [_take(breaker) for _ in range(8)]

        for i, ticket in enumerate(tickets):
            breaker.record_failure(ticket, f"t-{i}", _error(), elapsed=SLOW)

        assert breaker.cooldown == 1.0
        assert breaker.dropped == 7
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
            breaker.record_failure(ticket, "evt", _error(), elapsed=SLOW)

        threads = [threading.Thread(target=worker, args=(t,)) for t in tickets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        assert breaker.is_open is True
        assert breaker.cooldown == 1.0

    def test_probe_slower_than_cooldown_admits_no_second_probe(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _attempt_fails(breaker)
        clock.advance(1.0)
        probe = _take(breaker)

        # The probe's connect takes 2s, twice the 1s cooldown.
        clock.advance(2.0)
        assert breaker.allow() is None
        breaker.record_failure(probe, "probe", _error(), elapsed=SLOW)

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

        breaker.record_failure(stale, "stale", _error(), elapsed=SLOW)

        assert breaker.cooldown == 1.0
        assert breaker.dropped == 1
        breaker.record_success(probe, elapsed=FAST)
        assert breaker.is_open is False

    def test_in_flight_success_while_open_keeps_breaker_open(self) -> None:
        breaker = _make_breaker(FakeClock())
        stale = _take(breaker)
        _attempt_fails(breaker)

        breaker.record_success(stale, elapsed=FAST)

        assert breaker.is_open is True
        assert breaker.allow() is None

    def test_stale_failure_after_recovery_does_not_reopen(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        stale = _take(breaker)
        _attempt_fails(breaker)
        clock.advance(1.0)
        _attempt_succeeds(breaker)

        breaker.record_failure(stale, "stale", _error(), elapsed=SLOW)

        assert breaker.is_open is False
        assert _records(caplog)[-1].getMessage() == (
            "Failed to publish event stale to TestBroker; publishing continues "
            "(1 event(s) dropped since the last report)"
        )

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
        breaker.record_failure(lost, "lost", _error(), elapsed=SLOW)
        assert breaker.cooldown == 1.0
        breaker.record_success(replacement, elapsed=FAST)
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


class TestPublishBreakerFork:
    """A forked child starts with a closed breaker and a fresh lock."""

    def test_allow_in_forked_child_resets_inherited_open_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        breaker = _make_breaker(FakeClock())
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


class TestPublishBreakerSlowSuccesses:
    """A broker that still answers, but slowly, opens the breaker too."""

    def test_slow_successes_in_a_row_open_without_dropping_them(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        breaker = _make_breaker(FakeClock())

        _attempt_succeeds(breaker, elapsed=0.9)
        _attempt_succeeds(breaker, elapsed=0.9)
        assert breaker.is_open is False
        _attempt_succeeds(breaker, elapsed=0.5)

        assert breaker.is_open is True
        assert breaker.cooldown == 1.0
        assert breaker.allow() is None
        assert breaker.dropped == 1
        records = _records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].exc_info is None
        assert records[0].getMessage() == (
            "Publishing to TestBroker is slow (the last attempt took 0.5s); "
            "dropping events for 1.0s before trying again (0 event(s) dropped "
            "since the last report)"
        )

    def test_single_success_twice_as_slow_as_threshold_opens(self) -> None:
        breaker = _make_breaker(FakeClock())

        _attempt_succeeds(breaker, elapsed=1.0)

        assert breaker.is_open is True
        assert breaker.allow() is None

    def test_single_success_just_below_twice_threshold_does_not_open(self) -> None:
        breaker = _make_breaker(FakeClock())

        _attempt_succeeds(breaker, elapsed=0.99)
        _attempt_succeeds(breaker, elapsed=0.99)

        assert breaker.is_open is False

    def test_fast_success_resets_slow_success_count(self) -> None:
        breaker = _make_breaker(FakeClock())

        for _ in range(5):
            _attempt_succeeds(breaker, elapsed=0.9)
            _attempt_succeeds(breaker, elapsed=0.9)
            _attempt_succeeds(breaker, elapsed=FAST)

        assert breaker.is_open is False
        assert breaker.allow() is not None

    def test_slow_successes_and_fast_failures_count_together(self) -> None:
        breaker = _make_breaker(FakeClock())

        _attempt_succeeds(breaker, elapsed=0.9)
        _attempt_fails(breaker, elapsed=FAST)
        assert breaker.is_open is False
        _attempt_succeeds(breaker, elapsed=0.9)

        assert breaker.is_open is True

    def test_slow_probe_success_keeps_open_and_doubles_cooldown(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        for _ in range(3):
            _attempt_succeeds(breaker, elapsed=0.9)
        clock.advance(1.0)

        _attempt_succeeds(breaker, elapsed=0.8)

        assert breaker.is_open is True
        assert breaker.cooldown == 2.0
        assert breaker.allow() is None
        clock.advance(1.0)
        assert breaker.allow() is None
        clock.advance(1.0)
        probe = _take(breaker)
        breaker.record_success(probe, elapsed=FAST)
        assert breaker.is_open is False
        assert breaker.cooldown == 1.0
        # The slow probe's summary is rate-limited by the opening line.
        assert [r.getMessage()[:42] for r in _records(caplog)] == [
            "Publishing to TestBroker is slow (the last",
            "Publishing to TestBroker recovered after 3",
        ]

    def test_slow_probe_after_log_interval_logs_summary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        for _ in range(3):
            _attempt_succeeds(breaker, elapsed=0.9)
        assert breaker.allow() is None
        caplog.clear()
        clock.advance(61.0)

        _attempt_succeeds(breaker, elapsed=0.8)

        assert breaker.is_open is True
        assert [r.getMessage() for r in _records(caplog)] == [
            "TestBroker still slow after 61.0s (last attempt took 0.8s; "
            "1 event(s) dropped since the last report); next attempt in 2.0s"
        ]

    def test_slow_success_of_attempt_in_flight_while_open_changes_nothing(
        self,
    ) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        in_flight = _take(breaker)
        _attempt_fails(breaker, elapsed=SLOW)
        clock.advance(1.0)
        probe = _take(breaker)

        breaker.record_success(in_flight, elapsed=0.9)

        assert breaker.is_open is True
        breaker.record_success(probe, elapsed=FAST)
        assert breaker.is_open is False

    def test_fast_traffic_with_sporadic_failures_never_opens(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)

        delivered = _simulate(
            breaker, clock, seconds=60, rate=100, fails=lambda i: i % 10 == 0
        )

        assert breaker.is_open is False
        assert delivered == 5400

    def test_second_slow_opening_within_log_interval_is_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        for _ in range(3):
            _attempt_succeeds(breaker, elapsed=0.9)
        clock.advance(1.0)
        _attempt_succeeds(breaker)  # fast probe closes it
        caplog.clear()

        for _ in range(3):
            _attempt_succeeds(breaker, elapsed=0.9)

        assert breaker.is_open is True
        assert _records(caplog) == []

    def test_slow_success_of_attempt_started_before_last_outage_is_ignored(
        self,
    ) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        stale = _take(breaker)
        _attempt_fails(breaker, elapsed=SLOW)
        clock.advance(1.0)
        _attempt_succeeds(breaker)
        _attempt_succeeds(breaker, elapsed=0.9)
        _attempt_succeeds(breaker, elapsed=0.9)

        breaker.record_success(stale, elapsed=0.9)

        assert breaker.is_open is False
