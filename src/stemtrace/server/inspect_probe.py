"""Worker registry refresh that runs without a request.

``InspectProbe`` runs a refresh on its own thread when the server starts and
when the registry learns a new nodename. Triggers that arrive while a refresh
runs, or inside the minimum interval, coalesce into one follow-up. A new
nodename stays pending until an inspect reply names it, with a bounded backoff.
With a ``Periodic`` bound, it also refreshes every period while a worker is live.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# The UI requests workers and the registry in quick succession on page load;
# one inspect round trip serves both. The probe paces itself by the same interval.
INSPECT_REFRESH_MIN_INTERVAL_SECONDS = 2.0

# Below the consumer's 5 s stop join, so shutting down waits for them in turn.
INSPECT_PROBE_STOP_TIMEOUT_SECONDS = 2.0

_THREAD_NAME = "stemtrace-inspect-probe"

# A nodename no inspect reply has named yet (its worker may still be in its
# worker_ready receivers) is owed an attempt at join + 2, 6, 14, 30 and 60 s
# (the cumulative sums of these gaps); then it is dropped.
PENDING_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0, 30.0)
_SCHEDULE = tuple(itertools.accumulate(PENDING_BACKOFF_SECONDS))
PENDING_NODES_MAX = 1024


class ProbeTarget(Protocol):
    """What a probe drives: a refresher that shares its pacing with the probe."""

    def refresh_blocking(self) -> frozenset[str] | None:
        """Refresh now; the nodenames that answered, or None if one was in flight."""
        ...

    def ready_in(self) -> float:
        """Seconds until a refresh by the page-load path is old enough to follow."""
        ...


@dataclass(frozen=True, slots=True)
class Periodic:
    """A refresh owed every ``period_s`` while ``has_live_worker()`` is true.

    The probe still paces it by the target's ``ready_in`` (the shared interval
    and the failure backoff), so a period can't beat either.
    """

    period_s: float
    has_live_worker: Callable[[], bool]


class _Unbound:
    """Stands in until a target is bound; never runs (``trigger`` checks ``_bound``)."""

    refresh_blocking = staticmethod(lambda: frozenset[str]())
    ready_in = staticmethod(lambda: 0.0)


def _clock() -> float:
    """Return a monotonic reading (a seam for tests)."""
    return time.monotonic()


def _wait(event: threading.Event, seconds: float | None) -> bool:
    """Block up to ``seconds`` (forever if None) until ``event`` is set; True if it is."""
    return event.wait(seconds)


class InspectProbe:
    """Runs a bound refresh off-thread, paced by a minimum interval.

    ``trigger()`` never blocks and may be called from any thread. The refresh
    runs on the probe's own thread, one at a time. Pacing counts attempts, not
    successes, so a down broker costs one attempt per interval at most.
    """

    # Execution model: the consumer thread, request threads and the event loop
    # call trigger(); only the probe thread calls the refresh. ``_wake`` is the
    # whole trigger state: set means a refresh is owed.

    def __init__(
        self,
        min_interval_s: float = INSPECT_REFRESH_MIN_INTERVAL_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
        wait: Callable[[threading.Event, float | None], bool] | None = None,
    ) -> None:
        """Create a probe that starts attempts at least ``min_interval_s`` apart.

        Args:
            min_interval_s: Minimum seconds between two attempt starts.
            clock: Monotonic clock (tests); defaults to ``time.monotonic``.
            wait: Blocking wait on an event (tests); defaults to ``Event.wait``.
        """
        self._min_interval_s = min_interval_s
        self._clock = clock or _clock
        self._wait = wait or _wait
        self._target: ProbeTarget = _Unbound()
        self._bound = False
        # Nodename -> its owed attempt times not yet served, fixed at its join;
        # insertion order is age.
        self._pending: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_allowed = float("-inf")
        self._periodic: Periodic | None = None
        # When the next periodic refresh is owed; set by each attempt's start.
        self._periodic_at: float | None = None

    def bind(self, target: ProbeTarget, periodic: Periodic | None = None) -> None:
        """Set what the probe drives, and optionally a refresh owed every period."""
        self._target = target
        self._periodic = periodic
        self._bound = True

    @property
    def target(self) -> ProbeTarget | None:
        """What the probe drives, or None before ``bind``."""
        return self._target if self._bound else None

    def start(self) -> None:
        """Probe once now (the server just started). Safe to call again."""
        with self._lock:
            if self._stop.is_set():
                # Fresh events: an abandoned thread must stay stopped.
                self._stop = threading.Event()
                self._wake = threading.Event()
                self._periodic_at = None
        self.trigger()

    def trigger(self, nodename: str | None = None) -> None:
        """Ask for a refresh; coalesces with one already owed. Never blocks.

        A ``nodename`` stays pending, and keeps owing attempts, until an
        inspect reply names it.
        """
        with self._lock:
            if not self._bound or self._stop.is_set():
                return
            if nodename is not None and nodename not in self._pending:
                joined = self._clock()
                self._pending[nodename] = [joined + gap for gap in _SCHEDULE]
                if len(self._pending) > PENDING_NODES_MAX:
                    del self._pending[next(iter(self._pending))]
            # Set before the thread starts, so its first look sees the trigger.
            self._wake.set()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    args=(self._stop, self._wake),
                    name=_THREAD_NAME,
                    daemon=True,
                )
                self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop the probe thread, waiting up to ``timeout`` for a running refresh.

        A refresh blocked in the broker client can't be interrupted; the
        thread is a daemon, so it is abandoned after the timeout.
        """
        limit = INSPECT_PROBE_STOP_TIMEOUT_SECONDS if timeout is None else timeout
        with self._lock:
            self._stop.set()
            self._wake.set()
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=limit)
            if thread.is_alive():
                logger.warning("Inspect probe did not stop within %.1fs", limit)

    def _run(self, stop: threading.Event, wake: threading.Event) -> None:
        """Probe loop: sleep until a trigger or an owed time, then refresh."""
        # Ends when stop() sets the event; stop() also sets ``wake``, which every
        # wait below is on or ends on.
        while not stop.is_set():
            owed = wake.is_set()
            due = self._next_due()
            if not owed and due is None:
                self._wait(wake, None)
                continue
            now = self._clock()
            at = max(self._next_allowed, now + self._target.ready_in())
            if not owed and due is not None:
                at = max(at, due)
            delay = at - now
            if delay > 0:
                # A trigger already owed refreshes when the interval ends, and
                # only stop() cuts that short. Otherwise the sleep is for a node's
                # owed time and a new trigger wakes it: its refresh is due at
                # the end of the shared interval, not at another node's time.
                woken = self._wait(stop if owed else wake, delay)
                if stop.is_set():
                    return
                if woken:
                    continue
            if not owed and self._skip_idle_period():
                continue
            # A trigger from here on is owed a refresh of its own, not dropped:
            # celery 5.6.3 worker/loops.py asynloop() sends worker_ready (via
            # on_ready) after kombu pidbox Node.listen() bound the control
            # queue but before the loop drains it, so an inspect sent for a
            # worker_ready can time out and the next one must still happen.
            wake.clear()
            started = self._clock()
            self._next_allowed = started + self._min_interval_s
            if self._periodic is not None:
                self._periodic_at = started + self._periodic.period_s
            answered = self._attempt()
            if answered is None:
                # Another refresh held the slot: owe one rather than drop it.
                wake.set()
                continue
            self._settle(started, answered)

    def _next_due(self) -> float | None:
        """The soonest owed time: a pending nodename's or the periodic refresh's."""
        pending, periodic = self._earliest_due(), self._periodic_at
        if pending is None or periodic is None:
            return periodic if pending is None else pending
        return min(pending, periodic)

    def _skip_idle_period(self) -> bool:
        """True when only the period is due and no worker is live: wait another period.

        Nothing to refresh then. Checking again each period (instead of
        sleeping forever) picks up a worker that revives without a new nodename.
        """
        periodic, at = self._periodic, self._periodic_at
        now = self._clock()
        if periodic is None or at is None or at > now:
            return False
        pending = self._earliest_due()
        if pending is not None and pending <= now:
            return False
        if periodic.has_live_worker():
            return False
        self._periodic_at = now + periodic.period_s
        return True

    def _earliest_due(self) -> float | None:
        """The soonest owed time of any pending nodename, if there is one."""
        with self._lock:
            return min((dues[0] for dues in self._pending.values()), default=None)

    def _settle(self, started: float, answered: frozenset[str]) -> None:
        """After an attempt: drop the nodenames it named and the owed times it served.

        An attempt serves every owed time at or before its start, whichever
        trigger caused it. A nodename with none left is dropped unanswered.
        """
        with self._lock:
            for nodename, dues in list(self._pending.items()):
                remaining = [due for due in dues if due > started]
                if nodename in answered or not remaining:
                    del self._pending[nodename]
                else:
                    self._pending[nodename] = remaining

    def _attempt(self) -> frozenset[str] | None:
        """Run the bound refresh once; failures are logged, never raised."""
        # Fire-and-forget boundary: a probe must never take the server down.
        try:
            return self._target.refresh_blocking()
        except Exception:
            logger.debug("Inspect probe refresh failed", exc_info=True)
            return frozenset()
