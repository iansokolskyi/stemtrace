"""queue_wait_ms, runtime_ms and total_runtime_ms in API responses."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

T0 = datetime(2026, 10, 17, tzinfo=timezone.utc)
TIMING = ("duration_ms", "queue_wait_ms", "runtime_ms", "total_runtime_ms")


@pytest.fixture
def client() -> TestClient:
    """A client over a store with one retried task in a group, and one running."""
    store = GraphStore()
    events = [
        ("done", TaskState.PENDING, 0, 0),
        ("done", TaskState.STARTED, 1, 0),
        ("done", TaskState.RETRY, 2, 0),
        ("done", TaskState.STARTED, 12, 1),
        ("done", TaskState.SUCCESS, 15, 1),
        ("sibling", TaskState.SUCCESS, 15, 0),
        ("running", TaskState.PENDING, 0, 0),
        ("running", TaskState.STARTED, 4, 0),
    ]
    for task_id, state, at, retries in events:
        store.add_event(
            TaskEvent(
                task_id=task_id,
                name="app.encode",
                state=state,
                timestamp=T0 + timedelta(seconds=at),
                retries=retries,
                group_id="g" if task_id != "running" else None,
            )
        )
    app = FastAPI()
    app.include_router(create_api_router(store))
    return TestClient(app)


def _timing(payload: dict[str, object]) -> tuple[object, ...]:
    return tuple(payload[key] for key in TIMING)


def test_task_list_and_detail_split_duration(client: TestClient) -> None:
    tasks = {t["task_id"]: t for t in client.get("/api/tasks").json()["tasks"]}
    detail = client.get("/api/tasks/done").json()["task"]

    assert _timing(tasks["done"]) == (15000, 1000, 3000, 4000)
    assert _timing(detail) == _timing(tasks["done"])
    assert _timing(tasks["running"]) == (4000, 4000, None, None)


def test_graph_nodes_split_duration_and_group_has_none(client: TestClient) -> None:
    nodes = client.get("/api/graphs/group:g").json()["nodes"]
    group = nodes["group:g"]

    assert _timing(nodes["done"]) == (15000, 1000, 3000, 4000)
    assert group["node_type"] == "GROUP"
    assert _timing(group)[1:] == (None, None, None)
