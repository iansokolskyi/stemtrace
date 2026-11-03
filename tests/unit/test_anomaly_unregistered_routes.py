"""``/api/anomalies`` and the node fields for unregistered (D4)."""

import threading
from typing import Any

import pytest
from fastapi import FastAPI

from stemtrace.core.anomalies import AnomalySettings
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import anomaly_routes, routes
from stemtrace.server.store import GraphStore, WorkerRegistry
from tests.unit.anomaly_api import NOW_S, freeze_clock, get, make_app
from tests.unit.anomaly_pipeline import SMALL, at, task_event

BROKER = "redis://broker:6379/0"
FRESH_S = NOW_S - 30  # seconds after T0 of the last good inspect reply
KNOWN = "app.known"


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(monkeypatch)
    # With a broker URL the routes would try to inspect it; no inspector means
    # the registry stays as the test built it.
    monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: None)


def pending(task_id: str, seconds: float, name: str = "app.new") -> TaskEvent:
    return task_event(task_id, TaskState.PENDING, seconds, name=name)


def confirmed_registry(
    *, online: bool = True, names: tuple[str, ...] = (KNOWN,)
) -> WorkerRegistry:
    """A registry whose last inspect reply listed every worker's tasks, 30 s ago."""
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {"w@a": (1, list(names))}, now=at(FRESH_S))
    if not online:
        registry.mark_shutdown("w@a", 1)
    return registry


def store_of(*events: TaskEvent) -> GraphStore:
    store = GraphStore()
    for event in events:
        store.add_event(event)
    return store


async def list_body(app: FastAPI, query: str = "") -> dict[str, Any]:
    response = await get(app, f"/api/anomalies{query}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- The list -----------------------------------------------------------------


async def test_anomalies_returns_one_item_per_unregistered_name_with_the_contract_fields() -> (
    None
):
    store = store_of(pending("a2", 20, "app.a"), pending("a1", 10, "app.a"))
    app = make_app(store, registry=confirmed_registry(), broker_url=BROKER)

    body = await list_body(app)

    assert body["anomalies"] == [
        {
            "kind": "unregistered",
            "task_id": None,
            "name": "app.a",
            "since": "2026-11-02T12:00:10Z",
            "hostname": None,
            "queue": None,
            "task_count": 2,
            "elapsed_ms": None,
            "threshold_ms": None,
            "p95_ms": None,
            "runtime_samples": None,
            "evidence": None,
            "similar_name": None,
        }
    ]
    assert body["total"] == 1
    assert body["counts"]["unregistered"] == 1


async def test_anomalies_with_every_worker_stopped_give_the_fleet_item_and_no_names() -> (
    None
):
    store = store_of(pending("a", 5, "app.a"), pending("b", 9, "app.b"))
    app = make_app(store, registry=confirmed_registry(online=False), broker_url=BROKER)

    body = await list_body(app)

    assert [
        (a["kind"], a["task_id"], a["name"], a["task_count"]) for a in body["anomalies"]
    ] == [("no_workers_online", None, None, 2)]
    assert body["counts"] == {
        "stuck": 0,
        "interrupted": 0,
        "unregistered": 0,
        "no_workers_online": 1,
    }


def _mixed_app() -> FastAPI:
    store = store_of(
        task_event("s1", TaskState.STARTED, 0, name="app.slow", host="w@a"),
        pending("z", 0, "app.z"),
        pending("a", 0, "app.a"),
        pending("m", 50, "app.m"),
        pending("known", 0, KNOWN),
    )
    return make_app(store, registry=confirmed_registry(), broker_url=BROKER)


async def test_anomalies_sort_by_since_then_task_id_then_name_with_none_last() -> None:
    body = await list_body(_mixed_app())

    assert [
        (a["since"][-9:-1], a["task_id"], a["name"]) for a in body["anomalies"]
    ] == [
        ("12:00:00", "s1", "app.slow"),
        ("12:00:00", None, "app.a"),
        ("12:00:00", None, "app.z"),
        ("12:00:50", None, "app.m"),
    ]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("kind=unregistered", ["app.a", "app.z", "app.m"]),
        ("kind=stuck", ["app.slow"]),
        ("kind=no_workers_online", []),
        ("name=app.z", ["app.z"]),
        ("hostname=w@a", ["app.slow"]),
        ("kind=unregistered&name=app.slow", []),
        ("kind=unregistered&limit=1&offset=1", ["app.z"]),
    ],
)
async def test_anomalies_filters_apply_to_unregistered_items(
    query: str, expected: list[str]
) -> None:
    body = await list_body(_mixed_app(), f"?{query}")

    assert sorted(a["name"] for a in body["anomalies"]) == sorted(expected)
    assert body["counts"] == {
        "stuck": 1,
        "interrupted": 0,
        "unregistered": 3,
        "no_workers_online": 0,
    }


async def test_anomalies_kind_no_workers_online_is_accepted_and_empty_while_a_worker_is_online() -> (
    None
):
    app = make_app(
        store_of(pending("a", 5)),
        registry=confirmed_registry(),
        broker_url=BROKER,
    )

    body = await list_body(app, "?kind=no_workers_online")

    assert body["anomalies"] == []
    assert body["counts"]["no_workers_online"] == 0


# --- The gate through the router ----------------------------------------------


async def test_anomalies_with_a_broker_and_no_confirmed_inspect_flag_nothing() -> None:
    registry = WorkerRegistry()
    # worker_ready only, seen just now
    registry.register_worker("w@a", 1, [KNOWN], event_timestamp=at(NOW_S - 5))
    app = make_app(store_of(pending("t1", 0)), registry=registry, broker_url=BROKER)

    body = await list_body(app)

    assert (body["anomalies"], body["total"]) == ([], 0)


async def test_anomalies_with_a_stale_inspect_reply_flag_nothing() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {"w@a": (1, [KNOWN])}, now=at(NOW_S - 601))
    # The worker's task events keep it online; only the reply is old.
    registry.mark_seen("w@a", at(NOW_S - 5), now=at(NOW_S))
    app = make_app(store_of(pending("t1", 0)), registry=registry, broker_url=BROKER)

    assert (await list_body(app))["anomalies"] == []


async def test_anomalies_without_a_broker_trust_the_events_of_an_online_worker() -> (
    None
):
    registry = WorkerRegistry()
    registry.register_worker("w@a", 1, [KNOWN], event_timestamp=at(NOW_S - 5))
    app = make_app(store_of(pending("t1", 0)), registry=registry)

    body = await list_body(app)

    assert [a["kind"] for a in body["anomalies"]] == ["unregistered"]


async def test_anomalies_without_a_broker_and_no_workers_flag_nothing() -> None:
    app = make_app(store_of(pending("t1", 0)), registry=WorkerRegistry())

    assert (await list_body(app))["anomalies"] == []


async def test_anomalies_follow_the_configured_grace() -> None:
    settings = AnomalySettings(
        stuck_factor=SMALL.stuck_factor,
        pending_grace=SMALL.stuck_floor * 100,  # 6000 s: nothing has waited that long
    )
    app = make_app(
        store_of(pending("t1", 0)),
        settings=settings,
        registry=confirmed_registry(),
        broker_url=BROKER,
    )

    assert (await list_body(app))["anomalies"] == []


# --- Node fields --------------------------------------------------------------


def _node_app(*, online: bool = True) -> FastAPI:
    store = store_of(
        pending("u1", 0, "app.unknown"),
        pending("k1", 0, KNOWN),
        pending("young", NOW_S - 5, "app.unknown"),
    )
    return make_app(
        store, registry=confirmed_registry(online=online), broker_url=BROKER
    )


async def test_task_and_graph_nodes_of_an_unregistered_name_carry_the_kind() -> None:
    app = _node_app()

    tasks = (await get(app, "/api/tasks")).json()["tasks"]
    graphs = (await get(app, "/api/graphs")).json()["graphs"]
    graph = (await get(app, "/api/graphs/u1")).json()

    assert {t["task_id"]: t["anomaly"] for t in tasks} == {
        "u1": "unregistered",
        "k1": None,
        "young": None,
    }
    assert {g["task_id"]: g["anomaly"] for g in graphs} == {
        "u1": "unregistered",
        "k1": None,
        "young": None,
    }
    assert {tid: n["anomaly"] for tid, n in graph["nodes"].items()} == {
        "u1": "unregistered"
    }


async def test_task_detail_of_an_unregistered_task_has_its_names_item() -> None:
    app = _node_app()

    detail = (await get(app, "/api/tasks/u1")).json()
    listed = await list_body(app, "?name=app.unknown")

    assert detail["task"]["anomaly"] == "unregistered"
    assert detail["anomaly_detail"] == listed["anomalies"][0]
    assert detail["anomaly_detail"]["task_id"] is None
    assert detail["anomaly_detail"]["task_count"] == 1


# --- Off the loop and off the registry lock -----------------------------------


async def test_classification_runs_with_the_registry_lock_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = confirmed_registry()
    seen: dict[str, Any] = {}
    real = anomaly_routes.classify_anomalies

    def probing(*args: Any, **kwargs: Any) -> Any:
        def ingest() -> None:
            registry.register_worker("w@late", 9, ["app.late"])
            registry.registry_facts(broker_configured=True)

        other = threading.Thread(target=ingest)
        other.start()
        other.join(3)
        seen["free"] = not other.is_alive()
        seen["thread"] = other
        return real(*args, **kwargs)

    monkeypatch.setattr(anomaly_routes, "classify_anomalies", probing)
    app = make_app(store_of(pending("t1", 0)), registry=registry, broker_url=BROKER)

    response = await get(app, "/api/anomalies")
    seen["thread"].join()

    assert response.status_code == 200
    assert seen["free"] is True


# --- similar_name (D5) --------------------------------------------------------


def _renamed_app(registry: WorkerRegistry) -> FastAPI:
    store = store_of(
        pending("u1", 0, "app.sync_invoices_v2"), pending("u2", 0, "app.zzz")
    )
    return make_app(store, registry=registry, broker_url=BROKER)


async def test_anomalies_and_task_detail_carry_similar_name_for_a_renamed_task() -> (
    None
):
    app = _renamed_app(confirmed_registry(names=("app.sync_invoices",)))

    body = await list_body(app)
    detail = (await get(app, "/api/tasks/u1")).json()
    item = (await list_body(app, "?name=app.sync_invoices_v2"))["anomalies"][0]

    assert {a["name"]: a["similar_name"] for a in body["anomalies"]} == {
        "app.sync_invoices_v2": "app.sync_invoices",
        "app.zzz": None,
    }
    assert detail["anomaly_detail"]["similar_name"] == "app.sync_invoices"
    assert detail["anomaly_detail"] == item


async def test_similar_name_ignores_the_names_of_a_stopped_worker() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply(
        {},
        {"w@a": (1, ["app.sync_invoices"]), "w@b": (2, [KNOWN])},
        now=at(FRESH_S),
    )
    registry.mark_shutdown("w@a", 1)
    app = _renamed_app(registry)

    body = await list_body(app, "?name=app.sync_invoices_v2")

    assert [a["similar_name"] for a in body["anomalies"]] == [None]
