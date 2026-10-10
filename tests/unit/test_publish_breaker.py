"""Tests for the publish circuit breaker (no I/O, injectable clock)."""

import logging
import os
import threading
from typing import Any

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


def _make_breaker(clock: FakeClock) -> PublishBreaker:
    return PublishBreaker(
        "TestBroker",
        logger=logging.getLogger(LOGGER_NAME),
        initial_cooldown=1.0,
        max_cooldown=30.0,
        log_interval=60.0,
        monotonic=clock,
    )


def _fail(breaker: PublishBreaker, event_id: str = "evt") -> None:
    """Record a failed publish attempt, like publish() does."""
    try:
        raise ConnectionRefusedError("broker down")
    except ConnectionRefusedError as exc:
        breaker.record_failure(event_id, exc)


def _records(caplog: Any) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


class TestPublishBreaker:
    """Open/skip/probe/reset behaviour of PublishBreaker."""

    def test_allow_when_closed_returns_true(self) -> None:
        breaker = _make_breaker(FakeClock())

        assert breaker.allow() is True
        assert breaker.is_open is False
        assert breaker.dropped == 0

    def test_record_failure_opens_and_logs_traceback_once(self, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        breaker = _make_breaker(FakeClock())

        _fail(breaker, "task-1")

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

    def test_allow_while_open_skips_and_counts_drops(self, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)

        clock.advance(0.5)
        results = [breaker.allow() for _ in range(100)]

        assert results == [False] * 100
        assert breaker.dropped == 101
        assert len(_records(caplog)) == 1

    def test_allow_after_cooldown_lets_exactly_one_probe_through(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)

        clock.advance(1.0)

        assert breaker.allow() is True
        # The probe claimed the slot: concurrent callers keep dropping.
        assert breaker.allow() is False
        assert breaker.allow() is False
        assert breaker.dropped == 3

    def test_failed_probe_doubles_cooldown_up_to_cap(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)
        cooldowns = [breaker.cooldown]

        for _ in range(7):
            clock.advance(breaker.cooldown)
            assert breaker.allow() is True
            _fail(breaker)
            cooldowns.append(breaker.cooldown)

        assert cooldowns == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]

    def test_failed_probe_reopens_for_new_cooldown_from_now(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)
        clock.advance(1.0)
        assert breaker.allow() is True
        _fail(breaker)

        clock.advance(1.9)
        assert breaker.allow() is False
        clock.advance(0.1)
        assert breaker.allow() is True

    def test_record_success_after_outage_resets_and_logs_summary(
        self, caplog: Any
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)
        for _ in range(4):
            breaker.allow()
        clock.advance(1.0)
        assert breaker.allow() is True

        breaker.record_success()

        assert breaker.is_open is False
        assert breaker.dropped == 0
        assert breaker.cooldown == 1.0
        assert breaker.allow() is True
        records = _records(caplog)
        assert len(records) == 2
        assert records[1].levelno == logging.INFO
        assert records[1].getMessage() == (
            "Publishing to TestBroker recovered after 1.0s; dropped 5 event(s) "
            "during the outage"
        )

    def test_record_success_when_closed_logs_nothing(self, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        breaker = _make_breaker(FakeClock())

        breaker.record_success()

        assert _records(caplog) == []

    def test_reopen_after_recovery_starts_from_initial_cooldown(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)
        clock.advance(1.0)
        breaker.allow()
        _fail(breaker)
        assert breaker.cooldown == 2.0
        clock.advance(2.0)
        breaker.allow()
        breaker.record_success()

        _fail(breaker)

        assert breaker.cooldown == 1.0

    def test_long_outage_logs_bounded_summaries(self, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)

        # Ten minutes of outage, 100 events per second, every probe failing.
        for _ in range(600 * 100):
            clock.advance(0.01)
            if breaker.allow():
                _fail(breaker)

        records = _records(caplog)
        # One traceback when it opened, then at most one line per minute.
        assert len(records) <= 1 + 10
        assert len(records) >= 1 + 9
        assert all(r.exc_info is None for r in records[1:])
        assert records[1].levelno == logging.WARNING
        assert "TestBroker still unavailable" in records[1].getMessage()
        assert "next attempt in 30.0s" in records[-1].getMessage()
        assert breaker.dropped == 600 * 100 + 1

    def test_summary_reports_dropped_count_and_last_error(self, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)
        for _ in range(9):
            breaker.allow()

        clock.advance(60.0)
        assert breaker.allow() is True
        _fail(breaker)

        message = _records(caplog)[-1].getMessage()
        assert message == (
            "TestBroker still unavailable after 60.0s; dropped 11 event(s) so far "
            "(last error: ConnectionRefusedError: broker down); next attempt in 2.0s"
        )

    def test_allow_in_forked_child_resets_inherited_open_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)
        assert breaker.allow() is False
        parent_pid = os.getpid()

        # Simulate running in a prefork child: same object, different pid.
        monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)

        assert breaker.allow() is True
        assert breaker.is_open is False
        assert breaker.dropped == 0

    def test_allow_in_forked_child_replaces_lock_held_at_fork(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        breaker = _make_breaker(FakeClock())
        _fail(breaker)
        parent_pid = os.getpid()
        # A thread in the parent held the lock when the process forked.
        breaker._lock.acquire()

        monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)
        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(breaker.allow()))
        worker.start()
        worker.join(timeout=2.0)

        assert worker.is_alive() is False
        assert result == [True]

    def test_concurrent_allow_after_cooldown_admits_single_probe(self) -> None:
        clock = FakeClock()
        breaker = _make_breaker(clock)
        _fail(breaker)
        clock.advance(1.0)
        start = threading.Barrier(16)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker() -> None:
            start.wait()
            for _ in range(50):
                allowed = breaker.allow()
                with results_lock:
                    results.append(allowed)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        assert len(results) == 16 * 50
        assert results.count(True) == 1
        assert breaker.dropped == 1 + 16 * 50 - 1
