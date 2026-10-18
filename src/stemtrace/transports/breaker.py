"""Circuit breaker that keeps event publishing cheap while a broker is down.

Publishing runs inside Celery signal handlers, in the task's own thread, so a
broker outage must not slow tasks down. After a run of failed publishes the
breaker "opens": publish attempts are skipped (the events are
dropped, never queued) for a cooldown that doubles after each failed retry, up
to a cap, and resets once a publish succeeds. While open, each event costs a
lock and a clock read.
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable


def _warn_if_passed(value: float | None, call: str) -> None:
    """Warn that a deprecated, ignored argument was passed (removed in 1.0)."""
    if value is not None:
        warnings.warn(
            f"{call} is deprecated and ignored: every failure counts toward "
            "failures_to_open",
            DeprecationWarning,
            stacklevel=3,
        )


def _losses(dropped: int, unconfirmed: int) -> str:
    """Describe lost events for a log line."""
    if unconfirmed:
        return f"{dropped} event(s) dropped, {unconfirmed} sent but unconfirmed"
    return f"{dropped} event(s) dropped"


class PublishBreaker:
    """Thread- and fork-safe circuit breaker for fire-and-forget publishers.

    Call ``allow()`` before each attempt and skip it on None; otherwise report the
    outcome with the returned ticket (``record_success()``, ``record_failure()``).
    ``failures_to_open`` failures in a row open it; then one probe per cooldown
    (doubling up to ``max_cooldown``) decides. A forked child starts closed.
    """

    def __init__(
        self,
        name: str,
        *,
        logger: logging.Logger,
        initial_cooldown: float,
        max_cooldown: float,
        log_interval: float,
        probe_timeout: float,
        failures_to_open: int,
        monotonic: Callable[[], float] = time.monotonic,
        slow_attempt: float | None = None,
    ) -> None:
        """Initialize a closed breaker.

        Args:
            name: Broker name used in log messages (e.g. "RabbitMQ").
            logger: Logger that receives the breaker's messages.
            initial_cooldown: Seconds to drop events after the breaker opens.
            max_cooldown: Upper bound for the doubling cooldown, in seconds.
            log_interval: Minimum seconds between failure log lines.
            probe_timeout: Seconds after which an outstanding probe that never
                reported back is presumed lost and another may start. Must
                exceed the longest possible publish attempt.
            failures_to_open: Consecutive failures (of any kind) that open
                the breaker.
            monotonic: Clock (injectable for tests).
            slow_attempt: Deprecated and ignored (a single slow failure no
                longer opens the breaker); removed in 1.0.
        """
        _warn_if_passed(slow_attempt, "PublishBreaker(slow_attempt=...)")
        self._name = name
        self._logger = logger
        self._initial_cooldown = initial_cooldown
        self._max_cooldown = max_cooldown
        self._log_interval = log_interval
        self._probe_timeout = probe_timeout
        self._failures_to_open = failures_to_open
        self._monotonic = monotonic
        self._start_over()

    def _start_over(self) -> None:
        """Initialize all state: lock, epoch, loss counters, log limiter."""
        self._lock = threading.Lock()
        self._pid = os.getpid()
        # Bumped on every state change and probe claim; tickets carry the
        # value current when the attempt started.
        self._epoch = 0
        # Losses since the last log line (reported by the next one).
        self._dropped = 0
        self._unconfirmed = 0
        # When a failure-while-closed line / an opening line / a recovery line
        # paired with an opening line / any line that reported losses was
        # last written.
        self._last_closed_log: float | None = None
        self._last_open_log: float | None = None
        self._last_recovery_log: float | None = None
        # Length of an outage whose opening line was written but whose
        # recovery line was rate-limited; written once a line is due.
        self._recovery_owed: float | None = None
        self._last_report: float | None = None
        self._close()

    def _close(self) -> None:
        """Enter the closed state (caller holds the lock or owns the object)."""
        self._open_until: float | None = None
        self._probe_ticket: int | None = None
        self._probe_started = 0.0
        self._cooldown = self._initial_cooldown
        self._opened_at = 0.0
        self._outage_reported = False
        self._consecutive_failures = 0
        # Whether the last failure that changed the open state was unconfirmed.
        self._last_unconfirmed = False

    def _check_fork(self) -> None:
        """Start over with a fresh lock and closed state in a forked child."""
        if os.getpid() != self._pid:
            self._start_over()

    @property
    def is_open(self) -> bool:
        """Whether the breaker is dropping events (including while probing)."""
        return self._open_until is not None

    @property
    def dropped(self) -> int:
        """Events dropped since the last log line."""
        return self._dropped

    @property
    def unconfirmed(self) -> int:
        """Events sent but not acknowledged since the last log line."""
        return self._unconfirmed

    @property
    def cooldown(self) -> float:
        """Current cooldown in seconds."""
        return self._cooldown

    def allow(self) -> int | None:
        """Decide whether the caller may attempt a publish now.

        Returns:
            A ticket to pass to ``record_success()``/``record_failure()``
            while closed, and for the single probe let through after a
            cooldown expires; None (and the event counts as dropped) while
            open.
        """
        self._check_fork()
        # Unlocked fast path while closed. The epoch is read before the state
        # check, so the ticket is never newer than the state seen: if the
        # breaker opens (or opens and closes again) in between, the ticket is
        # merely stale, and the attempt is treated as already in flight. Read
        # the other way round, a caller could get the probe's newer ticket.
        ticket = self._epoch
        if not self.is_open:
            return ticket
        with self._lock:
            if self._open_until is None:
                return self._epoch
            now = self._monotonic()
            if self._probe_ticket is None:
                claim = now >= self._open_until
            else:
                claim = now - self._probe_started >= self._probe_timeout
            if claim:
                self._epoch += 1
                self._probe_ticket = self._epoch
                self._probe_started = now
                return self._probe_ticket
            self._dropped += 1
            return None

    def should_attempt(self, ticket: int) -> bool:
        """Whether an attempt that started earlier is still worth making.

        For a caller that had to wait after ``allow()`` (e.g. for a shared
        connection). False only while the breaker is open and ``ticket`` isn't
        the probe's.

        Args:
            ticket: The ticket ``allow()`` returned for the attempt.
        """
        self._check_fork()
        return not self.is_open or ticket == self._epoch

    def record_dropped(self, ticket: int) -> None:
        """Count an event dropped without an attempt (it says nothing about the broker).

        Reported with the next log line, but never a failure and never changes
        the state. A dropped probe frees its slot for the next caller.

        Args:
            ticket: The ticket ``allow()`` returned for the dropped attempt.
        """
        self._check_fork()
        with self._lock:
            self._dropped += 1
            if self._probe_ticket is not None and ticket == self._probe_ticket:
                self._probe_ticket = None

    def record_interrupted(self, ticket: int) -> None:
        """Release an attempt the task's own control flow cut short.

        For an attempt ended by something unrelated to the broker (a Celery soft
        time limit raised mid-publish): neither a failure nor a drop. A probe's
        slot is freed, so events aren't dropped until ``probe_timeout``.

        Args:
            ticket: The ticket ``allow()`` returned for the interrupted attempt.
        """
        self._check_fork()
        with self._lock:
            if self._probe_ticket is not None and ticket == self._probe_ticket:
                self._probe_ticket = None

    def record_success(self, ticket: int) -> None:
        """Report a successful publish attempt.

        While closed, ends the run of consecutive failures. While open,
        closes the breaker if ``ticket`` is the outstanding probe's.

        Args:
            ticket: The ticket ``allow()`` returned for the attempt.
        """
        self._check_fork()
        if not self.is_open:
            # Unlocked fast path: with no failure run to end and nothing to
            # report, a success changes nothing (the reads are atomic, so
            # skipping is equivalent to resetting a counter that is zero).
            # Otherwise the reset happens under the lock, like the increments
            # in record_failure(), so neither update can be lost.
            if (
                self._consecutive_failures
                or self._dropped
                or self._unconfirmed
                or self._recovery_owed is not None
            ):
                self._on_closed_success()
            return
        with self._lock:
            if self._probe_ticket is None or ticket != self._probe_ticket:
                return
            now = self._monotonic()
            outage = now - self._opened_at
            # Paired with a written opening line (rate-limited on its own),
            # or a report line if due; otherwise the losses stay pending for
            # a later report.
            paired = self._outage_reported and self._log_due(
                self._last_recovery_log, now
            )
            log = paired or self._log_due(self._last_report, now)
            if paired:
                self._last_recovery_log = now
            if log:
                losses = self._take_losses(now)
                self._recovery_owed = None
            elif self._outage_reported:
                # The opening line was written: owe it a recovery line.
                self._recovery_owed = outage
            self._epoch += 1
            self._close()
        if log:
            self._logger.warning(
                "Publishing to %s recovered after %.1fs (%s since the last report)",
                self._name,
                outage,
                losses,
            )

    def _on_closed_success(self) -> None:
        """End the failure run; write an owed recovery line or pending losses once due."""
        with self._lock:
            # Also right if a failure opened the breaker since record_success()
            # looked: opening starts the run over and none counts while open.
            self._consecutive_failures = 0
            now = self._monotonic()
            owed = self._recovery_owed
            recovery_due = self._log_due(self._last_recovery_log, now)
            report_due = self._log_due(self._last_report, now)
            # Writing the recovery line also reports (see _take_losses()), so
            # the last report is never older than the last recovery line: no
            # recovery line due means no report is due either.
            if owed is not None and recovery_due:
                self._last_recovery_log = now
                self._recovery_owed = None
            elif not (self._dropped or self._unconfirmed) or not report_due:
                return
            else:
                owed = None
            losses = self._take_losses(now)
        if owed is not None:
            self._logger.warning(
                "Publishing to %s recovered after %.1fs (%s since the last report)",
                self._name,
                owed,
                losses,
            )
        else:
            self._logger.warning(
                "Publishing to %s continues (%s since the last report)",
                self._name,
                losses,
            )

    def _take_losses(self, now: float) -> str:
        """Describe and reset the unreported losses (lock held)."""
        losses = _losses(self._dropped, self._unconfirmed)
        self._dropped = self._unconfirmed = 0
        self._last_report = now
        return losses

    def record_failure(
        self,
        ticket: int,
        event_id: str,
        exc: BaseException,
        *,
        unconfirmed: bool = False,
        elapsed: float | None = None,
    ) -> None:
        """Report a failed publish attempt; the event counts as lost.

        Args:
            ticket: The ticket ``allow()`` returned for the attempt.
            event_id: Identifier of the event that failed, for the log.
            exc: The exception the publish attempt raised.
            unconfirmed: The event was sent but the broker never acknowledged
                it (e.g. a Redis reply that timed out after the command was
                sent).
                It counts as unconfirmed rather than dropped; an unconfirmed
                probe right after an unconfirmed failure sets the cooldown to
                the maximum.
            elapsed: Deprecated and ignored (how long a failure took no
                longer matters); removed in 1.0.
        """
        _warn_if_passed(elapsed, "record_failure(elapsed=...)")
        self._check_fork()
        with self._lock:
            now = self._monotonic()
            if unconfirmed:
                self._unconfirmed += 1
            else:
                self._dropped += 1
            kind = self._transition_on_failure(ticket, unconfirmed, now)
            # One line per kind (failure while closed, opening, recovery,
            # report) per log_interval: at most four, never one per event.
            if kind is None:
                return
            if kind == "closed":
                if not self._log_due(self._last_closed_log, now):
                    return
                self._last_closed_log = now
            elif kind == "opened":
                if not self._log_due(self._last_open_log, now):
                    return
                self._last_open_log = now
                self._outage_reported = True
            elif not self._log_due(self._last_report, now):
                return
            losses = self._take_losses(now)
            cooldown = self._cooldown
            outage = now - self._opened_at

        if kind == "opened":
            self._logger.warning(
                "Failed to publish event %s to %s; dropping events for %.1fs "
                "before trying again (%s since the last report)",
                event_id,
                self._name,
                cooldown,
                losses,
                exc_info=exc,
            )
        elif kind == "closed":
            self._logger.warning(
                "Failed to publish event %s to %s; publishing continues "
                "(%s since the last report)",
                event_id,
                self._name,
                losses,
                exc_info=exc,
            )
        else:
            self._logger.warning(
                "%s still unavailable after %.1fs (%s since the last report; "
                "last error: %s: %s); next attempt in %.1fs",
                self._name,
                outage,
                losses,
                type(exc).__name__,
                exc,
                cooldown,
            )

    def _log_due(self, last: float | None, now: float) -> bool:
        """Whether a line last written at ``last`` may be written again now."""
        return last is None or now - last >= self._log_interval

    def _transition_on_failure(
        self, ticket: int, unconfirmed: bool, now: float
    ) -> str | None:
        """Apply a failure to the state (lock held).

        Returns:
            "opened", "closed" (a failure while staying closed) or "probe" (a
            failed probe) when the failure may be logged; None for an attempt
            that was already in flight while the breaker is open.
        """
        if self._open_until is None:
            # Every failure counts the same, however slow: a healthy broker can
            # look slow (a thread waiting for the GIL misses its deadline).
            if ticket != self._epoch:
                # Started before the breaker last opened and closed: the event
                # is lost, but it says nothing about the broker now.
                return "closed"
            self._consecutive_failures += 1
            if self._consecutive_failures < self._failures_to_open:
                return "closed"
            self._epoch += 1
            self._consecutive_failures = 0
            self._opened_at = now
            self._last_unconfirmed = unconfirmed
            self._open_until = now + self._cooldown
            return "opened"
        if self._probe_ticket is None or ticket != self._probe_ticket:
            # In flight when it opened: only the probe changes the state, so a
            # burst of concurrent failures opens the breaker once.
            return None
        self._probe_ticket = None
        # Two unconfirmed in a row usually mean a stalled broker, which can last
        # minutes; a single one may be a brief stall.
        if unconfirmed and self._last_unconfirmed:
            self._cooldown = self._max_cooldown
        else:
            self._cooldown = min(self._cooldown * 2, self._max_cooldown)
        self._last_unconfirmed = unconfirmed
        self._open_until = now + self._cooldown
        return "probe"
