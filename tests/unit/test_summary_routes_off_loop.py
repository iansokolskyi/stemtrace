"""The summary endpoints leave the event loop free while they aggregate.

At ``max_nodes`` of 30,000 the O(n) pass held the loop for 50 ms or more, which
stalled WebSocket pushes and anything else sharing the loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
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
MAX_LOOP_LAG_S = 0.020
# The overview is two cheap passes: unsliced, 30k nodes hold the loop 13-15 ms
# (sliced: 2-3 ms), under MAX_LOOP_LAG_S, so it gets a tighter bound to guard the route.
OVERVIEW_MAX_LOOP_LAG_S = 0.007
LOOP_LAG_RUNS = 3
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


async def _longest_loop_hold(app: FastAPI, path: str) -> float:
    """Request ``path`` and return the longest stretch the loop went without a turn."""
    done = asyncio.Event()
    longest = 0.0

    async def probe() -> None:
        nonlocal longest
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0)
            now = time.perf_counter()
            longest = max(longest, now - last)
            last = now

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
    """The longest stretch the loop is held, while the request runs, stays short."""
    app = _app(_big_store(big_stores, shape))
    # The first request builds the app's middleware stack (about 70 ms): not ours.
    assert (await _get(app, "/api/health")).status_code == 200
    # A loop held by the aggregation shows in every run; a preempted CPU (parallel
    # test workers) inflates one run, so the best of a few is the signal.
    holds = [await _longest_loop_hold(app, path) for _ in range(LOOP_LAG_RUNS)]
    longest = min(holds)
    budget = OVERVIEW_MAX_LOOP_LAG_S if "/overview" in path else MAX_LOOP_LAG_S
    assert longest <= budget, (
        f"loop held for {longest * 1000:.1f} ms (max {budget * 1000:.0f})"
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
