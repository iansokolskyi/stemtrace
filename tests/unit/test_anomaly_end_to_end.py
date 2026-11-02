"""Events through a real consumer and store: ``/api/anomalies`` equals the classifier."""

import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, WorkerEvent
from stemtrace.server.api import params, routes
from stemtrace.server.fastapi.extension import StemtraceExtension
from tests.unit.anomaly_api import NOW_S, seeded_events
from tests.unit.anomaly_pipeline import SMALL, at, classify

DEADLINE_S = 10


class _ReplayTransport:
    """Hands the consumer its events once, then waits until stopped."""

    def __init__(self, events: list[TaskEvent | WorkerEvent]) -> None:
        self._events = events
        self.consumed = 0
        self._stop = False

    def consume(self) -> Iterator[TaskEvent | WorkerEvent]:
        for event in self._events:
            yield event
            self.consumed += 1
        while not self._stop:
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop = True


def test_anomalies_after_a_real_consumer_equal_the_classifier_on_the_same_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks, workers = seeded_events()
    stream: list[TaskEvent | WorkerEvent] = [*tasks, *workers]
    transport = _ReplayTransport(stream)
    now = at(NOW_S)
    monkeypatch.setattr(params, "utcnow", lambda: now)

    async def no_inspect(_self: object) -> None:
        """The extension has a broker URL; don't wait on a real inspect."""

    monkeypatch.setattr(routes._InspectRefresher, "maybe_refresh", no_inspect)
    extension = StemtraceExtension(
        broker_url="memory://",
        serve_ui=False,
        anomaly_stuck_factor=SMALL.stuck_factor,
        anomaly_stuck_floor=SMALL.stuck_floor,
        anomaly_stuck_ceiling=SMALL.stuck_ceiling,
        anomaly_stuck_min_samples=SMALL.stuck_min_samples,
    )
    consumer = extension.consumer
    assert consumer is not None
    app = FastAPI()
    app.include_router(extension.router)

    with patch("stemtrace.server.consumer.get_transport", return_value=transport):
        consumer.start()
        try:
            deadline = time.monotonic() + DEADLINE_S
            while transport.consumed < len(stream) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert transport.consumed == len(stream)
            body = TestClient(app).get("/api/anomalies").json()
        finally:
            transport.stop()
            consumer.stop(timeout=2.0)

    report = classify(tasks, workers, now=now, settings=SMALL)
    expected = sorted(report.anomalies.values(), key=lambda a: (a.since, a.task_id))
    assert expected, "the scenario must hold anomalies"
    got: list[dict[str, Any]] = body["anomalies"]
    assert [
        (a["kind"], a["task_id"], a["name"], a["hostname"], a["since"]) for a in got
    ] == [
        (
            a.kind.value,
            a.task_id,
            a.name,
            a.hostname,
            a.since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for a in expected
    ]
    assert [(a["elapsed_ms"], a["threshold_ms"], a["evidence"]) for a in got] == [
        (
            a.elapsed_ms,
            a.threshold_ms,
            a.evidence.value if a.evidence is not None else None,
        )
        for a in expected
    ]
    assert body["counts"] == {k.value: v for k, v in report.counts.items()}
    assert body["total"] == len(expected)
