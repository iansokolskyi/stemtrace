"""Group resolution over every stored task name runs off the event loop.

With thousands of distinct names it takes a noticeable time (about 80 ms at
7,000 with automatic groups), so on the loop it held up every other request.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.groups import GroupConfig, ResolvedGroup, TaskAttributes
from stemtrace.server.api import routes, summary_routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry

try:
    import uvloop
except ImportError:  # uvicorn[standard] doesn't install it on Windows
    uvloop = None

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "loop",
    [
        "asyncio",
        pytest.param(
            "uvloop",
            marks=pytest.mark.skipif(uvloop is None, reason="uvloop not installed"),
        ),
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        "/api/tasks?task_group=billing",
        "/api/graphs?tag=money",
        "/api/groups",
        "/api/tasks/registry?refresh=false",
        "/api/stats/timeseries?group_by=task_group",
    ],
)
def test_resolving_all_names_leaves_the_loop_free(
    path: str, loop: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request made while names resolve completes before resolution ends.

    Also on uvloop, which uvicorn uses when installed: its ``run_in_executor``
    (behind ``asyncio.to_thread``) warns on Python 3.14.
    """
    store = GraphStore()
    for i, name in enumerate(["billing.charge", "search.reindex", "app.tasks.add"]):
        store.add_event(
            TaskEvent(task_id=f"t{i}", name=name, state=TaskState.SUCCESS, timestamp=T0)
        )
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))

    released = threading.Event()
    waits: list[bool] = []
    real_resolve = GroupConfig.resolve

    def resolve_after_other_request(
        self: GroupConfig, task: TaskAttributes
    ) -> ResolvedGroup:
        if not waits:
            # On the loop this blocks it, so the other request can't run and
            # the wait times out (False); in a thread the other one finishes.
            waits.append(released.wait(timeout=2))
        return real_resolve(self, task)

    monkeypatch.setattr(GroupConfig, "resolve", resolve_after_other_request)

    async def scenario() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:

            async def other_request() -> int:
                await asyncio.sleep(0.05)  # let the resolving request start first
                response = await client.get("/api/health")
                released.set()
                return response.status_code

            resolving, other = await asyncio.gather(client.get(path), other_request())
            return resolving.status_code, other

    # A loop of its own, never set as current: asyncio.run() would replace the
    # current loop, which pytest-asyncio 1.1.1 on Python 3.10 leaves set and
    # never closes, and the replaced loop warns (ResourceWarning) when collected.
    event_loop = (
        uvloop.new_event_loop() if loop == "uvloop" else asyncio.new_event_loop()
    )
    try:
        result = event_loop.run_until_complete(scenario())
    finally:
        event_loop.close()

    assert result == (200, 200)
    assert waits == [True]


@pytest.mark.parametrize(
    ("module", "path", "filtered", "work"),
    [
        (routes, "/api/tasks", "task_group=billing", "_group_task_names"),
        (routes, "/api/graphs", "task_group=billing", "_group_task_names"),
        (summary_routes, "/api/stats/timeseries", "group_by=task_group", "time_series"),
    ],
)
def test_unfiltered_lists_never_wait_for_a_worker_thread(
    module: Any, path: str, filtered: str, work: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the host app's sync handlers holding every thread, they'd stall."""
    store = GraphStore()
    store.add_event(
        TaskEvent(
            task_id="t", name="billing.charge", state=TaskState.SUCCESS, timestamp=T0
        )
    )
    calls: list[str] = []
    real = module.run_in_threadpool

    async def counting(func, *args, **kwargs):  # type: ignore[no-untyped-def]  # stand-in
        calls.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(module, "run_in_threadpool", counting)
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))

    with TestClient(app) as client:
        assert client.get(path).status_code == 200
        assert calls == []
        assert client.get(f"{path}?{filtered}").status_code == 200
        assert calls == [work]
