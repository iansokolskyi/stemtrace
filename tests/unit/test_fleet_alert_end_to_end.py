"""The fleet item through ``GET /api/anomalies``: route alone, then a real consumer.

The end-to-end tests run a real consumer, store, registry and the route's inspect
refresher; only the Celery ``Inspect`` and the transport are fakes (Celery 5.6.3
``stats()`` returns ``{nodename: ...}``, None for no reply, and raises when the
broker is unreachable).
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.server.api import routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.store import GraphStore, WorkerRegistry, WorkerTimeline
from tests.unit.test_anomaly_unregistered_end_to_end import (
    BROKER,
    NODE,
    _Inspect,
    fed,
    ready,
    waiting,
)

FLEET = "no_workers_online"


def body_of(client: TestClient, query: str = "") -> dict[str, Any]:
    """``GET /api/anomalies`` as JSON."""
    response = client.get(f"/api/anomalies{query}")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def items_of(client: TestClient) -> list[tuple[str, str | None, str | None, int]]:
    """The items as (kind, task_id, name, task_count)."""
    return [
        (a["kind"], a["task_id"], a["name"], a["task_count"])
        for a in body_of(client)["anomalies"]
    ]


def stopped(node: str = NODE, pid: int = 11) -> WorkerEvent:
    """A ``worker_shutdown`` stamped now."""
    return WorkerEvent(
        event_type=WorkerEventType.WORKER_SHUTDOWN,
        hostname=node,
        pid=pid,
        timestamp=datetime.now(timezone.utc),
    )


def ready_long_ago(minutes: float = 10) -> WorkerEvent:
    """A ``worker_ready`` for ``NODE`` stamped ``minutes`` ago (its last sign of life)."""
    return ready("app.known").model_copy(
        update={"timestamp": datetime.now(timezone.utc) - timedelta(minutes=minutes)}
    )


def task_from(task_id: str, name: str = "app.known") -> TaskEvent:
    """A STARTED task event from ``NODE`` (a worker nobody registered)."""
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=TaskState.STARTED,
        timestamp=datetime.now(timezone.utc),
        hostname=NODE,
    )


_EXTENSIONS: list[StemtraceExtension] = []


@pytest.fixture(autouse=True)
def _stop_probes() -> Iterator[None]:
    # The client never enters the extension's lifespan, so stop each probe thread a
    # worker_ready started; a leftover thread breaks later tests in the same process.
    yield
    while _EXTENSIONS:
        _EXTENSIONS.pop()._inspect_probe.stop()


def extension_client(
    monkeypatch: pytest.MonkeyPatch, inspector: _Inspect | None
) -> tuple[StemtraceExtension, TestClient]:
    monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: inspector)
    extension = StemtraceExtension(broker_url=BROKER, serve_ui=False)
    _EXTENSIONS.append(extension)
    app = FastAPI()
    app.include_router(extension.router)
    return extension, TestClient(app)


# --- The route over a registry (no consumer) -----------------------------------------


def test_route_reports_the_fleet_item_and_no_per_name_item_or_node_flag() -> None:
    store, registry = GraphStore(), WorkerRegistry()
    registry.register_worker(NODE, 11, ["app.known"])
    registry.mark_shutdown(NODE, 11)
    for event in (waiting("a", "app.known"), waiting("b", "app.missing", minutes=30)):
        store.add_event(event)
    app = FastAPI()
    app.include_router(
        create_api_router(
            store,
            worker_registry=registry,
            broker_url=None,
            worker_timeline=WorkerTimeline(),
        )
    )
    client = TestClient(app)

    body = body_of(client)
    only = body_of(client, f"?kind={FLEET}")

    assert [
        (a["kind"], a["task_id"], a["name"], a["task_count"]) for a in body["anomalies"]
    ] == [(FLEET, None, None, 2)]
    assert body["counts"] == {
        "stuck": 0,
        "interrupted": 0,
        "unregistered": 0,
        FLEET: 1,
    }
    assert body["total"] == 1
    assert only["total"] == 1
    assert body_of(client, "?kind=unregistered")["anomalies"] == []
    assert client.get("/api/tasks/a").json()["task"]["anomaly"] is None
    assert client.get("/api/tasks/b").json()["task"]["anomaly"] is None


def test_route_reports_no_fleet_item_before_any_worker_was_seen() -> None:
    store, registry = GraphStore(), WorkerRegistry()
    store.add_event(waiting("a", "app.known"))
    app = FastAPI()
    app.include_router(
        create_api_router(store, worker_registry=registry, broker_url=None)
    )

    assert items_of(TestClient(app)) == []


# --- A real consumer, store, registry and refresher ----------------------------------


def test_e2e_a_worker_that_reports_its_stop_gives_the_item_with_no_broker_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension, client = extension_client(monkeypatch, None)
    consumer = extension.consumer
    assert consumer is not None
    stream: list[TaskEvent | WorkerEvent] = [
        ready("app.known"),
        waiting("a", "app.known"),
        waiting("b", "app.missing", minutes=30),
        stopped(),
    ]

    with fed(consumer, stream):
        assert items_of(client) == [("no_workers_online", None, None, 2)]
        assert client.get("/api/tasks/a").json()["task"]["anomaly"] is None


def test_e2e_a_worker_known_only_from_task_events_that_stops_cleanly_gives_the_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension, client = extension_client(monkeypatch, None)
    consumer = extension.consumer
    assert consumer is not None
    stream: list[TaskEvent | WorkerEvent] = [
        task_from("t1"),
        waiting("a", "app.known"),
    ]

    with fed(consumer, stream):
        # Alive, no names, no inspect: the per-name gate stays closed and no item fires.
        assert items_of(client) == []
        extension.worker_registry.mark_shutdown(NODE, 11)
        assert items_of(client) == [("no_workers_online", None, None, 1)]
        assert extension.worker_registry.get_all_workers() == []


def test_e2e_a_silent_fleet_after_a_completed_inspect_gives_the_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension, client = extension_client(
        monkeypatch, _Inspect(stats=None, registered=None, ping=None)
    )
    consumer = extension.consumer
    assert consumer is not None
    stream: list[TaskEvent | WorkerEvent] = [
        ready_long_ago(),
        waiting("a", "app.known"),
    ]

    with fed(consumer, stream):
        # Some worker answered an inspect earlier, so the server can hear workers.
        registry = extension.worker_registry
        registry.apply_stats_reply({}, {"celery@probe": (9, [])})
        registry.mark_shutdown("celery@probe", 9)
        assert items_of(client) == [("no_workers_online", None, None, 1)]


def test_e2e_a_silent_fleet_whose_inspect_is_unreachable_gives_no_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension, client = extension_client(monkeypatch, None)
    consumer = extension.consumer
    assert consumer is not None
    stream: list[TaskEvent | WorkerEvent] = [
        ready_long_ago(),
        waiting("a", "app.known"),
    ]

    with fed(consumer, stream):
        assert items_of(client) == []


def test_e2e_a_worker_that_answers_the_inspect_keeps_the_fleet_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answered = _Inspect(
        stats={NODE: {"pid": 11}}, registered={NODE: ["app.known"]}, ping=None
    )
    extension, client = extension_client(monkeypatch, answered)
    consumer = extension.consumer
    assert consumer is not None
    stream: list[TaskEvent | WorkerEvent] = [
        ready_long_ago(),
        waiting("a", "app.known"),
        waiting("b", "app.missing"),
    ]

    with fed(consumer, stream):
        assert items_of(client) == [("unregistered", None, "app.missing", 1)]
