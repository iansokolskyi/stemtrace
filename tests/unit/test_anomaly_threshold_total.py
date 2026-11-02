"""The stuck threshold never raises, whatever the factor and the p95 (a 500 on every list)."""

import pytest

from stemtrace.core.anomalies import (
    MAX_STUCK_THRESHOLD,
    AnomalySettings,
    NodeFacts,
    RuntimeStats,
    classify_anomalies,
)
from stemtrace.core.events import TaskState
from stemtrace.server.stats import MAX_DURATION
from stemtrace.server.store import GraphStore
from tests.unit.anomaly_api import freeze_clock, get, make_app
from tests.unit.anomaly_pipeline import at, completed_runs, task_event

MAX_MS = int(MAX_DURATION.total_seconds() * 1000)


def test_the_threshold_cap_is_the_retention_cap() -> None:
    assert MAX_STUCK_THRESHOLD == MAX_DURATION


P95S = [1, 3_600_000, MAX_MS]
# 1: the smallest; 100_000: the largest the settings accept; the rest only reach
# the classifier through a hand-built ``AnomalySettings`` (public in core/).
FACTORS = [1, 100_000, 100_001, 1e15, 1e308, float("inf")]


def _running(task_id: str = "run") -> NodeFacts:
    return NodeFacts(
        task_id=task_id,
        name="app.work",
        state=TaskState.STARTED,
        last_updated=at(0),
        state_since=at(0),
    )


@pytest.mark.parametrize("p95_ms", P95S)
@pytest.mark.parametrize("factor", FACTORS)
def test_classifier_with_any_factor_and_p95_does_not_raise(
    factor: float, p95_ms: int
) -> None:
    report = classify_anomalies(
        [_running()],
        now=at(100 * 86400),
        runtime_stats={"app.work": RuntimeStats(p95_ms, 5)},
        timeline={},
        settings=AnomalySettings(stuck_factor=factor),
    )

    for anomaly in report.anomalies.values():
        assert anomaly.threshold_ms is not None
        assert anomaly.threshold_ms <= MAX_MS


@pytest.mark.parametrize("p95_ms", P95S)
@pytest.mark.parametrize("factor", FACTORS)
@pytest.mark.parametrize(
    "path", ["/api/tasks", "/api/graphs", "/api/anomalies", "/api/tasks/run"]
)
async def test_endpoints_with_any_factor_and_p95_return_200(
    factor: float, p95_ms: int, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_clock(monkeypatch, 100 * 86400)
    store = GraphStore()
    # Five finished runs of that p95, then one task that started and never ended.
    for event in completed_runs("app.work", 5, p95_ms / 1000):
        store.add_event(event)
    store.add_event(
        task_event("run", TaskState.STARTED, 0, name="app.work", host="w@a")
    )

    response = await get(
        make_app(store, settings=AnomalySettings(stuck_factor=factor)), path
    )

    assert response.status_code == 200, response.text


async def test_endpoint_p95_at_the_retention_cap_gets_a_threshold_of_at_most_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_clock(monkeypatch, 4000 * 86400)
    store = GraphStore()
    for event in completed_runs("app.work", 5, MAX_DURATION.total_seconds()):
        store.add_event(event)
    store.add_event(
        task_event("run", TaskState.STARTED, 0, name="app.work", host="w@a")
    )
    app = make_app(store, settings=AnomalySettings(stuck_factor=100_000))

    body = (await get(app, "/api/anomalies")).json()

    assert [a["threshold_ms"] for a in body["anomalies"]] == [MAX_MS]
