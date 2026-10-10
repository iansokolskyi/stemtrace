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
    when it returns False; report the outcome of an attempt with
    ``record_success()`` or ``record_failure()``.

    States:
        Closed: every attempt is allowed.
        Open: attempts are dropped until the cooldown expires. The first
            caller after that is let through as a probe, and the others keep
            dropping until the probe reports back. A failed probe doubles the
            cooldown (capped at ``max_cooldown``); a successful one closes
            the breaker and resets the cooldown.

    Logging is bounded: one WARNING with a traceback when the breaker opens,
    at most one summary WARNING per ``log_interval`` while it stays open
    (written when a probe fails), and one INFO line with the dropped count
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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize a closed breaker.

        Args:
            name: Broker name used in log messages (e.g. "RabbitMQ").
            logger: Logger that receives the breaker's messages.
            initial_cooldown: Seconds to drop events after the first failure.
            max_cooldown: Upper bound for the doubling cooldown, in seconds.
            log_interval: Minimum seconds between summary lines while open.
            monotonic: Clock (injectable for tests).
        """
        self._name = name
        self._logger = logger
        self._initial_cooldown = initial_cooldown
        self._max_cooldown = max_cooldown
        self._log_interval = log_interval
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._reset()

    def _reset(self) -> None:
        """Return to the closed state (caller holds the lock or owns the object)."""
        self._open_until: float | None = None
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
        """Current cooldown in seconds (the next one to apply when it reopens)."""
        return self._cooldown

    def allow(self) -> bool:
        """Whether the caller may attempt a publish now.

        Returns:
            True while closed, and for the single probe let through after a
            cooldown expires; False (and the event counts as dropped) while
            open.
        """
        self._check_fork()
        # Unlocked fast path while closed; re-checked under the lock.
        if not self.is_open:
            return True
        with self._lock:
            open_until = self._open_until
            if open_until is None:
                return True
            now = self._monotonic()
            if now >= open_until:
                # Claim the probe: other callers keep dropping until it reports.
                self._open_until = now + self._cooldown
                return True
            self._dropped += 1
            return False

    def record_success(self) -> None:
        """Report a successful publish; closes the breaker if it was open."""
        self._check_fork()
        if not self.is_open:
            return
        with self._lock:
            if self._open_until is None:
                return
            outage = self._monotonic() - self._opened_at
            dropped = self._dropped
            self._reset()
        self._logger.info(
            "Publishing to %s recovered after %.1fs; dropped %d event(s) "
            "during the outage",
            self._name,
            outage,
            dropped,
        )

    def record_failure(self, event_id: str, exc: BaseException) -> None:
        """Report a failed publish attempt; the event counts as dropped.

        Opens the breaker for ``initial_cooldown`` if it was closed, or
        doubles the cooldown (up to ``max_cooldown``) after a failed probe.

        Args:
            event_id: Identifier of the event that failed, for the log.
            exc: The exception the publish attempt raised.
        """
        self._check_fork()
        with self._lock:
            now = self._monotonic()
            self._dropped += 1
            if self._open_until is None:
                self._opened_at = now
                self._last_log = now
                self._open_until = now + self._cooldown
                opened, summary = True, False
            else:
                self._cooldown = min(self._cooldown * 2, self._max_cooldown)
                self._open_until = now + self._cooldown
                opened = False
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
