"""Attribution: which run, attempt and worker a task event belongs to.

Pure functions over a task's events (no I/O). They decide the order of a
node's events, which event decides its state, how events pair into runs, which
events are the task's own first attempt (ETA, queue wait), and which event
describes it (worker, queue, routing key, exception). ``TaskNode`` and
``TaskGraph`` in ``core.graph`` delegate here.

Internal: not part of stemtrace's public API; names may change without notice.
"""

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from pydantic_core import PydanticSerializationError

from stemtrace.core.events import TaskEvent, TaskState

__all__ = [
    "TERMINAL_STATES",
    "EventKey",
    "eta",
    "event_key",
    "is_pre_start",
    "latest",
    "precedence",
    "queue_wait_ms",
    "run_times",
    "with_aware_timestamp",
]

TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.SUCCESS,
        TaskState.FAILURE,
        TaskState.REVOKED,
        TaskState.REJECTED,
        TaskState.IGNORED,
    }
)

# Lifecycle phase of non-terminal states within one attempt. STARTED and RETRY
# share a phase: within an attempt they are ordered by time, because a run
# redelivered after a connection-loss RETRY reuses the same retries count.
_PHASE: dict[TaskState, int] = {
    TaskState.PENDING: 0,
    TaskState.RECEIVED: 1,
    TaskState.STARTED: 2,
    TaskState.RETRY: 2,
}

# Fixed state order used to break exact timestamp ties, both for the order of
# a node's events and for which event decides its state. It also orders the
# pre-start events (PENDING before RECEIVED) ahead of their timestamps. Keep in
# sync with STATE_PRIORITY in the UI (frontend/src/components/TimelineBar.tsx).
_STATE_ORDER: dict[TaskState, int] = {
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

# (pre-start group, pre-start state order, timestamp, state order, content digest)
EventKey = tuple[int, int, datetime, int, bytes]


def with_aware_timestamp(event: TaskEvent) -> TaskEvent:
    """Return the event with a naive timestamp interpreted as UTC.

    A node's events are compared by timestamp, and comparing naive with aware
    datetimes raises ``TypeError``.

    Args:
        event: The event to normalize.

    Returns:
        The event itself if its timestamp is aware, else a copy in UTC.
    """
    if event.timestamp.tzinfo is not None:
        return event
    return event.model_copy(
        update={"timestamp": event.timestamp.replace(tzinfo=timezone.utc)}
    )


_PRE_START = (TaskState.PENDING, TaskState.RECEIVED)


def is_pre_start(event: TaskEvent) -> bool:
    """Whether the event belongs before the first run of the task.

    The sender's PENDING (``after_task_publish`` fires after publish) can be stamped
    after the worker already started or even finished the task, so its
    timestamp can't place it.

    Args:
        event: A task event.

    Returns:
        True for an attempt-0 PENDING or RECEIVED.
    """
    return event.retries == 0 and event.state in _PRE_START


def _first_eta(events: list[TaskEvent]) -> datetime | None:
    """The ETA the first of ``events`` that carries one reports (they share one message).

    Args:
        events: Events of one task attempt.

    Returns:
        The first reported ETA, or None if none carries one.
    """
    return next((e.eta for e in events if e.eta is not None), None)


def _digest(event: TaskEvent) -> bytes:
    """Stable digest of an event's full content, computed once per insert."""
    try:
        payload = event.model_dump_json()
    except PydanticSerializationError:
        payload = repr(event)
    return hashlib.blake2b(payload.encode(), digest_size=16).digest()


def event_key(event: TaskEvent) -> EventKey:
    """Order of a node's events, also used as its duplicate-detection key.

    Attempt-0 PENDING/RECEIVED come first whatever their timestamps, the rest
    follows by timestamp; exact ties are broken by state, then by content.

    Args:
        event: A task event with a timezone-aware timestamp.

    Returns:
        A sortable key.
    """
    # PENDING sorts before RECEIVED (the sender's PENDING can be stamped after
    # the worker's RECEIVED). Exact timestamp ties are broken by a fixed state
    # order and then by content, so the order never depends on arrival order.
    pre_start = is_pre_start(event)
    return (
        0 if pre_start else 1,
        _STATE_ORDER[event.state] if pre_start else 0,
        event.timestamp,
        _STATE_ORDER[event.state],
        _digest(event),
    )


_NO_TIME = timedelta(0)


_MILLISECOND = timedelta(milliseconds=1)


def _ms(delta: timedelta) -> int:
    """Whole milliseconds of a span, truncated; negative spans count as 0.

    Integer division of the timedelta: going through float seconds loses a
    millisecond (``int(1.001 * 1000)`` is 1000).
    """
    return max(0, delta // _MILLISECOND)


# An unfinished run's key: its worker and attempt. Events without a worker
# (workers up to 0.4.0, and 0.4.1's main-process FAILURE) share one key, so
# they pair as they always did.
_RunKey = tuple[str | None, int | None]
_NO_WORKER: _RunKey = (None, None)


def _run_key(event: TaskEvent) -> _RunKey:
    """The run an event belongs to: its worker and attempt."""
    return _NO_WORKER if event.hostname is None else (event.hostname, event.retries)


def _run_start(open_runs: dict[_RunKey, TaskEvent], end: TaskEvent) -> TaskEvent | None:
    """Take the unfinished run an end event (RETRY or final) closes.

    The one of the end's worker and attempt; mid-upgrade, the run without a
    worker. An end without a worker (older workers) closes the run without
    one, else the latest unfinished run.
    """
    key = _run_key(end)
    if key in open_runs:
        return open_runs.pop(key)
    if end.hostname is not None:
        return open_runs.pop(_NO_WORKER, None)
    if not open_runs:
        return None
    return open_runs.pop(next(reversed(open_runs)))


def precedence(event: TaskEvent) -> tuple[int, int, int, datetime, int]:
    """Total order over a task's events; the maximum decides the node's state.

    Args:
        event: A task event with a timezone-aware timestamp.

    Returns:
        A sortable key; larger means further along.
    """
    # Events from different processes (sender, worker, worker main process)
    # reach the stream in no guaranteed order, and the sender's PENDING can even
    # be stamped after the worker's STARTED, so neither arrival order nor
    # timestamps alone can be trusted. Because this is a key (a total order),
    # the resulting state is the same for every arrival order:
    #
    # * Any terminal state beats any non-terminal state. A terminal's
    #   ``retries`` is ignored: Celery stamps ``retries=0`` on failures raised
    #   from the worker main process (hard time limit, lost worker).
    # * Terminal vs terminal: the later timestamp wins, except that IGNORED
    #   loses to any other terminal state. A replaced task's ID is reused by
    #   its replacement, whose outcome must win over an IGNORED from the
    #   replaced task (workers don't send one for a replace; this holds even
    #   if one arrives).
    # * Non-terminal vs non-terminal: a higher ``retries`` is a newer attempt
    #   (RETRY carries the failing attempt's count; the next STARTED carries
    #   count + 1). Within one attempt PENDING < RECEIVED < STARTED/RETRY, and
    #   STARTED vs RETRY is decided by time (RETRY wins an exact tie).
    tiebreak = _STATE_ORDER[event.state]
    if event.state in TERMINAL_STATES:
        rank = -1 if event.state == TaskState.IGNORED else 0
        return (1, rank, 0, event.timestamp, tiebreak)
    return (0, event.retries, _PHASE[event.state], event.timestamp, tiebreak)


def latest(
    events: list[TaskEvent], value: Callable[[TaskEvent], str | None]
) -> str | None:
    """The value from the furthest-along event that has one.

    "Furthest along" is ``precedence``, the order that decides the node's
    state, not ``events``' timestamp order: with worker clocks skewed, an
    earlier attempt's events can carry later timestamps.

    Args:
        events: A task's events.
        value: Reads the field to describe the task by from an event.

    Returns:
        The field of the furthest-along event whose field is set, else None.
    """
    found = [event for event in events if value(event) is not None]
    return value(max(found, key=precedence)) if found else None


def _own_first_attempt(
    events: list[TaskEvent], reported_parent_id: str | None
) -> list[TaskEvent]:
    """The task's own first-attempt events, without a ``task.replace()`` replacement's.

    The replacement reuses the ID with ``retries`` 0 but reports the task, or a
    task below it, as parent; the graph decides the task's own parent regardless
    of clocks (``reported_parent_id``). Without parent IDs (protocol 1, or a
    root task), the task's earliest events stand for its own.

    Args:
        events: A task's events.
        reported_parent_id: The first parent the task's events reported.

    Returns:
        The events judged to be the task's own first attempt.
    """
    first = [e for e in events if e.retries == 0]
    if all(e.parent_id is None for e in first):
        # Protocol 1 sends no parent_id (celery 5.6.3 app/amqp.py as_task_v1). A
        # replacement is only sent while the task runs, so the earliest event of
        # each state is the task's own if the clocks agree. Within a run, the
        # worker publishes RECEIVED before STARTED (bootsteps._wrap_strategy runs
        # before the strategy), so a RECEIVED after the first STARTED is a later
        # run's: the replacement's or a redelivery's (docs/features.md).
        earliest = {
            state: min(of_state, key=lambda e: e.timestamp)
            for state in (TaskState.STARTED, *_PRE_START)
            if (of_state := [e for e in first if e.state == state])
        }
        started = earliest.get(TaskState.STARTED)
        received = earliest.get(TaskState.RECEIVED)
        if started and received and received.timestamp > started.timestamp:
            del earliest[TaskState.RECEIVED]
        return list(earliest.values())
    # An older sender's PENDING carries no parent.
    return [e for e in first if e.parent_id in (None, reported_parent_id)]


def eta(events: list[TaskEvent], reported_parent_id: str | None) -> datetime | None:
    """When the first attempt was scheduled to run (``countdown`` or ``eta``), in UTC.

    None when the task could run at once or no event reported it. A retry's
    or a ``replace()`` replacement's ETA is on its own events (``TaskEvent.eta``).

    Args:
        events: A task's events.
        reported_parent_id: The first parent the task's events reported.

    Returns:
        The first attempt's ETA, or None.
    """
    return _first_eta(_own_first_attempt(events, reported_parent_id))


def queue_wait_ms(
    events: list[TaskEvent], reported_parent_id: str | None
) -> int | None:
    """Milliseconds from sending the task, or its ETA if later, to its first run.

    Counts the first attempt only (a retry's wait includes its countdown).
    None until the task has a sent event (PENDING or RECEIVED) and a first
    STARTED. The span crosses the sender's and the worker's clocks, so a
    negative wait counts as 0.

    Args:
        events: A task's events.
        reported_parent_id: The first parent the task's events reported.

    Returns:
        The wait in milliseconds, or None while it is unknown.
    """
    own = _own_first_attempt(events, reported_parent_id)
    sent = [e.timestamp for e in own if is_pre_start(e)]
    started = [e.timestamp for e in own if e.state == TaskState.STARTED]
    if not sent or not started:
        return None
    first_attempt_eta = _first_eta(own)
    wait_from = (
        min(sent) if first_attempt_eta is None else max(min(sent), first_attempt_eta)
    )
    return _ms(min(started) - wait_from)


def _runs(events: list[TaskEvent]) -> list[tuple[TaskEvent, TaskEvent]] | None:
    """Pair each run's STARTED with the RETRY or final event that ended it.

    Runs are read on one worker's clock (see ``_run_start``). None unless the
    task finished and its last run's STARTED is from the final event's attempt.
    """
    # A terminal before the final event closes its worker's latest unfinished
    # run, so a same-worker redelivery gives None; without a worker the runs
    # can't be told apart.
    # A FAILURE with retries=0 and no worker comes from an older worker's
    # main process (celery 5.6.3 worker/request.py Request.on_failure, where
    # app/task.py Task._get_request is an empty default) and can be any
    # attempt's; this release's workers fill it in (signals._failed_request).
    final = max(events, key=precedence, default=None)
    if final is None or final.state not in TERMINAL_STATES:
        return None
    ordered = sorted(
        (e for e in events if e is not final and e.state != TaskState.PENDING),
        key=precedence,
    )
    runs: list[tuple[TaskEvent, TaskEvent]] = []
    # Unfinished run per worker and attempt (see _run_key).
    open_runs: dict[_RunKey, TaskEvent] = {}
    for event in [*ordered, final]:
        if event.state == TaskState.STARTED:
            key = _run_key(event)
            open_runs.pop(key, None)
            open_runs[key] = event  # last in insertion order
            continue
        if event.state == TaskState.RECEIVED:
            continue
        if event is not final and event.state in TERMINAL_STATES:
            # A run that ended before the final event (retry(throw=False),
            # or the same attempt's earlier run on this worker) is left out.
            # STARTEDs sort before terminals, so this pops the latest STARTED
            # on its key, a redelivery's if there is one: a final event of that
            # attempt then finds no start, so it has no runtimes. Without a
            # worker (0.4.0 and earlier) nothing is popped.
            if event.hostname is not None:
                open_runs.pop(_run_key(event), None)
            continue
        started = _run_start(open_runs, event)
        if started is not None:
            runs.append((started, event))
    if not runs or runs[-1][1] is not final:
        return None
    # Only an older worker's main-process FAILURE (no worker, retries=0
    # whatever the attempt): this release's workers give it both.
    from_main_process = (
        final.state == TaskState.FAILURE
        and final.retries == 0
        and final.hostname is None
    )
    if final.retries != runs[-1][0].retries and not from_main_process:
        return None
    return runs


def run_times(events: list[TaskEvent]) -> tuple[int | None, int | None]:
    """Last-run and total runtime in milliseconds, from one pass over the events.

    Both are None until the task finished, and when the last run's STARTED is
    missing or can't be told from another's (see ``_runs``).

    Args:
        events: A task's events.

    Returns:
        ``(last run, all runs together)`` in milliseconds, or ``(None, None)``.
    """
    runs = _runs(events)
    if runs is None:
        return None, None
    spans = [max(end.timestamp - start.timestamp, _NO_TIME) for start, end in runs]
    return _ms(spans[-1]), _ms(sum(spans, _NO_TIME))
