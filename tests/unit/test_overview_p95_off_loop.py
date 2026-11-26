"""The p95 values are sorted in a worker thread, not on the event loop.

Like ``test_task_stats_off_loop.py``: no wall-time assertion. ``p95`` is replaced by a
probe that records its thread and blocks until the (still free) loop releases it.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import params, summary_routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.stats import p95
from stemtrace.server.store import GraphStore, WorkerRegistry

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


def _store() -> GraphStore:
    store = GraphStore()
    for i in range(10):
        end = NOW - timedelta(minutes=i + 1)
        for state, at in [
            (TaskState.PENDING, end - timedelta(seconds=3)),
            (TaskState.STARTED, end - timedelta(seconds=2)),
            (TaskState.SUCCESS, end),
        ]:
            store.add_event(
                TaskEvent(task_id=f"t{i}", name="app.work", state=state, timestamp=at)
            )
    return store


@pytest.mark.parametrize(
    "path",
    ["/api/overview?within=1h", "/api/stats/timeseries?within=1h&percentiles=p95"],
)
async def test_p95_sorts_off_the_event_loop(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    app.include_router(create_api_router(_store(), worker_registry=WorkerRegistry()))
    loop_thread = threading.get_ident()
    loop = asyncio.get_running_loop()
    released = threading.Event()
    threads: set[int] = set()
    waited: list[bool] = []

    def probe(samples: list[int]) -> Any:
        threads.add(threading.get_ident())
        # The first call blocks this thread until the event loop, still running, sets
        # the flag; later ones (one per bucket) pass at once.
        if not waited:
            waited.append(released.wait(timeout=2))
        return p95(samples)

    monkeypatch.setattr(summary_routes, "p95", probe)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = asyncio.create_task(client.get(path))
        # Only a free loop gets here while the sort is blocked.
        await asyncio.sleep(0.05)
        loop.call_soon(released.set)
        response = await request

    assert response.status_code == 200
    assert threads
    assert loop_thread not in threads
    assert all(waited)
