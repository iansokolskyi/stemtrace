"""Circuit breaker that keeps event publishing cheap while a broker is down.

Publishing runs inside Celery signal handlers, in the task's own thread, so a
broker outage must not slow tasks down. After a failed publish the breaker
"opens": publish attempts are skipped (the events are dropped, never queued)
for a cooldown that doubles after each failed retry, up to a cap, and resets
once a publish succeeds. While open, each event costs a lock and a clock read.
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable


class PublishBreaker:
    """Thread- and fork-safe circuit breaker for fire-and-forget publishers.

    Usage: call ``allow()`` before each publish attempt and skip the attempt
    when it returns None. Otherwise it returns a ticket; report the attempt's
    outcome by passing that ticket to ``record_success()`` or
    ``record_failure()``.

    States:
        Closed: every attempt is allowed. The first failure reported by an
            attempt started while closed opens the breaker for
            ``initial_cooldown``.
        Open: attempts are dropped until the cooldown expires. Then a single
            caller is let through as the probe; no other probe is admitted
            while it is outstanding, however long it takes (up to
            ``probe_timeout``, after which it is presumed lost and a new
            probe may start). Only the probe's outcome changes the state: a
            failed probe doubles the cooldown (capped at ``max_cooldown``)
            and restarts it; a successful one closes the breaker and resets
            the cooldown.

    Attempts that were already in flight when the breaker opened (other
    threads of the same process) only count as dropped when they fail and
    don't change the state when they succeed, so a burst of concurrent
    failures opens the breaker once, with the initial cooldown. Outcomes of
    attempts from before the last state change are otherwise ignored.

    Logging is bounded: one WARNING with a traceback when the breaker opens,
    at most one summary WARNING per ``log_interval`` while it stays open
    (written when a probe fails), and one WARNING line with the dropped count
    when it closes. Nothing is logged per dropped event.

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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize a closed breaker.

        Args:
            name: Broker name used in log messages (e.g. "RabbitMQ").
            logger: Logger that receives the breaker's messages.
            initial_cooldown: Seconds to drop events after the first failure.
            max_cooldown: Upper bound for the doubling cooldown, in seconds.
            log_interval: Minimum seconds between summary lines while open.
            probe_timeout: Seconds after which an outstanding probe that never
                reported back is presumed lost and another may start. Must
                exceed the longest possible publish attempt.
            monotonic: Clock (injectable for tests).
        """
        self._name = name
        self._logger = logger
        self._initial_cooldown = initial_cooldown
        self._max_cooldown = max_cooldown
        self._log_interval = log_interval
        self._probe_timeout = probe_timeout
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._pid = os.getpid()
        # Bumped on every state change and probe claim; tickets carry the
        # value current when the attempt started.
        self._epoch = 0
        self._reset()

    def _reset(self) -> None:
        """Return to the closed state (caller holds the lock or owns the object)."""
        self._open_until: float | None = None
        self._probe_ticket: int | None = None
        self._probe_started = 0.0
        self._cooldown = self._initial_cooldown
        self._dropped = 0
        self._opened_at = 0.0
        self._last_log = 0.0

    def _check_fork(self) -> None:
        """Start over with a fresh lock and closed state in a forked child."""
        pid = os.getpid()
        if pid != self._pid:
            self._lock = threading.Lock()
            self._pid = pid
            self._epoch += 1
            self._reset()

    @property
    def is_open(self) -> bool:
        """Whether the breaker is dropping events (including while probing)."""
        return self._open_until is not None

    @property
    def dropped(self) -> int:
        """Events dropped since the breaker opened (0 while closed)."""
        return self._dropped

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
        # Unlocked fast path while closed. If the breaker opens concurrently
        # the ticket is one epoch old, which only makes this attempt count as
        # already in flight.
        if not self.is_open:
            return self._epoch
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

        Closes the breaker if ``ticket`` is the outstanding probe's.

        Args:
            ticket: The ticket ``allow()`` returned for the attempt.
        """
        self._check_fork()
        if not self.is_open:
            return
        with self._lock:
            if self._probe_ticket is None or ticket != self._probe_ticket:
                return
            outage = self._monotonic() - self._opened_at
            dropped = self._dropped
            self._epoch += 1
            self._reset()
        self._logger.warning(
            "Publishing to %s recovered after %.1fs; dropped %d event(s) "
            "during the outage",
            self._name,
            outage,
            dropped,
        )

    def record_failure(self, ticket: int, event_id: str, exc: BaseException) -> None:
        """Report a failed publish attempt; the event counts as dropped.

        Opens the breaker for ``initial_cooldown`` if it is closed and the
        attempt started in the current closed period; doubles the cooldown
        (up to ``max_cooldown``) if ``ticket`` is the outstanding probe's.
        Failures of other in-flight attempts only count as dropped.

        Args:
            ticket: The ticket ``allow()`` returned for the attempt.
            event_id: Identifier of the event that failed, for the log.
            exc: The exception the publish attempt raised.
        """
        self._check_fork()
        opened = summary = False
        with self._lock:
            now = self._monotonic()
            if self._open_until is None:
                if ticket != self._epoch:
                    # Started before the breaker last opened and closed.
                    return
                self._epoch += 1
                self._dropped = 1
                self._opened_at = now
                self._last_log = now
                self._open_until = now + self._cooldown
                opened = True
            else:
                self._dropped += 1
                if self._probe_ticket is None or ticket != self._probe_ticket:
                    # Already in flight when the breaker opened.
                    return
                self._probe_ticket = None
                self._cooldown = min(self._cooldown * 2, self._max_cooldown)
                self._open_until = now + self._cooldown
                summary = now - self._last_log >= self._log_interval
                if summary:
                    self._last_log = now
            cooldown = self._cooldown
            dropped = self._dropped
            outage = now - self._opened_at

        if opened:
            self._logger.warning(
                "Failed to publish event %s to %s; dropping events for %.1fs "
                "before trying again",
                event_id,
                self._name,
                cooldown,
                exc_info=exc,
            )
        elif summary:
            self._logger.warning(
                "%s still unavailable after %.1fs; dropped %d event(s) so far "
                "(last error: %s: %s); next attempt in %.1fs",
                self._name,
                outage,
                dropped,
                type(exc).__name__,
                exc,
                cooldown,
            )
