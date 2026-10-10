"""Circuit breaker that keeps event publishing cheap while a broker is down.

Publishing runs inside Celery signal handlers, in the task's own thread, so a
broker outage must not slow tasks down. After a costly or persistent publish
failure the breaker "opens": publish attempts are skipped (the events are
dropped, never queued) for a cooldown that doubles after each failed retry, up
to a cap, and resets once a publish succeeds. While open, each event costs a
lock and a clock read.
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable


def _losses(dropped: int, unconfirmed: int) -> str:
    """Describe lost events for a log line."""
    if unconfirmed:
        return f"{dropped} event(s) dropped, {unconfirmed} sent but unconfirmed"
    return f"{dropped} event(s) dropped"


class PublishBreaker:
    """Thread- and fork-safe circuit breaker for fire-and-forget publishers.

    Usage: call ``allow()`` before each publish attempt and skip the attempt
    when it returns None. Otherwise it returns a ticket; report the attempt's
    outcome by passing that ticket to ``record_success()`` or
    ``record_failure()``.

    States:
        Closed: every attempt is allowed. A failure opens the breaker only if
            it was costly or persistent: the attempt took ``slow_attempt``
            seconds or more (a timeout or stall, which is what blocks tasks),
            it was unconfirmed (sent, but the broker never acknowledged), or
            it was the ``failures_to_open``-th failure in a row. Otherwise
            only that event is dropped, so sporadic failures on a healthy
            broker don't cost whole cooldowns of events.
        Open: attempts are dropped until the cooldown expires. Then a single
            caller is let through as the probe; no other probe is admitted
            while it is outstanding (up to ``probe_timeout``, after which it
            is presumed lost and a new probe may start). Only the probe's
            outcome changes the state: a failed probe doubles the cooldown
            (capped at ``max_cooldown``) and restarts it; a successful one
            closes the breaker and resets the cooldown. A probe that fails
            unconfirmed right after an unconfirmed failure (the one that
            opened the breaker or the previous probe) sets the cooldown
            straight to the cap and keeps it there: two in a row usually
            mean the broker is blocking publishers, which can last minutes,
            while a single one may just be a brief stall.

    Attempts that were already in flight when the breaker opened (other
    threads of the same process) only count as lost when they fail and
    don't change the state when they succeed, so a burst of concurrent
    failures opens the breaker once, with the initial cooldown.

    Logging is rate-limited independently of the state, so a flapping broker
    can't flood the logs. Failures while the breaker stays closed are logged
    (WARNING with traceback) at most once per ``log_interval``; outage lines
    (the failure that opens the breaker, with traceback, or a summary without
    traceback when a probe fails) also at most once per ``log_interval``. A
    recovery line is written when the breaker closes, but only if an outage
    line was written for that outage. So at most three lines per interval,
    however the broker behaves. Every line reports the events dropped (and
    sent but unconfirmed) since the previous line, so no loss goes
    unreported. Nothing is logged per dropped event.

    The state is guarded by a lock, so it can be shared by the threads of a
    threads/gevent/eventlet pool. A process that forks (Celery's prefork
    pool) starts over in the child with a closed breaker and a fresh lock: a
    breaker opened in the parent never silences a child, and a lock held by
    another parent thread at fork time can't deadlock it.
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
        slow_attempt: float,
        failures_to_open: int,
        monotonic: Callable[[], float] = time.monotonic,
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
            slow_attempt: A failed attempt that took at least this many
                seconds opens the breaker immediately.
            failures_to_open: Consecutive failures that open the breaker even
                when each one was fast.
            monotonic: Clock (injectable for tests).
        """
        self._name = name
        self._logger = logger
        self._initial_cooldown = initial_cooldown
        self._max_cooldown = max_cooldown
        self._log_interval = log_interval
        self._probe_timeout = probe_timeout
        self._slow_attempt = slow_attempt
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
        # When a failure-while-closed line / an outage line was last written.
        self._last_closed_log: float | None = None
        self._last_outage_log: float | None = None
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

    def record_success(self, ticket: int) -> None:
        """Report a successful publish attempt.

        While closed, ends the run of consecutive failures. While open,
        closes the breaker if ``ticket`` is the outstanding probe's.

        Args:
            ticket: The ticket ``allow()`` returned for the attempt.
        """
        self._check_fork()
        if not self.is_open:
            self._consecutive_failures = 0
            return
        with self._lock:
            if self._probe_ticket is None or ticket != self._probe_ticket:
                return
            outage = self._monotonic() - self._opened_at
            reported = self._outage_reported
            dropped, unconfirmed = self._dropped, self._unconfirmed
            if reported:
                self._dropped = self._unconfirmed = 0
            self._epoch += 1
            self._close()
        if reported:
            self._logger.warning(
                "Publishing to %s recovered after %.1fs (%s since the last report)",
                self._name,
                outage,
                _losses(dropped, unconfirmed),
            )

    def record_failure(
        self,
        ticket: int,
        event_id: str,
        exc: BaseException,
        *,
        elapsed: float,
        unconfirmed: bool = False,
    ) -> None:
        """Report a failed publish attempt; the event counts as lost.

        Args:
            ticket: The ticket ``allow()`` returned for the attempt.
            event_id: Identifier of the event that failed, for the log.
            exc: The exception the publish attempt raised.
            elapsed: Seconds the attempt took.
            unconfirmed: The event was sent but the broker never acknowledged
                it (e.g. a RabbitMQ that blocks publishers during a memory or
                disk alarm stops reading, so the connection close times out).
                It counts as unconfirmed rather than dropped; two in a row
                set the cooldown to the maximum.
        """
        self._check_fork()
        with self._lock:
            now = self._monotonic()
            if unconfirmed:
                self._unconfirmed += 1
            else:
                self._dropped += 1
            kind = self._transition_on_failure(ticket, now, elapsed, unconfirmed)
            if kind is None:
                return
            if kind == "closed":
                if not self._log_due(self._last_closed_log, now):
                    return
                self._last_closed_log = now
            else:
                if not self._log_due(self._last_outage_log, now):
                    return
                self._last_outage_log = now
                self._outage_reported = True
            losses = _losses(self._dropped, self._unconfirmed)
            self._dropped = self._unconfirmed = 0
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
        self, ticket: int, now: float, elapsed: float, unconfirmed: bool
    ) -> str | None:
        """Apply a failure to the state (lock held).

        Returns:
            "opened", "closed" (a failure while staying closed) or "probe" (a
            failed probe) when the failure may be logged; None for an attempt
            that was already in flight while the breaker is open.
        """
        if self._open_until is None:
            if ticket != self._epoch:
                # Started before the breaker last opened and closed: the event
                # is lost, but it says nothing about the broker now.
                return "closed"
            self._consecutive_failures += 1
            costly = unconfirmed or elapsed >= self._slow_attempt
            if not costly and self._consecutive_failures < self._failures_to_open:
                return "closed"
            self._epoch += 1
            self._consecutive_failures = 0
            self._opened_at = now
            self._last_unconfirmed = unconfirmed
            self._open_until = now + self._cooldown
            return "opened"
        if self._probe_ticket is None or ticket != self._probe_ticket:
            return None
        self._probe_ticket = None
        if unconfirmed and self._last_unconfirmed:
            self._cooldown = self._max_cooldown
        else:
            self._cooldown = min(self._cooldown * 2, self._max_cooldown)
        self._last_unconfirmed = unconfirmed
        self._open_until = now + self._cooldown
        return "probe"
