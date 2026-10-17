"""Tests for task groups in API responses and ``GET /api/groups``."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.core.groups import GroupConfig, GroupRule
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore, WorkerRegistry

_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)

_CONFIG = GroupConfig(
    rules=[
        GroupRule(
            name="money",
            match=["billing.*", "*.charge_*"],
            tags=["pci", "critical"],
            color="#16a34a",
        ),
        GroupRule(name="idle", match=["idle.*"], tags=["later"]),
    ]
)


def _event(
    task_id: str,
    name: str,
    *,
    seconds: int = 0,
    parent_id: str | None = None,
    root_id: str | None = None,
    group_id: str | None = None,
    kwargs: dict[str, Any] | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=TaskState.STARTED,
        timestamp=_BASE + timedelta(seconds=seconds),
        parent_id=parent_id,
        root_id=root_id,
        group_id=group_id,
        kwargs=kwargs,
    )


def _client(
    store: GraphStore,
    groups: GroupConfig | None = None,
    worker_registry: WorkerRegistry | None = None,
    node_alias_from_arguments: str | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_api_router(
            store,
            worker_registry=worker_registry,
            groups=groups,
            node_alias_from_arguments=node_alias_from_arguments,
        )
    )
    return TestClient(app)


@pytest.fixture
def store() -> GraphStore:
    return GraphStore()


def test_list_tasks_includes_code_and_automatic_groups(store: GraphStore) -> None:
    store.add_event(_event("t1", "billing.tasks.refund", seconds=1))
    store.add_event(_event("t2", "tasks.search.reindex", seconds=2))
    store.add_event(_event("t3", "add", seconds=3))

    tasks = _client(store, _CONFIG).get("/api/tasks").json()["tasks"]

    by_id = {t["task_id"]: (t["task_group"], t["tags"]) for t in tasks}
    assert by_id == {
        "t1": ("money", ["pci", "critical"]),
        "t2": ("search", []),
        "t3": ("ungrouped", []),
    }


def test_list_tasks_without_group_config_uses_automatic_groups(
    store: GraphStore,
) -> None:
    store.add_event(_event("t1", "billing.tasks.refund"))

    task = _client(store).get("/api/tasks").json()["tasks"][0]

    assert task["task_group"] == "billing"
    assert task["tags"] == []


def test_task_detail_and_children_include_groups(store: GraphStore) -> None:
    store.add_event(_event("parent", "billing.charge", seconds=1))
    store.add_event(
        _event(
            "child", "search.reindex", seconds=2, parent_id="parent", root_id="parent"
        )
    )
    client = _client(store, _CONFIG)

    detail = client.get("/api/tasks/parent").json()
    children = client.get("/api/tasks/parent/children").json()

    assert (detail["task"]["task_group"], detail["task"]["tags"]) == (
        "money",
        ["pci", "critical"],
    )
    assert [(c["task_group"], c["tags"]) for c in detail["children"]] == [
        ("search", [])
    ]
    assert [(c["task_group"], c["tags"]) for c in children] == [("search", [])]


def test_graph_nodes_include_groups_and_synthetic_nodes_have_none(
    store: GraphStore,
) -> None:
    store.add_event(_event("root", "billing.start", seconds=1))
    for i, name in enumerate(["search.a", "shop.charge_card"]):
        store.add_event(
            _event(
                f"m{i}",
                name,
                seconds=2 + i,
                parent_id="root",
                root_id="root",
                group_id="g1",
            )
        )
    client = _client(store, _CONFIG)

    nodes = client.get("/api/graphs/root").json()["nodes"]

    groups = {
        tid: (n["node_type"], n["task_group"], n["tags"]) for tid, n in nodes.items()
    }
    assert groups == {
        "root": ("TASK", "money", ["pci", "critical"]),
        "m0": ("TASK", "search", []),
        "m1": ("TASK", "money", ["pci", "critical"]),
        "group:g1": ("GROUP", None, []),
    }


def test_graph_list_includes_root_group(store: GraphStore) -> None:
    store.add_event(_event("root", "billing.start"))

    graphs = _client(store, _CONFIG).get("/api/graphs").json()["graphs"]

    assert [(g["task_id"], g["task_group"], g["tags"]) for g in graphs] == [
        ("root", "money", ["pci", "critical"])
    ]


def test_graph_node_group_comes_from_task_name_not_alias(store: GraphStore) -> None:
    store.add_event(_event("root", "search.reindex", kwargs={"label": "billing.x"}))

    node = (
        _client(store, _CONFIG, node_alias_from_arguments="label")
        .get("/api/graphs/root")
        .json()["nodes"]["root"]
    )

    assert node["name"] == "billing.x"
    assert node["task_group"] == "search"


def test_registry_includes_groups_for_run_and_registered_tasks(
    store: GraphStore,
) -> None:
    worker_registry = WorkerRegistry()
    worker_registry.register_worker(
        hostname="w1", pid=1, tasks=["billing.tasks.never_run", "app.tasks.add"]
    )
    store.add_event(_event("t1", "search.reindex"))

    tasks = (
        _client(store, _CONFIG, worker_registry=worker_registry)
        .get("/api/tasks/registry?refresh=false")
        .json()["tasks"]
    )

    assert [(t["name"], t["task_group"], t["tags"]) for t in tasks] == [
        ("app.tasks.add", "ungrouped", []),
        ("billing.tasks.never_run", "money", ["pci", "critical"]),
        ("search.reindex", "search", []),
    ]


def test_list_groups_lists_code_auto_and_ungrouped(store: GraphStore) -> None:
    worker_registry = WorkerRegistry()
    worker_registry.register_worker(hostname="w1", pid=1, tasks=["billing.refund"])
    store.add_event(_event("t1", "billing.charge", seconds=1))
    store.add_event(_event("t2", "search.reindex", seconds=2))
    store.add_event(_event("t3", "tasks.search.index", seconds=3))
    store.add_event(_event("t4", "celery.chord_unlock", seconds=4))
    store.add_event(_event("t5", "search.reindex", seconds=5))

    body = (
        _client(store, _CONFIG, worker_registry=worker_registry)
        .get("/api/groups")
        .json()
    )

    assert body == {
        "groups": [
            {
                "name": "idle",
                "source": "code",
                "tags": ["later"],
                "color": None,
                "task_names": [],
            },
            {
                "name": "money",
                "source": "code",
                "tags": ["pci", "critical"],
                "color": "#16a34a",
                "task_names": ["billing.charge", "billing.refund"],
            },
            {
                "name": "search",
                "source": "auto",
                "tags": [],
                "color": None,
                "task_names": ["search.reindex", "tasks.search.index"],
            },
            {
                "name": "ungrouped",
                "source": "auto",
                "tags": [],
                "color": None,
                "task_names": ["celery.chord_unlock"],
            },
        ],
        "total": 4,
    }


def test_list_groups_excludes_synthetic_nodes(store: GraphStore) -> None:
    for i in range(2):
        store.add_event(_event(f"m{i}", "search.a", seconds=i, group_id="g1"))

    body = _client(store).get("/api/groups").json()

    assert [(g["name"], g["task_names"]) for g in body["groups"]] == [
        ("search", ["search.a"])
    ]


def test_list_groups_empty_store_without_rules_is_empty(store: GraphStore) -> None:
    assert _client(store).get("/api/groups").json() == {"groups": [], "total": 0}


def test_create_router_passes_groups_to_api(store: GraphStore) -> None:
    store.add_event(_event("t1", "billing.charge"))
    app = FastAPI()
    app.include_router(create_router(store=store, groups=_CONFIG), prefix="/x")

    body = TestClient(app).get("/x/api/groups").json()

    assert [g["name"] for g in body["groups"]] == ["idle", "money"]


def test_extension_with_dict_rules_serves_them() -> None:
    extension = StemtraceExtension(
        "memory://",
        embedded_consumer=False,
        serve_ui=False,
        groups=[{"name": "money", "match": ["billing.*"], "tags": ["pci"]}],
    )
    extension.store.add_event(_event("t1", "billing.charge"))
    app = FastAPI()
    extension.init_app(app)

    task = TestClient(app).get("/stemtrace/api/tasks").json()["tasks"][0]

    assert extension.groups.rules == (
        GroupRule(name="money", match=["billing.*"], tags=["pci"]),
    )
    assert (task["task_group"], task["tags"]) == ("money", ["pci"])


def test_extension_without_groups_is_automatic_only() -> None:
    extension = StemtraceExtension("memory://", embedded_consumer=False)

    assert extension.groups == GroupConfig()


def test_extension_with_invalid_groups_raises() -> None:
    with pytest.raises(ConfigurationError, match=r"rules\.0\.match: Field required"):
        StemtraceExtension("memory://", embedded_consumer=False, groups=[{"name": "x"}])
