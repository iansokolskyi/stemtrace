"""List scans hold the store lock for O(limit) work, so ingestion doesn't wait on them."""

from __future__ import annotations

import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.store import GraphStore

if TYPE_CHECKING:
    from collections.abc import Callable

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
LIMIT = 50
SMALL, BIG = 3_000, 20_000


def _add_run(store: GraphStore, i: int) -> None:
    """A run of three events with worker, queue and (a quarter of them) an exception."""
    failed = i % 4 == 0
    for step, state in enumerate(
        (
            TaskState.PENDING,
            TaskState.STARTED,
            TaskState.FAILURE if failed else TaskState.SUCCESS,
        )
    ):
        store.add_event(
            TaskEvent(
                task_id=f"t{i}",
                name=f"app.task{i % 50}",
                state=state,
                timestamp=NOW - timedelta(seconds=(i * 7) % 7200 + 3 - step),
                hostname=f"worker{i % 5}@host{i % 5}",
                queue=f"queue{i % 4}",
                exception_type=f"Err{i % 7}" if failed and step == 2 else None,
            )
        )


def _store(count: int) -> GraphStore:
    store = GraphStore(max_nodes=count + 10_000)
    for i in range(count):
        _add_run(store, i)
    return store


@pytest.fixture(scope="module")
def small_store() -> GraphStore:
    return _store(SMALL)


@pytest.fixture(scope="module")
def big_store() -> GraphStore:
    return _store(BIG)


class _CountingLock:
    """An RLock that counts the holder's traced lines and calls in each outermost hold.

    A line trace, not a profile hook: a comprehension's loop is inlined in its
    frame (Python 3.12+), so only line events see O(n) work done in one.
    """

    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self._depth = 0
        self._work = 0
        self._previous: Any = None
        self._holder: Any = None
        self.longest = 0

    def _trace(self, frame: object, event: str, arg: object) -> Any:
        self._work += 1
        return self._trace

    def __enter__(self) -> None:
        self._lock.acquire()
        self._depth += 1
        if self._depth == 1:
            self._work = 0
            self._previous = sys.gettrace()
            self._holder = sys._getframe(1)  # the frame that took the lock
            sys.settrace(self._trace)
            self._holder.f_trace = self._trace

    def __exit__(self, *exc: object) -> None:
        if self._depth == 1:
            sys.settrace(self._previous)
            self._holder.f_trace = None
            self._holder = None
            self.longest = max(self.longest, self._work)
        self._depth -= 1
        self._lock.release()


def _longest_hold(store: GraphStore, scan: Callable[[GraphStore], object]) -> int:
    """The longest store-lock hold (profile events) of one call of ``scan``."""
    real = store._lock
    counting = _CountingLock(real)
    store._lock = counting  # type: ignore[assignment]  # same protocol
    try:
        scan(store)
    finally:
        store._lock = real
    return counting.longest


_FROM = NOW - timedelta(hours=1)
_TO = NOW - timedelta(minutes=30)

# Every list scan, with a filter that matches about a fifth of the runs or more.
SCANS: dict[str, Callable[[GraphStore], object]] = {
    "default": lambda s: s.get_nodes(limit=LIMIT),
    "offset": lambda s: s.get_nodes(limit=LIMIT, offset=2_000),
    "state": lambda s: s.get_nodes(limit=LIMIT, state=TaskState.FAILURE),
    "name_contains": lambda s: s.get_nodes(limit=LIMIT, name_contains="task7"),
    "name_exact": lambda s: s.get_nodes(limit=LIMIT, name_exact="app.task7"),
    "from_date": lambda s: s.get_nodes(limit=LIMIT, from_date=_FROM),
    "to_date": lambda s: s.get_nodes(limit=LIMIT, to_date=_TO),
    "hostname": lambda s: s.get_nodes(limit=LIMIT, hostname="worker3@host3"),
    "queue": lambda s: s.get_nodes(limit=LIMIT, queue="queue2"),
    "exception_type": lambda s: s.get_nodes(limit=LIMIT, exception_type="Err3"),
    "task_names": lambda s: s.get_nodes(limit=LIMIT, task_names={"app.task7"}),
    "sorted": lambda s: s.get_nodes(
        limit=LIMIT, hostname="worker3@host3", sort_key=lambda r: r.runtime_ms
    ),
    "root_flows_from": lambda s: s.get_root_nodes_with_children(
        limit=LIMIT, from_date=_FROM
    ),
    "root_flows_to": lambda s: s.get_root_nodes_with_children(limit=LIMIT, to_date=_TO),
    "root_nodes_from": lambda s: s.get_root_nodes(limit=LIMIT, from_date=_FROM),
    "run_summaries": lambda s: s.get_task_run_summaries(),
    "unique_task_names": lambda s: s.get_unique_task_names(),
}


@pytest.mark.parametrize("scan", SCANS)
def test_scan_holds_the_store_lock_for_work_that_does_not_grow_with_the_store(
    scan: str, small_store: GraphStore, big_store: GraphStore
) -> None:
    run = SCANS[scan]
    # Flow times are cached for an unchanged store; time the warm path (the
    # cold compute is a separate limitation).
    run(small_store)
    run(big_store)

    small = _longest_hold(small_store, run)
    big = _longest_hold(big_store, run)

    # 6x the runs may add a few events (a longer dict iteration is one C call),
    # never work per run.
    assert big <= small + 100, f"{scan}: held {small} events at {SMALL}, {big} at {BIG}"
    assert big <= 100 * LIMIT, f"{scan}: held {big} events for a page of {LIMIT}"


def _add_wait_s(
    store: GraphStore, scan: Callable[[GraphStore], object], prefix: str
) -> list[float]:
    """Seconds each ``add_event`` took while another thread scanned nonstop."""
    stop = threading.Event()
    scanned = threading.Event()

    def scanner() -> None:
        while not stop.is_set():
            scan(store)
            scanned.set()

    thread = threading.Thread(target=scanner, daemon=True)
    previous = sys.getswitchinterval()
    sys.setswitchinterval(0.0005)  # the GIL's share of the wait, kept small
    waits: list[float] = []
    thread.start()
    try:
        assert scanned.wait(timeout=30)
        for i in range(120):
            event = TaskEvent(
                task_id=f"{prefix}{i}",
                name="app.live",
                state=TaskState.STARTED,
                timestamp=NOW + timedelta(seconds=i),
                hostname="worker3@host3",
            )
            started = time.perf_counter()
            store.add_event(event)
            waits.append(time.perf_counter() - started)
            time.sleep(0.002)
    finally:
        stop.set()
        thread.join(timeout=30)
        sys.setswitchinterval(previous)
    return waits


@pytest.mark.slow
@pytest.mark.parametrize("scan", ["hostname", "queue", "exception_type", "default"])
def test_add_event_during_nonstop_scans_waits_p99_under_25_ms(
    scan: str, big_store: GraphStore
) -> None:
    waits = _add_wait_s(big_store, SCANS[scan], f"live-{scan}-")

    p99 = statistics.quantiles(waits, n=100)[98]
    assert p99 < 0.025, (
        f"{scan}: p99 add_event {p99 * 1000:.0f} ms, max {max(waits) * 1000:.0f} ms"
    )
