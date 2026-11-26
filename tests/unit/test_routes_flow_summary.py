"""``flow`` on the items of ``GET /api/graphs``: totals over each root's whole graph."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import routes
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
            "member_count": 2,
            "member_name": None,
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
        "member_count": None,
        "member_name": None,
    }


def test_list_graphs_summarizes_each_listed_flow_by_its_own_graph() -> None:
    client = _client(_store("chain", "chord_rejected_header", "group"))

    graphs = client.get("/api/graphs").json()["graphs"]

    flows = sorted(
        (g["flow"] for g in graphs), key=lambda f: (f["state"], f["member_count"] or 0)
    )
    assert flows == [
        {
            "task_count": 2,
            "state_counts": {"SUCCESS": 1, "REJECTED": 1},
            "not_run_count": 1,
            "state": "FAILURE",
            "member_count": 2,
            "member_name": None,
        },
        {
            "task_count": 3,
            "state_counts": {"SUCCESS": 3},
            "not_run_count": 0,
            "state": "SUCCESS",
            "member_count": None,
            "member_name": None,
        },
        {
            "task_count": 3,
            "state_counts": {"SUCCESS": 3},
            "not_run_count": 0,
            "state": "SUCCESS",
            "member_count": 3,
            "member_name": "add",
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


@pytest.mark.parametrize(
    "query",
    [
        {"from_date": "2025-01-01T00:00:00+00:00"},
        {"to_date": "2030-01-01T00:00:00+00:00"},
    ],
)
def test_list_graphs_date_filters_still_list_flows_with_a_summary(
    query: dict[str, str],
) -> None:
    graphs = _client(_store("chain")).get("/api/graphs", params=query).json()["graphs"]

    assert [g["flow"]["task_count"] for g in graphs] == [3]


def test_list_graphs_filtered_out_everything_lists_nothing() -> None:
    body = (
        _client(_store("chain"))
        .get("/api/graphs", params={"to_date": "2000-01-01T00:00:00+00:00"})
        .json()
    )

    assert body["graphs"] == []
    assert body["total"] == 0


def _member_event(
    task_id: str,
    name: str,
    seconds: int,
    *,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=TaskState.SUCCESS,
        timestamp=_T0 + timedelta(seconds=seconds),
        group_id="g1",
        args=args,
        kwargs=kwargs,
    )


def _member_fields_listed(
    members: list[TaskEvent], key: str | None
) -> tuple[int | None, str | None]:
    store = GraphStore()
    for member in members:
        store.add_event(member)
    app = FastAPI()
    app.include_router(create_api_router(store, node_alias_from_arguments=key))
    (graph,) = TestClient(app).get("/api/graphs").json()["graphs"]
    return graph["flow"]["member_count"], graph["flow"]["member_name"]


def test_list_graphs_member_name_uses_the_alias_when_members_alias_to_one_value() -> (
    None
):
    """Matrix 9: the page draws the alias, so the title names the alias's last segment."""
    members = [
        _member_event("a", "app.x", 0, kwargs={"site": "shop.eu"}),
        _member_event("b", "app.y", 1, kwargs={"site": "shop.eu"}),
    ]

    assert _member_fields_listed(members, "site") == (2, "eu")
    assert _member_fields_listed(members, None) == (2, None)


def test_list_graphs_member_name_with_positional_alias_key() -> None:
    members = [
        _member_event("a", "app.x", 0, args=["same"]),
        _member_event("b", "app.y", 1, args=["same"]),
    ]

    assert _member_fields_listed(members, "0") == (2, "same")


def test_list_graphs_member_name_is_none_when_aliases_differ() -> None:
    members = [
        _member_event("a", "app.x", 0, kwargs={"site": "eu"}),
        _member_event("b", "app.x", 1, kwargs={"site": "us"}),
    ]

    assert _member_fields_listed(members, "site") == (2, None)
    assert _member_fields_listed(members, None) == (2, "x")


def test_list_graphs_member_name_falls_back_to_the_task_name_when_the_key_is_missing() -> (
    None
):
    same = [
        _member_event("a", "app.fetch", 0, kwargs={"site": "app.fetch"}),
        _member_event("b", "app.fetch", 1),
    ]
    other = [
        _member_event("a", "app.fetch", 0, kwargs={"site": "eu"}),
        _member_event("b", "app.fetch", 1, kwargs={"other": "eu"}),
    ]

    assert _member_fields_listed(same, "site") == (2, "fetch")
    assert _member_fields_listed(other, "site") == (2, None)


def test_list_graphs_resolves_aliases_outside_the_store_lock_and_stops_at_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving a name reads and stringifies task arguments: never while the lock is held."""
    store = GraphStore()
    for i in range(5):
        store.add_event(_member_event(f"m{i}", "app.x", i, kwargs={"site": f"s{i}"}))
    resolved: list[tuple[str, bool]] = []
    real = routes._resolve_node_alias

    def recording(node: Any, key: str | None) -> str:
        # RLock._is_owned: whether the calling thread holds the store lock
        resolved.append((node.task_id, store._lock._is_owned()))
        return real(node, key)

    monkeypatch.setattr(routes, "_resolve_node_alias", recording)
    app = FastAPI()
    app.include_router(create_api_router(store, node_alias_from_arguments="site"))

    (graph,) = TestClient(app).get("/api/graphs").json()["graphs"]

    members = [(tid, held) for tid, held in resolved if tid.startswith("m")]
    assert graph["flow"]["member_name"] is None
    assert [held for _, held in members] == [False] * len(members)
    assert len(members) == 2
