"""Shared harness for the anomaly API tests: a seeded store, a timeline and an app."""

import time
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.anomalies import AnomalySettings
from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry, WorkerTimeline
from tests.unit.anomaly_pipeline import (
    READY,
    SHUTDOWN,
    SMALL,
    at,
    task_event,
    worker_event,
)


class ReplayTransport:
    """Hands a consumer its events once, then waits until stopped."""

    def __init__(self, events: list[TaskEvent | WorkerEvent]) -> None:
        self._events = events
        self.consumed = 0
        self._stop = False

    def consume(self) -> Iterator[TaskEvent | WorkerEvent]:
        for event in self._events:
            yield event
            self.consumed += 1
        while not self._stop:
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop = True


# "Now" of every API test: 1000 s after T0, so a task STARTED at T0 has run for
# 1000 s, well past SMALL's 300 s ceiling.
NOW_S = 1000


def freeze_clock(monkeypatch: pytest.MonkeyPatch, seconds: float = NOW_S) -> None:
    """Make the API's clock read ``seconds`` after T0."""
    now = at(seconds)
    monkeypatch.setattr(params, "utcnow", lambda: now)


def seeded_events() -> tuple[list[TaskEvent], list[WorkerEvent]]:
    """Five anomalies and one healthy task, in the order ``since`` sorts them.

    stuck s0 and s1 (same ``since``, so the task ID breaks the tie), interrupted
    i1 (since 50) and i2 (since 60), stuck s2 (since 100); ``ok`` finished.
    """
    tasks = [
        task_event("s1", TaskState.STARTED, 0, name="app.slow", host="w@a"),
        task_event("s0", TaskState.STARTED, 0, name="app.slow", host="w@a"),
        task_event("s2", TaskState.STARTED, 100, name="app.other", host="w@b"),
        task_event("i1", TaskState.STARTED, 10, name="app.work", host="w@c"),
        task_event("i2", TaskState.STARTED, 20, name="app.work", host="w@d"),
        task_event("ok", TaskState.STARTED, 5, name="app.work", host="w@e"),
        task_event("ok", TaskState.SUCCESS, 6, name="app.work", host="w@e"),
    ]
    workers = [
        worker_event(SHUTDOWN, 50, host="w@c"),
        worker_event(READY, 60, host="w@d"),
    ]
    return tasks, workers


def seeded() -> tuple[GraphStore, WorkerTimeline]:
    """A store and a timeline holding ``seeded_events``."""
    store = GraphStore()
    timeline = WorkerTimeline()
    tasks, workers = seeded_events()
    for event in tasks:
        store.add_event(event)
    for worker in workers:
        timeline.record(worker)
    return store, timeline


def make_app(
    store: GraphStore,
    timeline: WorkerTimeline | None = None,
    settings: AnomalySettings = SMALL,
    registry: WorkerRegistry | None = None,
    broker_url: str | None = None,
) -> FastAPI:
    """An app serving the API over ``store``, ``timeline``, ``registry`` and ``settings``."""
    app = FastAPI()
    app.include_router(
        create_api_router(
            store,
            worker_registry=registry,
            broker_url=broker_url,
            worker_timeline=timeline,
            anomaly_settings=settings,
        )
    )
    return app


async def get(app: FastAPI, path: str) -> httpx.Response:
    """GET ``path`` from ``app``."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)
