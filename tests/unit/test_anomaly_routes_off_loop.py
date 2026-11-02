"""The anomaly facts pass classifies off the event loop and off the store's locks."""

import asyncio
import threading
from typing import Any

import pytest

from stemtrace.core.events import TaskState
from stemtrace.server.api import anomaly_routes
from tests.unit.anomaly_api import freeze_clock, get, make_app, seeded
from tests.unit.anomaly_pipeline import READY, task_event, worker_event

PATHS = ["/api/anomalies", "/api/tasks", "/api/graphs", "/api/tasks/s1"]
LOCK_WAIT_S = 3
LOOP_WAIT_S = 5


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(monkeypatch)


@pytest.mark.parametrize("path", PATHS)
async def test_classification_runs_in_a_worker_thread_with_both_locks_free(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, timeline = seeded()
    loop_thread = threading.get_ident()
    seen: dict[str, Any] = {}
    real = anomaly_routes.classify_anomalies

    def probing(*args: Any, **kwargs: Any) -> Any:
        seen["thread"] = threading.get_ident()

        def ingest() -> None:
            # Each needs a store lock; a second thread, because an RLock lets
            # the classifying thread itself back in.
            store.add_event(task_event("late", TaskState.STARTED, 900, host="w@a"))
            timeline.record(worker_event(READY, 900, host="w@a"))
            store.run_records()
            timeline.snapshot()

        other = threading.Thread(target=ingest)
        other.start()
        other.join(LOCK_WAIT_S)
        seen["locks_free"] = not other.is_alive()
        try:
            return real(*args, **kwargs)
        finally:
            other.join()  # after the classification, so a held lock can't deadlock it

    monkeypatch.setattr(anomaly_routes, "classify_anomalies", probing)

    response = await get(make_app(store, timeline), path)

    assert response.status_code == 200
    assert seen["thread"] != loop_thread
    assert seen["locks_free"] is True


@pytest.mark.parametrize("path", PATHS)
async def test_event_loop_keeps_running_while_the_classification_is_busy(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, timeline = seeded()
    released = threading.Event()
    seen: dict[str, bool] = {}
    real = anomaly_routes.classify_anomalies

    def blocked_until_the_loop_ticks(*args: Any, **kwargs: Any) -> Any:
        seen["loop_ticked"] = released.wait(LOOP_WAIT_S)
        return real(*args, **kwargs)

    async def tick() -> None:
        await asyncio.sleep(0.05)
        released.set()

    monkeypatch.setattr(
        anomaly_routes, "classify_anomalies", blocked_until_the_loop_ticks
    )

    response, _ = await asyncio.gather(get(make_app(store, timeline), path), tick())

    assert response.status_code == 200
    assert seen["loop_ticked"] is True
