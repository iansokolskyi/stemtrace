"""Worker registry refresh that runs without a request.

``InspectProbe`` runs a refresh on its own thread when the server starts and
when the registry learns a new nodename. Triggers that arrive while a refresh
runs, or inside the minimum interval, coalesce into one follow-up. A new
nodename stays pending until an inspect reply names it, with a bounded backoff.
"""

from __future__ import annotations

import logging
import threading
import time
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

# Seconds before each extra attempt for a nodename no inspect reply has named
# yet (its worker may still be in its worker_ready receivers); then it is dropped.
PENDING_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0, 30.0)
PENDING_NODES_MAX = 1024


class ProbeTarget(Protocol):
    """What a probe drives: a refresher that shares its pacing with the probe."""

    def refresh_blocking(self) -> frozenset[str] | None:
        """Refresh now; the nodenames that answered, or None if one was in flight."""
        ...

    def ready_in(self) -> float:
        """Seconds until a refresh by the page-load path is old enough to follow."""
        ...


class _Unbound:
    """Stands in until a target is bound; never runs (``trigger`` checks ``_bound``)."""

    refresh_blocking = staticmethod(lambda: frozenset[str]())
    ready_in = staticmethod(lambda: 0.0)


def _clock() -> float:
    """Return a monotonic reading (a seam for tests)."""
    return time.monotonic()


def _wait(stop: threading.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``, waking early when ``stop`` is set; True if it is set."""
    return stop.wait(seconds)


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
        wait: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        """Create a probe that starts attempts at least ``min_interval_s`` apart.

        Args:
            min_interval_s: Minimum seconds between two attempt starts.
            clock: Monotonic clock (tests); defaults to ``time.monotonic``.
            wait: Stop-aware sleep (tests); defaults to ``Event.wait``.
        """
        self._min_interval_s = min_interval_s
        self._clock = clock or _clock
        self._wait = wait or _wait
        self._target: ProbeTarget = _Unbound()
        self._bound = False
        # Nodename -> backoff steps already scheduled for it; insertion order is age.
        self._pending: dict[str, int] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_allowed = float("-inf")

    def bind(self, target: ProbeTarget) -> None:
        """Set what the probe drives."""
        self._target = target
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
                self._pending[nodename] = 0
                if len(self._pending) > PENDING_NODES_MAX:
                    del self._pending[next(iter(self._pending))]
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    args=(self._stop, self._wake),
                    name=_THREAD_NAME,
                    daemon=True,
                )
                self._thread.start()
            self._wake.set()

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
        """Probe loop: wait for a trigger, honor the interval, refresh, repeat."""
        # Ends when stop() sets the event; every wait below wakes on it.
        while not stop.is_set():
            wake.wait()
            if stop.is_set():
                return
            delay = max(self._next_allowed - self._clock(), self._target.ready_in())
            if delay > 0 and self._wait(stop, delay):
                return
            # A trigger from here on is owed a refresh of its own, not dropped:
            # celery 5.6.3 worker/loops.py asynloop() sends worker_ready (via
            # on_ready) after kombu pidbox Node.listen() bound the control
            # queue but before the loop drains it, so an inspect sent for a
            # worker_ready can time out and the next one must still happen.
            wake.clear()
            started = self._clock()
            self._next_allowed = started + self._min_interval_s
            with self._lock:
                asked = set(self._pending)
            answered = self._attempt()
            if answered is None:
                # Another refresh held the slot: owe one rather than drop it.
                wake.set()
                continue
            backoff = self._settle(asked, answered)
            if backoff is not None:
                self._next_allowed = max(self._next_allowed, started + backoff)
                wake.set()

    def _settle(self, asked: set[str], answered: frozenset[str]) -> float | None:
        """Update the pending nodenames after an attempt; the delay owed, if any.

        Only nodenames pending before the attempt started count: a reply
        can't name one that joined during it (its own trigger owes a refresh).
        """
        delays: list[float] = []
        with self._lock:
            for nodename in asked:
                steps = self._pending.get(nodename)
                if steps is None or nodename in answered:
                    self._pending.pop(nodename, None)
                elif steps >= len(PENDING_BACKOFF_SECONDS):
                    del self._pending[nodename]
                else:
                    delays.append(PENDING_BACKOFF_SECONDS[steps])
                    self._pending[nodename] = steps + 1
        return min(delays) if delays else None

    def _attempt(self) -> frozenset[str] | None:
        """Run the bound refresh once; failures are logged, never raised."""
        # Fire-and-forget boundary: a probe must never take the server down.
        try:
            return self._target.refresh_blocking()
        except Exception:
            logger.debug("Inspect probe refresh failed", exc_info=True)
            return frozenset()
