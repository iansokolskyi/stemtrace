"""``GET /api/anomalies``: filters, counts, pagination, order, schema and auth."""

from datetime import timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.anomalies import AnomalyKind
from stemtrace.core.events import TaskState, WorkerEvent, WorkerEventType
from stemtrace.server.api import routes
from stemtrace.server.fastapi.auth import require_api_key
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore, WorkerRegistry, WorkerTimeline
from tests.unit.anomaly_api import freeze_clock, get, make_app, seeded, seeded_events
from tests.unit.anomaly_pipeline import at, task_event

ORDER = ["s0", "s1", "i1", "i2", "s2"]
ZERO_COUNTS = {kind.value: 0 for kind in AnomalyKind}
SEEDED_COUNTS = {**ZERO_COUNTS, "stuck": 3, "interrupted": 2}


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(monkeypatch)


async def _list(app: FastAPI, query: str = "") -> dict[str, Any]:
    response = await get(app, f"/api/anomalies{query}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _ids(body: dict[str, Any]) -> list[str]:
    return [item["task_id"] for item in body["anomalies"]]


async def test_anomalies_on_empty_store_returns_zero_counts_for_all_four_kinds() -> (
    None
):
    body = await _list(make_app(GraphStore()))

    assert body == {
        "anomalies": [],
        "counts": ZERO_COUNTS,
        "total": 0,
        "limit": 100,
        "offset": 0,
        "evaluated_at": "2026-11-02T12:16:40Z",
    }


async def test_anomalies_sorts_by_since_ascending_then_task_id() -> None:
    store, timeline = seeded()

    body = await _list(make_app(store, timeline))

    assert _ids(body) == ORDER
    assert [item["since"] for item in body["anomalies"]] == [
        "2026-11-02T12:00:00Z",
        "2026-11-02T12:00:00Z",
        "2026-11-02T12:00:50Z",
        "2026-11-02T12:01:00Z",
        "2026-11-02T12:01:40Z",
    ]


async def test_anomalies_returns_the_stuck_and_interrupted_fields_of_the_contract() -> (
    None
):
    store, timeline = seeded()

    body = await _list(make_app(store, timeline))

    assert body["anomalies"][1] == {
        "kind": "stuck",
        "task_id": "s1",
        "name": "app.slow",
        "since": "2026-11-02T12:00:00Z",
        "hostname": "w@a",
        "queue": None,
        "task_count": 1,
        "elapsed_ms": 1_000_000,
        "threshold_ms": 300_000,
        "p95_ms": None,
        "runtime_samples": 0,
        "evidence": None,
        "similar_name": None,
    }
    assert body["anomalies"][3] == {
        "kind": "interrupted",
        "task_id": "i2",
        "name": "app.work",
        "since": "2026-11-02T12:01:00Z",
        "hostname": "w@d",
        "queue": None,
        "task_count": 1,
        "elapsed_ms": None,
        "threshold_ms": None,
        "p95_ms": None,
        "runtime_samples": None,
        "evidence": "worker_restarted",
        "similar_name": None,
    }


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?kind=stuck",
        "?kind=interrupted",
        "?hostname=w@c",
        "?name=app.work",
        "?kind=unregistered",
        "?kind=stuck&hostname=w@a&name=app.slow",
        "?limit=1",
    ],
)
async def test_anomalies_counts_ignore_the_kind_hostname_and_name_filters(
    query: str,
) -> None:
    store, timeline = seeded()

    body = await _list(make_app(store, timeline), query)

    assert body["counts"] == SEEDED_COUNTS


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("kind=stuck", ["s0", "s1", "s2"]),
        ("kind=interrupted", ["i1", "i2"]),
        ("kind=unregistered", []),
        ("kind=no_workers_online", []),
        ("hostname=w@a", ["s0", "s1"]),
        ("hostname=w@c", ["i1"]),
        ("hostname=w@zzz", []),
        ("hostname=", []),
        ("name=app.work", ["i1", "i2"]),
        ("name=app", []),
        ("kind=stuck&name=app.slow", ["s0", "s1"]),
        ("kind=interrupted&hostname=w@a", []),
    ],
)
async def test_anomalies_filters_match_kind_and_exact_hostname_and_name(
    query: str, expected: list[str]
) -> None:
    store, timeline = seeded()

    body = await _list(make_app(store, timeline), f"?{query}")

    assert _ids(body) == expected
    assert body["total"] == len(expected)


async def test_anomalies_with_unknown_kind_is_a_422_naming_the_valid_kinds() -> None:
    response = await get(make_app(GraphStore()), "/api/anomalies?kind=nope")

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Unknown kind nope; valid kinds: stuck, interrupted, unregistered, "
        "no_workers_online"
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("limit=2", ["s0", "s1"]),
        ("limit=2&offset=2", ["i1", "i2"]),
        ("limit=2&offset=4", ["s2"]),
        ("limit=2&offset=9", []),
    ],
)
async def test_anomalies_paginates_after_sorting_and_reports_the_unpaged_total(
    query: str, expected: list[str]
) -> None:
    store, timeline = seeded()

    body = await _list(make_app(store, timeline), f"?{query}")

    assert _ids(body) == expected
    assert body["total"] == 5
    assert body["limit"] == int(query.split("&")[0].split("=")[1])


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1"])
async def test_anomalies_rejects_out_of_range_paging(query: str) -> None:
    response = await get(make_app(GraphStore()), f"/api/anomalies?{query}")

    assert response.status_code == 422


async def test_anomalies_serializes_interrupted_since_in_utc() -> None:
    store = GraphStore()
    timeline = WorkerTimeline()
    store.add_event(task_event("t", TaskState.STARTED, 10, host="w@a"))
    timeline.record(
        WorkerEvent(
            event_type=WorkerEventType.WORKER_SHUTDOWN,
            hostname="w@a",
            pid=1,
            timestamp=at(50).astimezone(timezone(timedelta(hours=2))),
        )
    )

    body = await _list(make_app(store, timeline))

    assert body["anomalies"][0]["since"] == "2026-11-02T12:00:50Z"


async def test_anomalies_evaluated_at_is_the_clock_the_classifier_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, timeline = seeded()
    app = make_app(store, timeline)
    freeze_clock(monkeypatch, 120)  # s0 and s1 ran 120 s: under the 300 s ceiling

    body = await _list(app)

    assert body["evaluated_at"] == "2026-11-02T12:02:00Z"
    assert body["counts"] == {**ZERO_COUNTS, "interrupted": 2}


async def test_anomalies_refreshes_the_worker_registry_like_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def spy(_self: object) -> None:
        calls.append(1)

    monkeypatch.setattr(routes._InspectRefresher, "maybe_refresh", spy)
    app = FastAPI()
    app.include_router(
        routes.create_api_router(GraphStore(), worker_registry=WorkerRegistry())
    )

    await get(app, "/api/anomalies")

    assert calls == [1]


def _client(**kwargs: Any) -> TestClient:
    store, timeline = seeded()
    app = FastAPI()
    app.include_router(create_router(store=store, worker_timeline=timeline, **kwargs))
    return TestClient(app)


def test_anomalies_respects_the_auth_dependency() -> None:
    client = _client(auth_dependency=require_api_key("secret"))

    assert client.get("/api/anomalies").status_code == 401
    ok = client.get("/api/anomalies", headers={"X-API-Key": "secret"})
    assert ok.status_code == 200
    # The default 1 h ceiling: nothing is stuck at 1,000 s, two are interrupted.
    assert ok.json()["total"] == 2


def test_anomalies_respects_the_bearer_api_key() -> None:
    key = "k" * 16
    client = _client(api_key=key)

    assert client.get("/api/anomalies").status_code == 401
    ok = client.get("/api/anomalies", headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200


def test_create_router_hands_the_worker_timeline_to_the_api() -> None:
    body = _client().get("/api/anomalies").json()

    assert [i["task_id"] for i in body["anomalies"] if i["kind"] == "interrupted"] == [
        "i1",
        "i2",
    ]


async def test_anomalies_without_a_worker_timeline_never_report_interrupted() -> None:
    store, _ = seeded()

    body = await _list(make_app(store, None))

    # i1 and i2 fall back to stuck: they started long ago and nothing finished them.
    assert body["counts"] == {**ZERO_COUNTS, "stuck": 5}
    assert {i["kind"] for i in body["anomalies"]} == {"stuck"}
    assert _ids(body) == ORDER


async def test_extension_worker_timeline_feeds_interrupted_for_a_custom_consumer() -> (
    None
):
    extension = StemtraceExtension(
        broker_url="memory://", embedded_consumer=False, serve_ui=False
    )
    tasks, workers = seeded_events()
    for event in tasks:
        extension.store.add_event(event)
    for worker in workers:
        extension.worker_timeline.record(worker)
    app = FastAPI()
    app.include_router(extension.router)

    body = (await get(app, "/api/anomalies?kind=interrupted")).json()

    assert _ids(body) == ["i1", "i2"]
