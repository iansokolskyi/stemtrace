"""``GET /api/tasks?sort=runtime|queue_wait`` orders runs slowest first."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from stemtrace.server.stats import RunRecord

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _ev(tid: str, state: TaskState, at: datetime, name: str) -> TaskEvent:
    return TaskEvent(task_id=tid, name=name, state=state, timestamp=at)


def _run(
    store: GraphStore,
    tid: str,
    *,
    wait: int | None,
    runtime: int | None,
    end: datetime = NOW,
    state: TaskState = TaskState.SUCCESS,
    name: str = "app.work",
) -> None:
    """Add a run that waited ``wait`` ms (None: no PENDING) and ran ``runtime`` ms.

    A ``runtime`` of None leaves the run STARTED (not finished).
    """
    started = end - timedelta(milliseconds=runtime or 0)
    if wait is not None:
        sent = started - timedelta(milliseconds=wait)
        store.add_event(_ev(tid, TaskState.PENDING, sent, name))
    store.add_event(_ev(tid, TaskState.STARTED, started, name))
    if runtime is not None:
        store.add_event(_ev(tid, state, end, name))


def _client(store: GraphStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)
    app = FastAPI()
    app.include_router(create_api_router(store))
    return TestClient(app)


def _ids(client: TestClient, query: str = "") -> list[str]:
    response = client.get(f"/api/tasks?limit=500{query}")
    assert response.status_code == 200
    return [t["task_id"] for t in response.json()["tasks"]]


@pytest.fixture
def mixed(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    store = GraphStore()
    _run(store, "fast", wait=900, runtime=10, end=NOW - timedelta(seconds=1))
    _run(store, "slow", wait=5, runtime=900, end=NOW - timedelta(seconds=2))
    _run(store, "mid", wait=300, runtime=200, end=NOW - timedelta(seconds=3))
    _run(store, "running", wait=50, runtime=None, end=NOW - timedelta(seconds=4))
    _run(store, "nosent", wait=None, runtime=5_000, end=NOW - timedelta(seconds=5))
    return _client(store, monkeypatch)


def test_sort_runtime_orders_slowest_first_with_unmeasured_runs_last(
    mixed: TestClient,
) -> None:
    # "running" has no runtime yet, so it is the only run with no value.
    assert _ids(mixed, "&sort=runtime") == [
        "nosent",
        "slow",
        "mid",
        "fast",
        "running",
    ]


def test_sort_queue_wait_orders_longest_wait_first_with_unmeasured_runs_last(
    mixed: TestClient,
) -> None:
    # "running" is not finished and "nosent" has no sent event: no wait for either.
    assert _ids(mixed, "&sort=queue_wait") == [
        "fast",
        "mid",
        "slow",
        "running",
        "nosent",
    ]


@pytest.mark.parametrize("sort", ["runtime", "queue_wait"])
def test_sort_zero_value_ranks_above_runs_with_no_value(
    sort: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GraphStore()
    _run(store, "zero", wait=0, runtime=0)
    _run(store, "none", wait=None, runtime=None)
    _run(store, "some", wait=7, runtime=7)

    ids = _ids(_client(store, monkeypatch), f"&sort={sort}")

    assert ids == ["some", "zero", "none"]


@pytest.mark.parametrize("sort", ["runtime", "queue_wait"])
def test_sort_ties_break_by_newest_update_then_task_id_whatever_the_arrival(
    sort: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = [
        ("b", NOW - timedelta(seconds=10)),
        ("a", NOW - timedelta(seconds=10)),
        ("c", NOW - timedelta(seconds=5)),
        ("d", NOW - timedelta(seconds=20)),
    ]
    for order in ([0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]):
        store = GraphStore()
        for i in order:
            _run(store, specs[i][0], wait=40, runtime=40, end=specs[i][1])

        ids = _ids(_client(store, monkeypatch), f"&sort={sort}")

        assert ids == ["c", "a", "b", "d"]


@pytest.mark.parametrize("sort", ["runtime", "queue_wait"])
def test_sort_runs_with_no_value_tie_break_by_task_id(
    sort: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GraphStore()
    for tid in ("z", "y", "x"):
        _run(store, tid, wait=None, runtime=None)
    _run(store, "keyed", wait=3, runtime=3)

    ids = _ids(_client(store, monkeypatch), f"&sort={sort}")

    assert ids == ["keyed", "x", "y", "z"]


def test_sort_combines_with_state_name_and_time_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GraphStore()
    _run(store, "ok_slow", wait=1, runtime=800, name="app.a")
    _run(store, "ok_fast", wait=1, runtime=100, name="app.a")
    _run(store, "bad_slowest", wait=1, runtime=2_000, state=TaskState.FAILURE)
    _run(store, "other_name", wait=1, runtime=3_000, name="app.zzz")
    old = NOW - timedelta(hours=3)
    _run(store, "old", wait=1, runtime=9_000, end=old, name="app.a")
    client = _client(store, monkeypatch)

    combined = _ids(client, "&sort=runtime&state=SUCCESS&name=app.a&within=1h")
    failed = client.get("/api/tasks?sort=runtime&state=FAILURE&within=1h").json()

    assert combined == ["ok_slow", "ok_fast"]
    assert [t["task_id"] for t in failed["tasks"]] == ["bad_slowest"]
    assert failed["total"] == 1


@pytest.mark.parametrize("sort", ["runtime", "queue_wait"])
def test_sort_pages_partition_the_sorted_list(
    sort: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(7)
    store = GraphStore()
    for i in range(60):
        value = rng.choice([None, 0, 5, 5, 5, 9])
        end = NOW - timedelta(seconds=rng.choice([1, 2, 3]))
        _run(store, f"t{i:02d}", wait=value, runtime=value, end=end)
    client = _client(store, monkeypatch)
    full = _ids(client, f"&sort={sort}")

    for size in (1, 7, 25):
        paged: list[str] = []
        for offset in range(0, 60, size):
            url = f"/api/tasks?sort={sort}&limit={size}&offset={offset}"
            body = client.get(url).json()
            assert body["total"] == 60
            paged += [t["task_id"] for t in body["tasks"]]
        assert paged == full
    assert len(set(full)) == 60


def test_sort_rejects_an_unknown_value(mixed: TestClient) -> None:
    assert mixed.get("/api/tasks?sort=bogus").status_code == 422


@pytest.mark.parametrize("seed", range(5))
def test_tasks_without_sort_keep_the_default_order_and_item_bytes(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``sort``: newest update first, ties in arrival order, same items as with it."""
    rng = random.Random(seed)
    store = GraphStore()
    for i in range(80):
        running = rng.random() < 0.2
        _run(
            store,
            f"t{i:02d}",
            wait=rng.choice([None, 0, 4, 9]),
            runtime=None if running else rng.choice([0, 3, 8]),
            end=NOW - timedelta(seconds=rng.choice([1, 1, 2, 30])),
            state=rng.choice([TaskState.SUCCESS, TaskState.FAILURE]),
        )
    client = _client(store, monkeypatch)
    records = store.run_records()  # in the order the runs first arrived
    last = {r.task_id: r.last_updated for r in records}
    # Python's sort is stable, so equal timestamps keep arrival order.
    oracle = sorted(last, key=lambda t: last[t], reverse=True)

    plain = client.get("/api/tasks?limit=500").json()
    sorted_items = {
        t["task_id"]: t
        for t in client.get("/api/tasks?limit=500&sort=runtime").json()["tasks"]
    }

    assert [t["task_id"] for t in plain["tasks"]] == oracle
    assert plain["tasks"] == [sorted_items[t] for t in oracle]
    assert set(plain) == {"tasks", "total", "limit", "offset"}


def _tile_poc_store() -> GraphStore:
    """The review's repro: runs the p95 tile leaves out, slower than every run it counts."""
    store = GraphStore()
    _run(store, "counted", wait=128, runtime=128, end=NOW - timedelta(seconds=9))
    _run(
        store, "revoked", wait=100, runtime=3_600_000, state=TaskState.REVOKED,
        end=NOW - timedelta(seconds=1),
    )  # fmt: skip
    _run(
        store, "builtin", wait=900_000, runtime=900_000,
        name="celery.chord_unlock", end=NOW - timedelta(seconds=2),
    )  # fmt: skip
    _run(
        store, "ignored", wait=500_000, runtime=500_000, state=TaskState.IGNORED,
        end=NOW - timedelta(seconds=3),
    )  # fmt: skip
    _run(store, "running", wait=5, runtime=None, end=NOW - timedelta(seconds=4))
    _run(
        store, "rejected", wait=5, runtime=700_000, state=TaskState.REJECTED,
        end=NOW - timedelta(seconds=6),
    )  # fmt: skip
    return store


def test_sort_runtime_ranks_only_the_runs_the_p95_tile_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_tile_poc_store(), monkeypatch)

    ids = _ids(client, "&sort=runtime")

    # REVOKED, IGNORED, celery.* and running runs have no runtime sample: newest first.
    assert ids == ["counted", "revoked", "builtin", "ignored", "running", "rejected"]


def test_sort_queue_wait_ranks_only_the_runs_the_p95_tile_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_tile_poc_store(), monkeypatch)

    ids = _ids(client, "&sort=queue_wait")

    # REVOKED and REJECTED runs did wait (they count); IGNORED, celery.* and running runs do not.
    assert ids[:3] == ["counted", "revoked", "rejected"]
    assert ids[3:] == ["builtin", "ignored", "running"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("hostname=w1", ["h_slow", "h_fast"]),
        ("queue=q1", ["h_slow", "h_fast"]),
        ("exception_type=Boom", ["h_fast"]),
    ],
)
def test_sort_combines_with_hostname_queue_and_exception_type_filters(
    query: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GraphStore()
    for tid, runtime, host, queue, exc, state in [
        ("h_slow", 900, "w1", "q1", None, TaskState.SUCCESS),
        ("h_fast", 100, "w1", "q1", "Boom", TaskState.FAILURE),
        ("other", 5_000, "w2", "q2", "Bang", TaskState.FAILURE),
    ]:
        started = NOW - timedelta(milliseconds=runtime)
        store.add_event(
            TaskEvent(
                task_id=tid, name="app.work", state=TaskState.STARTED,
                timestamp=started, hostname=host, queue=queue,
            )
        )  # fmt: skip
        store.add_event(
            TaskEvent(
                task_id=tid, name="app.work", state=state, timestamp=NOW,
                hostname=host, queue=queue, exception_type=exc,
            )
        )  # fmt: skip

    ids = _ids(_client(store, monkeypatch), f"&sort=runtime&{query}")

    assert ids == expected


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        # Tail is newest first: nostart (no STARTED: no runtime, no wait) beats nosent (no PENDING: no wait).
        ("runtime", ["big", "nosent", "zero", "nostart"]),
        ("queue_wait", ["big", "zero", "nostart", "nosent"]),
    ],
)
def test_sort_finished_runs_with_no_measurable_value_come_after_a_zero(
    sort: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GraphStore()
    _run(store, "big", wait=50, runtime=50, end=NOW - timedelta(seconds=9))
    _run(store, "zero", wait=0, runtime=0, end=NOW - timedelta(seconds=8))
    # Finished, newer than "zero", but its value can't be measured.
    store.add_event(
        _ev("nostart", TaskState.PENDING, NOW - timedelta(seconds=5), "app.work")
    )
    store.add_event(
        _ev("nostart", TaskState.SUCCESS, NOW - timedelta(seconds=2), "app.work")
    )
    _run(store, "nosent", wait=None, runtime=20, end=NOW - timedelta(seconds=3))
    client = _client(store, monkeypatch)

    ids = _ids(client, f"&sort={sort}")

    assert ids == expected


def test_sort_page_one_changes_when_a_slower_run_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GraphStore()
    for i in range(5):
        _run(store, f"t{i}", wait=1, runtime=100 + i)
    client = _client(store, monkeypatch)
    url = "/api/tasks?sort=runtime&limit=3"

    before = [t["task_id"] for t in client.get(url).json()["tasks"]]
    _run(store, "newcomer", wait=1, runtime=10_000)
    after = [t["task_id"] for t in client.get(url).json()["tasks"]]

    assert before == ["t4", "t3", "t2"]
    assert after == ["newcomer", "t4", "t3"]


def _mutate_once(action: Callable[[], None]) -> Callable[[RunRecord], int]:
    """A ``sort_key`` that runs ``action`` on its first call, outside the store lock."""
    calls: list[int] = []

    def key(record: RunRecord) -> int:
        if not calls:
            action()
        calls.append(1)
        return 1

    return key


def test_sorted_page_drops_a_row_that_left_the_state_filter_meanwhile() -> None:
    store = GraphStore()
    for i, tid in enumerate(["a", "b", "c"]):
        at = NOW + timedelta(seconds=i)
        store.add_event(_ev(tid, TaskState.STARTED, at, "app.work"))
    finish = _mutate_once(
        lambda: store.add_event(
            _ev("b", TaskState.SUCCESS, NOW + timedelta(minutes=1), "app.work")
        )
    )

    page, total = store.get_nodes(state=TaskState.STARTED, sort_key=finish)

    assert sorted(n.task_id for n in page) == ["a", "c"]
    assert total == 3


def test_sorted_page_drops_a_row_evicted_meanwhile() -> None:
    store = GraphStore(max_nodes=10)
    for i in range(9):
        at = NOW + timedelta(seconds=i)
        store.add_event(_ev(f"old{i}", TaskState.STARTED, at, "app.work"))

    def flood() -> None:
        for i in range(4):
            at = NOW + timedelta(minutes=1, seconds=i)
            store.add_event(_ev(f"new{i}", TaskState.STARTED, at, "app.work"))

    page, _ = store.get_nodes(sort_key=_mutate_once(flood))

    live = {n.task_id for n in store.get_nodes(limit=500)[0]}
    assert len(live) < 13
    assert {n.task_id for n in page} <= live
    assert len(page) == len(live & {f"old{i}" for i in range(9)})


@pytest.mark.parametrize(("limit", "status"), [(500, 200), (501, 422)])
def test_task_list_limit_is_capped_at_500(
    limit: int, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(GraphStore(), monkeypatch)
    assert client.get(f"/api/tasks?limit={limit}").status_code == status


def _seed_page_of_three() -> GraphStore:
    store = GraphStore()
    for i, tid in enumerate(["a", "b", "c"]):
        store.add_event(
            TaskEvent(
                task_id=tid,
                name="app.work",
                state=TaskState.FAILURE,
                timestamp=NOW + timedelta(seconds=i),
                hostname="w1@h",
                queue="q1",
                exception_type="E1",
            )
        )
    return store


def _add_later_event_to_b(store: GraphStore, **changes: str) -> None:
    fields: dict[str, Any] = {
        "task_id": "b",
        "name": "app.work",
        "state": TaskState.FAILURE,
        "timestamp": NOW + timedelta(minutes=5),
        "hostname": "w1@h",
        "queue": "q1",
        "exception_type": "E1",
    }
    fields.update(changes)
    store.add_event(TaskEvent(**fields))


@pytest.mark.parametrize(
    ("filter_", "change"),
    [
        ({"hostname": "w1@h"}, {"hostname": "w2@h"}),
        ({"queue": "q1"}, {"queue": "q2"}),
        ({"exception_type": "E1"}, {"exception_type": "E2"}),
    ],
    ids=["hostname", "queue", "exception_type"],
)
def test_sorted_page_drops_a_row_that_stopped_matching_a_filter_meanwhile(
    filter_: dict[str, str], change: dict[str, str]
) -> None:
    store = _seed_page_of_three()
    page, total = store.get_nodes(
        sort_key=_mutate_once(lambda: _add_later_event_to_b(store, **change)),
        **filter_,
    )

    assert sorted(n.task_id for n in page) == ["a", "c"]
    assert total == 3
