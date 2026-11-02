"""Independent, deliberately slow reference for the stuck/interrupted classifier.

Written from ``.context/features/anomaly-detection.md`` alone, over raw event lists.
It must not use ``_EventIndex``, ``TaskNode`` helpers, ``RunRecord``, ``task_stats``,
``WorkerTimeline`` or ``attribution.precedence``/``latest``: the state rules are
re-expressed here. Clarity beats speed. ``SPEC-AMBIGUOUS`` marks a reading the spec
leaves open.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from stemtrace.core.events import (
    CANCELLED_RETRY_REASON,
    TaskEvent,
    TaskState,
    WorkerEvent,
    WorkerEventType,
)

__all__ = ["RefAnomaly", "reference_anomalies"]

_TERMINAL = frozenset(
    {
        TaskState.SUCCESS,
        TaskState.FAILURE,
        TaskState.REVOKED,
        TaskState.REJECTED,
        TaskState.IGNORED,
    }
)

# Fixed state order that breaks exact ties (written out from the spec's fact 11 and
# attribution.py as documentation).
_STATE_ORDER = {
    TaskState.PENDING: 0,
    TaskState.RECEIVED: 1,
    TaskState.STARTED: 2,
    TaskState.RETRY: 3,
    TaskState.SUCCESS: 4,
    TaskState.FAILURE: 5,
    TaskState.REJECTED: 6,
    TaskState.REVOKED: 7,
    TaskState.IGNORED: 8,
}
# Within one attempt STARTED and RETRY share a phase and are ordered by time.
_PHASE = {
    TaskState.PENDING: 0,
    TaskState.RECEIVED: 1,
    TaskState.STARTED: 2,
    TaskState.RETRY: 2,
}

_EVIDENCE_TYPES = frozenset(
    {
        WorkerEventType.WORKER_SHUTDOWN,
        WorkerEventType.WORKER_TERMINATING,
        WorkerEventType.WORKER_READY,
    }
)
_MS = timedelta(milliseconds=1)


@dataclass(frozen=True)
class _Settings:
    factor: float
    floor: timedelta
    ceiling: timedelta
    min_samples: int
    now: datetime


@dataclass(frozen=True)
class RefAnomaly:
    """One reference verdict for a task."""

    kind: Literal["stuck", "interrupted"]
    since: datetime
    evidence: str | None
    threshold_ms: int | None


def _utc(moment: datetime) -> datetime:
    """Aware UTC; a naive time is read as UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _content_tiebreak(event: TaskEvent) -> bytes:
    # SPEC-AMBIGUOUS (spec line 351: "(precedence, event_key) tie-break"): events equal
    # in state, retries and timestamp are split by a content digest, as event_key does.
    # Digest of the JSON dump, same recipe as attribution._digest.
    return hashlib.blake2b(event.model_dump_json().encode(), digest_size=16).digest()


def _precedence(event: TaskEvent) -> tuple[int, int, int, datetime, int]:
    """Spec fact 11: any terminal beats any non-terminal; later terminal wins (IGNORED
    loses to every other terminal); otherwise a higher attempt wins, then the phase,
    then time.
    """
    tiebreak = _STATE_ORDER[event.state]
    when = _utc(event.timestamp)
    if event.state in _TERMINAL:
        rank = -1 if event.state == TaskState.IGNORED else 0
        return (1, rank, 0, when, tiebreak)
    return (0, event.retries, _PHASE[event.state], when, tiebreak)


def _state_event(events: Sequence[TaskEvent]) -> TaskEvent:
    """The event that decides the task's state: the greatest by precedence."""
    return max(events, key=lambda e: (_precedence(e), _content_tiebreak(e)))


def _lifecycle_key(event: TaskEvent) -> tuple[int, int, datetime, int, bytes]:
    """Spec fact 12: attempt-0 PENDING/RECEIVED sort first, the rest by timestamp."""
    pre_start = event.retries == 0 and event.state in (
        TaskState.PENDING,
        TaskState.RECEIVED,
    )
    return (
        0 if pre_start else 1,
        _STATE_ORDER[event.state] if pre_start else 0,
        _utc(event.timestamp),
        _STATE_ORDER[event.state],
        _content_tiebreak(event),
    )


def _last_updated(events: Sequence[TaskEvent]) -> datetime:
    """Timestamp of the last event in lifecycle order (spec line 217 (b), fact 12)."""
    # SPEC-AMBIGUOUS (spec line 217 vs the brief "newest event time"): the spec says
    # `last_updated`, which is the last event in lifecycle order (a late-stamped
    # attempt-0 PENDING does not count once the task ran), not the max timestamp.
    return _utc(max(events, key=_lifecycle_key).timestamp)


def _attempt_started(events: Sequence[TaskEvent], state: TaskEvent) -> TaskEvent | None:
    """Spec line 233: the latest STARTED with the state event's retries."""
    started = [
        e for e in events if e.state == TaskState.STARTED and e.retries == state.retries
    ]
    if not started:
        return None
    # SPEC-AMBIGUOUS (line 233 "latest STARTED"): latest by timestamp, ties by content.
    return max(started, key=lambda e: (_utc(e.timestamp), _content_tiebreak(e)))


def _is_cancelled_retry(event: TaskEvent) -> bool:
    return event.state == TaskState.RETRY and event.exception == CANCELLED_RETRY_REASON


def _interrupted(
    events: Sequence[TaskEvent],
    state: TaskEvent,
    worker_events: Sequence[WorkerEvent],
) -> RefAnomaly | None:
    """Spec lines 230-235 and matrix rows 4, 9, 15, 17-19, 21-24."""
    host = state.hostname
    if host is None:  # row 9: cannot attribute
        return None
    cancelled = _is_cancelled_retry(state)
    if state.state != TaskState.STARTED and not cancelled:  # eligibility, line 232
        return None
    if state.state == TaskState.STARTED:
        attempt = state
    else:
        attempt_started = _attempt_started(events, state)
        if attempt_started is None:  # line 233: cancelled RETRY without its STARTED
            return None
        attempt = attempt_started
    # SPEC-AMBIGUOUS (line 233): `a` carries no hostname restriction; only the
    # worker events are matched on the state event's host H.
    after = [
        w
        for w in worker_events
        if w.hostname == host
        and w.event_type in _EVIDENCE_TYPES
        and _utc(w.timestamp) > _utc(attempt.timestamp)
    ]
    if not after:
        return None
    # Earliest such w. SPEC-AMBIGUOUS (line 235): an exact-timestamp tie is split the
    # way the D1 timeline sorts entries: (timestamp, event_type value, pid).
    first = min(after, key=lambda w: (_utc(w.timestamp), w.event_type.value, w.pid))
    evidence: str
    if cancelled:
        evidence = "cancelled_retry"  # wins over the worker type
    elif first.event_type == WorkerEventType.WORKER_READY:
        evidence = "worker_restarted"
    else:
        evidence = first.event_type.value
    return RefAnomaly("interrupted", _utc(first.timestamp), evidence, None)


def _replaced_progress(
    events: Sequence[TaskEvent], reported_parent: str | None
) -> datetime | None:
    """Rule (a), spec line 217 and matrix rows 12, 12a."""
    best: datetime | None = None
    for e in events:
        if e.state not in (TaskState.PENDING, TaskState.RECEIVED):
            continue
        # A sender PENDING with the original parent is excluded; so are retry STARTEDs
        # (not PENDING/RECEIVED). A retry RECEIVED (parent = itself) counts (row 12a).
        if e.parent_id is None or e.parent_id == reported_parent:
            continue
        # SPEC-AMBIGUOUS (line 217 "e.eta or e.timestamp"): `or` read as "is not None".
        candidate = _utc(e.eta) if e.eta is not None else _utc(e.timestamp)
        candidate = max(_utc(e.timestamp), candidate)
        if best is None or candidate > best:
            best = candidate
    return best


def _subtree_progress(
    task_id: str,
    by_task: Mapping[str, Sequence[TaskEvent]],
    reported_parent: Mapping[str, str | None],
) -> datetime | None:
    """Rule (b), spec line 217 and matrix row 13: newest last_updated below the node."""
    children: dict[str, list[str]] = {}
    for tid, parent in reported_parent.items():
        if parent is not None and tid in by_task:
            children.setdefault(parent, []).append(tid)
    best: datetime | None = None
    seen = {task_id}  # SPEC-AMBIGUOUS: the node itself is not part of its subtree.
    frontier = list(children.get(task_id, []))
    while frontier:
        current = frontier.pop()
        if current in seen:  # a reported-parent cycle must not loop
            continue
        seen.add(current)
        updated = _last_updated(by_task[current])
        if best is None or updated > best:
            best = updated
        frontier.extend(children.get(current, []))
    return best


def _stuck(
    task_id: str,
    state: TaskEvent,
    by_task: Mapping[str, Sequence[TaskEvent]],
    facts: tuple[Mapping[str, tuple[int | None, int]], Mapping[str, str | None]],
    settings: _Settings,
) -> RefAnomaly | None:
    """Spec lines 213-226 and matrix rows 1-3, 12, 12a, 13, 15, 16, 30."""
    if state.state != TaskState.STARTED:  # eligibility: RECEIVED/RETRY/PENDING never
        return None
    runtime_stats, reported_parent = facts
    started = _utc(state.timestamp)
    progress = started
    replaced = _replaced_progress(by_task[task_id], reported_parent.get(task_id))
    if replaced is not None:
        progress = max(progress, replaced)
    subtree = _subtree_progress(task_id, by_task, reported_parent)
    if subtree is not None:
        progress = max(progress, subtree)

    p95_ms, samples = runtime_stats.get(state.name, (None, 0))
    if state.name.startswith("celery."):  # row 30: no p95 row, threshold = ceiling
        p95_ms, samples = None, 0
    if samples >= settings.min_samples and p95_ms is not None:
        # SPEC-AMBIGUOUS (line 219): k x p95 is rounded by timedelta to microseconds;
        # threshold_ms is T truncated to whole milliseconds.
        threshold = max(
            timedelta(milliseconds=settings.factor * p95_ms), settings.floor
        )
    else:
        threshold = max(settings.ceiling, settings.floor)
    if _utc(settings.now) - progress > threshold:  # strict >
        return RefAnomaly("stuck", started, None, threshold // _MS)
    return None


def reference_anomalies(
    task_events: Sequence[TaskEvent],
    worker_events: Sequence[WorkerEvent],
    runtime_stats: Mapping[str, tuple[int | None, int]],
    reported_parent: Mapping[str, str | None],
    now: datetime,
    *,
    stuck_factor: float,
    stuck_floor: timedelta,
    stuck_ceiling: timedelta,
    stuck_min_samples: int,
) -> dict[str, RefAnomaly]:
    """Classify every task id with raw events; nodes with no anomaly are absent."""
    by_task: dict[str, list[TaskEvent]] = {}
    for event in task_events:
        by_task.setdefault(event.task_id, []).append(event)

    settings = _Settings(
        stuck_factor, stuck_floor, stuck_ceiling, stuck_min_samples, now
    )
    result: dict[str, RefAnomaly] = {}
    for task_id, events in by_task.items():
        state = _state_event(events)
        if state.state in _TERMINAL:  # common rule: terminal has no anomaly
            continue
        # Priority: interrupted > stuck.
        found = _interrupted(events, state, worker_events) or _stuck(
            task_id,
            state,
            by_task,
            (runtime_stats, reported_parent),
            settings,
        )
        if found is not None:
            result[task_id] = found
    return result
