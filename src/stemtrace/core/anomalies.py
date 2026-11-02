"""Anomaly classification: which tasks are stuck or interrupted (pure, clock injected).

Takes plain facts about each task node, per-name runtime stats and the workers'
event timeline, and returns at most one anomaly per node. No I/O. The server
adapts its run records and worker timeline into these facts; this module does
not import them.

Internal: not part of stemtrace's public API; names may change without notice.
"""

import bisect
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Final, NamedTuple

from stemtrace.core.events import TaskState, WorkerEventType

__all__ = [
    "DEFAULT_SETTINGS",
    "Anomaly",
    "AnomalyEvidence",
    "AnomalyKind",
    "AnomalyReport",
    "AnomalySettings",
    "NodeFacts",
    "RuntimeStats",
    "TimelineEntry",
    "classify_anomalies",
]


class AnomalyKind(str, Enum):
    """What is wrong with a task. Only the first two are classified so far."""

    STUCK = "stuck"
    INTERRUPTED = "interrupted"
    UNREGISTERED = "unregistered"
    NO_WORKERS_ONLINE = "no_workers_online"


class AnomalyEvidence(str, Enum):
    """What shows an interrupted task's worker stopped while it ran."""

    WORKER_SHUTDOWN = "worker_shutdown"
    WORKER_TERMINATING = "worker_terminating"
    WORKER_RESTARTED = "worker_restarted"
    CANCELLED_RETRY = "cancelled_retry"


_EVIDENCE_OF: Final[dict[WorkerEventType, AnomalyEvidence]] = {
    WorkerEventType.WORKER_SHUTDOWN: AnomalyEvidence.WORKER_SHUTDOWN,
    WorkerEventType.WORKER_TERMINATING: AnomalyEvidence.WORKER_TERMINATING,
    WorkerEventType.WORKER_READY: AnomalyEvidence.WORKER_RESTARTED,
}

# Celery's built-ins get no p95 (they have no stats row), so they use the ceiling.
_BUILTIN_PREFIX: Final[str] = "celery."


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeFacts:
    """What the classifier needs to know about one TASK node.

    Times are timezone-aware. ``state_since``/``state_host`` belong to the
    event that decides ``state``; ``attempt_started`` is the latest STARTED of
    that event's attempt.
    """

    task_id: str
    name: str
    state: TaskState
    last_updated: datetime
    state_since: datetime | None = None
    state_host: str | None = None
    cancelled: bool = False
    reported_parent_id: str | None = None
    attempt_started: datetime | None = None
    replaced_progress: datetime | None = None


class TimelineEntry(NamedTuple):
    """One worker event as the worker's own clock stamped it."""

    timestamp: datetime
    event_type: WorkerEventType


@dataclass(frozen=True, slots=True)
class RuntimeStats:
    """Runtime samples of one task name: the nearest-rank p95 and how many."""

    p95_ms: int | None
    runtime_samples: int


@dataclass(frozen=True, slots=True)
class AnomalySettings:
    """Stuck thresholds: ``max(factor x p95, floor)`` with ``min_samples`` samples, else the ceiling."""

    stuck_factor: float = 3
    stuck_floor: timedelta = timedelta(minutes=10)
    stuck_ceiling: timedelta = timedelta(hours=1)
    stuck_min_samples: int = 5


DEFAULT_SETTINGS: Final[AnomalySettings] = AnomalySettings()


@dataclass(frozen=True, slots=True, kw_only=True)
class Anomaly:
    """One flagged task. The stuck fields are None for interrupted, and vice versa."""

    kind: AnomalyKind
    task_id: str
    name: str
    since: datetime
    hostname: str | None = None
    elapsed_ms: int | None = None
    threshold_ms: int | None = None
    p95_ms: int | None = None
    runtime_samples: int | None = None
    evidence: AnomalyEvidence | None = None


@dataclass(frozen=True, slots=True)
class AnomalyReport:
    """The anomalies by task ID, and a count for every kind (0 included)."""

    anomalies: Mapping[str, Anomaly]
    counts: Mapping[AnomalyKind, int] = field(default_factory=dict)


_MILLISECOND = timedelta(milliseconds=1)
_NO_STATS = RuntimeStats(None, 0)


def classify_anomalies(
    nodes: Iterable[NodeFacts],
    *,
    now: datetime,
    runtime_stats: Mapping[str, RuntimeStats],
    timeline: Mapping[str, Sequence[TimelineEntry]],
    settings: AnomalySettings = DEFAULT_SETTINGS,
) -> AnomalyReport:
    """Flag the stuck and interrupted nodes; interrupted wins over stuck.

    ``nodes`` must hold every TASK node (the subtree rule walks them all).
    ``timeline`` maps a nodename to its worker events, ascending by timestamp.
    Costs O(n log n): one pass for subtree progress, one bisect per node.
    """
    node_list = list(nodes)
    subtree = _subtree_progress(node_list)
    found: dict[str, Anomaly] = {}
    for node in node_list:
        anomaly = _interrupted(node, timeline) or _stuck(
            node, now, runtime_stats, subtree, settings
        )
        if anomaly is not None:
            found[node.task_id] = anomaly
    counts = dict.fromkeys(AnomalyKind, 0)
    for anomaly in found.values():
        counts[anomaly.kind] += 1
    return AnomalyReport(anomalies=found, counts=counts)


def _interrupted(
    node: NodeFacts, timeline: Mapping[str, Sequence[TimelineEntry]]
) -> Anomaly | None:
    """The node's worker reported a stop or restart after the attempt started."""
    host = node.state_host
    if host is None or node.state_since is None:
        return None
    attempt_started: datetime | None
    if node.state == TaskState.STARTED:
        attempt_started = node.state_since
    elif node.state == TaskState.RETRY and node.cancelled:
        # Compared with the attempt's STARTED, not the RETRY: the RETRY may be
        # stamped before or after WORKER_TERMINATING.
        attempt_started = node.attempt_started
    else:
        return None
    if attempt_started is None:
        return None
    entries = timeline.get(host, ())
    first = bisect.bisect_right(entries, attempt_started, key=_timestamp)
    if first == len(entries):
        return None
    stop = entries[first]
    evidence = (
        AnomalyEvidence.CANCELLED_RETRY
        if node.cancelled
        else _EVIDENCE_OF[stop.event_type]
    )
    return Anomaly(
        kind=AnomalyKind.INTERRUPTED,
        task_id=node.task_id,
        name=node.name,
        since=stop.timestamp,
        hostname=host,
        evidence=evidence,
    )


def _timestamp(entry: TimelineEntry) -> datetime:
    return entry.timestamp


def _stuck(
    node: NodeFacts,
    now: datetime,
    runtime_stats: Mapping[str, RuntimeStats],
    subtree: Mapping[str, datetime],
    settings: AnomalySettings,
) -> Anomaly | None:
    """A STARTED node with no sign of progress for longer than its threshold."""
    started = node.state_since
    if node.state != TaskState.STARTED or started is None:
        return None
    progress = max(
        started,
        node.replaced_progress or started,
        subtree.get(node.task_id, started),
    )
    stats = (
        _NO_STATS
        if node.name.startswith(_BUILTIN_PREFIX)
        else runtime_stats.get(node.name, _NO_STATS)
    )
    threshold = _threshold(stats, settings)
    if now - progress <= threshold:
        return None
    return Anomaly(
        kind=AnomalyKind.STUCK,
        task_id=node.task_id,
        name=node.name,
        since=started,
        hostname=node.state_host,
        elapsed_ms=(now - progress) // _MILLISECOND,
        threshold_ms=threshold // _MILLISECOND,
        p95_ms=stats.p95_ms,
        runtime_samples=stats.runtime_samples,
    )


def _threshold(stats: RuntimeStats, settings: AnomalySettings) -> timedelta:
    """``max(k x p95, floor)`` with enough samples, else ``max(ceiling, floor)``."""
    if stats.p95_ms is not None and stats.runtime_samples >= settings.stuck_min_samples:
        scaled = timedelta(milliseconds=settings.stuck_factor * stats.p95_ms)
        return max(scaled, settings.stuck_floor)
    return max(settings.stuck_ceiling, settings.stuck_floor)


def _subtree_progress(nodes: Sequence[NodeFacts]) -> dict[str, datetime]:
    """Newest ``last_updated`` below each node, walking ``reported_parent_id`` edges.

    Only nodes that have descendants appear. One memoized pass; a parent cycle
    (impossible from Celery's IDs) is cut at its back edge and the nodes it
    touches are recomputed by a plain walk, so the answer stays exact.
    """
    by_id = {node.task_id: node for node in nodes}
    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        parent = node.reported_parent_id
        if parent is not None and parent in by_id and parent != node.task_id:
            children[parent].append(node.task_id)
    newest: dict[str, datetime] = {}
    tainted: set[str] = set()
    on_path: set[str] = set()
    for root in children:
        if root in newest or root in on_path:
            continue
        _walk(
            root,
            by_id=by_id,
            children=children,
            newest=newest,
            tainted=tainted,
            on_path=on_path,
        )
    for task_id in tainted:
        newest.pop(task_id, None)
        newest[task_id] = _reachable_newest(task_id, by_id, children)
    return newest


def _walk(
    root: str,
    *,
    by_id: Mapping[str, NodeFacts],
    children: Mapping[str, list[str]],
    newest: dict[str, datetime],
    tainted: set[str],
    on_path: set[str],
) -> None:
    """Fill ``newest`` for ``root`` and everything below it (iterative post-order)."""
    stack: list[tuple[str, int]] = [(root, 0)]
    on_path.add(root)
    while stack:
        task_id, index = stack.pop()
        kids = children.get(task_id, ())
        if index < len(kids):
            stack.append((task_id, index + 1))
            kid = kids[index]
            if kid in on_path:
                tainted.add(task_id)
            elif kid not in newest and kid in children:
                on_path.add(kid)
                stack.append((kid, 0))
            continue
        on_path.discard(task_id)
        # ``kids`` is never empty: only nodes that have children are walked.
        seen = [by_id[kid].last_updated for kid in kids]
        seen += [newest[kid] for kid in kids if kid in newest]
        newest[task_id] = max(seen)
        if any(kid in tainted for kid in kids):
            tainted.add(task_id)


def _reachable_newest(
    start: str,
    by_id: Mapping[str, NodeFacts],
    children: Mapping[str, list[str]],
) -> datetime:
    """Newest ``last_updated`` among the nodes reachable below ``start`` (not itself).

    ``start`` has a child other than itself, so something is always reachable.
    """
    seen = {start}
    pending = list(children[start])
    reached: list[datetime] = []
    while pending:
        task_id = pending.pop()
        if task_id in seen:
            continue
        seen.add(task_id)
        reached.append(by_id[task_id].last_updated)
        pending.extend(children.get(task_id, ()))
    return max(reached)
