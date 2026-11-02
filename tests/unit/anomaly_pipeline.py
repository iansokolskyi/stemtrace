"""Shared harness: raw events -> real store and timeline -> adapter -> classifier."""

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stemtrace.core.anomalies import (
    AnomalyReport,
    AnomalySettings,
    classify_anomalies,
)
from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.server.anomaly_facts import node_facts, runtime_facts, timeline_facts
from stemtrace.server.store import GraphStore, WorkerTimeline

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
READY = WorkerEventType.WORKER_READY
SHUTDOWN = WorkerEventType.WORKER_SHUTDOWN
TERMINATING = WorkerEventType.WORKER_TERMINATING

# Small thresholds so both branches of T are reachable in seconds.
SMALL = AnomalySettings(
    stuck_factor=3,
    stuck_floor=timedelta(seconds=60),
    stuck_ceiling=timedelta(seconds=300),
    stuck_min_samples=3,
)


def at(seconds: float) -> datetime:
    """A time ``seconds`` after ``T0``."""
    return T0 + timedelta(seconds=seconds)


def task_event(
    task_id: str,
    state: TaskState,
    seconds: float,
    *,
    name: str = "app.work",
    host: str | None = None,
    retries: int = 0,
    parent: str | None = None,
    eta: float | None = None,
    exception: str | None = None,
) -> TaskEvent:
    """A task event stamped ``seconds`` after ``T0``."""
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=state,
        timestamp=at(seconds),
        hostname=host,
        retries=retries,
        parent_id=parent,
        eta=None if eta is None else at(eta),
        exception=exception,
    )


def worker_event(
    event_type: WorkerEventType, seconds: float, *, host: str = "w@a", pid: int = 1
) -> WorkerEvent:
    """A worker event stamped ``seconds`` after ``T0``."""
    return WorkerEvent(
        event_type=event_type, hostname=host, pid=pid, timestamp=at(seconds)
    )


def completed_runs(
    name: str, count: int, runtime_s: float, *, prefix: str = "done"
) -> list[TaskEvent]:
    """``count`` finished runs of ``name`` that each took ``runtime_s``, long ago."""
    events: list[TaskEvent] = []
    for i in range(count):
        task_id = f"{prefix}-{name}-{i}"
        events.append(task_event(task_id, TaskState.STARTED, -10_000, name=name))
        events.append(
            task_event(task_id, TaskState.SUCCESS, -10_000 + runtime_s, name=name)
        )
    return events


def classify(
    task_events: Iterable[TaskEvent],
    worker_events: Iterable[WorkerEvent] = (),
    *,
    now: float | datetime,
    settings: AnomalySettings = SMALL,
    max_nodes: int = 10_000,
    timeline: WorkerTimeline | None = None,
) -> AnomalyReport:
    """Feed the raw events to a real store and timeline, then classify."""
    store = GraphStore(max_nodes=max_nodes)
    for event in task_events:
        store.add_event(event)
    timeline = WorkerTimeline() if timeline is None else timeline
    for worker in worker_events:
        timeline.record(worker)
    moment = now if isinstance(now, datetime) else at(now)
    records = store.run_records()
    return classify_anomalies(
        node_facts(records),
        now=moment,
        runtime_stats=runtime_facts(records, moment),
        timeline=timeline_facts(timeline.snapshot()),
        settings=settings,
    )


def load_recording(name: str) -> list[TaskEvent | WorkerEvent]:
    """The events of a real-worker recording in ``tests/data``, in stream order."""
    path = Path(__file__).parents[1] / "data" / f"anomaly_{name}.json"
    entries: list[TaskEvent | WorkerEvent] = []
    for raw in json.loads(path.read_text())["entries"]:
        model = WorkerEvent if "event_type" in raw else TaskEvent
        entries.append(model.model_validate(raw))
    return entries


def through_nth_ready(
    entries: list[TaskEvent | WorkerEvent], n: int
) -> list[TaskEvent | WorkerEvent]:
    """The stream up to and including its ``n``-th ``worker_ready``.

    A polled snapshot may already hold events published right after that
    ``worker_ready`` (a redelivery lands within ~20 ms), so judging "as of the
    restart" must cut at the event, not at the poll.
    """
    seen = 0
    for index, entry in enumerate(entries):
        if isinstance(entry, WorkerEvent) and entry.event_type == READY:
            seen += 1
            if seen == n:
                return entries[: index + 1]
    raise AssertionError(f"fewer than {n} worker_ready events")


def sample_runs(
    name: str,
    runtimes: Iterable[float],
    *,
    at_s: float = -10_000,
    prefix: str = "sample",
) -> list[TaskEvent]:
    """One finished run of ``name`` per runtime, each stamped ``at_s`` seconds from ``T0``."""
    events: list[TaskEvent] = []
    for i, runtime in enumerate(runtimes):
        task_id = f"{prefix}-{name}-{i}"
        events.append(task_event(task_id, TaskState.STARTED, at_s, name=name))
        events.append(task_event(task_id, TaskState.SUCCESS, at_s + runtime, name=name))
    return events
