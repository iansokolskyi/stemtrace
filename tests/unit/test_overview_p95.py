"""``/api/overview`` p95 runtime and queue wait, and ``/api/stats/timeseries?percentiles=p95``.

Spec: ``overview-p95-and-polish`` section 1 (matrix A1-A9). A run counts at its
``last_updated`` under its current state, like every aggregate. The fleet runtime p95
equals the p95 of the union of the Registry rows' samples (the oracle below uses
``task_stats``'s own group rollup); the queue-wait oracle reads ``TaskNode.queue_wait_ms``.
"""

import itertools
import json
import random
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.stats import (
    Window,
    WindowSamples,
    collect_samples,
    p95,
    task_stats,
)
from stemtrace.server.store import GraphStore, WorkerRegistry

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


@pytest.fixture
def store() -> GraphStore:
    return GraphStore()


def _client(store: GraphStore) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))
    return TestClient(app)


def _run(
    store: GraphStore,
    *,
    end: datetime,
    runtime_ms: int | None = 1000,
    wait_ms: int | None = 100,
    state: TaskState = TaskState.SUCCESS,
    name: str = "app.work",
) -> None:
    """A run that ends ``state`` at ``end``: PENDING, STARTED and an end event."""
    task_id = f"run-{next(_ids)}"
    started = end - timedelta(milliseconds=runtime_ms or 0)

    def add(event_state: TaskState, at: datetime) -> None:
        store.add_event(
            TaskEvent(task_id=task_id, name=name, state=event_state, timestamp=at)
        )

    if wait_ms is not None:
        add(TaskState.PENDING, started - timedelta(milliseconds=wait_ms))
    if runtime_ms is not None:
        add(TaskState.STARTED, started)
    add(state, end)


def _overview(store: GraphStore, within: str = "1h") -> dict[str, Any]:
    response = _client(store).get(f"/api/overview?within={within}")
    assert response.status_code == 200
    return response.json()


def _runtime(body: dict[str, Any]) -> dict[str, Any]:
    return body["p95_runtime_ms"]


def _wait(body: dict[str, Any]) -> dict[str, Any]:
    return body["p95_queue_wait_ms"]


# --- /api/overview ---------------------------------------------------------


def test_a1_no_samples_gives_null_values_and_zero_counts(store: GraphStore) -> None:
    body = _overview(store)

    empty = {"value": None, "baseline": None, "samples": 0, "baseline_samples": 0}
    assert _runtime(body) == empty
    assert _wait(body) == empty


def test_a2_one_sample_is_the_value(store: GraphStore) -> None:
    _run(store, end=NOW - timedelta(minutes=5), runtime_ms=1234, wait_ms=56)

    body = _overview(store)

    assert _runtime(body) == {
        "value": 1234,
        "baseline": None,
        "samples": 1,
        "baseline_samples": 0,
    }
    assert _wait(body) == {
        "value": 56,
        "baseline": None,
        "samples": 1,
        "baseline_samples": 0,
    }


def test_a3_twenty_samples_give_rank_nineteen(store: GraphStore) -> None:
    for i in range(1, 21):
        _run(store, end=NOW - timedelta(minutes=i), runtime_ms=i, wait_ms=i * 10)

    body = _overview(store)

    assert _runtime(body)["value"] == 19
    assert _runtime(body)["samples"] == 20
    assert _wait(body)["value"] == 190
    assert _wait(body)["samples"] == 20


@pytest.mark.parametrize("n", [1, 2, 19, 20, 21, 39, 40, 41, 59, 60, 61, 99, 100, 101])
def test_a4_rank_is_the_integer_nearest_rank(store: GraphStore, n: int) -> None:
    for i in range(1, n + 1):
        _run(store, end=NOW - timedelta(seconds=i), runtime_ms=i, wait_ms=i)

    body = _overview(store)

    want = -(-Fraction(95 * n, 100) // 1)
    assert _runtime(body)["value"] == want
    assert _wait(body)["value"] == want


def test_a5_a_run_stamped_after_now_is_in_the_current_window(
    store: GraphStore,
) -> None:
    _run(store, end=NOW + timedelta(minutes=10), runtime_ms=700, wait_ms=70)

    body = _overview(store)

    assert _runtime(body)["samples"] == 1
    assert _runtime(body)["baseline_samples"] == 0
    assert _wait(body)["value"] == 70


def test_a6_a_run_exactly_at_the_window_start_is_current_not_baseline(
    store: GraphStore,
) -> None:
    _run(store, end=NOW - HOUR, runtime_ms=900, wait_ms=90)

    body = _overview(store)

    assert _runtime(body)["samples"] == 1
    assert _runtime(body)["baseline_samples"] == 0


def test_a6_a_run_just_before_the_window_start_is_baseline(store: GraphStore) -> None:
    _run(store, end=NOW - HOUR - timedelta(milliseconds=1), runtime_ms=900, wait_ms=90)

    body = _overview(store)

    assert _runtime(body) == {
        "value": None,
        "baseline": 900,
        "samples": 0,
        "baseline_samples": 1,
    }
    assert _wait(body)["baseline"] == 90


def test_the_baseline_is_the_window_of_equal_length_before(store: GraphStore) -> None:
    _run(store, end=NOW - timedelta(minutes=10), runtime_ms=100, wait_ms=10)
    _run(store, end=NOW - timedelta(minutes=90), runtime_ms=300, wait_ms=30)
    _run(store, end=NOW - timedelta(minutes=121), runtime_ms=999, wait_ms=99)

    body = _overview(store)

    assert _runtime(body) == {
        "value": 100,
        "baseline": 300,
        "samples": 1,
        "baseline_samples": 1,
    }
    assert _wait(body)["baseline"] == 30


def test_a8_the_value_is_the_windows_p95_not_a_mean_of_bucket_p95s(
    store: GraphStore,
) -> None:
    # Bucket 11:00: one run of 100 ms. Bucket 11:05: 20 runs of 1..20 s (p95 19 s).
    # The window's 21 samples have p95 at rank 20 (19 s); the mean of the two
    # bucket p95s is 9.55 s.
    _run(store, end=datetime(2026, 10, 18, 11, 1, tzinfo=timezone.utc), runtime_ms=100)
    for i in range(1, 21):
        end = datetime(2026, 10, 18, 11, 6, tzinfo=timezone.utc) + timedelta(seconds=i)
        _run(store, end=end, runtime_ms=i * 1000)
    client = _client(store)

    overview = client.get("/api/overview?within=1h").json()
    series = client.get(
        "/api/stats/timeseries?within=1h&bucket=5m&percentiles=p95"
    ).json()

    per_bucket = [v for v in series["p95_runtime_ms"] if v is not None]
    assert per_bucket == [100, 19000]
    assert _runtime(overview)["value"] == 19000
    assert sum(per_bucket) / len(per_bucket) == 9550


def test_non_terminal_runs_give_no_samples(store: GraphStore) -> None:
    for state, minutes in [(TaskState.PENDING, 3), (TaskState.STARTED, 2)]:
        store.add_event(
            TaskEvent(
                task_id="r",
                name="app.work",
                state=state,
                timestamp=NOW - timedelta(minutes=minutes),
            )
        )

    body = _overview(store)

    assert _runtime(body)["samples"] == 0
    assert _wait(body)["samples"] == 0


def test_celery_builtin_tasks_give_no_samples(store: GraphStore) -> None:
    _run(store, end=NOW - timedelta(minutes=5), name="celery.chord_unlock")
    _run(store, end=NOW - timedelta(minutes=5), runtime_ms=40, wait_ms=4)

    body = _overview(store)

    assert _runtime(body)["samples"] == 1
    assert _runtime(body)["value"] == 40
    assert _wait(body)["samples"] == 1
    assert _wait(body)["value"] == 4


def test_revoked_after_start_waited_but_has_no_runtime_sample(
    store: GraphStore,
) -> None:
    _run(store, end=NOW - timedelta(minutes=5), state=TaskState.REVOKED, wait_ms=250)
    _run(store, end=NOW - timedelta(minutes=5), state=TaskState.FAILURE, wait_ms=50)

    body = _overview(store)

    assert _runtime(body)["samples"] == 1
    assert _wait(body)["samples"] == 2
    assert _wait(body)["value"] == 250


def test_ignored_runs_give_no_samples(store: GraphStore) -> None:
    _run(store, end=NOW - timedelta(minutes=5), state=TaskState.IGNORED)

    body = _overview(store)

    assert _runtime(body)["samples"] == 0
    assert _wait(body)["samples"] == 0


def test_a_run_without_a_sent_event_gives_runtime_but_no_wait(
    store: GraphStore,
) -> None:
    _run(store, end=NOW - timedelta(minutes=5), runtime_ms=500, wait_ms=None)

    body = _overview(store)

    assert _runtime(body)["samples"] == 1
    assert _wait(body)["samples"] == 0
    assert _wait(body)["value"] is None


def test_wait_and_runtime_come_from_different_events(store: GraphStore) -> None:
    _run(store, end=NOW - timedelta(minutes=5), runtime_ms=7000, wait_ms=300)

    body = _overview(store)

    assert _runtime(body)["value"] == 7000
    assert _wait(body)["value"] == 300


def test_samples_count_samples_not_runs(store: GraphStore) -> None:
    for _ in range(3):
        _run(store, end=NOW - timedelta(minutes=5), runtime_ms=None, wait_ms=None)
    _run(store, end=NOW - timedelta(minutes=5), runtime_ms=10, wait_ms=None)

    body = _overview(store)

    assert body["runs"]["value"] == 4
    assert _runtime(body)["samples"] == 1


# --- /api/stats/timeseries -------------------------------------------------


def test_a9_without_percentiles_the_response_keeps_its_keys(store: GraphStore) -> None:
    _run(store, end=NOW - timedelta(minutes=5))

    body = _client(store).get("/api/stats/timeseries?within=1h&bucket=5m").json()

    assert sorted(body) == [
        "bucket_seconds",
        "by_task_group",
        "counts",
        "starts",
        "window",
    ]


def test_timeseries_percentiles_are_per_bucket_with_sample_counts(
    store: GraphStore,
) -> None:
    base = datetime(2026, 10, 18, 11, 10, tzinfo=timezone.utc)
    _run(store, end=base + timedelta(minutes=1), runtime_ms=100, wait_ms=10)
    _run(store, end=base + timedelta(minutes=2), runtime_ms=300, wait_ms=None)
    _run(store, end=base + timedelta(minutes=21), runtime_ms=50, wait_ms=5)

    body = (
        _client(store)
        .get("/api/stats/timeseries?within=1h&bucket=10m&percentiles=p95")
        .json()
    )

    starts = body["starts"]
    first = starts.index("2026-10-18T11:10:00Z")
    third = starts.index("2026-10-18T11:30:00Z")
    for key in (
        "p95_runtime_ms",
        "p95_queue_wait_ms",
        "runtime_samples",
        "queue_wait_samples",
    ):
        assert len(body[key]) == len(starts), key
    assert body["p95_runtime_ms"][first] == 300
    assert body["runtime_samples"][first] == 2
    assert body["p95_queue_wait_ms"][first] == 10
    assert body["queue_wait_samples"][first] == 1
    assert body["p95_runtime_ms"][third] == 50
    assert body["p95_runtime_ms"][first + 1] is None
    assert body["runtime_samples"][first + 1] == 0
    assert body["counts"]["SUCCESS"][first] == 2


def test_timeseries_places_a_future_run_in_the_last_bucket(store: GraphStore) -> None:
    _run(store, end=NOW + timedelta(minutes=30), runtime_ms=111, wait_ms=11)

    body = (
        _client(store)
        .get("/api/stats/timeseries?within=1h&bucket=5m&percentiles=p95")
        .json()
    )

    assert body["p95_runtime_ms"][-1] == 111
    assert body["p95_queue_wait_ms"][-1] == 11
    assert [v for v in body["p95_runtime_ms"] if v is not None] == [111]


def test_timeseries_celery_builtin_tasks_give_no_samples(store: GraphStore) -> None:
    _run(store, end=NOW - timedelta(minutes=5), name="celery.chord_unlock")

    body = (
        _client(store)
        .get("/api/stats/timeseries?within=1h&bucket=5m&percentiles=p95")
        .json()
    )

    assert sum(body["runtime_samples"]) == 0
    assert sum(body["queue_wait_samples"]) == 0
    assert body["counts"]["SUCCESS"][-2] == 1  # still counted as a run by state


def test_timeseries_skips_runs_before_the_first_bucket(store: GraphStore) -> None:
    _run(store, end=NOW - timedelta(hours=3), runtime_ms=111)

    body = (
        _client(store)
        .get("/api/stats/timeseries?within=1h&bucket=5m&percentiles=p95")
        .json()
    )

    assert sum(body["runtime_samples"]) == 0


def test_timeseries_percentiles_with_group_by_is_422(store: GraphStore) -> None:
    response = _client(store).get(
        "/api/stats/timeseries?within=1h&group_by=task_group&percentiles=p95"
    )

    assert response.status_code == 422


@pytest.mark.parametrize("value", ["p99", "p50", "", "P95"])
def test_timeseries_unknown_percentiles_is_422(store: GraphStore, value: str) -> None:
    response = _client(store).get(f"/api/stats/timeseries?percentiles={value}")

    assert response.status_code == 422


# --- differential over the recorded flows ---------------------------------


def _all_flows_store(seed: int) -> GraphStore:
    """Every recorded flow, replayed in one store; orders shuffled per seed."""
    rng = random.Random(seed)
    store = GraphStore()
    for flow in sorted(_CAPTURES):
        events = [TaskEvent.model_validate(e) for e in _CAPTURES[flow]]
        if seed:
            rng.shuffle(events)
        for event in events:
            store.add_event(event)
    return store


# Written out here, not imported: the oracle must not share the code under test.
_OUTCOME = {
    TaskState.SUCCESS,
    TaskState.FAILURE,
    TaskState.REVOKED,
    TaskState.REJECTED,
}
_RUNTIME = {TaskState.SUCCESS, TaskState.FAILURE}


def _oracle_samples(store: GraphStore, window: Window) -> tuple[list[int], list[int]]:
    """Sorted runtime and wait samples read off the nodes, not through ``RunRecord``."""
    runtime: list[int] = []
    wait: list[int] = []
    with store._lock:
        nodes = list(store._graph.nodes.values())
    for node in nodes:
        if (
            node.node_type != NodeType.TASK
            or node.name.startswith("celery.")
            or node.last_updated is None
            or not window.contains(node.last_updated)
        ):
            continue
        if node.state in _RUNTIME and node.runtime_ms is not None:
            runtime.append(node.runtime_ms)
        if node.state in _OUTCOME and node.queue_wait_ms is not None:
            wait.append(node.queue_wait_ms)
    return sorted(runtime), sorted(wait)


def _random_window(store: GraphStore, rng: random.Random) -> Window:
    stamps = sorted(r.last_updated for r in store.run_records())
    low, high = stamps[0], stamps[-1]
    span = (high - low).total_seconds()
    start = low + timedelta(seconds=rng.uniform(-1, 1) * span / 2)
    end = start + timedelta(seconds=rng.uniform(0.1, 1.5) * span)
    # Land some edges exactly on a run's stamp (boundary rows A5 and A6).
    if rng.random() < 0.5:
        start = rng.choice(stamps)
    if rng.random() < 0.5:
        end = rng.choice(stamps)
    return Window(start, max(end, start), open_end=rng.random() < 0.5)


@pytest.mark.parametrize("seed", range(6))
def test_a7_fleet_p95_equals_the_p95_of_the_registry_rows_samples(seed: int) -> None:
    store = _all_flows_store(seed)
    records = store.run_records()
    rng = random.Random(seed)
    measured = 0
    for _ in range(25):
        window = _random_window(store, rng)
        samples = WindowSamples()
        collect_samples(samples, records, window)
        # The Registry's group rollup, with everything in one group, is the union
        # of every row's samples.
        _, groups = task_stats(records, window, lambda _name: "all")
        registry_p95 = groups[0].p95_ms if groups else None
        runtime, wait = _oracle_samples(store, window)

        assert p95(samples.runtime) == registry_p95
        assert len(samples.runtime) == (groups[0].runtime_samples if groups else 0)
        assert sorted(samples.runtime) == runtime
        assert sorted(samples.queue_wait) == wait
        measured += len(runtime) + len(wait)

    assert measured > 0  # the windows reach real samples


def test_the_flows_give_runtime_and_wait_samples_and_celery_builtins() -> None:
    store = _all_flows_store(0)
    records = store.run_records()

    samples = WindowSamples()
    collect_samples(samples, records, Window(NOW - timedelta(days=9999), NOW, True))

    assert len(samples.runtime) > 20
    assert len(samples.queue_wait) > 20
    assert any(r.name.startswith("celery.") for r in records)


@pytest.mark.parametrize("seed", range(3))
def test_overview_endpoint_matches_the_oracle_on_the_flows(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _all_flows_store(seed)
    now = max(r.last_updated for r in store.run_records()) + timedelta(minutes=1)
    monkeypatch.setattr(params, "utcnow", lambda: now)
    client = _client(store)

    for within in ("1h", "1d", "7d", "30d"):
        window = Window(now - params.parse_within(within), now, open_end=True)
        body = client.get(f"/api/overview?within={within}").json()
        runtime, wait = _oracle_samples(store, window)
        base_runtime, base_wait = _oracle_samples(store, window.previous())

        assert _runtime(body)["samples"] == len(runtime), within
        assert _runtime(body)["value"] == _nearest(runtime), within
        assert _runtime(body)["baseline_samples"] == len(base_runtime), within
        assert _runtime(body)["baseline"] == _nearest(base_runtime), within
        assert _wait(body)["samples"] == len(wait), within
        assert _wait(body)["value"] == _nearest(wait), within
        assert _wait(body)["baseline_samples"] == len(base_wait), within
        assert _wait(body)["baseline"] == _nearest(base_wait), within


def _nearest(sorted_samples: list[int]) -> int | None:
    """Nearest-rank p95 as a definition: the first value with 95 % at or below it."""
    n = len(sorted_samples)
    for i, value in enumerate(sorted_samples, start=1):
        if i * 100 >= 95 * n:
            return value
    return None
