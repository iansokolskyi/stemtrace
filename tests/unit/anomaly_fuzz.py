"""Differential fuzz of the anomaly classifier against the independent reference.

A cell is one structure (flow prefix, hosting, skew, worker events, samples,
arrival order) judged at many "now" values. The classifier runs on the real
``GraphStore`` and ``WorkerTimeline`` through the server adapter; the
reference gets the same raw events. Used by ``test_anomaly_differential.py``.
"""

import json
import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stemtrace.core.anomalies import (
    AnomalyReport,
    AnomalySettings,
    classify_anomalies,
)
from stemtrace.core.events import (
    CANCELLED_RETRY_REASON,
    TaskEvent,
    TaskState,
    WorkerEvent,
    WorkerEventType,
)
from stemtrace.server.anomaly_facts import node_facts, runtime_facts, timeline_facts
from stemtrace.server.store import GraphStore, WorkerTimeline
from tests.unit.anomaly_pipeline import load_recording
from tests.unit.anomaly_reference import RefAnomaly, reference_anomalies

DATA = Path(__file__).parents[1] / "data"
HOUR = 3600.0
SETTINGS = AnomalySettings(
    stuck_factor=3,
    stuck_floor=timedelta(seconds=1),
    stuck_ceiling=timedelta(seconds=5),
    stuck_min_samples=3,
)
# Offsets of "now" from the newest event: both sides of the floor (1 s), the
# scaled threshold (3 x 1.5 s) and the ceiling (5 s), and 2 h later.
NOW_OFFSETS = (0.0, 0.999, 1.001, 4.499, 4.501, 4.999, 5.001, 7200.0)
# Offsets from a node's own clock (state, replacement, last update).
NODE_OFFSETS = (0.999, 1.0, 1.001, 4.499, 4.5, 4.501, 4.999, 5.0, 5.001)
WORKER_TYPES = tuple(WorkerEventType)
POSITIONS = ("before", "mid", "after")
HOSTS = ("w@h1", "w@h2")
SKEWS = (-HOUR, 0.0, HOUR)
SAMPLE_RUNS = {"none": 0, "few": 2, "enough": 3}
WORKER_SIDE = frozenset(TaskState) - {TaskState.PENDING}

WorkerSpec = tuple[tuple[WorkerEventType, str, str], ...]


@dataclass(frozen=True)
class Cell:
    """One structure to classify: everything except "now"."""

    name: str
    events: tuple[TaskEvent, ...]
    hosting: str  # "one" or "two"
    skew: float  # seconds added to host w@h1's clock
    workers: WorkerSpec
    samples: str
    cancel_retries: bool
    order_seed: int

    def label(self) -> str:
        """A short description for failure messages."""
        return (
            f"{self.name}[:{len(self.events)}] {self.hosting}-host skew={self.skew} "
            f"workers={[(t.value, p, h) for t, p, h in self.workers]} "
            f"samples={self.samples} cancel={self.cancel_retries} order={self.order_seed}"
        )


def _synthetic(
    task: str, state: TaskState, seconds: float, host: str | None = None, **kw: object
) -> TaskEvent:
    return TaskEvent(
        task_id=task,
        name="syn.work",
        state=state,
        timestamp=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        + timedelta(seconds=seconds),
        hostname=host,
        **kw,  # type: ignore[arg-type]
    )


def synthetic_flows() -> dict[str, list[TaskEvent]]:
    """Cells no capture reaches (fact 14): cancelled RETRY, REVOKED, redelivery, row 12a."""
    s, r, p = TaskState.STARTED, TaskState.RECEIVED, TaskState.PENDING
    retry = TaskState.RETRY
    return {
        "syn_cancelled_retry": [
            _synthetic("c1", p, 0),
            _synthetic("c1", s, 1, "w@h1"),
            _synthetic("c1", retry, 2, "w@h1", exception=CANCELLED_RETRY_REASON),
            _synthetic("c1", s, 3, "w@h2", retries=1),
        ],
        "syn_revoked": [
            _synthetic("v1", p, 0),
            _synthetic("v1", TaskState.REVOKED, 1, "w@h1"),
            _synthetic("v2", p, 0),
            _synthetic("v2", s, 1, "w@h1"),
            _synthetic("v2", TaskState.REVOKED, 2, "w@h1"),
        ],
        "syn_redelivery_later": [
            _synthetic("d1", s, 1, "w@h1"),
            _synthetic("d1", s, 3, "w@h2"),
        ],
        "syn_redelivery_behind": [
            _synthetic("d2", s, 3, "w@h1"),
            _synthetic("d2", s, 1, "w@h2"),
        ],
        "syn_retry_12a_cross_host": [
            _synthetic("r1", s, 0, "w@h1"),
            _synthetic("r1", retry, 1, "w@h1", exception="boom", eta=None),
            _synthetic(
                "r1",
                r,
                2,
                "w@h2",
                retries=1,
                parent_id="r1",
                eta=datetime(2026, 11, 2, 12, 0, 3, tzinfo=timezone.utc),
            ),
            _synthetic("r1", s, 3, "w@h1", retries=1),
        ],
    }


def load_flows() -> dict[str, list[TaskEvent]]:
    """Every capture in ``celery_flows.json``, plus the synthetic cells."""
    raw = json.loads((DATA / "celery_flows.json").read_text())
    flows = {
        name: [TaskEvent.model_validate(e) for e in events]
        for name, events in raw.items()
    }
    return {**flows, **synthetic_flows(), **recorded_flows()}


def recorded_flows() -> dict[str, list[TaskEvent]]:
    """The task events of the real-worker recordings (M3b, M4, M5), on host w@h1.

    Their worker events are left out: cells inject their own.
    """
    flows: dict[str, list[TaskEvent]] = {}
    for name in ("m3b_cold_stop", "m4_acks_late_sigkill", "m5_sigkill_restart"):
        flows[f"rec_{name}"] = [
            e.model_copy(update={"hostname": HOSTS[0]})
            for e in load_recording(name)
            if isinstance(e, TaskEvent)
        ]
    return flows


def _shift(moment: datetime, seconds: float) -> datetime:
    return moment + timedelta(seconds=seconds)


def prepare(cell: Cell) -> tuple[list[TaskEvent], list[WorkerEvent]]:
    """The cell's raw task and worker events, hosted, skewed and shuffled."""
    ids = sorted({e.task_id for e in cell.events})
    host_of = {
        task_id: HOSTS[i % 2 if cell.hosting == "two" else 0]
        for i, task_id in enumerate(ids)
    }
    task_events: list[TaskEvent] = []
    for event in cell.events:
        if event.state not in WORKER_SIDE:
            task_events.append(event)
            continue
        host = event.hostname or host_of[event.task_id]
        update: dict[str, object] = {"hostname": host}
        if host == HOSTS[0]:
            update["timestamp"] = _shift(event.timestamp, cell.skew)
        if cell.cancel_retries and event.state == TaskState.RETRY:
            update["exception"] = CANCELLED_RETRY_REASON
        task_events.append(event.model_copy(update=update))
    stamps = [e.timestamp for e in cell.events]
    low, high = min(stamps), max(stamps)
    middle = sorted(stamps)[len(stamps) // 2]
    anchor = {
        "before": _shift(low, -1.0),
        "mid": _shift(middle, 0.001),
        "after": _shift(high, 1.0),
    }
    worker_events = [
        WorkerEvent(
            event_type=kind,
            hostname=host,
            pid=1000 + i,
            timestamp=_shift(anchor[position], cell.skew if host == HOSTS[0] else 0),
        )
        for i, (kind, position, host) in enumerate(cell.workers)
    ]
    names = sorted({e.name for e in cell.events})
    for name in names:
        for run in range(SAMPLE_RUNS[cell.samples]):
            started = _shift(low, -10_000 - run)
            task_id = f"sample-{name}-{run}"
            task_events.append(
                TaskEvent(
                    task_id=task_id,
                    name=name,
                    state=TaskState.STARTED,
                    timestamp=started,
                )
            )
            task_events.append(
                TaskEvent(
                    task_id=task_id,
                    name=name,
                    state=TaskState.SUCCESS,
                    timestamp=_shift(started, 1.5),
                )
            )
    if cell.order_seed:
        rng = random.Random(cell.order_seed)
        rng.shuffle(task_events)
        rng.shuffle(worker_events)
    return task_events, worker_events


@dataclass
class Built:
    """A cell loaded into the real store and timeline."""

    task_events: list[TaskEvent]
    worker_events: list[WorkerEvent]
    store: GraphStore
    timeline: WorkerTimeline
    base: datetime


def build(cell: Cell) -> Built:
    """Load the cell's events into a fresh store and timeline."""
    task_events, worker_events = prepare(cell)
    store = GraphStore()
    for event in task_events:
        store.add_event(event)
    timeline = WorkerTimeline()
    for worker in worker_events:
        timeline.record(worker)
    base = max(
        [e.timestamp for e in task_events if not e.task_id.startswith("sample-")]
        + [w.timestamp for w in worker_events]
    )
    return Built(task_events, worker_events, store, timeline, base)


def candidate_nows(built: Built) -> list[datetime]:
    """Base-relative offsets plus offsets from each node's own clocks."""
    found = {_shift(built.base, d) for d in NOW_OFFSETS}
    for record in built.store.run_records():
        if record.state != TaskState.STARTED:
            continue
        for clock in (
            record.state_since,
            record.replaced_progress,
            record.last_updated,
        ):
            if clock is not None:
                found.update(_shift(clock, d) for d in NODE_OFFSETS)
    return sorted(found)


def classify_fast(built: Built, now: datetime) -> AnomalyReport:
    """The classifier through the server adapter."""
    records = built.store.run_records()
    return classify_anomalies(
        node_facts(records),
        now=now,
        runtime_stats=runtime_facts(records, now),
        timeline=timeline_facts(built.timeline.snapshot()),
        settings=SETTINGS,
    )


def classify_reference(built: Built, now: datetime) -> dict[str, RefAnomaly]:
    """The reference over the same raw events, with stats and parents from the store."""
    records = built.store.run_records()
    stats = runtime_facts(records, now)
    parents: dict[str, str | None] = {}
    for record in records:
        node = built.store.get_node(record.task_id)
        parents[record.task_id] = None if node is None else node.reported_parent_id
    # The reference reads the name off the state event; spec line 218 says "the
    # node's name" (a ``replace()`` replacement carries another name, the
    # graph keeps the task's own). It is fed the node's name on every event.
    names = {r.task_id: r.name for r in records}
    task_events = [
        e.model_copy(update={"name": names[e.task_id]}) for e in built.task_events
    ]
    return reference_anomalies(
        task_events,
        built.worker_events,
        {name: (s.p95_ms, s.runtime_samples) for name, s in stats.items()},
        parents,
        now,
        stuck_factor=SETTINGS.stuck_factor,
        stuck_floor=SETTINGS.stuck_floor,
        stuck_ceiling=SETTINGS.stuck_ceiling,
        stuck_min_samples=SETTINGS.stuck_min_samples,
    )


def mismatches(built: Built, now: datetime, terminal: frozenset[str]) -> list[str]:
    """Differences between classifier and reference, and broken invariants."""
    fast = classify_fast(built, now).anomalies
    ref = classify_reference(built, now)
    problems: list[str] = []
    if set(fast) != set(ref):
        problems.append(f"flagged ids differ: fast={sorted(fast)} ref={sorted(ref)}")
    for task_id in set(fast) & set(ref):
        a, r = fast[task_id], ref[task_id]
        got = (
            a.kind.value,
            a.since,
            None if a.evidence is None else a.evidence.value,
            a.threshold_ms if a.kind.value == "stuck" else None,
        )
        if got != (r.kind, r.since, r.evidence, r.threshold_ms):
            problems.append(f"{task_id}: fast={got} ref={r}")
    problems += [f"terminal node flagged: {t}" for t in fast if t in terminal]
    if built.worker_events:
        without = _without_workers(built, now)
        for task_id, anomaly in fast.items():
            if anomaly.kind.value == "stuck" and task_id not in without:
                problems.append(f"stuck {task_id} not stuck without workers")
            if anomaly.kind.value == "interrupted" and anomaly.elapsed_ms is not None:
                problems.append(f"interrupted {task_id} carries stuck fields")
    return problems


def _without_workers(built: Built, now: datetime) -> dict[str, object]:
    records = built.store.run_records()
    return dict(
        classify_anomalies(
            node_facts(records),
            now=now,
            runtime_stats=runtime_facts(records, now),
            timeline={},
            settings=SETTINGS,
        ).anomalies
    )


def terminal_ids(built: Built) -> frozenset[str]:
    """IDs of nodes whose state is terminal."""
    done = {
        TaskState.SUCCESS,
        TaskState.FAILURE,
        TaskState.REVOKED,
        TaskState.REJECTED,
        TaskState.IGNORED,
    }
    return frozenset(r.task_id for r in built.store.run_records() if r.state in done)


def run_cell(cell: Cell, nows: Sequence[datetime] | None = None) -> list[str]:
    """Judge one cell at every candidate "now"; returns labelled problems."""
    built = build(cell)
    terminal = frozenset(terminal_ids(built))
    found: list[str] = []
    for now in nows if nows is not None else candidate_nows(built):
        found += [
            f"{cell.label()} now={now.isoformat()}: {p}"
            for p in mismatches(built, now, terminal)
        ]
    return found


def worker_specs() -> list[WorkerSpec]:
    """No worker events, or one of each type x position x host."""
    return [()] + [
        ((kind, position, host),)
        for kind in WORKER_TYPES
        for position in POSITIONS
        for host in HOSTS
    ]


def prefixes(
    flows: dict[str, list[TaskEvent]],
) -> Iterator[tuple[str, tuple[TaskEvent, ...]]]:
    """Every prefix (1..n events) of every flow."""
    for name, events in flows.items():
        for k in range(1, len(events) + 1):
            yield f"{name}", tuple(events[:k])


def full_cells(flows: dict[str, list[TaskEvent]]) -> Iterator[Cell]:
    """The whole structured product (the full sweep, sharded by the caller)."""
    for name, events in prefixes(flows):
        has_retry = any(e.state == TaskState.RETRY for e in events)
        for hosting in ("one", "two"):
            for skew in SKEWS:
                for workers in worker_specs():
                    for samples in SAMPLE_RUNS:
                        for cancel in (False, True) if has_retry else (False,):
                            yield Cell(
                                name, events, hosting, skew, workers, samples, cancel, 0
                            )


def random_cell(rng: random.Random, flows: dict[str, list[TaskEvent]]) -> Cell:
    """One cell drawn from the same space, with arrival order and up to 3 worker events."""
    # Captures hold almost none of the interesting cells; favor the synthetic ones.
    pool = [n for n in sorted(flows) if n.startswith(("syn_", "rec_"))]
    name = rng.choice(pool if rng.random() < 0.3 else sorted(flows))
    events = tuple(flows[name][: rng.randint(1, len(flows[name]))])
    workers = tuple(
        (rng.choice(WORKER_TYPES), rng.choice(POSITIONS), rng.choice(HOSTS))
        for _ in range(rng.choice((0, 1, 1, 2, 3)))
    )
    return Cell(
        name,
        events,
        rng.choice(("one", "two")),
        rng.choice(SKEWS),
        workers,
        rng.choice(list(SAMPLE_RUNS)),
        rng.random() < 0.3,
        rng.choice((0, rng.randint(1, 10_000))),
    )
