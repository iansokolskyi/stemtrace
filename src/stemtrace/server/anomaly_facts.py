"""Adapt the store's run records and the worker timeline into classifier facts.

``core/anomalies.py`` can't import ``RunRecord`` or ``WorkerTimelineEntry``, so
the server converts them here. Pure and O(n log n); callers take the two
snapshots under their own locks and run this off both.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import NamedTuple

from stemtrace.core.anomalies import (
    LinkLike,
    NodeFacts,
    RuntimeStats,
    TimelineEntry,
)
from stemtrace.core.events import TaskState
from stemtrace.server.stats import MAX_DURATION, RunRecord, Window, task_stats
from stemtrace.server.store import WorkerTimelineEntry

__all__ = [
    "ClassifierFacts",
    "classifier_facts",
    "node_facts",
    "runtime_facts",
    "timeline_facts",
]


def node_facts(records: Iterable[RunRecord]) -> list[NodeFacts]:
    """One ``NodeFacts`` per record that has a state event."""
    return [
        NodeFacts(
            task_id=r.task_id,
            name=r.name,
            state=r.state,
            last_updated=r.last_updated,
            state_since=r.state_since,
            state_host=r.state_host,
            cancelled=r.cancelled,
            reported_parent_id=r.reported_parent_id,
            attempt_started=r.attempt_started,
            replaced_progress=r.replaced_progress,
        )
        for r in records
    ]


def timeline_facts(
    snapshot: Mapping[str, Sequence[WorkerTimelineEntry]],
) -> dict[str, list[TimelineEntry]]:
    """The worker timeline as classifier entries, each nodename ascending."""
    return {
        node: [TimelineEntry(e.timestamp, e.event_type) for e in entries]
        for node, entries in snapshot.items()
    }


def runtime_facts(
    records: Iterable[RunRecord],
    now: datetime,
    names: Collection[str] | None = None,
) -> dict[str, RuntimeStats]:
    """Per-name ``(p95_ms, runtime_samples)`` over the whole retention.

    The open-ended window from ``now - MAX_DURATION`` equals whole retention
    without the endpoint's extra pass for the oldest record. With ``names``,
    only those names are computed (a name's row reads only its own records).
    """
    if names is not None:
        records = [r for r in records if r.name in names]
    rows, _ = task_stats(
        records, Window(now - MAX_DURATION, now, open_end=True), lambda name: name
    )
    return {row.name: RuntimeStats(row.p95_ms, row.runtime_samples) for row in rows}


class ClassifierFacts(NamedTuple):
    """Keyword arguments of ``classify_anomalies`` that come from the store."""

    nodes: list[NodeFacts]
    links: list[LinkLike]
    runtime_stats: dict[str, RuntimeStats]
    timeline: dict[str, list[TimelineEntry]]


def classifier_facts(
    records: Iterable[RunRecord],
    snapshot: Mapping[str, Sequence[WorkerTimelineEntry]],
    now: datetime,
) -> ClassifierFacts:
    """Facts for ``classify_anomalies``, built only for what can be flagged.

    Only a STARTED task or a cold-stop cancelled RETRY can be flagged, so only
    those get full ``NodeFacts``; other records pass on as they are, to keep
    their ancestors' subtree progress. Runtime stats cover the names of the
    STARTED tasks, the only ones judged by a p95.
    """
    records = list(records)
    nodes: list[NodeFacts] = []
    links: list[LinkLike] = []
    started_names: set[str] = set()
    for r in records:
        if r.state in (TaskState.STARTED, TaskState.PENDING) or (
            r.state == TaskState.RETRY and r.cancelled
        ):
            nodes.extend(node_facts((r,)))
            if r.state == TaskState.STARTED:
                started_names.add(r.name)
        else:
            links.append(r)
    return ClassifierFacts(
        nodes,
        links,
        runtime_facts(records, now, started_names) if started_names else {},
        timeline_facts(snapshot),
    )
