"""Anomaly classification: stuck, interrupted and unregistered tasks (pure, clock injected).

Takes plain facts about each task node, per-name runtime stats, the workers'
event timeline and what the worker registry knows, and returns at most one
anomaly per node. No I/O. The server adapts its run records, worker timeline
and registry into these facts; this module does not import them.

Internal: not part of stemtrace's public API; names may change without notice.
"""

import bisect
import itertools
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Final, NamedTuple, Protocol

from stemtrace.core.events import TaskState, WorkerEventType

__all__ = [
    "DEFAULT_SETTINGS",
    "MAX_STUCK_FACTOR",
    "MAX_STUCK_THRESHOLD",
    "Anomaly",
    "AnomalyEvidence",
    "AnomalyKind",
    "AnomalyReport",
    "AnomalySettings",
    "LinkLike",
    "NodeFacts",
    "NodeLink",
    "RegistryFacts",
    "RuntimeStats",
    "TimelineEntry",
    "classify_anomalies",
]


class AnomalyKind(str, Enum):
    """What is wrong with a task, or with the fleet it waits for."""

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

# The factor's largest value and the longest ``factor x p95`` threshold: the
# server's longest accepted duration (``MAX_DURATION``, 3650 days; a test pins it).
MAX_STUCK_FACTOR: Final[int] = 100_000
MAX_STUCK_THRESHOLD: Final[timedelta] = timedelta(days=3650)

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


class LinkLike(Protocol):
    """What rule (b)'s subtree walk reads of a node that can't be flagged.

    Such a node (not STARTED, not a cancelled RETRY) still holds its ancestors'
    progress up, so it is passed as a link instead of full ``NodeFacts``. Any
    object with these attributes works, so a caller can pass its own records
    without copying them.
    """

    @property
    def task_id(self) -> str:
        """The task's ID."""
        ...

    @property
    def reported_parent_id(self) -> str | None:
        """The parent the task's events reported."""
        ...

    @property
    def last_updated(self) -> datetime:
        """The lifecycle-last event's timestamp."""
        ...


class NodeLink(NamedTuple):
    """A plain ``LinkLike``."""

    task_id: str
    reported_parent_id: str | None
    last_updated: datetime


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
class RegistryFacts:
    """What the worker registry knows, copied at one moment.

    ``online_names`` are the task names the online workers registered.
    ``authority_at`` is when the last inspect reply that listed every
    replying worker's tasks arrived, and ``broker_configured`` whether the
    server can inspect at all.
    """

    online_names: frozenset[str]
    any_online: bool
    authority_at: datetime | None
    broker_configured: bool


@dataclass(frozen=True, slots=True)
class AnomalySettings:
    """Stuck thresholds, the wait before a task is unregistered, and how fresh the registry must be.

    A task is stuck after ``max(factor x p95, floor)`` with ``min_samples``
    samples, else the ceiling. ``pending_grace`` is the wait before a PENDING
    task counts as unregistered; ``authority_window`` is how old the last
    inspect reply may be for the registry to be trusted.
    """

    stuck_factor: float = 3
    stuck_floor: timedelta = timedelta(minutes=10)
    stuck_ceiling: timedelta = timedelta(hours=1)
    stuck_min_samples: int = 5
    pending_grace: timedelta = timedelta(minutes=5)
    authority_window: timedelta = timedelta(minutes=10)


DEFAULT_SETTINGS: Final[AnomalySettings] = AnomalySettings()


@dataclass(frozen=True, slots=True, kw_only=True)
class Anomaly:
    """One flagged item: a task, a task name, or (``no_workers_online``) the fleet.

    ``task_id`` is None for a per-name item and ``name`` for the fleet item.
    ``task_count`` is how many PENDING tasks an unregistered or fleet item
    covers. The stuck fields are None for the other kinds, and vice versa.
    """

    kind: AnomalyKind
    task_id: str | None
    name: str | None
    since: datetime
    hostname: str | None = None
    task_count: int = 1
    elapsed_ms: int | None = None
    threshold_ms: int | None = None
    p95_ms: int | None = None
    runtime_samples: int | None = None
    evidence: AnomalyEvidence | None = None


@dataclass(frozen=True, slots=True)
class AnomalyReport:
    """What is flagged, by task for the node fields and as list items, and a count per kind.

    ``anomalies`` maps a task ID to the item that covers it (an unregistered
    task maps to its name's item). ``items`` is the list: each stuck or
    interrupted task, one item per unregistered name, at most one fleet
    item. ``counts`` tallies ``items`` and has every kind (0 included).
    """

    anomalies: Mapping[str, Anomaly]
    counts: Mapping[AnomalyKind, int] = field(default_factory=dict)
    items: Sequence[Anomaly] = ()


_MILLISECOND = timedelta(milliseconds=1)
_NO_STATS = RuntimeStats(None, 0)


def classify_anomalies(
    nodes: Iterable[NodeFacts],
    *,
    now: datetime,
    runtime_stats: Mapping[str, RuntimeStats],
    timeline: Mapping[str, Sequence[TimelineEntry]],
    settings: AnomalySettings = DEFAULT_SETTINGS,
    links: Iterable[LinkLike] = (),
    registry: RegistryFacts | None = None,
) -> AnomalyReport:
    """Flag the stuck, interrupted and unregistered nodes; interrupted wins over stuck.

    ``nodes`` and ``links`` together must cover every TASK node (the subtree
    rule walks them all); only ``nodes`` are classified. ``timeline`` maps a
    nodename to its worker events, ascending by timestamp. Costs O(n log n).
    """
    node_list = list(nodes)
    subtree = _subtree_progress(
        itertools.chain(node_list, links),
        [n.task_id for n in node_list if n.state == TaskState.STARTED],
    )
    found: dict[str, Anomaly] = {}
    for node in node_list:
        anomaly = _interrupted(node, timeline) or _stuck(
            node, now, runtime_stats, subtree, settings
        )
        if anomaly is not None:
            found[node.task_id] = anomaly
    items = list(found.values())
    by_node, listed = _unregistered(node_list, now, registry, settings)
    found.update(by_node)
    items.extend(listed)
    counts = dict.fromkeys(AnomalyKind, 0)
    for anomaly in items:
        counts[anomaly.kind] += 1
    return AnomalyReport(anomalies=found, counts=counts, items=items)


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


def _authoritative(
    registry: RegistryFacts, now: datetime, settings: AnomalySettings
) -> bool:
    """Whether the registry can say a name is unknown, not just not heard of yet.

    True after a recent inspect reply that listed every replying worker's
    tasks, or, with no broker to inspect, while an online worker has
    registered tasks (the events are then the only source).
    """
    confirmed = registry.authority_at
    if confirmed is not None and now - confirmed <= settings.authority_window:
        return True
    return not registry.broker_configured and bool(registry.online_names)


def _unregistered(
    nodes: Iterable[NodeFacts],
    now: datetime,
    registry: RegistryFacts | None,
    settings: AnomalySettings,
) -> tuple[dict[str, Anomaly], list[Anomaly]]:
    """The unregistered tasks by ID and their per-name items, or the one fleet item.

    A PENDING task waits past ``pending_grace`` for a name no online worker
    registered. With no worker online the names say nothing, so the waiting
    tasks make one ``no_workers_online`` item and no task is flagged.
    """
    if registry is None or not _authoritative(registry, now, settings):
        return {}, []
    waiting = [
        (node.state_since, node)
        for node in nodes
        if node.state == TaskState.PENDING
        and node.state_since is not None
        and not node.name.startswith(_BUILTIN_PREFIX)
        and now - node.state_since > settings.pending_grace
    ]
    if not waiting:
        return {}, []
    if not registry.any_online:
        oldest = min(since for since, _ in waiting)
        return {}, [
            Anomaly(
                kind=AnomalyKind.NO_WORKERS_ONLINE,
                task_id=None,
                name=None,
                since=oldest,
                task_count=len(waiting),
            )
        ]
    by_name: dict[str, list[tuple[datetime, NodeFacts]]] = defaultdict(list)
    for since, node in waiting:
        if node.name not in registry.online_names:
            by_name[node.name].append((since, node))
    by_node: dict[str, Anomaly] = {}
    items: list[Anomaly] = []
    for name, group in by_name.items():
        item = Anomaly(
            kind=AnomalyKind.UNREGISTERED,
            task_id=None,
            name=name,
            since=min(since for since, _ in group),
            task_count=len(group),
        )
        items.append(item)
        by_node.update((node.task_id, item) for _, node in group)
    return by_node, items


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
        scaled_ms = settings.stuck_factor * stats.p95_ms
        # Total for any factor: timedelta raises OverflowError above ~8.6e16 ms,
        # and inf x 0 is nan, which fails the comparison and takes the cap too.
        scaled = (
            timedelta(milliseconds=scaled_ms)
            if scaled_ms < MAX_STUCK_THRESHOLD / _MILLISECOND
            else MAX_STUCK_THRESHOLD
        )
        return max(scaled, settings.stuck_floor)
    return max(settings.stuck_ceiling, settings.stuck_floor)


def _subtree_progress(
    entries: Iterable[LinkLike], roots: Sequence[str]
) -> dict[str, datetime]:
    """Newest ``last_updated`` below each of ``roots``, walking ``reported_parent_id`` edges.

    Only roots that have descendants appear. One memoized pass; a parent cycle
    (impossible from Celery's IDs) is cut at its back edge and the nodes it
    touches are recomputed by a plain walk, so the answer stays exact.
    """
    if not roots:
        return {}
    updated: dict[str, datetime] = {}
    children: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        updated[entry.task_id] = entry.last_updated
        parent = entry.reported_parent_id
        # A parent that holds no node is never walked: roots and children are nodes.
        if parent is not None and parent != entry.task_id:
            children[parent].append(entry.task_id)
    newest: dict[str, datetime] = {}
    tainted: set[str] = set()
    on_path: set[str] = set()
    for root in roots:
        if root not in children or root in newest or root in on_path:
            continue
        _walk(
            root,
            updated=updated,
            children=children,
            newest=newest,
            tainted=tainted,
            on_path=on_path,
        )
    for task_id in tainted:
        newest.pop(task_id, None)
        newest[task_id] = _reachable_newest(task_id, updated, children)
    return newest


def _walk(
    root: str,
    *,
    updated: Mapping[str, datetime],
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
        seen = [updated[kid] for kid in kids]
        seen += [newest[kid] for kid in kids if kid in newest]
        newest[task_id] = max(seen)
        if any(kid in tainted for kid in kids):
            tainted.add(task_id)


def _reachable_newest(
    start: str,
    updated: Mapping[str, datetime],
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
        reached.append(updated[task_id])
        pending.extend(children.get(task_id, ()))
    return max(reached)
