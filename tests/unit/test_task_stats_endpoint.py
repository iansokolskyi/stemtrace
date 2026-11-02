"""``GET /api/stats/tasks`` against a brute-force oracle read off the store's nodes.

The store holds every recorded flow of ``tests/data/celery_flows.json`` at once,
in a shuffled order, with the clocks of a third of the flows skewed by an hour.
The oracle shares nothing with the aggregate but the nodes: it walks
``store.get_nodes`` and writes each rule out with its own literals.
"""

import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import TaskNode
from stemtrace.core.groups import GroupRule
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.groups import build_group_config, group_lookup
from stemtrace.server.store import GraphStore, WorkerRegistry

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
GROUPS = [
    GroupRule(name="adders", match=["flows.add*"]),
    GroupRule(name="troublemakers", match=["flows.fail", "flows.flaky*", "flows.re*"]),
]
# Minutes before NOW at which a flow ends: some inside 1m and 1h, some outside.
_AGES_MIN = [0.2, 0.5, 5, 30, 59, 61, 90, 240]

_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)


def _events(seed: int) -> list[TaskEvent]:
    """Every capture, moved to end ``_AGES_MIN`` before NOW (+-1h skew on a third), shuffled."""
    rng = random.Random(seed)
    out: list[TaskEvent] = []
    for index, flow in enumerate(sorted(_CAPTURES)):
        events = [TaskEvent.model_validate(e) for e in _CAPTURES[flow]]
        end = max(e.timestamp for e in events)
        shift = NOW - timedelta(minutes=rng.choice(_AGES_MIN)) - end
        if index % 3 == 0:
            shift += rng.choice([-HOUR, HOUR])
        out.extend(
            e.model_copy(update={"timestamp": e.timestamp + shift}) for e in events
        )
    rng.shuffle(out)
    return out


def _store(seed: int) -> GraphStore:
    store = GraphStore(max_nodes=100_000)
    for event in _events(seed):
        store.add_event(event)
    return store


def _app(store: GraphStore) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_api_router(store, worker_registry=WorkerRegistry(), groups=GROUPS)
    )
    return app


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(params, "utcnow", lambda: NOW)


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _nearest_rank(samples: list[int], percent: int) -> int:
    ordered = sorted(samples)
    return ordered[math.ceil(Fraction(percent * len(ordered), 100)) - 1]


# Spelled out, not imported from stemtrace.server.stats.
_OUTCOMES = {
    TaskState.SUCCESS,
    TaskState.FAILURE,
    TaskState.REVOKED,
    TaskState.REJECTED,
}
_SAMPLED = {TaskState.SUCCESS, TaskState.FAILURE}


def _oracle(
    nodes: list[TaskNode], start: datetime | None, name: str | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Per-name and per-group stats from the nodes, one rule at a time."""
    group_of = group_lookup(build_group_config(GROUPS))
    by_name: dict[str, list[TaskNode]] = defaultdict(list)
    for node in nodes:
        if node.last_updated is None or node.name.startswith("celery."):
            continue
        if name is None or node.name == name:
            by_name[node.name].append(node)

    rows: dict[str, dict[str, Any]] = {}
    samples_of_group: dict[str, list[int]] = defaultdict(list)
    groups: dict[str, dict[str, Any]] = {}
    for task_name, members in by_name.items():
        placed = [n for n in members if start is None or n.last_updated >= start]
        runs = [n for n in placed if n.state in _OUTCOMES]
        samples = [
            n.runtime_ms
            for n in runs
            if n.state in _SAMPLED and n.runtime_ms is not None
        ]
        newest = max(members, key=lambda n: (n.last_updated, n.task_id))
        row = {
            "runs": len(runs),
            "failures": sum(n.state == TaskState.FAILURE for n in runs),
            "samples": sorted(samples),
            "last_run": newest.last_updated,
            "last_state": newest.state,
            "group": group_of(task_name).name,
        }
        rows[task_name] = row
        group = groups.setdefault(
            row["group"],
            {"runs": 0, "failures": 0, "names": 0, "last_run": newest.last_updated},
        )
        group["runs"] += row["runs"]
        group["failures"] += row["failures"]
        group["names"] += 1
        group["last_run"] = max(group["last_run"], newest.last_updated)
        samples_of_group[row["group"]] += samples
    for group_name, group in groups.items():
        group["samples"] = sorted(samples_of_group[group_name])
    return rows, groups


def _assert_percentiles(body: dict[str, Any], samples: list[int]) -> None:
    assert body["runtime_samples"] == len(samples)
    if samples:
        assert body["p50_ms"] == _nearest_rank(samples, 50)
        assert body["p95_ms"] == _nearest_rank(samples, 95)
    else:
        assert (body["p50_ms"], body["p95_ms"]) == (None, None)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


WITHINS = [("1m", NOW - timedelta(minutes=1)), ("1h", NOW - HOUR), (None, None)]


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize(("within", "start"), WITHINS)
async def test_endpoint_matches_the_brute_force_oracle(
    seed: int, within: str | None, start: datetime | None
) -> None:
    store = _store(seed)
    nodes, _ = store.get_nodes(limit=100_000)
    want_rows, want_groups = _oracle(nodes, start)

    response = await _get(
        _app(store), "/api/stats/tasks" + (f"?within={within}" if within else "")
    )

    assert response.status_code == 200
    body = response.json()
    assert [t["name"] for t in body["tasks"]] == sorted(want_rows)
    for task in body["tasks"]:
        want = want_rows[task["name"]]
        assert (task["runs"], task["failures"]) == (want["runs"], want["failures"])
        assert task["task_group"] == want["group"]
        assert task["failure_rate"] == (
            want["failures"] / want["runs"] if want["runs"] else None
        )
        _assert_percentiles(task, want["samples"])
        assert task["last_run"] == _iso(want["last_run"])
        assert task["last_state"] == want["last_state"].value
    assert [g["name"] for g in body["groups"]] == sorted(want_groups)
    for group in body["groups"]:
        want = want_groups[group["name"]]
        assert (group["runs"], group["failures"], group["task_count"]) == (
            want["runs"],
            want["failures"],
            want["names"],
        )
        _assert_percentiles(group, want["samples"])
        assert group["last_run"] == _iso(want["last_run"])


async def test_oracle_inputs_are_not_vacuous() -> None:
    """The captures hold what the exclusions and the window sweep need."""
    store = _store(0)
    nodes, _ = store.get_nodes(limit=100_000)
    states = {n.state for n in nodes}
    names = {n.name for n in nodes}

    assert {TaskState.SUCCESS, TaskState.FAILURE, TaskState.IGNORED} <= states
    assert any(n.startswith("celery.") for n in names)
    assert any(n.runtime_ms is not None for n in nodes)
    # Every window of the sweep ends up with different runs.
    counts = {
        within: sum(r["runs"] for r in _oracle(nodes, start)[0].values())
        for within, start in WITHINS
    }
    assert 0 < counts["1m"] < counts["1h"] < counts[None]


@pytest.mark.parametrize("seed", [0, 1])
@pytest.mark.parametrize(("within", "start"), WITHINS)
async def test_endpoint_invariants_hold_in_every_window(
    seed: int, within: str | None, start: datetime | None
) -> None:
    store = _store(seed)
    app = _app(store)
    nodes, _ = store.get_nodes(limit=100_000)
    observed = defaultdict(set)
    for node in nodes:
        if node.state in _SAMPLED and node.runtime_ms is not None:
            observed[node.name].add(node.runtime_ms)
    query = f"?within={within}" if within else ""

    body = (await _get(app, "/api/stats/tasks" + query)).json()

    for task in body["tasks"]:
        assert task["failures"] <= task["runs"]
        assert task["runtime_samples"] <= task["runs"]
        assert (task["failure_rate"] is None) == (task["runs"] == 0)
        if task["p95_ms"] is not None:
            assert task["p50_ms"] <= task["p95_ms"]
            assert task["p50_ms"] in observed[task["name"]]
            assert task["p95_ms"] in observed[task["name"]]
    for group in body["groups"]:
        assert group["failures"] <= group["runs"]
        assert (group["failure_rate"] is None) == (group["runs"] == 0)
        if group["p95_ms"] is not None:
            assert group["p50_ms"] <= group["p95_ms"]
    assert sum(g["runs"] for g in body["groups"]) == sum(
        t["runs"] for t in body["tasks"]
    )
    assert sum(g["task_count"] for g in body["groups"]) == len(body["tasks"])
    assert not any(t["name"].startswith("celery.") for t in body["tasks"])

    # Same runs as the overview of the same window, less celery.* built-ins.
    overview = (await _get(app, f"/api/overview?within={within or '3650d'}")).json()
    builtins = sum(
        n.state in _OUTCOMES
        and n.name.startswith("celery.")
        and n.last_updated is not None
        and (start is None or n.last_updated >= start)
        for n in nodes
    )
    assert builtins > 0 or within == "1m"
    assert (
        sum(g["runs"] for g in body["groups"]) == overview["runs"]["value"] - builtins
    )


async def test_endpoint_with_name_returns_only_that_name_and_its_rollup() -> None:
    store = _store(0)
    nodes, _ = store.get_nodes(limit=100_000)
    want_rows, _ = _oracle(nodes, None, name="flows.add")

    body = (await _get(_app(store), "/api/stats/tasks?name=flows.add")).json()

    assert [t["name"] for t in body["tasks"]] == ["flows.add"]
    assert body["tasks"][0]["runs"] == want_rows["flows.add"]["runs"] > 0
    assert [(g["name"], g["task_count"]) for g in body["groups"]] == [("adders", 1)]


async def test_endpoint_with_unknown_name_returns_no_rows() -> None:
    body = (await _get(_app(_store(0)), "/api/stats/tasks?name=nope")).json()

    assert (body["tasks"], body["groups"]) == ([], [])


async def test_endpoint_excludes_ignored_revoked_and_synthetic_nodes() -> None:
    store = GraphStore()
    t = NOW - timedelta(minutes=10)

    def add(task_id: str, state: TaskState, seconds: int, **extra: Any) -> None:
        store.add_event(
            TaskEvent(
                task_id=task_id,
                name=extra.pop("name", "app.work"),
                state=state,
                timestamp=t + timedelta(seconds=seconds),
                **extra,
            )
        )

    add("ok", TaskState.STARTED, 0)
    add("ok", TaskState.SUCCESS, 2)  # sample 2000 ms
    add("ign", TaskState.STARTED, 0)
    add("ign", TaskState.IGNORED, 1)  # not a run
    add("rev", TaskState.STARTED, 0)
    add("rev", TaskState.REVOKED, 30)  # a run, no sample (D2)
    add("m0", TaskState.SUCCESS, 1, group_id="g1", name="app.member")
    add("m1", TaskState.SUCCESS, 1, group_id="g1", name="app.member")
    add("cu", TaskState.SUCCESS, 1, name="celery.chord_unlock")
    assert store.get_node("group:g1") is not None  # the synthetic node exists

    body = (await _get(_app(store), "/api/stats/tasks?within=1h")).json()

    rows = {t["name"]: t for t in body["tasks"]}
    assert set(rows) == {"app.work", "app.member"}
    work = rows["app.work"]
    assert (work["runs"], work["failures"], work["runtime_samples"]) == (2, 0, 1)
    assert work["p50_ms"] == work["p95_ms"] == 2000
    assert rows["app.member"]["runs"] == 2
    assert sum(g["runs"] for g in body["groups"]) == 4


async def test_endpoint_window_response_and_errors() -> None:
    app = _app(_store(0))

    body = (await _get(app, "/api/stats/tasks?within=1h")).json()
    assert body["window"] == {"start": _iso(NOW - HOUR), "end": _iso(NOW)}

    assert (await _get(app, "/api/stats/tasks?within=banana")).status_code == 422
    both = await _get(app, f"/api/stats/tasks?within=1h&from_date={_iso(NOW - HOUR)}")
    assert both.status_code == 422
    future = await _get(app, f"/api/stats/tasks?from_date={_iso(NOW + HOUR)}")
    assert future.status_code == 422


async def test_endpoint_from_date_equals_the_same_within() -> None:
    app = _app(_store(1))

    by_within = (await _get(app, "/api/stats/tasks?within=1h")).json()
    by_date = (await _get(app, f"/api/stats/tasks?from_date={_iso(NOW - HOUR)}")).json()

    assert by_date == by_within


async def test_endpoint_on_an_empty_store_returns_empty_lists() -> None:
    body = (await _get(_app(GraphStore()), "/api/stats/tasks")).json()

    assert (body["tasks"], body["groups"]) == ([], [])
