"""Celery built-in tasks (``celery.*``) in the task registry.

Workers never register ``celery.*`` names (``library/signals.py`` filters them), so the
registry must not report built-ins the store saw as ``not_registered``.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

_FLOWS: dict[str, list[dict[str, object]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_CAPTURED_BUILTINS = {"celery.chord_unlock"}  # the only built-in kept as a TASK node
_BUILTINS = [
    "celery.accumulate",
    "celery.backend_cleanup",
    "celery.chain",
    "celery.chord",
    "celery.chord_unlock",
    "celery.chunks",
    "celery.group",
    "celery.map",
    "celery.starmap",
]


def _client(store: GraphStore) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(store))
    return TestClient(app)


def _run(store: GraphStore, task_id: str, name: str) -> None:
    store.add_event(
        TaskEvent(
            task_id=task_id,
            name=name,
            state=TaskState.SUCCESS,
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
    )


def _registry_names(client: TestClient, query: str = "") -> list[str]:
    response = client.get(f"/api/tasks/registry?refresh=false{query}")
    assert response.status_code == 200
    return [t["name"] for t in response.json()["tasks"]]


def test_registry_with_real_capture_has_no_not_registered_builtins() -> None:
    store = GraphStore()
    for events in _FLOWS.values():
        for raw in events:
            store.add_event(TaskEvent.model_validate(raw))
    names = {n for n in store.get_unique_task_names() if n.startswith("celery.")}
    assert names >= _CAPTURED_BUILTINS  # the capture really holds the built-ins

    client = _client(store)
    flagged = _registry_names(client, "&status=not_registered")
    assert [n for n in flagged if n.startswith("celery.")] == []
    assert [n for n in _registry_names(client) if n.startswith("celery.")] == []


@pytest.mark.parametrize("name", _BUILTINS)
def test_registry_excludes_celery_builtin_the_store_saw(name: str) -> None:
    store = GraphStore()
    _run(store, "t1", name)
    _run(store, "t2", "myapp.tasks.real")
    client = _client(store)

    assert _registry_names(client) == ["myapp.tasks.real"]
    assert _registry_names(client, f"&query={name}") == []


def test_registry_regular_task_without_worker_is_still_not_registered() -> None:
    store = GraphStore()
    _run(store, "t1", "myapp.tasks.real")
    _run(store, "t2", "celery.chord_unlock")

    response = _client(store).get("/api/tasks/registry?refresh=false")

    tasks = response.json()["tasks"]
    assert [(t["name"], t["status"]) for t in tasks] == [
        ("myapp.tasks.real", "not_registered")
    ]


def test_registry_keeps_user_task_that_only_contains_celery_in_its_name() -> None:
    store = GraphStore()
    _run(store, "t1", "myapp.celery.cleanup")
    _run(store, "t2", "celeryish.task")

    assert _registry_names(_client(store)) == ["celeryish.task", "myapp.celery.cleanup"]
