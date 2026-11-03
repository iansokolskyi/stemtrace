"""``updated_before`` on the task list: the exclusive upper bound a chart bucket needs."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
BUCKET = timedelta(minutes=5)


def _add(store: GraphStore, task_id: str, state: TaskState, ended: datetime) -> None:
    store.add_event(
        TaskEvent(
            task_id=task_id,
            name="billing.tasks.charge",
            state=state,
            timestamp=ended,
        )
    )


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


@pytest.fixture
def store() -> GraphStore:
    return GraphStore()


def _client(store: GraphStore) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(store))
    return TestClient(app)


def _ids(client: TestClient, query: str) -> list[str]:
    response = client.get(f"/api/tasks?{query}")
    assert response.status_code == 200
    return sorted(t["task_id"] for t in response.json()["tasks"])


def test_get_nodes_updated_before_is_exclusive(store: GraphStore) -> None:
    _add(store, "early", TaskState.SUCCESS, NOW - timedelta(minutes=10))
    _add(store, "at-bound", TaskState.SUCCESS, NOW - timedelta(minutes=5))
    _add(store, "late", TaskState.SUCCESS, NOW - timedelta(minutes=1))

    nodes, total = store.get_nodes(updated_before=NOW - timedelta(minutes=5))

    assert [n.task_id for n in nodes] == ["early"]
    assert total == 1


def test_updated_before_with_from_date_lists_exactly_one_chart_bucket(
    store: GraphStore,
) -> None:
    start = NOW - timedelta(minutes=10)
    end = start + BUCKET
    _add(store, "before", TaskState.FAILURE, start - timedelta(seconds=1))
    _add(store, "first", TaskState.FAILURE, start)
    _add(store, "inside", TaskState.FAILURE, start + timedelta(minutes=2))
    _add(store, "next-bucket", TaskState.FAILURE, end)
    client = _client(store)

    ids = _ids(
        client,
        f"state=FAILURE&from_date={start.isoformat().replace('+00:00', 'Z')}"
        f"&updated_before={end.isoformat().replace('+00:00', 'Z')}",
    )
    series = client.get("/api/stats/timeseries?bucket=5m&within=10m").json()

    assert ids == ["first", "inside"]
    # The list and the chart place a run by the same time, so the counts agree
    assert series["counts"]["FAILURE"][0] == len(ids)


def test_updated_before_ignores_a_start_that_began_earlier(store: GraphStore) -> None:
    # to_date compares first_seen, so a run that started before the bucket and ended after it
    # would be listed under it; updated_before compares the time the chart counts the run at
    start = NOW - timedelta(minutes=10)
    _add(store, "long", TaskState.STARTED, start - timedelta(minutes=30))
    _add(store, "long", TaskState.FAILURE, start + BUCKET + timedelta(minutes=1))
    client = _client(store)

    ids = _ids(
        client,
        f"from_date={start.isoformat().replace('+00:00', 'Z')}"
        f"&updated_before={(start + BUCKET).isoformat().replace('+00:00', 'Z')}",
    )

    assert ids == []


def test_updated_before_without_a_zone_is_utc(store: GraphStore) -> None:
    _add(
        store, "a", TaskState.SUCCESS, datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
    )
    _add(
        store,
        "b",
        TaskState.SUCCESS,
        datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc),
    )
    client = _client(store)

    assert _ids(client, "updated_before=2026-10-18T10:00:00") == ["a"]


def test_updated_before_with_within_keeps_the_recent_window(store: GraphStore) -> None:
    _add(store, "old", TaskState.SUCCESS, NOW - timedelta(hours=3))
    _add(store, "mid", TaskState.SUCCESS, NOW - timedelta(minutes=30))
    _add(store, "new", TaskState.SUCCESS, NOW - timedelta(minutes=1))
    client = _client(store)

    before = (NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    assert _ids(client, f"within=1h&updated_before={before}") == ["mid"]
