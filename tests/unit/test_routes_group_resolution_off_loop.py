"""Group resolution over every stored task name runs off the event loop.

With thousands of distinct names it takes a noticeable time (about 128 ms at
7,000), so on the loop it held up every other request meanwhile.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.groups import GroupConfig, ResolvedGroup, TaskAttributes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "path",
    [
        "/api/tasks?task_group=billing",
        "/api/graphs?tag=money",
        "/api/groups",
        "/api/tasks/registry?refresh=false",
    ],
)
def test_resolving_all_names_leaves_the_loop_free(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request made while names resolve completes before resolution ends."""
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

    assert asyncio.run(scenario()) == (200, 200)
    assert waits == [True]
