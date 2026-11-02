"""``anomaly`` on task and graph nodes, ``anomaly_detail`` on the task page."""

from typing import Any

import pytest

from stemtrace.core.anomalies import Anomaly, AnomalyKind
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType
from stemtrace.core.groups import GroupConfig
from stemtrace.server.api import anomaly_routes, routes
from stemtrace.server.groups import group_lookup
from stemtrace.server.store import GraphStore
from tests.unit.anomaly_api import freeze_clock, get, make_app, seeded
from tests.unit.anomaly_pipeline import at, task_event


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(monkeypatch)


def _flow_store() -> GraphStore:
    """Root P (done) with a group of m1 (stuck) and m2 (done), and a stuck child c."""
    store = GraphStore()
    for event in [
        task_event("P", TaskState.SUCCESS, 0, host="w@a"),
        task_event("c", TaskState.STARTED, 1, name="app.child", host="w@a", parent="P"),
        *(
            TaskEvent(
                task_id=member,
                name="app.member",
                state=state,
                timestamp=at(2),
                parent_id="P",
                group_id="g",
                hostname="w@a",
            )
            for member, state in [
                ("m1", TaskState.STARTED),
                ("m2", TaskState.SUCCESS),
            ]
        ),
    ]:
        store.add_event(event)
    return store


async def _json(app: Any, path: str) -> Any:
    response = await get(app, path)
    assert response.status_code == 200, response.text
    return response.json()


async def test_tasks_list_flags_a_stuck_and_an_interrupted_task_and_no_other() -> None:
    store, timeline = seeded()

    body = await _json(make_app(store, timeline), "/api/tasks")

    assert {t["task_id"]: t["anomaly"] for t in body["tasks"]} == {
        "s0": "stuck",
        "s1": "stuck",
        "s2": "stuck",
        "i1": "interrupted",
        "i2": "interrupted",
        "ok": None,
    }


async def test_tasks_list_flags_tasks_outside_the_requested_page() -> None:
    store, timeline = seeded()

    body = await _json(make_app(store, timeline), "/api/tasks?limit=1&name=app.other")

    assert [(t["task_id"], t["anomaly"]) for t in body["tasks"]] == [("s2", "stuck")]


async def test_task_detail_has_the_anomaly_and_its_detail_for_a_stuck_task() -> None:
    store, timeline = seeded()
    app = make_app(store, timeline)

    detail = await _json(app, "/api/tasks/s1")
    listed = await _json(app, "/api/anomalies?name=app.slow")

    assert detail["task"]["anomaly"] == "stuck"
    assert detail["anomaly_detail"] == listed["anomalies"][1]
    assert detail["anomaly_detail"]["kind"] == "stuck"
    assert detail["anomaly_detail"]["threshold_ms"] == 300_000


async def test_task_detail_has_the_detail_of_an_interrupted_task() -> None:
    store, timeline = seeded()

    detail = await _json(make_app(store, timeline), "/api/tasks/i1")

    assert detail["task"]["anomaly"] == "interrupted"
    assert detail["anomaly_detail"]["kind"] == "interrupted"
    assert detail["anomaly_detail"]["evidence"] == "worker_shutdown"
    assert detail["anomaly_detail"]["since"] == "2026-11-02T12:00:50Z"


async def test_task_detail_of_a_healthy_task_has_no_anomaly() -> None:
    store, timeline = seeded()

    detail = await _json(make_app(store, timeline), "/api/tasks/ok")

    assert detail["task"]["anomaly"] is None
    assert detail["anomaly_detail"] is None


async def test_task_detail_and_children_flag_the_stuck_child() -> None:
    app = make_app(_flow_store())

    detail = await _json(app, "/api/tasks/P")
    children = await _json(app, "/api/tasks/P/children")

    assert detail["task"]["anomaly"] is None
    assert detail["anomaly_detail"] is None
    assert {c["task_id"]: c["anomaly"] for c in detail["children"]} == {
        "c": "stuck",
        "group:g": None,
    }
    assert {c["task_id"]: c["anomaly"] for c in children} == {
        "c": "stuck",
        "group:g": None,
    }


async def test_graph_flags_stuck_members_and_never_the_group_node() -> None:
    graph = await _json(make_app(_flow_store()), "/api/graphs/P")

    nodes = graph["nodes"]
    assert nodes["group:g"]["node_type"] == "GROUP"
    assert {tid: n["anomaly"] for tid, n in nodes.items()} == {
        "P": None,
        "c": "stuck",
        "group:g": None,
        "m1": "stuck",
        "m2": None,
    }


async def test_graph_list_flags_its_root() -> None:
    store, timeline = seeded()

    body = await _json(make_app(store, timeline), "/api/graphs")

    assert {g["task_id"]: g["anomaly"] for g in body["graphs"]} == {
        "s0": "stuck",
        "s1": "stuck",
        "s2": "stuck",
        "i1": "interrupted",
        "i2": "interrupted",
        "ok": None,
    }


@pytest.mark.parametrize(
    "path", ["/api/tasks", "/api/tasks/s1", "/api/graphs", "/api/graphs/s1"]
)
async def test_node_routes_compute_the_facts_once_per_request(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = anomaly_routes.classify_anomalies

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(anomaly_routes, "classify_anomalies", counting)
    store, timeline = seeded()

    await _json(make_app(store, timeline), path)

    assert calls == [1]


def _stuck(task_id: str) -> Anomaly:
    return Anomaly(
        kind=AnomalyKind.STUCK, task_id=task_id, name="x", since=at(0), hostname="w@a"
    )


def test_node_builders_never_attach_an_anomaly_to_a_synthetic_node() -> None:
    store = _flow_store()
    nodes = store.get_graph_from_root("P")
    group = nodes["group:g"]
    assert group.node_type == NodeType.GROUP
    assert not group.events
    task = nodes["m1"]
    reported = {n: _stuck(n) for n in nodes}
    group_of = group_lookup(GroupConfig())

    as_graph = routes._node_to_graph_response(
        group, nodes, group_of=group_of, anomalies=reported
    )
    as_task = routes._node_to_response(group, group_of=group_of, anomalies=reported)
    member = routes._node_to_graph_response(
        task, nodes, group_of=group_of, anomalies=reported
    )

    assert as_graph.anomaly is None
    assert as_task.anomaly is None
    assert member.anomaly == "stuck"
