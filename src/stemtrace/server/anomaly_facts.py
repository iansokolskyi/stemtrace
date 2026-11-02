"""Adapt the store's run records and the worker timeline into classifier facts.

``core/anomalies.py`` can't import ``RunRecord`` or ``WorkerTimelineEntry``, so
the server converts them here. Pure and O(n log n); callers take the two
snapshots under their own locks and run this off both.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from stemtrace.core.anomalies import NodeFacts, RuntimeStats, TimelineEntry
from stemtrace.server.stats import MAX_DURATION, RunRecord, Window, task_stats
from stemtrace.server.store import WorkerTimelineEntry

__all__ = ["node_facts", "runtime_facts", "timeline_facts"]


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
    records: Iterable[RunRecord], now: datetime
) -> dict[str, RuntimeStats]:
    """Per-name ``(p95_ms, runtime_samples)`` over the whole retention.

    The open-ended window from ``now - MAX_DURATION`` equals whole retention
    without the endpoint's extra pass for the oldest record.
    """
    rows, _ = task_stats(
        records, Window(now - MAX_DURATION, now, open_end=True), lambda name: name
    )
    return {row.name: RuntimeStats(row.p95_ms, row.runtime_samples) for row in rows}
