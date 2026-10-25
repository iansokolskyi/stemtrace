"""The summary endpoints leave the event loop free while they aggregate.

At ``max_nodes`` of 30,000 the O(n) pass held the loop for 50 ms or more, which
stalled WebSocket pushes and anything else sharing the loop.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import params, summary_routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
MAX_NODES = 30_000
# Loop work between two turns, in profile events (Python and C calls and returns) on the
# loop thread (see _longest_loop_hold). At 30k nodes a sliced turn (2,048 records) takes
# 8-29k events; the pre-#172 unsliced routes took 110-250k in one turn. The budget sits
# between.
MAX_LOOP_WORK = 60_000
PATHS = ["/api/overview?within=1h", "/api/failures", "/api/stats/timeseries"]
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
    store.add_event(
        TaskEvent(
            task_id=f"t{i}",
            name=f"app.task{i % 50}",
            state=state,
            timestamp=NOW - timedelta(seconds=seconds_ago),
            exception_type=f"Err{i % types}" if state == TaskState.FAILURE else None,
        )
    )


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
        cache[shape] = _populated(MAX_NODES, shape)
    return cache[shape]


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def _longest_loop_hold(app: FastAPI, path: str) -> int:
    """Request ``path``; return the most work the loop did between two of its turns.

    Work is the count of profile events (``sys.setprofile``: Python and C calls and
    returns) on the loop thread, not time: on a CPU-throttled or busy host a stolen or
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


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("path", [*PATHS, "/api/stats/timeseries?bucket=1m&within=2h"])
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
