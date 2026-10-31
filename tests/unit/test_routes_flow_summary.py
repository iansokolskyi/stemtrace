"""``flow`` on the items of ``GET /api/graphs``: totals over each root's whole graph."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(
    task_id: str,
    state: TaskState,
    seconds: int,
    *,
    parent_id: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.task",
        state=state,
        timestamp=_T0 + timedelta(seconds=seconds),
        parent_id=parent_id,
    )


def _client(store: GraphStore) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(store))
    return TestClient(app)


def _store(*captures: str) -> GraphStore:
    store = GraphStore()
    for capture in captures:
        for raw in _CAPTURES[capture]:
            store.add_event(TaskEvent.model_validate(raw))
    return store


def test_list_graphs_items_carry_the_flow_summary() -> None:
    graphs = _client(_store("chord_failed_header")).get("/api/graphs").json()["graphs"]

    assert [g["flow"] for g in graphs] == [
        {
            "task_count": 2,
            "state_counts": {"SUCCESS": 1, "FAILURE": 1},
            "not_run_count": 1,
            "state": "FAILURE",
        }
    ]


def test_list_graphs_flow_state_is_worse_than_a_succeeded_root() -> None:
    store = GraphStore()
    store.add_event(_event("root", TaskState.SUCCESS, 0))
    store.add_event(_event("child", TaskState.FAILURE, 1, parent_id="root"))
    store.add_event(_event("grand", TaskState.STARTED, 2, parent_id="child"))

    (graph,) = _client(store).get("/api/graphs").json()["graphs"]

    assert graph["state"] == "SUCCESS"
    assert graph["flow"] == {
        "task_count": 3,
        "state_counts": {"SUCCESS": 1, "STARTED": 1, "FAILURE": 1},
        "not_run_count": 0,
        "state": "FAILURE",
    }


def test_list_graphs_summarizes_each_listed_flow_by_its_own_graph() -> None:
    client = _client(_store("chain", "chord_rejected_header", "group"))

    graphs = client.get("/api/graphs").json()["graphs"]

    flows = sorted((g["flow"] for g in graphs), key=lambda f: f["state"])
    assert flows == [
        {
            "task_count": 2,
            "state_counts": {"SUCCESS": 1, "REJECTED": 1},
            "not_run_count": 1,
            "state": "FAILURE",
        },
        {
            "task_count": 3,
            "state_counts": {"SUCCESS": 3},
            "not_run_count": 0,
            "state": "SUCCESS",
        },
        {
            "task_count": 3,
            "state_counts": {"SUCCESS": 3},
            "not_run_count": 0,
            "state": "SUCCESS",
        },
    ]


def test_get_graph_nodes_have_no_flow() -> None:
    client = _client(_store("chord_failed_header"))
    (root,) = (g["task_id"] for g in client.get("/api/graphs").json()["graphs"])

    nodes = client.get(f"/api/graphs/{root}").json()["nodes"]

    assert len(nodes) == 3
    assert [n["flow"] for n in nodes.values()] == [None, None, None]


def test_list_graphs_pagination_pages_flows_and_keeps_the_total() -> None:
    store = GraphStore()
    for i, state in enumerate(
        (TaskState.SUCCESS, TaskState.FAILURE, TaskState.REVOKED)
    ):
        store.add_event(_event(f"root-{i}", state, i))
    client = _client(store)

    pages = [
        client.get(f"/api/graphs?limit=1&offset={offset}").json() for offset in range(4)
    ]

    assert [p["total"] for p in pages] == [3, 3, 3, 3]
    # Most recent first
    assert [[g["task_id"] for g in p["graphs"]] for p in pages] == [
        ["root-2"],
        ["root-1"],
        ["root-0"],
        [],
    ]
    assert [g["flow"]["state"] for p in pages for g in p["graphs"]] == [
        "REVOKED",
        "FAILURE",
        "SUCCESS",
    ]


@pytest.mark.parametrize("query", ["from_date=2025-01-01", "to_date=2030-01-01"])
def test_list_graphs_date_filters_still_list_flows_with_a_summary(query: str) -> None:
    graphs = _client(_store("chain")).get(f"/api/graphs?{query}").json()["graphs"]

    assert [g["flow"]["task_count"] for g in graphs] == [3]


def test_list_graphs_filtered_out_everything_lists_nothing() -> None:
    body = _client(_store("chain")).get("/api/graphs?to_date=2000-01-01").json()

    assert body["graphs"] == []
    assert body["total"] == 0
