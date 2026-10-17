"""Tests for the summary endpoints and the list-filter additions (T6, PR 1)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.groups import GroupConfig, GroupRule
from stemtrace.server import stats
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _event(
    task_id: str,
    state: TaskState,
    minutes_ago: float,
    *,
    name: str = "billing.tasks.charge",
    exception_type: str | None = None,
    parent_id: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=state,
        timestamp=NOW - timedelta(minutes=minutes_ago),
        exception_type=exception_type,
        parent_id=parent_id,
        root_id=parent_id,
    )


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


@pytest.fixture
def store() -> GraphStore:
    return GraphStore()


def _client(
    store: GraphStore,
    *,
    worker_registry: WorkerRegistry | None = None,
    broker_url: str | None = None,
    groups: GroupConfig | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_api_router(
            store, worker_registry=worker_registry, broker_url=broker_url, groups=groups
        )
    )
    return TestClient(app)


# --- /api/overview ----------------------------------------------------------


def test_overview_compares_the_window_with_the_one_before(store: GraphStore) -> None:
    # last hour: 3 finished (1 failed), 1 running; hour before: 1 finished, failed
    for task_id, state, ago in [
        ("a", TaskState.SUCCESS, 5),
        ("b", TaskState.SUCCESS, 30),
        ("c", TaskState.FAILURE, 59),
        ("d", TaskState.STARTED, 1),
        ("e", TaskState.FAILURE, 61),
        ("f", TaskState.SUCCESS, 121),  # older than both windows
    ]:
        store.add_event(_event(task_id, state, ago))
    registry = WorkerRegistry()
    registry.register_worker(hostname="w1@h", pid=1, tasks=[])
    registry.register_worker(hostname="w2@h", pid=2, tasks=[])
    registry.mark_shutdown("w2@h", 2)

    body = _client(store, worker_registry=registry).get("/api/overview").json()

    assert body["window"] == {
        "start": "2026-10-18T11:00:00Z",
        "end": "2026-10-18T12:00:00Z",
    }
    assert body["baseline_window"] == {
        "start": "2026-10-18T10:00:00Z",
        "end": "2026-10-18T11:00:00Z",
    }
    assert body["runs"] == {"value": 3, "baseline": 1}
    assert body["failures"] == {"value": 1, "baseline": 1}
    assert body["runs_per_minute"] == {
        "value": pytest.approx(3 / 60),
        "baseline": pytest.approx(1 / 60),
    }
    assert body["failure_rate"] == {"value": pytest.approx(1 / 3), "baseline": 1.0}
    assert body["workers"] == {"online": 1, "total": 2}


def test_overview_window_edges(store: GraphStore) -> None:
    store.add_event(_event("at-start", TaskState.SUCCESS, 60))  # 11:00 → window
    store.add_event(_event("future", TaskState.SUCCESS, -3))  # clock ahead → window
    store.add_event(_event("just-before", TaskState.SUCCESS, 60.001))  # → baseline

    body = _client(store).get("/api/overview?within=1h").json()

    assert body["runs"] == {"value": 2, "baseline": 1}


def test_overview_without_runs_or_workers_is_all_zero(store: GraphStore) -> None:
    body = _client(store).get("/api/overview?within=15m").json()

    assert body["runs"] == {"value": 0, "baseline": 0}
    assert body["failure_rate"] == {"value": 0.0, "baseline": 0.0}
    assert body["workers"] == {"online": 0, "total": 0}
    assert body["window"]["start"] == "2026-10-18T11:45:00Z"


@pytest.mark.parametrize("within", ["0m", "1w", "abc", ""])
def test_overview_bad_within_is_422(store: GraphStore, within: str) -> None:
    resp = _client(store).get(f"/api/overview?within={within}")

    assert resp.status_code == 422
    assert "within" in resp.json()["detail"]


# --- /api/stats/timeseries --------------------------------------------------


def test_timeseries_counts_runs_per_bucket_and_state(store: GraphStore) -> None:
    store.add_event(_event("a", TaskState.SUCCESS, 14))  # 11:46 → 11:45 bucket
    store.add_event(_event("b", TaskState.FAILURE, 6))  # 11:54 → 11:50
    store.add_event(_event("c", TaskState.SUCCESS, 4))  # 11:56 → 11:55
    store.add_event(_event("d", TaskState.RETRY, 0))  # 12:00 → 12:00

    body = _client(store).get("/api/stats/timeseries?bucket=5m&within=15m").json()

    assert body["bucket_seconds"] == 300
    assert body["starts"] == [
        "2026-10-18T11:45:00Z",
        "2026-10-18T11:50:00Z",
        "2026-10-18T11:55:00Z",
        "2026-10-18T12:00:00Z",
    ]
    assert body["counts"] == {
        "SUCCESS": [1, 0, 1, 0],
        "FAILURE": [0, 1, 0, 0],
        "RETRY": [0, 0, 0, 1],
    }
    assert body["by_task_group"] is None


def test_timeseries_without_within_covers_all_retained_runs(store: GraphStore) -> None:
    store.add_event(_event("old", TaskState.SUCCESS, 180))
    store.add_event(_event("new", TaskState.SUCCESS, 1))

    body = _client(store).get("/api/stats/timeseries?bucket=1h").json()

    assert body["starts"][0] == "2026-10-18T09:00:00Z"
    assert body["counts"] == {"SUCCESS": [1, 0, 1, 0]}  # 11:59 is in the 11:00 bucket
    assert body["window"]["start"] == "2026-10-18T09:00:00Z"


def test_timeseries_by_task_group(store: GraphStore) -> None:
    store.add_event(_event("a", TaskState.SUCCESS, 1, name="billing.charge"))
    store.add_event(_event("b", TaskState.FAILURE, 1, name="search.reindex"))
    store.add_event(_event("c", TaskState.FAILURE, 1, name="shop.charge_card"))
    groups = GroupConfig(rules=[GroupRule(name="money", match=["*.charge*"])])

    body = (
        _client(store, groups=groups)
        .get("/api/stats/timeseries?bucket=1h&within=1h&group_by=task_group")
        .json()
    )

    assert body["by_task_group"] == {
        "money": {"SUCCESS": [1, 0], "FAILURE": [1, 0]},
        "search": {"FAILURE": [1, 0]},
    }


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("bucket=1m&within=2d", "at most 1440"),
        ("bucket=0s", "bucket"),
        ("bucket=5m&group_by=queue", "group_by"),
        ("within=1h&from_date=2026-10-18T10:00:00Z", "not both"),
    ],
)
def test_timeseries_bad_query_is_422(
    store: GraphStore, query: str, detail: str
) -> None:
    resp = _client(store).get(f"/api/stats/timeseries?{query}")

    assert resp.status_code == 422
    assert detail in resp.json()["detail"]


# --- /api/failures ----------------------------------------------------------


def test_failures_grouped_by_exception(store: GraphStore) -> None:
    store.add_event(_event("v1", TaskState.FAILURE, 30, exception_type="ValueError"))
    store.add_event(
        _event("v2", TaskState.FAILURE, 10, name="b.y", exception_type="ValueError")
    )
    store.add_event(_event("k1", TaskState.FAILURE, 20, exception_type="KeyError"))
    store.add_event(_event("old", TaskState.FAILURE, 120, exception_type="KeyError"))

    body = _client(store).get("/api/failures?group_by=exception&within=1h").json()

    assert body["group_by"] == "exception"
    assert body["total"] == 3
    assert body["groups"] == [
        {
            "exception_type": "ValueError",
            "count": 2,
            "first_seen": "2026-10-18T11:30:00Z",
            "last_seen": "2026-10-18T11:50:00Z",
            "task_names": ["b.y", "billing.tasks.charge"],
            "sample_task_ids": ["v2", "v1"],
        },
        {
            "exception_type": "KeyError",
            "count": 1,
            "first_seen": "2026-10-18T11:40:00Z",
            "last_seen": "2026-10-18T11:40:00Z",
            "task_names": ["billing.tasks.charge"],
            "sample_task_ids": ["k1"],
        },
    ]


def test_failures_without_within_cover_all_retained(store: GraphStore) -> None:
    store.add_event(_event("old", TaskState.FAILURE, 600, exception_type="KeyError"))

    body = _client(store).get("/api/failures").json()

    assert body["total"] == 1
    assert body["window"]["start"] == "2026-10-18T02:00:00Z"


def test_failures_other_grouping_is_422(store: GraphStore) -> None:
    assert _client(store).get("/api/failures?group_by=task").status_code == 422


# --- G2: state= list ---------------------------------------------------------


def _states(client: TestClient, query: str) -> list[str]:
    return sorted(t["state"] for t in client.get(f"/api/tasks?{query}").json()["tasks"])


def test_state_filter_takes_a_list_in_any_case(store: GraphStore) -> None:
    for task_id, state in [
        ("f", TaskState.FAILURE),
        ("r", TaskState.RETRY),
        ("s", TaskState.SUCCESS),
    ]:
        store.add_event(_event(task_id, state, 1))
    client = _client(store)

    assert _states(client, "state=FAILURE,RETRY") == ["FAILURE", "RETRY"]
    assert _states(client, "state=failure") == ["FAILURE"]
    assert _states(client, "state= Retry , ") == ["RETRY"]
    assert _states(client, "state=") == ["FAILURE", "RETRY", "SUCCESS"]


@pytest.mark.parametrize("state", ["FAILED", "FAILURE,DONE"])
def test_unknown_state_is_422(store: GraphStore, state: str) -> None:
    resp = _client(store).get(f"/api/tasks?state={state}")

    assert resp.status_code == 422
    assert "Unknown state" in resp.json()["detail"]
    assert "FAILURE" in resp.json()["detail"]


# --- within on lists ---------------------------------------------------------


def test_within_on_task_and_graph_lists(store: GraphStore) -> None:
    store.add_event(_event("recent", TaskState.SUCCESS, 10))
    store.add_event(_event("old", TaskState.SUCCESS, 90))
    client = _client(store)

    tasks = client.get("/api/tasks?within=1h").json()["tasks"]
    graphs = client.get("/api/graphs?within=1h").json()["graphs"]

    assert [t["task_id"] for t in tasks] == ["recent"]
    assert [g["task_id"] for g in graphs] == ["recent"]
    both = client.get("/api/tasks?within=1h&from_date=2026-10-18T10:00:00Z")
    assert both.status_code == 422
    assert client.get("/api/graphs?within=soon").status_code == 422


# --- compact ------------------------------------------------------------------


def test_compact_lists_leave_out_events(store: GraphStore) -> None:
    store.add_event(_event("p", TaskState.SUCCESS, 2))
    store.add_event(_event("c", TaskState.SUCCESS, 1, parent_id="p"))
    client = _client(store)

    compact = client.get("/api/tasks?compact=1").json()["tasks"]
    full = client.get("/api/tasks").json()["tasks"]
    children = client.get("/api/tasks/p/children?compact=true").json()

    assert [t["events"] for t in compact] == [None, None]
    assert all(len(t["events"]) == 1 for t in full)
    assert [c["events"] for c in children] == [None]
    assert len(client.get("/api/tasks/p").json()["task"]["events"]) == 1


# --- G3: broker kind in /api/health ------------------------------------------


@pytest.mark.parametrize(
    ("broker_url", "kind"),
    [
        ("redis://user:secret@redis.internal:6379/0", "redis"),
        ("rediss://host:6380/0", "redis"),
        ("amqp://guest:guest@rabbit//", "rabbitmq"),
        ("pyamqp://rabbit//", "rabbitmq"),
        ("amqps://rabbit//", "rabbitmq"),
        ("memory://", "memory"),
        ("sqs://queue", None),
        (None, None),
    ],
)
def test_health_names_the_broker_kind_only(
    store: GraphStore, broker_url: str | None, kind: str | None
) -> None:
    body = _client(store, broker_url=broker_url).get("/api/health").json()

    assert body["broker"] == kind
    assert "secret" not in str(body)


# --- cost: aggregation runs outside the store lock ----------------------------


def test_summaries_aggregate_outside_the_store_lock(
    store: GraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_event(_event("a", TaskState.FAILURE, 1, exception_type="E"))
    held: list[bool] = []
    for name in ("window_stats", "time_series", "failure_groups"):
        real = getattr(stats, name)

        def spy(*args: object, _real: object = real, **kwargs: object) -> object:
            held.append(store._lock._is_owned())  # type: ignore[attr-defined]  # RLock internals
            return _real(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(f"stemtrace.server.api.summary_routes.{name}", spy)
    client = _client(store)

    client.get("/api/overview")
    client.get("/api/stats/timeseries?within=1h")
    client.get("/api/failures")

    assert held == [False, False, False, False]


# --- review round 1 regressions -----------------------------------------------


def test_naive_from_date_is_utc_on_summaries(store: GraphStore) -> None:
    """A time without a timezone means UTC, as on /api/tasks (it was a 500)."""
    store.add_event(_event("f", TaskState.FAILURE, 30, exception_type="E"))
    client = _client(store)

    failures = client.get("/api/failures?from_date=2026-10-18T00:00:00")
    series = client.get("/api/stats/timeseries?bucket=1h&from_date=2026-10-18T00:00:00")

    assert failures.status_code == 200
    assert failures.json()["window"]["start"] == "2026-10-18T00:00:00Z"
    assert failures.json()["total"] == 1
    assert series.status_code == 200
    assert series.json()["starts"][0] == "2026-10-18T00:00:00Z"


def test_date_only_from_date_behaves_as_on_the_task_list(store: GraphStore) -> None:
    """The UI sends dates: accepted as UTC midnight where pydantic parses them.

    pydantic 2.4 (the floor) rejects a date for a datetime parameter with a
    422 on every endpoint; later versions read it as midnight. Never a 500.
    """
    store.add_event(_event("f", TaskState.FAILURE, 30, exception_type="E"))
    client = _client(store)

    tasks = client.get("/api/tasks?from_date=2026-10-18")
    failures = client.get("/api/failures?from_date=2026-10-18")
    series = client.get("/api/stats/timeseries?bucket=1h&from_date=2026-10-18")

    assert failures.status_code == series.status_code == tasks.status_code
    if tasks.status_code == 200:
        assert failures.json()["window"]["start"] == "2026-10-18T00:00:00Z"


@pytest.mark.parametrize(
    "url",
    [
        "/api/stats/timeseries?from_date=2099-01-01T00:00:00Z",
        "/api/failures?from_date=2099-01-01T00:00:00Z",
    ],
)
def test_future_from_date_is_422(store: GraphStore, url: str) -> None:
    resp = _client(store).get(url)

    assert resp.status_code == 422
    assert "after now" in resp.json()["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "/api/overview?within=99999999d",
        "/api/failures?within=99999999d",
        "/api/tasks?within=9999999999d",
        "/api/graphs?within=9999999999d",
        "/api/stats/timeseries?bucket=9999999999d",
        "/api/stats/timeseries?within=3651d&bucket=7d",
    ],
)
def test_oversized_durations_are_422(store: GraphStore, url: str) -> None:
    resp = _client(store).get(url)

    assert resp.status_code == 422
    assert "at most 3650d" in resp.json()["detail"]


def test_default_timeseries_fits_any_retained_span(store: GraphStore) -> None:
    store.add_event(_event("stuck", TaskState.STARTED, 6 * 24 * 60))  # 6 days ago
    store.add_event(_event("new", TaskState.SUCCESS, 1))

    body = _client(store).get("/api/stats/timeseries").json()

    assert body["bucket_seconds"] == 900  # 5m would need 1729 buckets
    assert len(body["starts"]) <= 1440
    assert sum(sum(series) for series in body["counts"].values()) == 2


def test_timeseries_window_starts_at_the_first_bucket_edge(store: GraphStore) -> None:
    store.add_event(_event("a", TaskState.SUCCESS, 57))  # 11:03, before 11:07

    body = _client(store).get("/api/stats/timeseries?within=53m&bucket=1h").json()

    # now - 53m = 11:07; the 1h bucket starts at 11:00 and counts from there
    assert body["window"]["start"] == "2026-10-18T11:00:00Z"
    assert body["starts"][0] == body["window"]["start"]
    assert body["counts"] == {"SUCCESS": [1, 0]}
