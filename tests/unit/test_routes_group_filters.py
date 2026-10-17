"""Tests for the ``task_group`` and ``tag`` filters on ``/api/tasks`` and ``/api/graphs``."""

import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.groups import GroupConfig, GroupRule, ResolvedGroup, TaskAttributes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)

_CONFIG = GroupConfig(
    rules=[
        GroupRule(
            name="money", match=["billing.*", "*.charge_*"], tags=["pci", "critical"]
        ),
        GroupRule(name="idle", match=["idle.*"], tags=["later", "critical"]),
    ]
)


def _event(
    task_id: str,
    name: str,
    *,
    seconds: int = 0,
    state: TaskState = TaskState.STARTED,
    parent_id: str | None = None,
    group_id: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=state,
        timestamp=_BASE + timedelta(seconds=seconds),
        parent_id=parent_id,
        group_id=group_id,
    )


def _client(store: GraphStore, groups: GroupConfig | None = _CONFIG) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(store, groups=groups))
    return TestClient(app)


def _task_ids(client: TestClient, query: str) -> tuple[set[str], int]:
    body = client.get(f"/api/tasks?limit=500&{query}").json()
    return {t["task_id"] for t in body["tasks"]}, body["total"]


def _graph_ids(client: TestClient, query: str) -> tuple[set[str], int]:
    body = client.get(f"/api/graphs?limit=100&{query}").json()
    return {g["task_id"] for g in body["graphs"]}, body["total"]


@pytest.fixture
def store() -> GraphStore:
    store = GraphStore()
    store.add_event(_event("refund", "billing.tasks.refund", seconds=1))
    store.add_event(_event("charge", "orders.charge_card", seconds=2))
    store.add_event(_event("nap", "idle.nap", seconds=3))
    store.add_event(_event("reindex", "search.reindex", seconds=4))
    store.add_event(_event("add", "add", seconds=5, state=TaskState.FAILURE))
    return store


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("task_group=money", {"refund", "charge"}),
        ("task_group=idle", {"nap"}),
        # automatic groups: from the task name, and the catch-all
        ("task_group=search", {"reindex"}),
        ("task_group=ungrouped", {"add"}),
        ("task_group=nope", set()),
        # exact, case-sensitive: not a substring or prefix
        ("task_group=mon", set()),
        ("task_group=Money", set()),
        ("tag=pci", {"refund", "charge"}),
        ("tag=critical", {"refund", "charge", "nap"}),
        ("tag=nope", set()),
        ("task_group=money&tag=later", set()),
        ("task_group=idle&tag=critical", {"nap"}),
        ("task_group=ungrouped&state=FAILURE", {"add"}),
        ("task_group=money&state=FAILURE", set()),
    ],
)
def test_list_tasks_group_filter_returns_matching_tasks(
    store: GraphStore, query: str, expected: set[str]
) -> None:
    ids, total = _task_ids(_client(store), query)

    assert ids == expected
    assert total == len(expected)


def test_list_tasks_group_filter_counts_before_paging(store: GraphStore) -> None:
    client = _client(store)

    first = client.get("/api/tasks?tag=critical&limit=2&offset=0").json()
    rest = client.get("/api/tasks?tag=critical&limit=2&offset=2").json()

    assert first["total"] == rest["total"] == 3
    # Most recent first: nap (3s), charge (2s), then refund (1s).
    assert [t["task_id"] for t in first["tasks"]] == ["nap", "charge"]
    assert [t["task_id"] for t in rest["tasks"]] == ["refund"]


def test_list_tasks_group_filter_without_config_uses_automatic_groups(
    store: GraphStore,
) -> None:
    ids, total = _task_ids(_client(store, groups=None), "task_group=billing")

    assert ids == {"refund"}
    assert total == 1


@pytest.fixture
def graph_store() -> GraphStore:
    """Three workflows: billing with a search subtask, search alone, and a
    group (synthetic GROUP root) of two idle tasks."""
    store = GraphStore()
    store.add_event(_event("checkout", "billing.checkout", seconds=1))
    store.add_event(_event("index", "search.index", seconds=2, parent_id="checkout"))
    store.add_event(_event("lone", "search.lone", seconds=3))
    store.add_event(_event("i1", "idle.one", seconds=4, group_id="g"))
    store.add_event(_event("i2", "idle.two", seconds=5, group_id="g"))
    return store


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # a graph matches when any of its tasks does, not just its root
        ("task_group=search", {"checkout", "lone"}),
        ("task_group=money", {"checkout"}),
        ("task_group=idle", {"group:g"}),
        ("tag=later", {"group:g"}),
        ("tag=critical", {"checkout", "group:g"}),
        ("task_group=money&tag=later", set()),
        ("task_group=nope", set()),
    ],
)
def test_list_graphs_group_filter_matches_any_task_in_graph(
    graph_store: GraphStore, query: str, expected: set[str]
) -> None:
    ids, total = _graph_ids(_client(graph_store), query)

    assert ids == expected
    assert total == len(expected)


def test_list_graphs_group_filter_counts_before_paging(graph_store: GraphStore) -> None:
    client = _client(graph_store)

    first = client.get("/api/graphs?task_group=search&limit=1").json()
    second = client.get("/api/graphs?task_group=search&limit=1&offset=1").json()

    assert first["total"] == second["total"] == 2
    assert [g["task_id"] for g in first["graphs"]] == ["lone"]
    assert [g["task_id"] for g in second["graphs"]] == ["checkout"]


class _CountingGroups(GroupConfig):
    """GroupConfig that records each resolve() call."""

    def resolve(self, task: TaskAttributes) -> ResolvedGroup:
        _RESOLVED.append(task.name)
        return super().resolve(task)


_RESOLVED: list[str] = []


@pytest.mark.parametrize(
    "path", ["/api/tasks?task_group=search&tag=x", "/api/graphs?task_group=search"]
)
def test_group_filter_resolves_each_task_name_once_per_request(
    graph_store: GraphStore, path: str
) -> None:
    graph_store.add_event(_event("lone2", "search.lone", seconds=6))
    client = _client(graph_store, _CountingGroups(rules=_CONFIG.rules))
    _RESOLVED.clear()

    assert client.get(path).status_code == 200

    assert len(_RESOLVED) == len(set(_RESOLVED))


_NAMES = ["billing.a", "billing.b", "x.charge_y", "idle.z", "search.q", "add"]
_FILTERS = (
    [{"task_group": g} for g in ("money", "idle", "search", "ungrouped", "billing")]
    + [{"tag": t} for t in ("pci", "critical", "later", "none")]
    + [
        {"task_group": "money", "tag": "pci"},
        {"task_group": "idle", "tag": "pci"},
    ]
)


def _random_store(seed: int) -> GraphStore:
    """Tasks with random names, parents (earlier tasks or none) and groups."""
    rng = random.Random(seed)
    store = GraphStore()
    ids: list[str] = []
    for i in range(30):
        task_id = f"t{i}"
        parent = rng.choice([None, None, *ids[-5:]]) if ids else None
        group = rng.choice([None, None, None, "ga", "gb"])
        store.add_event(
            _event(
                task_id, rng.choice(_NAMES), seconds=i, parent_id=parent, group_id=group
            )
        )
        ids.append(task_id)
    return store


def _expected(store: GraphStore, query: dict[str, str]) -> tuple[set[str], set[str]]:
    """Matching task IDs and root IDs, worked out from the graph independently."""
    nodes = store._graph.nodes

    def matches(name: str) -> bool:
        group = _CONFIG.resolve(TaskAttributes(name=name))
        if "task_group" in query and group.name != query["task_group"]:
            return False
        return "tag" not in query or query["tag"] in group.tags

    tasks = {
        tid
        for tid, n in nodes.items()
        if n.node_type.value == "TASK" and matches(n.name)
    }

    def subtree(node_id: str) -> set[str]:
        found, todo = set(), [node_id]
        while todo:
            current = todo.pop()
            if current not in found:
                found.add(current)
                todo.extend(nodes[current].children)
        return found

    roots = {r for r in store._graph.root_ids if subtree(r) & tasks}
    return tasks, roots


@pytest.mark.parametrize("seed", range(40))
def test_group_filters_match_independent_reference(seed: int) -> None:
    store = _random_store(seed)
    client = _client(store)

    for query in _FILTERS:
        tasks, roots = _expected(store, query)
        params = "&".join(f"{k}={v}" for k, v in query.items())

        assert _task_ids(client, params) == (tasks, len(tasks)), (seed, query)
        assert _graph_ids(client, params) == (roots, len(roots)), (seed, query)
