"""``/api/stats/tasks`` sorts runtimes in a worker thread, outside the store lock.

Like ``test_summary_routes_off_loop.py``, no assertion depends on wall time: the
aggregate itself is replaced by a probe that records where it ran, and blocks
until the event loop (still free) releases it.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import params, routes, summary_routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.stats import task_stats
from stemtrace.server.store import GraphStore, WorkerRegistry

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


def _store() -> GraphStore:
    store = GraphStore()
    for i in range(20):
        store.add_event(
            TaskEvent(
                task_id=f"t{i}",
                name=f"app.task{i % 3}",
                state=TaskState.SUCCESS,
                timestamp=NOW - timedelta(seconds=i),
            )
        )
    return store


async def test_aggregate_runs_off_the_event_loop_and_without_the_store_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store()
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))
    loop_thread = threading.get_ident()
    loop = asyncio.get_running_loop()
    released = threading.Event()
    seen: dict[str, Any] = {}

    def probe(*args: Any, **kwargs: Any) -> Any:
        seen["thread"] = threading.get_ident()
        # The consumer thread must be able to ingest while we aggregate.
        got = store._lock.acquire(timeout=1)
        if got:
            store._lock.release()
        seen["lock_free"] = got
        # Blocks this thread until the event loop, still running, sets the flag.
        seen["released"] = released.wait(timeout=5)
        return task_stats(*args, **kwargs)

    monkeypatch.setattr(summary_routes, "task_stats", probe)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = asyncio.create_task(client.get("/api/stats/tasks"))
        # Only a free loop gets here while the aggregate is blocked.
        await asyncio.sleep(0.05)
        loop.call_soon(released.set)
        response = await request

    assert response.status_code == 200
    assert seen["thread"] != loop_thread
    assert seen["lock_free"] is True
    assert seen["released"] is True
    assert len(response.json()["tasks"]) == 3


async def test_endpoint_never_triggers_a_celery_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/workers`` asks the workers when a broker is set; the stats don't."""
    inspected: list[str] = []

    def spy(broker_url: str, **kwargs: Any) -> None:
        inspected.append(broker_url)

    monkeypatch.setattr(routes, "_get_inspector", spy)
    app = FastAPI()
    app.include_router(
        create_api_router(
            _store(), worker_registry=WorkerRegistry(), broker_url="memory://"
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/stats/tasks")
        assert (response.status_code, inspected) == (200, [])
        # The spy can see an inspect at all: /workers triggers one.
        await client.get("/api/workers")

    assert inspected == ["memory://"]
