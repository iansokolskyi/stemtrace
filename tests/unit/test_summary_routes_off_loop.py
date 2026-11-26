"""The summary endpoints leave the event loop free while they aggregate.

At ``max_nodes`` of 30,000 the O(n) pass held the loop for 50 ms or more, which
stalled WebSocket pushes and anything else sharing the loop.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server import store as store_module
from stemtrace.server.api import params, summary_routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry

if TYPE_CHECKING:
    from stemtrace.core.graph import TaskNode

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
MAX_NODES = 30_000
# Loop work between two turns, in profile events (Python and C calls and returns, and C
# exceptions) on the loop thread (see _longest_loop_hold). At 30k nodes a sliced turn (2,048 records) takes
# 8-29k events; the pre-#172 unsliced routes took 110-250k in one turn. The budget sits
# between.
MAX_LOOP_WORK = 60_000
PATHS = [
    "/api/overview?within=1h",
    "/api/failures",
    "/api/stats/timeseries",
    "/api/stats/timeseries?percentiles=p95",
]
# Not in PATHS: the tests over PATHS read the summary response shape.
TASK_SORT_PATHS = [
    "/api/tasks?sort=runtime&limit=100",
    "/api/tasks?sort=queue_wait&limit=100&state=SUCCESS,FAILURE",
]
# Task and graph lists at their largest page, over the realistic store (see _add_realistic).
# Every filter value matches nodes: a scan that matches nothing builds no response.
TASK_LIST_PATHS = [
    "/api/tasks?limit=500",
    "/api/tasks?limit=500&state=FAILURE",
    "/api/tasks?limit=500&name=task7",
    "/api/tasks?limit=500&name_exact=app.task7",
    "/api/tasks?limit=500&hostname=worker3@host3",
    "/api/tasks?limit=500&queue=queue2",
    "/api/tasks?limit=500&exception_type=Err3",
    "/api/tasks?limit=500&within=1h",
    "/api/tasks?limit=500&from_date=2026-10-18T11:00:00Z",
    "/api/graphs?limit=100",
    "/api/graphs?limit=100&within=1h",
]
_STATES = [TaskState.FAILURE, TaskState.SUCCESS, TaskState.SUCCESS, TaskState.REVOKED]
# (name, failure_share_is_all, exception types): the mix has a quarter of the runs
# failing over 7 types; a storm has every run failing, over 1 or 50 types.
SHAPES = {
    "mix": (False, 7),
    "storm_1_type": (True, 1),
    "storm_50_types": (True, 50),
}


def _add(store: GraphStore, i: int, *, seconds_ago: int, shape: str = "mix") -> None:
    storm, types = SHAPES[shape]
    state = TaskState.FAILURE if storm else _STATES[i % len(_STATES)]
    # PENDING and STARTED first, so finished runs give runtime and wait samples
    # (the p95 paths collect and sort them).
    for earlier, extra in ((TaskState.PENDING, 3), (TaskState.STARTED, 2)):
        store.add_event(
            TaskEvent(
                task_id=f"t{i}",
                name=f"app.task{i % 50}",
                state=earlier,
                timestamp=NOW - timedelta(seconds=seconds_ago + extra),
            )
        )
    store.add_event(
        TaskEvent(
            task_id=f"t{i}",
            name=f"app.task{i % 50}",
            state=state,
            timestamp=NOW - timedelta(seconds=seconds_ago),
            exception_type=f"Err{i % types}" if state == TaskState.FAILURE else None,
        )
    )


def _add_realistic(store: GraphStore, i: int, *, seconds_ago: int) -> None:
    """One run of 10 events (2 retries) with worker, queue and exception set.

    Every third run is a root and the next two its children, so graphs have
    children to read. The attempts follow Celery's received, started, retry
    order, then the last one ends in FAILURE or SUCCESS.
    """
    name = f"app.task{i % 50}"
    base = {
        "task_id": f"t{i}",
        "name": name,
        "hostname": f"worker{i % 5}@host{i % 5}",
        "queue": f"queue{i % 4}",
    }
    if i % 3:
        base["parent_id"] = f"t{i - i % 3}"
        base["root_id"] = f"t{i - i % 3}"
    steps = [(TaskState.PENDING, 0, 0)]
    for attempt in range(3):
        steps.append((TaskState.RECEIVED, attempt, 0))
        steps.append((TaskState.STARTED, attempt, 0))
        if attempt < 2:
            steps.append((TaskState.RETRY, attempt, attempt + 1))
    final = TaskState.FAILURE if i % 4 == 0 else TaskState.SUCCESS
    steps.append((final, 2, 2))
    for n, (state, _attempt, retries) in enumerate(steps):
        failed = state in (TaskState.FAILURE, TaskState.RETRY)
        store.add_event(
            TaskEvent(
                state=state,
                timestamp=NOW - timedelta(seconds=seconds_ago + len(steps) - n),
                retries=retries,
                exception_type=f"Err{i % 7}" if failed else None,
                exception="boom" if failed else None,
                **base,
            )
        )


def _populated_realistic(count: int) -> GraphStore:
    store = GraphStore(max_nodes=count + 5_000)
    for i in range(count):
        _add_realistic(store, i, seconds_ago=(i * 7) % 7200)
    return store


def _populated(count: int, shape: str = "mix") -> GraphStore:
    store = GraphStore(max_nodes=count + 5_000)
    for i in range(count):
        _add(store, i, seconds_ago=(i * 7) % 7200, shape=shape)
    return store


def _app(store: GraphStore) -> FastAPI:
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))
    return app


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


@pytest.fixture(scope="module")
def big_stores() -> dict[str, GraphStore]:
    return {}


def _big_store(cache: dict[str, GraphStore], shape: str) -> GraphStore:
    """The 30k-node store of ``shape``, built once per shape for the module."""
    if shape not in cache:
        cache[shape] = (
            _populated_realistic(MAX_NODES)
            if shape == "realistic"
            else _populated(MAX_NODES, shape)
        )
    return cache[shape]


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def _longest_loop_hold(app: FastAPI, path: str) -> int:
    """Request ``path``; return the most work the loop did between two of its turns.

    Work is the count of profile events (``sys.setprofile``: Python and C calls and
    returns, and C exceptions) on the loop thread, not time: on a CPU-throttled or busy host a stolen or
    preempted stretch looks like a held loop in wall time, and also in
    ``time.thread_time`` when the hypervisor steals.
    """
    done = asyncio.Event()
    work = 0
    longest = 0

    def count(frame: object, event: str, arg: object) -> None:
        nonlocal work
        work += 1

    async def probe() -> None:
        nonlocal work, longest
        previous = sys.getprofile()
        sys.setprofile(count)  # per thread: the loop's, since the probe runs on it
        try:
            work = 0
            while not done.is_set():
                await asyncio.sleep(0)
                longest = max(longest, work)
                work = 0
        finally:
            sys.setprofile(previous)

    probing = asyncio.create_task(probe())
    await asyncio.sleep(0)  # the probe's first turn
    try:
        response = await _get(app, path)
    finally:
        done.set()
        await probing
    assert response.status_code == 200
    return longest


@pytest.mark.slow
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("path", PATHS)
async def test_summary_request_at_30k_nodes_keeps_loop_lag_within_budget(
    path: str, shape: str, big_stores: dict[str, GraphStore]
) -> None:
    """The most work the loop does between two of its turns stays bounded."""
    app = _app(_big_store(big_stores, shape))
    # The first request builds the app's middleware stack (about 70 ms): not ours.
    assert (await _get(app, "/api/health")).status_code == 200
    longest = await _longest_loop_hold(app, path)
    assert longest <= MAX_LOOP_WORK, (
        f"loop did {longest} events between turns (max {MAX_LOOP_WORK})"
    )


@pytest.mark.slow
@pytest.mark.parametrize("path", TASK_SORT_PATHS)
async def test_sorted_task_list_at_30k_nodes_keeps_loop_lag_within_budget(
    path: str, big_stores: dict[str, GraphStore]
) -> None:
    """Sorting every matching run (a Python key per node) runs off the loop."""
    app = _app(_big_store(big_stores, "mix"))
    assert (await _get(app, "/api/health")).status_code == 200
    longest = await _longest_loop_hold(app, path)
    assert longest <= MAX_LOOP_WORK, (
        f"loop did {longest} events between turns (max {MAX_LOOP_WORK})"
    )


@pytest.mark.slow
@pytest.mark.parametrize("cache", ["cold", "warm"])
@pytest.mark.parametrize("path", TASK_LIST_PATHS)
async def test_task_and_graph_lists_at_30k_nodes_keep_loop_lag_within_budget(
    path: str, cache: str, big_stores: dict[str, GraphStore]
) -> None:
    """Scanning and building the largest page of a list runs off the loop."""
    store = _big_store(big_stores, "realistic")
    app = _app(store)
    assert (await _get(app, "/api/health")).status_code == 200
    if cache == "cold":
        store._flow_cache = None  # the first graph list rebuilds the flow times
    else:
        assert (await _get(app, path)).status_code == 200
    longest = await _longest_loop_hold(app, path)
    assert longest <= MAX_LOOP_WORK, (
        f"loop did {longest} events between turns (max {MAX_LOOP_WORK})"
    )


@pytest.mark.parametrize("path", TASK_LIST_PATHS)
async def test_task_and_graph_lists_at_30k_nodes_return_matches(
    path: str, big_stores: dict[str, GraphStore]
) -> None:
    """Each list case matches nodes, so the loop test builds a real response."""
    response = await _get(_app(_big_store(big_stores, "realistic")), path)
    body = response.json()
    assert body["total"] > 0
    assert len(body.get("tasks", body.get("graphs"))) > 0


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    "path",
    [
        *PATHS,
        "/api/stats/timeseries?bucket=1m&within=2h",
        "/api/stats/timeseries?bucket=1m&within=2h&percentiles=p95",
    ],
)
@pytest.mark.parametrize("slice_size", [1, 7, 500])
async def test_summary_response_is_the_same_whatever_the_slice_size(
    path: str, shape: str, slice_size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One slice is the unsliced computation; smaller slices give the same bytes."""
    app = _app(_populated(1_500, shape))
    monkeypatch.setattr(summary_routes, "_SLICE", 10**9)
    expected = await _get(app, path)
    monkeypatch.setattr(summary_routes, "_SLICE", slice_size)
    actual = await _get(app, path)

    assert expected.status_code == actual.status_code == 200
    assert actual.content == expected.content


@pytest.mark.parametrize("path", PATHS)
async def test_summary_request_during_ingestion_returns_a_consistent_snapshot(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events added while slices run (loop and thread) never break or tear a response."""
    initial = 600
    store = _populated(initial)
    app = _app(store)
    monkeypatch.setattr(summary_routes, "_SLICE", 16)
    added = 400

    def ingest_from_thread() -> None:
        for i in range(initial, initial + added):
            _add(store, i, seconds_ago=60)

    ingester = threading.Thread(target=ingest_from_thread)

    async def ingest_from_loop() -> None:
        for i in range(initial + added, initial + 2 * added):
            _add(store, i, seconds_ago=60)
            await asyncio.sleep(0)

    ingester.start()
    try:
        response, _ = await asyncio.gather(_get(app, path), ingest_from_loop())
    finally:
        ingester.join(timeout=30)
    assert not ingester.is_alive()

    assert response.status_code == 200
    body = response.json()
    if path.startswith("/api/failures"):
        assert body["total"] == sum(g["count"] for g in body["groups"])
        assert initial // 4 <= body["total"] <= (initial + 2 * added) // 4 + 1
    elif path.startswith("/api/stats"):
        total = sum(sum(series) for series in body["counts"].values())
        assert initial <= total <= initial + 2 * added
    else:
        assert initial // 2 - 5 <= body["runs"]["value"] <= initial + 2 * added


class _CountingLock:
    """An RLock that counts profile events (work) of the thread during each outermost hold."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self._work = 0
        self._previous: object = None
        self.longest = 0

    def _count(self, frame: object, event: str, arg: object) -> None:
        self._work += 1

    def __enter__(self) -> None:
        self._lock.acquire()
        self._depth += 1
        if self._depth == 1:
            self._work = 0
            self._previous = sys.getprofile()
            sys.setprofile(self._count)

    def __exit__(self, *exc: object) -> None:
        if self._depth == 1:
            sys.setprofile(self._previous)  # type: ignore[arg-type]  # restores what getprofile returned
            self.longest = max(self.longest, self._work)
        self._depth -= 1
        self._lock.release()


async def _longest_lock_hold(store: GraphStore, path: str) -> int:
    """Request ``path`` with the store's lock counting; return its longest hold (events)."""
    counting = _CountingLock()
    store._lock = counting  # type: ignore[assignment]  # a stand-in with the same protocol
    try:
        response = await _get(_app(store), path)
    finally:
        store._lock = counting._lock
    assert response.status_code == 200
    return counting.longest


@pytest.mark.slow
@pytest.mark.parametrize("sort", ["runtime", "queue_wait"])
async def test_sorted_task_list_holds_the_store_lock_no_longer_than_the_default(
    sort: str, big_stores: dict[str, GraphStore]
) -> None:
    """The sort runs outside the lock, so a writer waits no longer than for the default list."""
    store = _big_store(big_stores, "mix")
    default = await _longest_lock_hold(store, "/api/tasks?limit=100")
    sorted_hold = await _longest_lock_hold(store, f"/api/tasks?limit=100&sort={sort}")

    assert sorted_hold <= default * 1.5, (
        f"sorted request held the lock for {sorted_hold} events "
        f"(default list: {default})"
    )


async def test_sorted_task_list_sorts_while_the_store_lock_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The O(n log n) sort is C code the profile-event count can't see: check the lock itself."""
    store = _populated(300)
    counting = _CountingLock()
    held_during_sort: list[int] = []
    real_sort = store_module._slowest_first

    def spy(ranked: list[store_module._Ranked]) -> list[TaskNode]:
        held_during_sort.append(counting._depth)
        return real_sort(ranked)

    monkeypatch.setattr(store_module, "_slowest_first", spy)
    store._lock = counting  # type: ignore[assignment]  # a stand-in with the same protocol

    response = await _get(_app(store), "/api/tasks?sort=runtime")

    assert response.status_code == 200
    assert held_during_sort == [0]
