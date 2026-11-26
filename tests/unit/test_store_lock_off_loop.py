"""The event loop never takes ``GraphStore._lock``, whatever a thread holds it for.

Threadpool scans and eviction hold the lock for hundreds of milliseconds at 30k
nodes; a route that took it on the loop thread stalled every other request and
the WebSocket pushes for the rest of the hold.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry

if TYPE_CHECKING:
    from types import TracebackType

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
GOLDEN = Path(__file__).parent.parent / "data" / "store_lock_routes.json"
GOLDEN_URLS = (
    "/api/tasks/root",
    "/api/tasks/root/children",
    "/api/graphs/root",
    "/api/health",
)
HOLD_S = 0.4
MAX_LOOP_GAP_S = 0.1
TICK_S = 0.005
# Realistic query strings per route template. Every GET route the router has needs
# an entry: a new route fails test_every_route_has_a_recorded_request until added.
QUERIES: dict[str, list[str]] = {
    "/api/health": [""],
    "/api/health/live": [""],
    "/api/tasks": [
        "",
        "?limit=50&state=FAILURE",
        "?hostname=w1@host&queue=q1&exception_type=Err",
        "?sort=runtime&limit=10",
        "?within=1h&compact=true",
        "?task_group=ungrouped",
    ],
    "/api/tasks/registry": [""],
    "/api/tasks/{task_id}": [""],
    "/api/tasks/{task_id}/children": ["", "?compact=true"],
    "/api/graphs": ["", "?within=1h"],
    "/api/graphs/{root_id}": [""],
    "/api/groups": [""],
    "/api/overview": ["?within=1h"],
    "/api/stats/timeseries": ["", "?percentiles=p95"],
    "/api/failures": [""],
    "/api/stats/tasks": [""],
    "/api/workers": [""],
    "/api/workers/{hostname}": [""],
    "/api/anomalies": [""],
}
FILL = {"task_id": "root", "root_id": "root", "hostname": "w1@host"}
# Routes that answer 404 on the seeded store (no worker with that name).
ALLOWED_404 = {"/api/workers/{hostname}"}
# Routes that never touch the store, so they have nothing to record.
NO_STORE_READ = {
    "/api/health",
    "/api/health/live",
    "/api/workers",
    "/api/workers/{hostname}",
}


class RecordingLock:
    """An RLock that records the thread of every acquire."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.acquirers: list[int] = []

    def __enter__(self) -> bool:
        self.acquirers.append(threading.get_ident())
        return self._lock.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._lock.release()

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        self.acquirers.append(threading.get_ident())
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()


def _event(task_id: str, state: TaskState, secs: int, **extra: Any) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="app.task",
        state=state,
        timestamp=NOW - timedelta(seconds=secs),
        hostname="w1@host",
        queue="q1",
        **extra,
    )


def _seeded() -> GraphStore:
    """A root with two children, one of them failed with an exception type."""
    store = GraphStore()
    store.add_event(_event("root", TaskState.STARTED, 30))
    store.add_event(_event("root", TaskState.SUCCESS, 20))
    link: dict[str, Any] = {"parent_id": "root", "root_id": "root"}
    store.add_event(_event("c0", TaskState.STARTED, 15, **link))
    store.add_event(_event("c0", TaskState.SUCCESS, 10, **link))
    store.add_event(_event("c1", TaskState.STARTED, 14, **link))
    store.add_event(_event("c1", TaskState.FAILURE, 9, exception_type="Err", **link))
    return store


def _router(store: GraphStore) -> APIRouter:
    return create_api_router(store, worker_registry=WorkerRegistry())


def _app(store: GraphStore) -> FastAPI:
    app = FastAPI()
    app.include_router(_router(store))
    return app


def _get_routes(router: APIRouter) -> list[APIRoute]:
    return [r for r in router.routes if isinstance(r, APIRoute) and "GET" in r.methods]


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


async def _get(app: FastAPI, url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(url)


def test_every_route_has_a_recorded_request() -> None:
    paths = {r.path for r in _get_routes(_router(GraphStore()))}
    assert paths == set(QUERIES)


@pytest.mark.parametrize("template", sorted(QUERIES))
async def test_route_takes_the_store_lock_off_the_loop(template: str) -> None:
    store = _seeded()
    recorder = RecordingLock()
    store._lock = recorder  # type: ignore[assignment]  # records the acquiring thread
    app = _app(store)
    want = {200, 404} if template in ALLOWED_404 else {200}
    for query in QUERIES[template]:
        url = template.format(**FILL) + query
        response = await _get(app, url)
        assert response.status_code in want, (url, response.text)
    if template not in NO_STORE_READ:
        assert recorder.acquirers, f"{template} never took the lock: vacuous check"
    on_loop = [t for t in recorder.acquirers if t == threading.get_ident()]
    assert on_loop == [], f"{template}: {len(on_loop)} store lock acquires on the loop"


async def test_loop_keeps_ticking_while_a_thread_holds_the_store_lock() -> None:
    store = _seeded()
    app = _app(store)
    holding = threading.Event()
    urls = [
        "/api/health",
        "/api/tasks/root",
        "/api/tasks/root/children",
        "/api/graphs/root",
        "/api/overview?within=1h",
        "/api/tasks",
    ]
    for url in urls:  # first requests build response models: not the lock's stall
        assert (await _get(app, url)).status_code == 200
    release = threading.Event()

    def hold() -> None:
        with store._lock:
            holding.set()
            release.wait(HOLD_S * 5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert await asyncio.to_thread(holding.wait, 5)
    gaps: list[float] = []
    running = True

    async def ticker() -> None:
        last = time.perf_counter()
        while running:
            await asyncio.sleep(TICK_S)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    requests = [asyncio.create_task(_get(app, u)) for u in urls]
    await asyncio.sleep(HOLD_S)
    release.set()
    responses = await asyncio.wait_for(asyncio.gather(*requests), 10)
    running = False
    await tick
    thread.join(5)
    assert [r.status_code for r in responses] == [200] * len(urls)
    worst = max(gaps)
    assert worst < MAX_LOOP_GAP_S, f"loop gap {worst:.3f}s during a {HOLD_S}s hold"


async def test_responses_match_the_bodies_recorded_before_the_change() -> None:
    app = _app(_seeded())
    got: dict[str, Any] = {}
    for url in GOLDEN_URLS:
        body = (await _get(app, url)).json()
        if isinstance(body, dict):
            body.pop("version", None)
        got[url] = body
    assert got == json.loads(GOLDEN.read_text())
