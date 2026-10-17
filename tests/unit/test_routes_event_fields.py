"""exception_type, hostname, queue and routing_key in API responses and task filters."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

T0 = datetime(2026, 10, 17, tzinfo=timezone.utc)
FIELDS = {
    "exception_type": "myapp.errors.PartnerAPIError",
    "hostname": "celery@media-gpu-1",
    "queue": "media",
    "routing_key": "media",
}


@pytest.fixture
def store() -> GraphStore:
    """A store holding a failed task on media and a succeeded one on default."""
    store = GraphStore()
    events = [
        ("failed", TaskState.STARTED, {"hostname": "celery@media-gpu-1", "queue": "media", "routing_key": "media"}),
        ("failed", TaskState.FAILURE, {"hostname": "celery@media-gpu-1", "exception_type": "myapp.errors.PartnerAPIError"}),
        ("ok", TaskState.STARTED, {"hostname": "celery@web-1", "queue": "celery", "routing_key": "celery"}),
        ("ok", TaskState.SUCCESS, {"hostname": "celery@web-1"}),
        ("old", TaskState.STARTED, {}),
    ]  # fmt: skip
    for second, (task_id, state, fields) in enumerate(events):
        store.add_event(
            TaskEvent(
                task_id=task_id,
                name="app.encode",
                state=state,
                timestamp=T0 + timedelta(seconds=second),
                **fields,
            )
        )
    return store


@pytest.fixture
def client(store: GraphStore) -> TestClient:
    """A client for the API router over the store."""
    app = FastAPI()
    app.include_router(create_api_router(store))
    return TestClient(app)


def _fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in FIELDS}


class TestResponses:
    """The node fields and the event fields are served."""

    def test_task_detail_has_node_and_event_fields(self, client: TestClient) -> None:
        task = client.get("/api/tasks/failed").json()["task"]

        assert _fields(task) == FIELDS
        assert [_fields(e) for e in task["events"]] == [
            {
                "exception_type": None,
                "hostname": "celery@media-gpu-1",
                "queue": "media",
                "routing_key": "media",
            },
            {
                "exception_type": "myapp.errors.PartnerAPIError",
                "hostname": "celery@media-gpu-1",
                "queue": None,
                "routing_key": None,
            },
        ]

    def test_task_list_has_node_fields(self, client: TestClient) -> None:
        tasks = client.get("/api/tasks").json()["tasks"]

        assert {t["task_id"]: _fields(t) for t in tasks} == {
            "failed": FIELDS,
            "ok": {
                "exception_type": None,
                "hostname": "celery@web-1",
                "queue": "celery",
                "routing_key": "celery",
            },
            "old": dict.fromkeys(FIELDS),
        }

    def test_graph_node_has_node_fields(self, client: TestClient) -> None:
        nodes = client.get("/api/graphs/failed").json()["nodes"]

        assert _fields(nodes["failed"]) == FIELDS


class TestFilters:
    """hostname, queue and exception_type filter the task list exactly."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ({"hostname": "celery@media-gpu-1"}, ["failed"]),
            ({"hostname": "celery@web-1"}, ["ok"]),
            ({"hostname": "celery@media"}, []),
            ({"queue": "media"}, ["failed"]),
            ({"queue": "celery"}, ["ok"]),
            ({"exception_type": "myapp.errors.PartnerAPIError"}, ["failed"]),
            ({"exception_type": "PartnerAPIError"}, []),
            ({"queue": "media", "state": "SUCCESS"}, []),
            ({"queue": "media", "hostname": "celery@media-gpu-1"}, ["failed"]),
            ({}, ["old", "ok", "failed"]),
        ],
    )
    def test_filter(
        self, client: TestClient, query: dict[str, str], expected: list[str]
    ) -> None:
        response = client.get("/api/tasks", params=query).json()

        assert [t["task_id"] for t in response["tasks"]] == expected
        assert response["total"] == len(expected)
