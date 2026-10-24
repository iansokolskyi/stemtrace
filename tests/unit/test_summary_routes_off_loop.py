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
PATHS = ["/api/overview?within=1h", "/api/failures", "/api/stats/timeseries"]
_STATES = [TaskState.FAILURE, TaskState.SUCCESS, TaskState.SUCCESS, TaskState.REVOKED]


def _add(store: GraphStore, i: int, *, seconds_ago: int) -> None:
    state = _STATES[i % len(_STATES)]
    store.add_event(
        TaskEvent(
            task_id=f"t{i}",
            name=f"app.task{i % 50}",
            state=state,
            timestamp=NOW - timedelta(seconds=seconds_ago),
            exception_type=f"Err{i % 7}" if state == TaskState.FAILURE else None,
        )
    )


def _populated(count: int) -> GraphStore:
    store = GraphStore(max_nodes=count + 5_000)
    for i in range(count):
        _add(store, i, seconds_ago=(i * 7) % 7200)
    return store


def _app(store: GraphStore) -> FastAPI:
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))
    return app


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


@pytest.fixture(scope="module")
def big_store() -> GraphStore:
    return _populated(MAX_NODES)


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.slow
@pytest.mark.parametrize("path", PATHS)
async def test_summary_request_at_30k_nodes_keeps_loop_lag_under_20ms(
    path: str, big_store: GraphStore
) -> None:
    """The longest stretch the loop is held, while the request runs, stays short."""
    app = _app(big_store)
    # The first request builds the app's middleware stack (about 70 ms): not ours.
    assert (await _get(app, "/api/health")).status_code == 200
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
    assert longest <= MAX_LOOP_LAG_S, f"loop held for {longest * 1000:.1f} ms"


@pytest.mark.parametrize("path", [*PATHS, "/api/stats/timeseries?bucket=1m&within=2h"])
@pytest.mark.parametrize("slice_size", [1, 7, 500])
async def test_summary_response_is_the_same_whatever_the_slice_size(
    path: str, slice_size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One slice is the unsliced computation; smaller slices give the same bytes."""
    app = _app(_populated(1_500))
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
