"""Task graph models for representing task execution flows."""

import bisect
import hashlib
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    computed_field,
    model_validator,
)
from pydantic_core import PydanticSerializationError

from stemtrace.core.chord_body import body_first_task_id
from stemtrace.core.events import TaskEvent, TaskState

if TYPE_CHECKING:
    # Only an annotation: pydantic exports it from 2.11 on, below our minimum.
    from pydantic import ModelWrapValidatorHandler

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
_EventKey = tuple[int, int, datetime, int, bytes]

# Synthetic GROUP/CHORD nodes are keyed "group:<celery group_id>".
_GROUP_NODE_PREFIX = "group:"

# A group's state is the result of the first rule matching any member state;
# a group whose members all succeeded is SUCCESS. REJECTED ranks right below
# FAILURE: a rejected member means the group didn't complete cleanly.
_GROUP_STATE_RULES: tuple[tuple[frozenset[TaskState], TaskState], ...] = (
    (frozenset({TaskState.FAILURE}), TaskState.FAILURE),
    (frozenset({TaskState.REJECTED}), TaskState.REJECTED),
    (frozenset({TaskState.STARTED, TaskState.RECEIVED}), TaskState.STARTED),
    (frozenset({TaskState.PENDING}), TaskState.PENDING),
    (frozenset({TaskState.RETRY}), TaskState.RETRY),
    (frozenset({TaskState.REVOKED}), TaskState.REVOKED),
    (frozenset({TaskState.IGNORED}), TaskState.IGNORED),
)


def _aggregate_group_state(states: Collection[TaskState]) -> TaskState:
    """A group's state from its members' states (see ``_GROUP_STATE_RULES``).

    Priority: FAILURE > REJECTED > STARTED/RECEIVED > PENDING > RETRY >
    REVOKED > IGNORED > SUCCESS; a group without known members is PENDING.
    """
    if not states:
        return TaskState.PENDING
    for triggers, group_state in _GROUP_STATE_RULES:
        if not triggers.isdisjoint(states):
            return group_state
    return TaskState.SUCCESS


def _group_node_id(group_id: str) -> str:
    """ID of the synthetic GROUP/CHORD node for a Celery ``group_id``."""
    return f"{_GROUP_NODE_PREFIX}{group_id}"


def _with_aware_timestamp(event: TaskEvent) -> TaskEvent:
    """Return the event with a naive timestamp interpreted as UTC.

    A node's events are compared by timestamp, and comparing naive with aware
    datetimes raises ``TypeError``.
    """
    if event.timestamp.tzinfo is not None:
        return event
    return event.model_copy(
        update={"timestamp": event.timestamp.replace(tzinfo=timezone.utc)}
    )


_PRE_START = (TaskState.PENDING, TaskState.RECEIVED)


def _is_pre_start(event: TaskEvent) -> bool:
    """Whether the event belongs before the first run of the task.

    The sender's PENDING (``after_task_publish`` fires after publish) can be stamped
    after the worker already started or even finished the task, so its
    timestamp can't place it.
    """
    return event.retries == 0 and event.state in _PRE_START


def _first_eta(events: list[TaskEvent]) -> datetime | None:
    """The ETA the first of ``events`` that carries one reports (they share one message)."""
    return next((e.eta for e in events if e.eta is not None), None)


def _digest(event: TaskEvent) -> bytes:
    """Stable digest of an event's full content, computed once per insert."""
    try:
        payload = event.model_dump_json()
    except PydanticSerializationError:
        payload = repr(event)
    return hashlib.blake2b(payload.encode(), digest_size=16).digest()


def _event_key(event: TaskEvent) -> _EventKey:
    """Order of a node's events, also used as its duplicate-detection key.

    Attempt-0 PENDING/RECEIVED come first whatever their timestamps, PENDING
    before RECEIVED (the sender's PENDING can be stamped after the worker's
    RECEIVED); everything else follows by timestamp. Exact timestamp ties are
    broken by a fixed state order and then by content, so the order never
    depends on arrival order.
    """
    pre_start = _is_pre_start(event)
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


def _precedence(event: TaskEvent) -> tuple[int, int, int, datetime, int]:
    """Total order over a task's events; the maximum decides the node's state.

    Events from different processes (sender, worker, worker main process)
    reach the stream in no guaranteed order, and the sender's PENDING can even
    be stamped after the worker's STARTED, so neither arrival order nor
    timestamps alone can be trusted. Because this is a key (a total order),
    the resulting state is the same for every arrival order:

    * Any terminal state beats any non-terminal state. A terminal's
      ``retries`` is ignored: Celery stamps ``retries=0`` on failures raised
      from the worker main process (hard time limit, lost worker).
    * Terminal vs terminal: the later timestamp wins, except that IGNORED
      loses to any other terminal state. A replaced task's ID is reused by
      its replacement, whose outcome must win over an IGNORED from the
      replaced task (workers don't send one for a replace; this holds even
      if one arrives).
    * Non-terminal vs non-terminal: a higher ``retries`` is a newer attempt
      (RETRY carries the failing attempt's count; the next STARTED carries
      count + 1). Within one attempt PENDING < RECEIVED < STARTED/RETRY, and
      STARTED vs RETRY is decided by time (RETRY wins an exact tie).

    Args:
        event: A task event with a timezone-aware timestamp.

    Returns:
        A sortable key; larger means further along.
    """
    tiebreak = _STATE_ORDER[event.state]
    if event.state in TERMINAL_STATES:
        rank = -1 if event.state == TaskState.IGNORED else 0
        return (1, rank, 0, event.timestamp, tiebreak)
    return (0, event.retries, _PHASE[event.state], event.timestamp, tiebreak)


ChordNotRunReason = Literal["header_failed", "header_revoked", "header_ignored"]
"""Why a chord's callback hasn't run: a header failed (or was rejected), was
revoked, or raised ``Ignore``."""

# A header's state -> the reason it gives, and which reason wins (higher).
# celery 5.6.3: every chord path joins header results with propagate=True
# (backends redis.py/base.py on_chord_part_return, builtins.py unlock_chord);
# handle_reject and handle_ignore (app/trace.py) only log, so the chord never
# completes (unless the task called backend.mark_as_done itself). Workers
# publish REJECTED only when the message won't run again.
_HEADER_REASONS: dict[TaskState, tuple[int, ChordNotRunReason]] = {
    TaskState.FAILURE: (3, "header_failed"),
    TaskState.REJECTED: (3, "header_failed"),
    TaskState.REVOKED: (2, "header_revoked"),
    TaskState.IGNORED: (1, "header_ignored"),
}


class NodeType(str, Enum):
    """Type of node in the task graph."""

    TASK = "TASK"
    GROUP = "GROUP"
    CHORD = "CHORD"


class TaskNode(BaseModel):
    """Mutable node in the task graph. Tracks event history and child relationships.

    ``events`` is kept in lifecycle order: attempt-0 PENDING then RECEIVED
    first, then everything else by timestamp (see ``_event_key``).
    """

    model_config = ConfigDict(validate_assignment=True)

    task_id: str
    name: str
    state: TaskState
    node_type: NodeType = NodeType.TASK
    group_id: str | None = None
    chord_id: str | None = None
    chord_callback_id: str | None = None
    events: list[TaskEvent] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    # First parent the task's events reported. ``parent_id`` is where the node
    # sits in the graph: a group member's is its group node, a chord
    # callback's the CHORD node, a task whose parent isn't known yet has none.
    reported_parent_id: str | None = None

    # Insert index (not serialized; rebuilt on first use, e.g. after
    # deserialization or when ``events`` was changed directly).
    _index: "_EventIndex | None" = PrivateAttr(default=None)

    @property
    def first_seen(self) -> datetime | None:
        """Earliest event time, or None for nodes without events (GROUP/CHORD)."""
        if not self.events:
            return None
        first = self.events[0].timestamp
        # Only the few leading pre-start events can be later than the first run.
        for event in self.events:
            if not _is_pre_start(event):
                return min(first, event.timestamp)
            first = min(first, event.timestamp)
        return first

    @property
    def last_updated(self) -> datetime | None:
        """Time of the last event in lifecycle order, or None without events.

        A late-stamped PENDING doesn't count once the task has run.
        """
        return self.events[-1].timestamp if self.events else None

    def _latest(self, value: Callable[[TaskEvent], str | None]) -> str | None:
        """The value from the furthest-along event that has one.

        "Furthest along" is ``_precedence``, the order that decides the node's
        state, not ``events``' timestamp order: with worker clocks skewed, an
        earlier attempt's events can carry later timestamps.
        """
        found = [event for event in self.events if value(event) is not None]
        return value(max(found, key=_precedence)) if found else None

    @property
    def exception_type(self) -> str | None:
        """Class of the furthest-along exception the task raised (also a retried one)."""
        return self._latest(lambda event: event.exception_type)

    @property
    def hostname(self) -> str | None:
        """Nodename of the worker of the task's furthest-along event that has one."""
        return self._latest(lambda event: event.hostname)

    @property
    def queue(self) -> str | None:
        """Queue of the task's furthest-along event that has one, if known."""
        return self._latest(lambda event: event.queue)

    @property
    def routing_key(self) -> str | None:
        """Routing key of the task's furthest-along event that has one."""
        return self._latest(lambda event: event.routing_key)

    def _own_first_attempt(self) -> list[TaskEvent]:
        """The task's own first-attempt events, without a ``task.replace()`` replacement's.

        The replacement reuses the ID with ``retries`` 0 but reports the task, or a
        task below it, as parent; the graph decides the task's own parent regardless
        of clocks (``reported_parent_id``). Without parent IDs (protocol 1, or a
        root task), the task's earliest events stand for its own.
        """
        first = [e for e in self.events if e.retries == 0]
        if all(e.parent_id is None for e in first):
            # Protocol 1 sends no parent_id (celery 5.6.3 app/amqp.py as_task_v1). A
            # replacement is only sent while the task runs, so the earliest event of
            # each state is the task's own if the clocks agree. Within a run, the
            # worker publishes RECEIVED before STARTED (bootsteps._wrap_strategy runs
            # before the strategy), so a RECEIVED after the first STARTED is a later
            # run's: the replacement's or a redelivery's (docs/features.md).
            earliest = {
                state: min(events, key=lambda e: e.timestamp)
                for state in (TaskState.STARTED, *_PRE_START)
                if (events := [e for e in first if e.state == state])
            }
            started = earliest.get(TaskState.STARTED)
            received = earliest.get(TaskState.RECEIVED)
            if started and received and received.timestamp > started.timestamp:
                del earliest[TaskState.RECEIVED]
            return list(earliest.values())
        # An older sender's PENDING carries no parent.
        return [e for e in first if e.parent_id in (None, self.reported_parent_id)]

    @property
    def eta(self) -> datetime | None:
        """When the first attempt was scheduled to run (``countdown`` or ``eta``), in UTC.

        None when the task could run at once or no event reported it. A retry's
        or a ``replace()`` replacement's ETA is on its own events (``TaskEvent.eta``).
        """
        return _first_eta(self._own_first_attempt())

    @property
    def queue_wait_ms(self) -> int | None:
        """Milliseconds from sending the task, or its ETA if later, to its first run.

        Counts the first attempt only (a retry's wait includes its countdown).
        None until the task has a sent event (PENDING or RECEIVED) and a first
        STARTED. The span crosses the sender's and the worker's clocks, so a
        negative wait counts as 0.
        """
        own = self._own_first_attempt()
        sent = [e.timestamp for e in own if _is_pre_start(e)]
        started = [e.timestamp for e in own if e.state == TaskState.STARTED]
        if not sent or not started:
            return None
        eta = _first_eta(own)
        wait_from = min(sent) if eta is None else max(min(sent), eta)
        return _ms(min(started) - wait_from)

    @property
    def runtime_ms(self) -> int | None:
        """Milliseconds the task's last run took, from its STARTED to the end.

        None until the task finished, and when the last run's STARTED is
        missing (see ``_runs``). For both run fields, ``run_times()`` reads
        the events once.
        """
        return self.run_times()[0]

    @property
    def total_runtime_ms(self) -> int | None:
        """Milliseconds all of the task's runs took together (see ``_runs``).

        None exactly when ``runtime_ms`` is.
        """
        return self.run_times()[1]

    def run_times(self) -> tuple[int | None, int | None]:
        """``runtime_ms`` and ``total_runtime_ms`` from one pass over the events."""
        runs = self._runs()
        if runs is None:
            return None, None
        spans = [max(end.timestamp - start.timestamp, _NO_TIME) for start, end in runs]
        return _ms(spans[-1]), _ms(sum(spans, _NO_TIME))

    def _runs(self) -> list[tuple[TaskEvent, TaskEvent]] | None:
        """Pair each run's STARTED with the RETRY or final event that ended it.

        Runs are read on one worker's clock (see ``_run_start``). A terminal
        before the final event closes its worker's latest unfinished run, so a
        same-worker redelivery gives None; without a worker the runs can't be
        told apart. None unless the task finished and its last run's STARTED is
        from the final event's attempt.
        """
        # A FAILURE with retries=0 and no worker comes from an older worker's
        # main process (celery 5.6.3 worker/request.py Request.on_failure, where
        # app/task.py Task._get_request is an empty default) and can be any
        # attempt's; this release's workers fill it in (signals._failed_request).
        final = max(self.events, key=_precedence, default=None)
        if final is None or final.state not in TERMINAL_STATES:
            return None
        ordered = sorted(
            (e for e in self.events if e is not final and e.state != TaskState.PENDING),
            key=_precedence,
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

    def snapshot(self) -> "TaskNode":
        """Return a copy whose ``events`` and ``children`` lists are its own.

        Events are immutable, so copying the lists is enough for the copy to
        stay unchanged while the original keeps receiving events.
        """
        copy = self.model_copy(
            update={"events": list(self.events), "children": list(self.children)}
        )
        copy._index = None  # never share the original's insert index
        return copy

    def _ensure_index(self) -> "_EventIndex":
        """Return the insert index, sorting events and rebuilding it if needed."""
        index = self._index
        if index is None or len(index.keys) != len(self.events):
            index = _EventIndex(self.events)
            self._index = index
        return index

    def _record_event(self, event: TaskEvent) -> bool:
        """Insert an event in lifecycle order and update ``state``.

        O(log n) plus the list insert: duplicates are found through a hash of
        the event key (full equality is only checked on a key match), and the
        new event is compared against the current state-deciding event only.

        Args:
            event: Event for this node, with a timezone-aware timestamp.

        Returns:
            False if an identical event was already recorded (nothing changes),
            True otherwise.
        """
        index = self._ensure_index()
        key = _event_key(event)
        existing = index.by_key.get(key)
        if existing is not None and existing == event:
            return False

        position = bisect.bisect_right(index.keys, key)
        index.keys.insert(position, key)
        self.events.insert(position, event)
        index.by_key[key] = event
        index.last_position = position
        index.last_key = key
        parent = event.parent_id if event.parent_id != self.task_id else None
        index.new_identity = event.name not in index.names or (
            parent is not None and parent not in index.parents
        )
        was_reused = len(index.names) > 1 or len(index.parents) > 1
        index.names.add(event.name)
        if parent is not None:
            index.parents.add(parent)
        index.became_reused = not was_reused and (
            len(index.names) > 1 or len(index.parents) > 1
        )

        current = index.state_event
        if current is None or _precedence(event) > _precedence(current):
            current = event
            index.state_event = event
        if self.state != current.state:
            self.state = current.state
        return True

    def _fill_missing_ids(self, event: TaskEvent) -> None:
        """Take ``group_id``/``chord_id`` from the event if not known yet.

        Not every event of a task carries them; the first value seen is kept.
        """
        if self.group_id is None and event.group_id is not None:
            self.group_id = event.group_id
        if self.chord_id is None and event.chord_id is not None:
            self.chord_id = event.chord_id

    def _note_reported_parent(
        self, is_replacement: "Callable[[TaskEvent], bool]" = lambda _: False
    ) -> bool:
        """Update the name and reported parent after recording an event.

        Both come from the task's own events in lifecycle order, not arrival
        order: the name from the earliest one, the parent from the earliest
        one under that name that reports one (the sender's PENDING from an
        older stemtrace carries none). A task can't be its own parent (Celery
        reports a retried task as its own parent).

        ``task.replace()`` makes Celery reuse the replaced task's ID for the
        replacement (its last task), whose events, including a worker's
        second attempt-0 PENDING, report a parent from the replacement.
        ``is_replacement(event)`` says so (see ``TaskGraph._replacement_check``)
        and those events are ignored, whatever their timestamps (they can
        come from another host's clock).

        Returns:
            True if the reported parent changed.
        """
        index = self._ensure_index()
        previous = self.reported_parent_id
        event = self.events[index.last_position]
        if index.became_reused:
            # A second name or parent: the task was replaced, and its events
            # are told apart by another rule. Once the ID carries two tasks,
            # whether an event is its replacement's depends on that event
            # (its parent and root) only, so later ones don't recompute.
            return self._recompute_identity(is_replacement)
        if is_replacement(event):
            if index.last_position <= index.first_own:
                index.first_own += 1
            return False
        if index.last_position <= index.first_own or (
            index.parent_key is None and previous is not None
        ):
            return self._recompute_identity(is_replacement)
        if (
            event.parent_id is not None
            and event.parent_id != self.task_id
            and event.name == self.name
            and index.last_key is not None
            and (index.parent_key is None or index.last_key < index.parent_key)
        ):
            self.reported_parent_id = event.parent_id
            index.parent_key = index.last_key
        return self.reported_parent_id != previous

    def _is_reused(self) -> bool:
        """Whether the ID carries two tasks: two names or two parents.

        Celery's ``task.replace()`` reuses the replaced task's ID; a retry
        keeps its name and reports itself as parent, which doesn't count.
        """
        index = self._ensure_index()
        return len(index.names) > 1 or len(index.parents) > 1

    def _recompute_identity(
        self, is_replacement: "Callable[[TaskEvent], bool]"
    ) -> bool:
        """Recompute name and reported parent from all events; True if the parent changed."""
        index = self._ensure_index()
        previous = self.reported_parent_id
        own = [
            (position, key, event)
            for position, (key, event) in enumerate(
                zip(index.keys, self.events, strict=True)
            )
            if not is_replacement(event)
        ]
        if not own:  # only a replacement's events so far
            index.first_own = len(self.events)
            self.reported_parent_id, index.parent_key = None, None
            return previous is not None
        index.first_own = own[0][0]
        name = own[0][2].name
        if self.name != name:
            self.name = name
        self.reported_parent_id, index.parent_key = None, None
        for _, key, candidate in own:
            if (
                candidate.parent_id is not None
                and candidate.parent_id != self.task_id
                and candidate.name == name
            ):
                self.reported_parent_id, index.parent_key = candidate.parent_id, key
                break
        return self.reported_parent_id != previous


class _EventIndex:
    """Sort keys, duplicate lookup and state-deciding event for one node."""

    __slots__ = (
        "became_reused",
        "by_key",
        "first_own",
        "keys",
        "last_key",
        "last_position",
        "names",
        "new_identity",
        "parent_key",
        "parents",
        "state_event",
    )

    def __init__(self, events: list[TaskEvent]) -> None:
        """Sort ``events`` in place into lifecycle order and index them."""
        # Where the last recorded event went, and the key of the event the
        # reported parent comes from (None: recompute).
        self.last_position = 0
        self.last_key: _EventKey | None = None
        self.parent_key: _EventKey | None = None
        # Position of the earliest event that isn't a replacement's, the task
        # names the events carry (a replaced task's carry two), whether the
        # last recorded event brought a new name or parent, and whether that
        # made the ID carry two tasks (see ``TaskNode._is_reused``).
        self.first_own = 0
        self.names: set[str] = {event.name for event in events}
        self.parents: set[str] = {
            e.parent_id
            for e in events
            if e.parent_id is not None and e.parent_id != e.task_id
        }
        self.new_identity = False
        self.became_reused = False
        pairs = sorted(((_event_key(e), e) for e in events), key=lambda p: p[0])
        events[:] = [event for _, event in pairs]
        self.keys: list[_EventKey] = [key for key, _ in pairs]
        self.by_key: dict[_EventKey, TaskEvent] = dict(pairs)
        self.state_event: TaskEvent | None = (
            max(events, key=_precedence) if events else None
        )


# Celery's polling task that fires a chord's body when the result backend
# can't (e.g. database backends); the body reports it as its parent.
_CHORD_UNLOCK_TASK = "celery.chord_unlock"


class _GroupTally:
    """One Celery group's members, with running counts over those that count.

    Every task that carried the ``group_id`` is in ``members``. Once any of
    them carried a ``chord_callback_id`` or ``chord_body_first_id`` (``marked``)
    the group is a chord, and only those (``headers``) count as members: Celery
    also stamps a chord's group ID on its callback and on the last task of a
    chain body, which aren't headers. Without any, the members a
    ``celery.chord_unlock`` task lists are the headers (Celery 5.2 puts neither
    on a chain body's headers). The counts (each counted member's vote for the
    group's parent, and their states) keep the group's parent and state O(1)
    per event.
    """

    __slots__ = (
        "direct_parents",
        "firsts",
        "headers",
        "marked",
        "members",
        "parent_votes",
        "read_parent",
        "rootish",
        "stamps",
        "state_counts",
        "status",
        "unparented",
        "vote_of",
    )

    def __init__(self) -> None:
        """Start an empty group."""
        self.members: dict[str, None] = {}
        self.headers: dict[str, None] = {}
        self.marked: dict[str, None] = {}
        self.parent_votes: Counter[str] = Counter()
        self.state_counts: Counter[TaskState] = Counter()
        self.vote_of: dict[str, str] = {}
        # Each counted member's own reported parent ("" if a worker event
        # showed it has none); members with no worker event yet are
        # ``unparented``. Counts of both decide whether chains can form.
        self.status: dict[str, str] = {}
        self.direct_parents: Counter[str] = Counter()
        self.rootish = 0
        self.unparented: set[str] = set()
        # The ``chord_callback_id`` and ``chord_body_first_id`` values the
        # headers' events carry, each with the headers that carried it. Kept
        # here, not only on the CHORD node: removing the node keeps the
        # headers, and a recreated node must know what their events said.
        self.stamps: dict[str, dict[str, None]] = {}
        self.firsts: dict[str, dict[str, None]] = {}
        # ``parent()`` when the group node was last placed: a chord whose
        # callback must not hang above it re-resolves when it moves.
        self.read_parent: str | None = None

    def counted(self) -> dict[str, None]:
        """The members that make up the group (headers, for a chord)."""
        return self.headers if self.headers else self.members

    def chains_allowed(self) -> bool:
        """Whether a member's one-child parent can be a chain link.

        Only on positive evidence that it isn't the group's own parent: every
        member's parent is known, and they report different parents or one
        reports none (a top-level member). A parent all members report is
        the group's parent.
        """
        return not self.unparented and (
            len(self.direct_parents) >= 2 or self.rootish > 0
        )

    def set_status(self, member_id: str, status: str | None) -> None:
        """Record a counted member's own parent ("" none, None not known yet)."""
        self.clear_status(member_id)
        if status is None:
            self.unparented.add(member_id)
        else:
            self.status[member_id] = status
            if status:
                _bump(self.direct_parents, status, 1)
            else:
                self.rootish += 1

    def clear_status(self, member_id: str) -> None:
        """Forget a member's parent status (it no longer counts)."""
        self.unparented.discard(member_id)
        previous = self.status.pop(member_id, None)
        if previous:
            _bump(self.direct_parents, previous, -1)
        elif previous == "":
            self.rootish -= 1

    def set_vote(self, member_id: str, parent_id: str | None) -> None:
        """Record which parent a counted member votes for (None: no vote)."""
        previous = self.vote_of.pop(member_id, None)
        if previous is not None:
            _bump(self.parent_votes, previous, -1)
        if parent_id is not None:
            self.vote_of[member_id] = parent_id
            _bump(self.parent_votes, parent_id, 1)

    def parent(self) -> str | None:
        """The parent most counted members vote for (ties: the smallest ID).

        Members of a group normally agree. When they don't, a fixed choice
        keeps the graph independent of event order.
        """
        if not self.parent_votes:
            return None
        return min(self.parent_votes.items(), key=lambda kv: (-kv[1], kv[0]))[0]


class _UnlockInfo(NamedTuple):
    """What a ``celery.chord_unlock`` task's events say about its chord.

    Attributes:
        group_id: The chord's header group ID (its first argument), if readable.
        first: The task ID its body signature (second argument) starts with.
        result: The header task IDs of its ``result`` keyword, if readable.
    """

    group_id: str | None
    first: str | None
    result: frozenset[str] | None


def _is_id(value: object) -> bool:
    """Whether a value looks like a Celery UUID task or group ID."""
    return isinstance(value, str) and len(value) == 36 and value.count("-") == 4


def _result_ids(result: Any) -> frozenset[str] | None:
    """The header IDs of a ``chord_unlock`` ``result`` (``[[[id, parent], ...], ...]``)."""
    if not isinstance(result, list):
        return None
    ids: set[str] = set()
    for entry in result:
        try:
            task_id = entry[0][0]
        except (IndexError, KeyError, TypeError):
            return None
        if not _is_id(task_id):
            return None
        ids.add(task_id)
    return frozenset(ids)


def _read_unlock(node: TaskNode) -> _UnlockInfo:
    """Read the chord a ``celery.chord_unlock`` task belongs to from its events."""
    args = next((e.args for e in node.events if isinstance(e.args, list)), None)
    kwargs = next((e.kwargs for e in node.events if isinstance(e.kwargs, dict)), None)
    group_id = args[0] if args and _is_id(args[0]) else None
    first = body_first_task_id(args[1]) if args is not None and len(args) > 1 else None
    result = _result_ids(kwargs.get("result")) if kwargs is not None else None
    return _UnlockInfo(group_id, first, result)


def _bump(counter: "Counter[str] | Counter[TaskState]", key: object, sign: int) -> None:
    """Add ``sign`` to a count, dropping it at zero."""
    counts: dict[object, int] = counter  # type: ignore[assignment]  # Counter is a dict
    value = counts.get(key, 0) + sign
    if value > 0:
        counts[key] = value
    else:
        counts.pop(key, None)


def _add_to(index: dict[str, dict[str, None]], key: str, value: str) -> None:
    """Add ``value`` to the ordered set stored under ``key``."""
    index.setdefault(key, {})[value] = None


def _named_parents(node: TaskNode) -> set[str]:
    """IDs a task's events name as their parent (itself excluded)."""
    return {
        event.parent_id
        for event in node.events
        if event.parent_id is not None and event.parent_id != node.task_id
    }


def _named_ids(node: TaskNode) -> set[str]:
    """IDs a task's events name as their parent or root."""
    return {
        named
        for event in node.events
        for named in (event.parent_id, event.root_id)
        if named is not None
    }


# Most reused IDs whose own parent one replacement check works out at once
# (each nested replace() level is one; the work recurses, so it's bounded).
_NESTING_LIMIT = 16


class _OwnParentWalk:
    """Walks up the parents tasks' own events report, for one replacement check.

    Only a task's own parenthood counts as an edge: a task whose ID carries
    one task follows its ``reported_parent_id``; a reused ID (see
    ``TaskNode._is_reused``) follows the one parent its events report that
    isn't its replacement's, worked out the same way, or none while that
    isn't known. Its replacement's events point down into its own subtree
    (the replacement's previous task); following them made a nested
    replacement look like it descends from the outer task's replacement.

    Every task the walks visit (also a missing one, where a walk stops) is
    recorded for ``task_id``: a change to one re-runs its check
    (``TaskGraph._recheck_walkers``). A walk is a path (one edge per task),
    so it visits each task at most once; results are kept for the check.
    Working out a reused ID's own parent walks from its parents in turn, so
    a walk through ``replace()`` nested more than ``_NESTING_LIMIT`` levels
    deep stops there, as at a missing task.
    """

    def __init__(self, graph: "TaskGraph", task_id: str) -> None:
        """Prepare walks for the replacement check of ``task_id``."""
        self._graph = graph
        self._task_id = task_id
        self._deps = graph._walk_deps
        self._walks_of = graph._walks_of
        # Reused IDs whose own parent is being worked out: a walk that meets
        # one stops there, as at a missing task, so working out can't loop.
        self._busy = {task_id}
        self._edges: dict[str, str | None] = {}
        self._reached: dict[str, bool] = {}

    def reaches(self, start: str) -> bool:
        """Whether the checked task is ``start`` or above it by own parents."""
        known = self._reached.get(start)
        if known is None:
            known = self._reaches(start, self._task_id)
            self._reached[start] = known
        return known

    def _reaches(self, start: str, target: str) -> bool:
        """Whether ``target`` is ``start`` or above it by own parents."""
        seen: set[str] = set()
        current: str | None = start
        while current is not None and current not in seen:
            if current == target:
                return True
            seen.add(current)
            self._note_dep(current)
            current = self._own_parent(current)
        return False

    def _note_dep(self, node_id: str) -> None:
        """Record that the check walked through ``node_id``."""
        owner = self._task_id
        walked = self._walks_of.get(owner)
        if walked is None:
            walked = self._walks_of[owner] = set()
        if node_id not in walked and node_id != owner:
            walked.add(node_id)
            _add_to(self._deps, node_id, owner)

    def _own_parent(self, task_id: str) -> str | None:
        """The parent a task's own events report, or None (unknown or a root)."""
        node = self._graph.nodes.get(task_id)
        if node is None:
            return None
        if not node._is_reused():
            return node.reported_parent_id
        if task_id in self._busy or len(self._busy) > _NESTING_LIMIT:
            return None
        if task_id not in self._edges:
            self._busy.add(task_id)
            try:
                self._edges[task_id] = self._reused_own_parent(node)
            finally:
                self._busy.discard(task_id)
        return self._edges[task_id]

    def _reused_own_parent(self, node: TaskNode) -> str | None:
        """The own parent of a reused ID: the only one not below it, if known.

        A replacement's events report a task below the reused ID (or the ID
        itself, which the index leaves out). The task's own events report
        one parent, or none for a root (its worker events name it as root
        with no parent): then no other parent can be its own. While two
        parents are left (a replacement whose tasks haven't all been seen),
        which one is its own isn't known.
        """
        task_id = node.task_id
        if any(e.parent_id is None and e.root_id == task_id for e in node.events):
            return None
        index = node._ensure_index()
        own = [p for p in sorted(index.parents) if not self._reaches(p, task_id)]
        return own[0] if len(own) == 1 else None


def _drop_from(index: dict[str, dict[str, None]], key: str, value: str) -> None:
    """Remove ``value`` from the ordered set under ``key``, dropping it if empty."""
    values = index.get(key)
    if values is not None:
        values.pop(value, None)
        if not values:
            del index[key]


class TaskGraph(BaseModel):
    """DAG of task executions, built incrementally from events.

    Linking rules: a node's ``parent_id`` is None (it is a root) or names a
    node in the graph that lists it among its ``children``, so every node is
    reachable from a root. For the events Celery workflows emit (the
    captured workflows in the tests, replayed in shuffled orders and with
    skewed worker clocks), the final shape doesn't depend on event order.
    Arbitrary events (e.g. cycles of reported parents no Celery workflow
    makes) can still end in a shape that depends on the order they came in.

    * A task sits under the parent its events reported (``reported_parent_id``,
      the first one wins). While that parent isn't in the graph (not seen yet,
      or evicted) the task is a root; it moves under the parent when the
      parent's node is created.
    * Tasks sharing a ``group_id`` get a synthetic GROUP node (from 2
      members) or CHORD node (from the first header carrying a
      ``chord_callback_id`` or ``chord_body_first_id``; then only headers are
      members). A member sits
      inside its group; the group sits under its members' reported parent
      (the most common one if they disagree), or is a root while that parent
      isn't in the graph.
    * Chains inside a group: a member's reported parent whose only child is
      that member (and which belongs to no group and isn't a chord callback)
      is a link of the member's chain, drawn inside the group, and so on up
      the chain. The group then sits under the chain's first task's parent.
    * A CHORD's callback is the node its body starts with, taken only from
      IDs its events name, never from a parent pointer, a timestamp or the
      order events arrive in: the task ID its headers' ``chord_body_first_id``
      and its ``celery.chord_unlock`` task's body signature give, else the
      callback ID its headers name (``chord_callback_id``), when that is a
      task with events that isn't also a member of the chord's group, or a
      group other than the chord's own. Evidence that conflicts, or that two
      chords share, draws no callback. The CHORD lists the callback among its
      children and names it in ``chord_id``; it sits under the CHORD.
    * A node is never linked under itself or one of its descendants; such a
      cycle is broken at a group's link rather than a member's.

    Nodes are keyed by their ``task_id``. Remove nodes with ``remove_node``
    so these links stay consistent. A validated (deserialized) graph is
    rebuilt from its nodes' events.
    """

    model_config = ConfigDict(validate_assignment=True)

    nodes: dict[str, TaskNode] = Field(default_factory=dict)

    # Root IDs in the order they became roots: a dict used as an ordered set,
    # so adding and removing a root is O(1). Exposed as ``root_ids``.
    _roots: dict[str, None] = PrivateAttr(default_factory=dict)
    # Existing tasks whose name changed since the last take_renamed(): a
    # task.replace() reuse can re-pick a task's name on another task's event.
    _renamed_ids: set[str] = PrivateAttr(default_factory=set)
    # Celery group ID -> its members and counts.
    _groups: dict[str, _GroupTally] = PrivateAttr(default_factory=dict)
    # Parent ID -> tasks with any event naming it (replace() detection).
    _raw_children: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    # Root ID -> other tasks with any event naming it as root (replace()
    # detection: they descend from it).
    _descendants: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    # Task ID (also of a missing task) -> reused tasks whose replacement
    # check walked through it, and the reverse (see ``_OwnParentWalk``).
    _walk_deps: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    _walks_of: dict[str, set[str]] = PrivateAttr(default_factory=dict)
    # Reported parent ID -> tasks that reported it, in arrival order.
    _reported_children: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    # Group member -> its chain links inside the group (bottom-up), and the
    # reverse: link -> member.
    _chain_of: dict[str, list[str]] = PrivateAttr(default_factory=dict)
    _link_owner: dict[str, str] = PrivateAttr(default_factory=dict)
    # Link -> its index in its member's chain.
    _link_pos: dict[str, int] = PrivateAttr(default_factory=dict)
    # chord_unlock task ID -> what its events say, and the same by the group
    # ID it names as its chord.
    _unlocks: dict[str, _UnlockInfo] = PrivateAttr(default_factory=dict)
    _unlocks_by_group: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    # The one callback ID a chord's headers name (not its own group ID) ->
    # the chords that name it, and the reverse.
    _chords_by_stamp: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    _stamp_of: dict[str, str] = PrivateAttr(default_factory=dict)
    # Chord group ID -> the node its evidence points to, and the reverse: a
    # node two chords point to is drawn after neither.
    _candidate: dict[str, str] = PrivateAttr(default_factory=dict)
    _claims: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    # Node drawn as a chord's callback -> that chord's group ID; and the
    # chord_unlock task drawn under the chord -> the chord's group ID.
    _callback_of: dict[str, str] = PrivateAttr(default_factory=dict)
    _unlock_of: dict[str, str] = PrivateAttr(default_factory=dict)
    _unlock_by_chord: dict[str, str] = PrivateAttr(default_factory=dict)
    # Task and group IDs a chord's callback resolution read -> chord group
    # IDs, and the reverse; a change to one of those re-resolves the chord.
    _chord_deps: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    _deps_of_chord: dict[str, set[str]] = PrivateAttr(default_factory=dict)
    # Missing parent ID -> nodes (in arrival order) to move under it once its
    # node is created, and the reverse map to keep both in sync.
    _awaiting_parent: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    _awaited_by: dict[str, str] = PrivateAttr(default_factory=dict)
    # Nodes being removed: no longer placed or used as chain links.
    _removing: set[str] = PrivateAttr(default_factory=set)
    # How many times a link was refused because it would close a cycle (task
    # parents can loop, e.g. through task.replace(), which reuses a task ID;
    # where a cycle breaks then depends on order).
    _cycles_refused: int = PrivateAttr(default=0)
    # Chords whose callback is being resolved: a nested request for the same
    # chord (its own placement changing mid-resolution) is skipped.
    _resolving: set[str] = PrivateAttr(default_factory=set)
    # Nodes kept off their parent (or groups made roots) to break a cycle;
    # placed again after each change.
    _cycle_broken: dict[str, None] = PrivateAttr(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def _rebuild_from_events(
        cls, data: object, handler: "ModelWrapValidatorHandler[TaskGraph]"
    ) -> "TaskGraph":
        """Rebuild a validated graph from its nodes' events.

        The links, groups and indexes aren't serialized; the graph is a
        function of its events, so replaying them restores all of it.
        Synthetic nodes are recreated; ``root_ids`` order is kept when the
        same roots come back.
        """
        graph = handler(data)
        if not graph.nodes:
            return graph
        events = [event for node in graph.nodes.values() for event in node.events]
        graph.nodes.clear()  # in place: assigning would validate (and recurse)
        for event in events:
            graph.add_event(event)
        saved_roots = data.get("root_ids") if isinstance(data, dict) else None
        if saved_roots is not None and set(saved_roots) == set(graph._roots):
            graph._roots = dict.fromkeys(saved_roots)
        return graph

    @computed_field  # type: ignore[prop-decorator]  # mypy can't type decorated properties
    @property
    def root_ids(self) -> list[str]:
        """IDs of the root nodes, in the order they became roots (a copy)."""
        return list(self._roots)

    def add_event(self, event: TaskEvent) -> TaskEvent | None:
        """Add event, creating node if needed, and update the graph's links.

        Events may arrive out of order. The node's events stay in lifecycle
        order (see ``_event_key``), and its state comes from the most advanced event (see
        ``_precedence``) rather than the last one to arrive, so a late PENDING
        or RECEIVED can't move a finished task backwards. Naive timestamps are
        treated as UTC. Exact duplicates are ignored.

        Also tracks group membership and creates synthetic GROUP/CHORD nodes.

        Args:
            event: The task event to add.

        Returns:
            The event as stored (naive timestamps normalized to UTC), or None
            if it was an exact duplicate and was ignored.
        """
        event = _with_aware_timestamp(event)
        node = self.nodes.get(event.task_id)
        is_new = node is None
        old_parent: str | None = None
        if node is None:
            node = self._insert_node(event)
            new_parent = node.reported_parent_id is not None
            old_state = None
        else:
            old_state = node.state
            old_parent = node.reported_parent_id
            recorded = self._record_on(node, event)
            if recorded is None:
                return None
            new_parent = recorded

        self._index_raw_parent(node, event)
        tally = self._groups.get(node.group_id) if node.group_id else None
        if old_state is not None and old_state != node.state:
            self._state_changed(node, tally, old_state)
        if new_parent:
            self._reported_parent_changed(node, old_parent)
        self._walk_edge_recorded(node, event, new_parent=new_parent)
        if tally is not None and node.task_id in tally.counted():
            self._parent_evidence_changed(tally, node, event, new_parent=new_parent)
        if event.group_id is not None and event.group_id == node.group_id:
            self._join_from_event(node, event)
        if is_new:
            self._on_children_changed(node.task_id)
        self._place(node)
        self._refresh_group_of(node)
        self._refresh_unlock(node)
        if is_new:
            self._retrigger(node.task_id)
        if self._cycle_broken:
            self._retry_cycle_broken()
        return event

    def _walk_edge_recorded(
        self, node: TaskNode, event: TaskEvent, *, new_parent: bool
    ) -> None:
        """Re-run the checks that walked through a task the event changed.

        What a walk follows from a task (see ``_OwnParentWalk``) may change
        when the task is new, when a new name or parent makes the ID reused
        or gives it another parent to choose from, when a reused root's own
        event rules out every parent, and when a single task's reported
        parent changes.
        """
        if node._ensure_index().new_identity:  # also a new node's first event
            changed = True
        elif not node._is_reused():
            changed = new_parent
        else:
            changed = event.parent_id is None and event.root_id == node.task_id
        if changed:
            self._recheck_walkers(node.task_id)

    def _record_on(self, node: TaskNode, event: TaskEvent) -> bool | None:
        """Record an event on an existing node and re-pick its identity.

        Returns:
            Whether the reported parent changed, or None for a duplicate.
        """
        was_reused = node._is_reused()
        old_name = node.name
        if not node._record_event(event):
            return None
        node._fill_missing_ids(event)
        new_parent = node._note_reported_parent(self._replacement_check(node))
        if not was_reused and node._is_reused():
            self._recheck_mutual(node)
        if node.name != old_name:
            self._renamed(node, old_name)
        return new_parent

    def _replacement_check(self, node: TaskNode) -> Callable[[TaskEvent], bool]:
        """Whether an event of ``node`` comes from a ``task.replace()`` reuse.

        Such an event reports a parent the task can't have:

        * a parent that descends from the task, when the event names the
          task as its own root (the replacement inherits the root and runs
          after its other tasks). The root ID alone doesn't say it: Celery
          5.6 (``amqp.as_task_v2``) defaults it to the task's own ID when a
          client sends a task, also one sent with an explicit ``parent_id``;
        * for an ID carrying two tasks (two names or parents), the task
          itself (a single-signature replacement) or a task that descends
          from it by the parents tasks' own events report (the
          replacement's previous task, a child of the task for a two-task
          chain, deeper for a longer one, also through tasks that were
          replaced in turn), see ``_OwnParentWalk``;
        * a task that reports this one as its parent while this one reports
          it: a two-task cycle that only ID reuse makes. With nothing else to
          go on (e.g. the server missed the original events, and neither ID
          carries two tasks), the task with the larger ID gives up that
          parent, so the result is stable.
        """
        task_id = node.task_id
        children = self._raw_children.get(task_id)
        descendants = self._descendants.get(task_id)
        # Called after the node's latest event was recorded, before the next.
        reused = node._is_reused()
        walk = _OwnParentWalk(self, task_id)

        def check(event: TaskEvent) -> bool:
            """Whether the event comes from a replacement (see above)."""
            parent = event.parent_id
            if parent is None or (parent == task_id and not reused):
                return False
            if (
                event.root_id == task_id
                and descendants is not None
                and parent in descendants
            ):
                return True
            if reused:
                return parent == task_id or walk.reaches(parent)
            if children is None or parent not in children or task_id < parent:
                return False
            other = self.nodes.get(parent)
            return other is None or not other._is_reused()

        return check

    def _recheck_walkers(self, node_id: str) -> None:
        """Re-run the replacement checks that walked through a changed task.

        Called when the task appeared or went away, or what a walk follows
        from it may have changed: its reported parent (one task under its
        ID), or the parents and names its events report.
        """
        # A copy: rechecking a task walks again, and ``_OwnParentWalk._note_dep``
        # adds the task to ``_walk_deps`` meanwhile.
        for task_id in list(self._walk_deps.get(node_id, {})):  # NOSONAR
            self._recheck(task_id)

    def _join_from_event(self, node: TaskNode, event: TaskEvent) -> None:
        """Track the group membership an event carries."""
        self._join_group(
            node,
            event.group_id or "",
            event.chord_callback_id,
            event.chord_body_first_id,
        )

    def _index_raw_parent(self, node: TaskNode, event: TaskEvent) -> None:
        """Index the parent and root an event of ``node`` names (replace() detection).

        An event naming its own task as root may be a replacement's: it isn't
        indexed as naming its parent (the replacement's previous task would
        look like the task's child).
        """
        task_id = node.task_id
        root_id = event.root_id
        if root_id is not None and root_id != task_id:
            descendants = self._descendants.get(root_id)
            if descendants is None or task_id not in descendants:
                _add_to(self._descendants, root_id, task_id)
                self._recheck_identity(root_id, task_id)
        parent_id = event.parent_id
        if parent_id is not None and task_id not in (parent_id, root_id):
            self._note_raw_child(parent_id, task_id)

    def _note_raw_child(self, parent_id: str, child_id: str) -> None:
        """Index an event of ``child_id`` naming ``parent_id`` as parent."""
        children = self._raw_children.get(parent_id)
        if children is not None and child_id in children:
            return
        _add_to(self._raw_children, parent_id, child_id)
        self._recheck_identity(parent_id, child_id)

    def _recheck_identity(self, parent_id: str, child_id: str) -> None:
        """Re-pick a task's name and parent once the index around it changed.

        Called when ``child_id`` was found to report the task as its parent
        or root, or was removed: the task's events naming ``child_id`` as
        their parent may (no longer) come from a replacement.
        """
        parent = self.nodes.get(parent_id)
        if parent is not None and child_id in parent._ensure_index().parents:
            self._recheck(parent_id)

    def _recheck(self, task_id: str) -> None:
        """Recompute a task's name and reported parent, and move it if it changed."""
        node = self.nodes[task_id]
        old_parent = node.reported_parent_id
        old_name = node.name
        parent_changed = node._recompute_identity(self._replacement_check(node))
        if node.name != old_name:
            self._renamed(node, old_name)
        if parent_changed:
            self._reported_parent_changed(node, old_parent)
            self._place(node)
            self._retrigger(task_id)
            if not node._is_reused():
                self._recheck_walkers(task_id)

    def take_renamed(self) -> set[str]:
        """IDs of existing tasks renamed since the last call, and forget them.

        Every rename of an existing task goes through ``_renamed``; with
        ``task.replace()`` it can happen on another task's event, or when a
        node is removed, so a cache keyed by the event's task ID misses it.

        Returns:
            The renamed task IDs (removed tasks included).
        """
        renamed, self._renamed_ids = self._renamed_ids, set()
        return renamed

    def _renamed(self, node: TaskNode, old_name: str) -> None:
        """Re-run what read a task's name after its events gave it another.

        Only whether it is ``celery.chord_unlock`` is read: such a task names
        a chord's body (``_refresh_unlock``) and is never a chain link
        (``_can_link``). A ``task.replace()`` reuse can rename a task.
        """
        self._renamed_ids.add(node.task_id)
        if (old_name == _CHORD_UNLOCK_TASK) == (node.name == _CHORD_UNLOCK_TASK):
            return
        self._rechain_owner_of(node.task_id)  # it may stop being a link
        self._on_children_changed(node.task_id)  # or become one
        self._refresh_unlock(node)

    def _recheck_mutual(self, node: TaskNode) -> None:
        """Re-pick the parents of tasks in a two-task cycle with a reused ID.

        Once ``node`` carries two tasks, its own check settles the cycle, so
        the other task keeps the parent it reports (see ``_replacement_check``).
        """
        children = self._raw_children.get(node.task_id, {})
        for task_id in sorted(_named_parents(node) & children.keys()):
            self._recheck(task_id)

    def _reported_parent_changed(self, node: TaskNode, old_parent: str | None) -> None:
        """Move a task in the parent index after its reported parent changed."""
        if old_parent is not None:
            self._forget_reported_child(node.task_id, old_parent)
        self._note_reported_child(node)

    def _parent_evidence_changed(
        self, tally: _GroupTally, node: TaskNode, event: TaskEvent, *, new_parent: bool
    ) -> None:
        """Refresh a member's parent status after a parent report or worker event."""
        first_worker_event = (
            node.task_id in tally.unparented and event.state != TaskState.PENDING
        )
        if new_parent or first_worker_event:
            self._update_status(tally, node)

    def _state_changed(
        self, node: TaskNode, tally: _GroupTally | None, old_state: TaskState
    ) -> None:
        """Move a task's state in its group's counts."""
        if old_state == TaskState.PENDING:
            self._retrigger(node.task_id)  # a chord that read it as a callback
        if tally is not None and node.task_id in tally.counted():
            _bump(tally.state_counts, old_state, -1)
            _bump(tally.state_counts, node.state, 1)

    def remove_node(self, task_id: str) -> None:
        """Remove a node and every link to it; unknown IDs are ignored.

        The node leaves its parent's children (and a CHORD's, as its
        callback), the roots, its group and any chain. Its children become
        roots, waiting for it like for any missing parent; a removed
        GROUP/CHORD releases its members to their own parents (or the roots)
        but keeps their membership, so a later member event can recreate it
        (a CHORD with the evidence its headers gave). Headers that name
        different callbacks, or two chords that name one, aren't Celery
        shapes: those chords draw no callback, and removing the task that
        caused the conflict brings it back.

        Args:
            task_id: ID of the node to remove.
        """
        node = self.nodes.get(task_id)
        if node is None or task_id in self._removing:
            return
        self._removing.add(task_id)
        try:
            self._remove(node)
        finally:
            self._removing.discard(task_id)
        if self._cycle_broken and not self._removing:
            self._retry_cycle_broken()

    def _remove(self, node: TaskNode) -> None:
        """Do the work of ``remove_node`` (``node`` is marked as removing)."""
        task_id = node.task_id
        self._unlink(node)
        self._set_awaited(task_id, None)
        chords = self._drop_chord_roles(task_id)
        if node.node_type == NodeType.TASK:
            chords.update(self._forget_task(node))
        else:
            chords.update(self._forget_group_node(node))
        del self.nodes[task_id]
        if node.node_type == NodeType.TASK:
            self._recheck_after_removal(node)
        if node.group_id is not None:
            self._dissolve_if_too_small(node.group_id)
        owner = self._link_owner.pop(task_id, None)
        self._link_pos.pop(task_id, None)
        if owner is not None and owner in self.nodes:
            self._rechain(self.nodes[owner])  # the chain now ends below it
        for child_id in node.children:
            child = self.nodes.get(child_id)
            if child is not None and child.parent_id == task_id:
                self._place(child)
        self._retrigger(task_id)
        if node.node_type != NodeType.TASK and node.group_id is not None:
            self._retrigger(node.group_id)  # a chord that read the group
        for group_id in chords:
            self._sync_unlock_headers(group_id)
            self._resolve_callback(group_id)

    def _recheck_after_removal(self, node: TaskNode) -> None:
        """Re-pick identities the removed task's events took part in deciding."""
        for walked in self._walks_of.pop(node.task_id, set()):
            _drop_from(self._walk_deps, walked, node.task_id)
        for related in _named_ids(node) - {node.task_id}:
            self._recheck_identity(related, node.task_id)
        self._recheck_walkers(node.task_id)

    def _dissolve_if_too_small(self, group_id: str) -> None:
        """Remove a GROUP node left with fewer than 2 members.

        That is the graph its remaining events would build (a GROUP needs 2
        members); a later member event recreates it.
        """
        group_node = self.nodes.get(_group_node_id(group_id))
        tally = self._groups.get(group_id)
        if (
            group_node is not None
            and group_node.node_type == NodeType.GROUP
            and (tally is None or len(tally.counted()) < 2)
        ):
            self.remove_node(group_node.task_id)

    def _drop_chord_roles(self, task_id: str) -> set[str]:
        """Stop a removed node being a chord's callback or chord_unlock task.

        Returns:
            Group IDs of the chords it served.
        """
        chords: set[str] = set()
        for index in (self._callback_of, self._unlock_of):
            group_id = index.pop(task_id, None)
            if group_id is not None:
                chords.add(group_id)
                if self._unlock_by_chord.get(group_id) == task_id:
                    del self._unlock_by_chord[group_id]
        for group_id in chords:
            chord_node = self.nodes.get(_group_node_id(group_id))
            if chord_node is not None and chord_node.chord_id == task_id:
                chord_node.chord_id = None
                chord_node.children.remove(task_id)
        return chords

    # -- placement ---------------------------------------------------------

    def _insert_node(self, event: TaskEvent) -> TaskNode:
        """Create the node for a task's first event and link it."""
        node = TaskNode(
            task_id=event.task_id,
            name=event.name,
            state=event.state,
            group_id=event.group_id,
            chord_id=event.chord_id,
        )
        node._record_event(event)
        node._note_reported_parent(self._replacement_check(node))
        self.nodes[event.task_id] = node
        node.parent_id = self._safe_parent(node, self._desired_parent(node))
        self._link(node)
        self._adopt_waiting_children(node.task_id)
        return node

    def _adopt_waiting_children(self, parent_id: str) -> None:
        """Move the nodes that were waiting for this parent under it."""
        for child_id in self._awaiting_parent.pop(parent_id, {}):
            if self._awaited_by.get(child_id) == parent_id:
                del self._awaited_by[child_id]
            self._place(self.nodes[child_id])

    def _desired_parent(self, node: TaskNode) -> str | None:
        """The parent the linking rules give a node (see the class docstring).

        Also records which missing parent the node waits for, if any.
        """
        inside = self._parent_inside_group(node)
        if inside is not None:
            self._set_awaited(node.task_id, None)
            return inside
        if node.node_type == NodeType.TASK:
            reported = node.reported_parent_id
        else:
            tally = self._groups.get(node.group_id or "")
            reported = tally.parent() if tally is not None else None
        chord_group = self._callback_of.get(node.task_id) or self._unlock_of.get(
            node.task_id
        )
        if chord_group is not None and _group_node_id(chord_group) in self.nodes:
            self._set_awaited(node.task_id, None)
            return _group_node_id(chord_group)
        if reported is not None and reported in self.nodes:
            self._set_awaited(node.task_id, None)
            return reported
        self._set_awaited(node.task_id, reported)
        return None

    def _parent_inside_group(self, node: TaskNode) -> str | None:
        """Where a group member or chain link sits inside its group, if it does.

        A member sits under the first link of its chain, or directly in the
        group; a link under the next link up, or in the group if it's the
        chain's first task.
        """
        member_id = self._link_owner.get(node.task_id)
        if member_id is None:
            group_node_id = self._member_group_node_id(node)
            if group_node_id is None:
                return None
            chain = self._chain_of.get(node.task_id)
            return chain[0] if chain else group_node_id
        group_node_id = self._member_group_node_id(self.nodes[member_id])
        chain = self._chain_of[member_id]
        position = self._link_pos[node.task_id]
        return chain[position + 1] if position + 1 < len(chain) else group_node_id

    def _safe_parent(self, node: TaskNode, parent_id: str | None) -> str | None:
        """``parent_id`` unless linking under it would create a cycle.

        Only a node with children can have a descendant to loop through.
        """
        if parent_id is None or parent_id == node.task_id:
            return None
        if node.children and self._is_ancestor(node.task_id, parent_id):
            return None
        return parent_id

    def _is_ancestor(self, candidate_id: str, node_id: str) -> bool:
        """Whether ``candidate_id`` is one of ``node_id``'s ancestors.

        A parent that isn't in the graph (mid-``remove_node``) ends the walk.
        """
        seen = {node_id}
        current_node = self.nodes.get(node_id)
        while current_node is not None and current_node.parent_id is not None:
            current = current_node.parent_id
            if current == candidate_id:
                return True
            if current in seen:
                return False
            seen.add(current)
            current_node = self.nodes.get(current)
        return False

    def _place(self, node: TaskNode) -> None:
        """Move a node to the parent the linking rules give it, if that changed.

        If a member or chain link can't take its place inside its group
        because the group sits below it, the group's link is the one broken:
        the group becomes a root.
        """
        desired = self._desired_parent(node)
        if desired != node.parent_id:
            parent_id = self._safe_parent(node, desired)
            if parent_id is None and desired is not None:
                parent_id = self._break_cycle(node, desired)
            if parent_id != node.parent_id:
                self._move(node, parent_id)
                if node.node_type != NodeType.TASK:
                    # A chord whose callback is this group resolves against its
                    # place, and so does a chord's own callback (the ancestor
                    # check).
                    self._retrigger(node.task_id)
                    if node.node_type == NodeType.CHORD and node.group_id is not None:
                        self._resolve_callback(node.group_id)
        if node.node_type != NodeType.TASK:
            self._group_parent_read(node)

    def _group_parent_read(self, group_node: TaskNode) -> None:
        """Re-resolve a chord whose own parent changed.

        A chord's callback can't be its own parent or hang above it
        (``_loops_back``). The group's place doesn't always follow its parent:
        drawn as a chord's callback, it stays under the chord. What changes a
        group's parent votes places its node next (``_rechain``,
        ``_leave_group``, and ``add_event`` through ``_refresh_group_of``
        after a member joins or the group becomes a chord).
        """
        group_id = group_node.group_id
        assert group_id is not None  # a synthetic node carries its group's ID
        tally = self._groups[group_id]
        parent = tally.parent()
        if parent == tally.read_parent:
            return
        tally.read_parent = parent
        if tally.headers:
            self._resolve_callback(group_id)

    def _break_cycle(self, node: TaskNode, desired: str) -> str | None:
        """Make room for a link that would close a cycle; returns the parent.

        First a chord callback edge on the way gives way; then, for a member
        or chain link, its group's link (the group becomes a root). Otherwise
        the node becomes a root and is retried after the next change.
        """
        self._cycles_refused += 1
        if self._drop_callback_on_path(desired):
            parent_id = self._safe_parent(node, desired)
            if parent_id is not None:
                return parent_id
        group_node_id = self._inside_group_of(node)
        # A group being removed is already unlinked: it isn't moved again.
        if group_node_id is not None and group_node_id not in self._removing:
            self._move(self.nodes[group_node_id], None)
            self._cycle_broken[group_node_id] = None
            parent_id = self._safe_parent(node, desired)
            if parent_id is not None:
                return parent_id
        self._cycle_broken[node.task_id] = None  # retry once it's gone
        return None

    def _retry_cycle_broken(self) -> None:
        """Re-place nodes kept off their parent by a cycle that may be gone now."""
        # A copy: ``_place_if_present`` can break a cycle again, which adds to
        # ``_cycle_broken`` (and the pop above removes from it).
        for node_id in list(self._cycle_broken):  # NOSONAR
            self._cycle_broken.pop(node_id, None)
            self._place_if_present(node_id)

    def _drop_callback_on_path(self, node_id: str) -> bool:
        """Release a chord callback edge on the path from ``node_id`` upwards.

        A placement loses to nothing but a cycle, and a callback edge always
        gives way: the chord re-resolves (and rejects a looping callback).

        Returns:
            True if a callback was released.
        """
        seen: set[str] = set()
        current = self.nodes.get(node_id)
        while current is not None and current.task_id not in seen:
            seen.add(current.task_id)
            group_id = self._callback_of.get(current.task_id)
            chord_node = self.nodes.get(_group_node_id(group_id)) if group_id else None
            if chord_node is not None and current.parent_id == chord_node.task_id:
                chord_node.chord_id = None
                self._release_callback(chord_node, current.task_id)
                self._resolve_callback(group_id or "")
                return True
            current = self.nodes.get(current.parent_id or "")
        return False

    def _inside_group_of(self, node: TaskNode) -> str | None:
        """The group node a member or chain link is drawn inside, if any."""
        member = self.nodes.get(self._link_owner.get(node.task_id, node.task_id))
        return self._member_group_node_id(member) if member is not None else None

    def _move(self, node: TaskNode, parent_id: str | None) -> None:
        """Re-link a node under ``parent_id`` (None: as a root)."""
        self._unlink(node)
        node.parent_id = parent_id
        self._link(node)

    def _link(self, node: TaskNode) -> None:
        """List a node under its ``parent_id``, or as a root if it has none.

        Only call it for a node that isn't listed yet (new or just unlinked).
        """
        if node.parent_id is None:
            self._roots[node.task_id] = None
            return
        parent = self.nodes[node.parent_id]
        # Only a CHORD can already list the node: as its callback.
        if parent.chord_id == node.task_id and node.task_id in parent.children:
            return
        parent.children.append(node.task_id)

    def _unlink(self, node: TaskNode) -> None:
        """Drop a node from its parent's children, or from the roots.

        A CHORD keeps its callback listed: that entry draws the chord's edge.
        """
        if node.parent_id is None:
            self._roots.pop(node.task_id, None)
            return
        parent = self.nodes.get(node.parent_id)
        if parent is None or parent.chord_id == node.task_id:
            return
        parent.children.remove(node.task_id)

    def _set_awaited(self, node_id: str, parent_id: str | None) -> None:
        """Record the missing parent a node waits for (None: it waits for none)."""
        previous = self._awaited_by.get(node_id)
        if previous == parent_id:
            return
        if previous is not None:
            _drop_from(self._awaiting_parent, previous, node_id)
            del self._awaited_by[node_id]
        if parent_id is not None:
            _add_to(self._awaiting_parent, parent_id, node_id)
            self._awaited_by[node_id] = parent_id

    # -- groups ------------------------------------------------------------

    def _member_group_node_id(self, node: TaskNode) -> str | None:
        """ID of the synthetic node a task sits in as a group member, if any."""
        if node.group_id is None:
            return None
        tally = self._groups.get(node.group_id)
        if tally is None or node.task_id not in tally.counted():
            return None
        group_node_id = _group_node_id(node.group_id)
        return group_node_id if group_node_id in self.nodes else None

    def _join_group(
        self,
        node: TaskNode,
        group_id: str,
        stamp: str | None,
        first: str | None,
    ) -> None:
        """Track a task as a member (a header, if it names a stamp or a first task).

        Creates the GROUP node at 2 members and the CHORD node at the first
        header; refreshes the group's counts and the chord's evidence.
        """
        tally = self._groups.setdefault(group_id, _GroupTally())
        changed = self._add_membership(node, group_id, tally, stamp, first)
        if changed:
            # Membership ends a chain link's claim and may start a chain.
            self._rechain_owner_of(node.task_id)
            self._retrigger(node.task_id)
            self._sync_unlock_headers(group_id)

        group_node = self.nodes.get(_group_node_id(group_id))
        if group_node is None and (tally.headers or len(tally.members) >= 2):
            self._create_group_node(group_id, tally)
        elif group_node is not None:
            group_node.state = _aggregate_group_state(tally.state_counts.keys())
            if changed:
                self._rechain(node)
        if changed:
            self._chord_evidence_changed(group_id, tally)

    def _add_membership(
        self,
        node: TaskNode,
        group_id: str,
        tally: _GroupTally,
        stamp: str | None,
        first: str | None,
    ) -> bool:
        """Add a task to a group's members (and headers); True if that's new.

        A task is a header once an event of it names the group and carries a
        chord stamp or first-task ID: the sender's PENDING carries the group
        ID for headers and body tasks alike, but not those.
        """
        was_chord = bool(tally.headers)
        changed = False
        if node.task_id not in tally.members:
            tally.members[node.task_id] = None
            changed = True
            if not was_chord:
                self._count_member(tally, node)
        if (stamp is not None or first is not None) and node.task_id not in (
            tally.marked
        ):
            replaces_unlock_headers = was_chord and not tally.marked
            tally.marked[node.task_id] = None
            changed = True
            if replaces_unlock_headers:
                self._apply_headers(group_id, tally, dict(tally.marked))
            else:
                tally.headers[node.task_id] = None
                if was_chord:
                    self._count_member(tally, node)
                else:
                    self._become_chord_group(group_id, tally)
        for evidence, value in ((tally.stamps, stamp), (tally.firsts, first)):
            if value is not None and node.task_id not in evidence.get(value, ()):
                _add_to(evidence, value, node.task_id)
                changed = True
        return changed

    def _wanted_headers(self, group_id: str, tally: _GroupTally) -> dict[str, None]:
        """The tasks that count as the chord's headers.

        The ones whose events name a stamp or first task; else, when a
        ``celery.chord_unlock`` task names the group, the members its ``result``
        lists (all of them when it lists none that can be read).
        """
        named = self._unlocks_by_group.get(group_id)
        if tally.marked or not named:
            return tally.marked
        listed: set[str] = set()
        for task_id in named:
            result = self._unlocks[task_id].result
            if result is None:
                return dict(tally.members)
            listed |= result
        return {m: None for m in tally.members if m in listed}

    def _sync_unlock_headers(self, group_id: str) -> None:
        """Make a group without header evidence of its own follow its unlock task."""
        tally = self._groups.get(group_id)
        if (
            tally is None
            or tally.marked
            or not (tally.headers or group_id in self._unlocks_by_group)
        ):
            return
        wanted = self._wanted_headers(group_id, tally)
        if wanted.keys() != tally.headers.keys():
            self._apply_headers(group_id, tally, wanted)

    def _apply_headers(
        self, group_id: str, tally: _GroupTally, wanted: dict[str, None]
    ) -> None:
        """Make ``wanted`` the group's headers (it may become or stop being a chord)."""
        tally.headers.clear()
        tally.headers.update(wanted)
        group_node = self.nodes.get(_group_node_id(group_id))
        if not wanted:
            self._stop_being_chord(group_id, tally, group_node)
            return
        self._become_chord_group(group_id, tally)
        if group_node is None:
            self._create_group_node(group_id, tally)
            return
        for member_id in tally.counted():
            self._rechain(self.nodes[member_id])
            self._place(self.nodes[member_id])
        group_node.state = _aggregate_group_state(tally.state_counts.keys())
        self._place(group_node)

    def _count_member(self, tally: _GroupTally, node: TaskNode) -> None:
        """Add a newly counted member's state, parent vote and parent status."""
        _bump(tally.state_counts, node.state, 1)
        tally.set_vote(node.task_id, self._member_vote(node))
        self._update_status(tally, node)

    def _update_status(self, tally: _GroupTally, node: TaskNode) -> None:
        """Refresh a counted member's parent status; rechain if that flips.

        Celery puts the group ID only on worker events, which carry the
        parent when there is one: after any non-PENDING event, a member
        without a reported parent has none.
        """
        if node.reported_parent_id is not None:
            status: str | None = node.reported_parent_id
        elif any(event.state != TaskState.PENDING for event in node.events):
            status = ""
        else:
            status = None
        if tally.status.get(node.task_id) == status and (
            status is not None or node.task_id in tally.unparented
        ):
            return
        was_allowed = tally.chains_allowed()
        tally.set_status(node.task_id, status)
        if tally.chains_allowed() != was_allowed:
            self._rechain_group(tally)

    def _rechain_group(self, tally: _GroupTally) -> None:
        """Recompute every counted member's chain (the evidence changed)."""
        for member_id in tally.counted():
            self._rechain(self.nodes[member_id])

    def _member_vote(self, node: TaskNode) -> str | None:
        """The parent a member votes for: its chain's first task's parent."""
        chain = self._chain_of.get(node.task_id)
        top = self.nodes.get(chain[-1]) if chain else node
        return top.reported_parent_id if top is not None else None

    def _recount(self, tally: _GroupTally) -> None:
        """Rebuild the counts, e.g. after the group turned out to be a chord."""
        tally.state_counts.clear()
        tally.parent_votes.clear()
        tally.vote_of.clear()
        tally.status.clear()
        tally.direct_parents.clear()
        tally.rootish = 0
        tally.unparented.clear()
        for member_id in tally.counted():
            self._count_member(tally, self.nodes[member_id])

    def _become_chord_group(self, group_id: str, tally: _GroupTally) -> None:
        """Count only headers from now on, releasing the other members."""
        for member_id in tally.members:
            if member_id not in tally.headers:
                self._drop_chain(member_id)
        self._recount(tally)
        group_node = self.nodes.get(_group_node_id(group_id))
        if group_node is not None:
            group_node.node_type = NodeType.CHORD
            group_node.name = "chord"
        for member_id in tally.members:
            member = self.nodes.get(member_id)
            if member is not None and member_id not in tally.headers:
                self._place(member)
                # It no longer counts as a member: chords reading it see a task.
                self._retrigger(member_id)

    def _create_group_node(self, group_id: str, tally: _GroupTally) -> None:
        """Create the synthetic GROUP/CHORD node and move its members into it."""
        is_chord = bool(tally.headers)
        group_node = TaskNode(
            task_id=_group_node_id(group_id),
            name="chord" if is_chord else "group",
            state=_aggregate_group_state(tally.state_counts.keys()),
            node_type=NodeType.CHORD if is_chord else NodeType.GROUP,
            group_id=group_id,
            chord_callback_id=self._named_stamp(tally) if is_chord else None,
        )
        self.nodes[group_node.task_id] = group_node
        group_node.parent_id = self._safe_parent(
            group_node, self._desired_parent(group_node)
        )
        self._link(group_node)
        for member_id in tally.counted():
            member = self.nodes[member_id]
            self._rechain(member)
            self._place(member)
        self._adopt_waiting_children(group_node.task_id)
        self._place(group_node)
        self._retrigger(group_node.task_id)
        if is_chord:
            self._resolve_callback(group_id)

    def _refresh_group_of(self, node: TaskNode) -> None:
        """Re-place the synthetic node of a task's group, if there is one."""
        member_id = self._link_owner.get(node.task_id, node.task_id)
        member = self.nodes.get(member_id)
        if member is None or member.group_id is None:
            return
        group_node = self.nodes.get(_group_node_id(member.group_id))
        if group_node is not None:
            self._place(group_node)

    # -- chains inside groups ------------------------------------------------

    def _note_reported_child(self, node: TaskNode) -> None:
        """Index a task under the parent it reported, then update chains."""
        parent_id = node.reported_parent_id
        if parent_id is None:
            return
        _add_to(self._reported_children, parent_id, node.task_id)
        self._on_children_changed(parent_id)
        # A chain whose first task this is can now extend upwards.
        self._extend_chain_through(node.task_id)

    def _forget_reported_child(self, task_id: str, parent_id: str) -> None:
        """A task no longer reports ``parent_id`` (an earlier event said else)."""
        _drop_from(self._reported_children, parent_id, task_id)
        self._on_children_changed(parent_id)
        self._rechain_owner_of(task_id)

    def _single_child(self, parent_id: str) -> str | None:
        """The only task that reported ``parent_id`` as parent, if just one did."""
        children = self._reported_children.get(parent_id)
        if children is None or len(children) != 1:
            return None
        return next(iter(children))

    def _on_children_changed(self, parent_id: str) -> None:
        """Re-check chains near a task whose reported children (or node) changed.

        Only going to or from exactly one child changes whether it is a link:
        with one child, that child's chain may extend through it; with two,
        a chain it was a link of ends below it.
        """
        children = self._reported_children.get(parent_id)
        if not children or len(children) > 2:
            return
        if len(children) == 1:
            self._extend_chain_through(next(iter(children)))
        elif parent_id in self._link_owner:
            self._rechain_owner_of(parent_id)

    def _extend_chain_through(self, task_id: str) -> None:
        """Extend the chain whose first task (or chainless member) this is."""
        member_id = self._link_owner.get(task_id, task_id)
        chain = self._chain_of.get(member_id)
        top = chain[-1] if chain else member_id
        member = self.nodes.get(member_id)
        if top == task_id and member is not None:
            self._rechain(member, extend=True)

    def _rechain_owner_of(self, task_id: str) -> None:
        """Recompute the chain of the member a task is (or is a link of)."""
        self._rechain(self.nodes[self._link_owner.get(task_id, task_id)])

    def _can_link(self, task_id: str, child_id: str) -> bool:
        """Whether a task is a chain link above ``child_id`` (see class docstring)."""
        node = self.nodes.get(task_id)
        if node is None or node.node_type != NodeType.TASK or task_id in self._removing:
            return False
        if self._single_child(task_id) != child_id:
            return False
        tally = self._groups.get(node.group_id) if node.group_id else None
        if tally is not None and task_id in tally.members:
            return False
        if task_id in self._callback_of or task_id in self._unlock_of:
            return False
        return node.name != _CHORD_UNLOCK_TASK

    def _rechain(self, member: TaskNode, *, extend: bool = False) -> None:
        """Recompute a group member's chain links and re-place what moved.

        With ``extend``, only links above the chain's first task are looked
        for (nothing below it changed): O(new links).
        """
        group_node_id = self._member_group_node_id(member)
        tally = self._groups.get(member.group_id or "")
        previous = self._chain_of.get(member.task_id, [])
        if group_node_id is None or tally is None or not tally.chains_allowed():
            moved = self._set_chain(member.task_id, [], previous)
        elif extend:
            top = previous[-1] if previous else member.task_id
            moved = self._extend_chain(member.task_id, top)
        else:
            chain = self._links_above(member.task_id, member.task_id, extending=False)
            moved = self._set_chain(member.task_id, chain, previous)
        if not moved and not self._refresh_vote(member, tally):
            return  # nothing moved
        self._refresh_vote(member, tally)
        # The group before and after the links: its new parent may be a link
        # that is only now leaving the group, or one that is joining it.
        if group_node_id is not None:
            self._place(self.nodes[group_node_id])
        for node_id in moved:
            self._place_if_present(node_id)
        if moved and group_node_id is not None:
            self._place(self.nodes[group_node_id])

    def _links_above(
        self, start_id: str, member_id: str, *, extending: bool
    ) -> list[str]:
        """Chain links above a task, bottom-up (see the class docstring).

        Stops at a task already in the walk, or (``extending`` an existing
        chain) already in the member's chain: a parent cycle.
        """
        links: list[str] = []
        seen = {start_id, member_id}
        current = self.nodes[start_id]
        while (
            (parent_id := current.reported_parent_id) is not None
            and parent_id not in seen
            and not (extending and self._link_owner.get(parent_id) == member_id)
            and self._can_link(parent_id, current.task_id)
        ):
            current = self.nodes[parent_id]
            seen.add(parent_id)
            links.append(parent_id)
        return links

    def _extend_chain(self, member_id: str, top: str) -> list[str]:
        """Append the links above a chain's first task; returns what moved."""
        chain = self._chain_of.get(member_id, [])
        new_links = self._links_above(top, member_id, extending=True)
        if not new_links:
            return []
        if not chain:
            chain = self._chain_of[member_id] = []
        for link_id in new_links:
            self._link_owner[link_id] = member_id
            self._link_pos[link_id] = len(chain)
            chain.append(link_id)
        # The old first task now sits under the first new link.
        return [top, *new_links]

    def _set_chain(
        self, member_id: str, chain: list[str], previous: list[str]
    ) -> list[str]:
        """Replace a member's chain links; returns the nodes whose place moved."""
        if chain == previous:
            return []
        for link_id in previous:
            if self._link_owner.get(link_id) == member_id:
                del self._link_owner[link_id]
                del self._link_pos[link_id]
        for position, link_id in enumerate(chain):
            self._link_owner[link_id] = member_id
            self._link_pos[link_id] = position
        if chain:
            self._chain_of[member_id] = chain
        else:
            self._chain_of.pop(member_id, None)
        return [*reversed(chain), *previous, member_id]

    def _refresh_vote(self, member: TaskNode, tally: _GroupTally | None) -> bool:
        """Update a counted member's vote (its chain's first task's parent).

        Returns:
            True if the vote changed.
        """
        if tally is None or member.task_id not in tally.counted():
            return False
        vote = self._member_vote(member)
        if tally.vote_of.get(member.task_id) == vote:
            return False
        tally.set_vote(member.task_id, vote)
        return True

    def _drop_chain(self, member_id: str) -> None:
        """Release a task's chain links (it stopped being a counted member)."""
        for link_id in self._chain_of.pop(member_id, []):
            del self._link_owner[link_id]
            del self._link_pos[link_id]
            self._place(self.nodes[link_id])

    def _forget_task(self, node: TaskNode) -> set[str]:
        """Drop a removed task from its group, chains and the parent index.

        Returns:
            Group IDs of chords whose callback may have changed.
        """
        chords: set[str] = set()
        for parent in {e.parent_id for e in node.events if e.parent_id is not None}:
            _drop_from(self._raw_children, parent, node.task_id)
        for root in {e.root_id for e in node.events if e.root_id is not None}:
            _drop_from(self._descendants, root, node.task_id)
        owner = self._link_owner.pop(node.task_id, None)
        self._drop_chain(node.task_id)
        parent_id = node.reported_parent_id
        if parent_id is not None:
            _drop_from(self._reported_children, parent_id, node.task_id)
            self._on_children_changed(parent_id)
        if node.task_id in self._unlocks:
            chords.update(self._drop_unlock(node.task_id))
        if node.group_id is not None:
            chords.update(self._leave_group(node, node.group_id))
            chords.add(node.group_id)
        if owner is not None:
            # Recomputed once the node is gone (see remove_node).
            self._link_owner[node.task_id] = owner
        return chords

    def _leave_group(self, node: TaskNode, group_id: str) -> set[str]:
        """Drop a removed task from its group's members and counts.

        Returns:
            Group IDs of other chords whose callback may have changed.
        """
        tally = self._groups[group_id]
        if node.task_id in tally.counted():
            _bump(tally.state_counts, node.state, -1)
            tally.set_vote(node.task_id, None)
        was_allowed = tally.chains_allowed()
        tally.clear_status(node.task_id)
        del tally.members[node.task_id]
        was_header = node.task_id in tally.headers
        tally.headers.pop(node.task_id, None)
        tally.marked.pop(node.task_id, None)
        for evidence in (tally.stamps, tally.firsts):
            for value in [v for v, ids in evidence.items() if node.task_id in ids]:
                _drop_from(evidence, value, node.task_id)
        peers = self._refresh_stamp_index(group_id, tally)
        if tally.chains_allowed() != was_allowed:
            self._rechain_group(tally)
        group_node = self.nodes.get(_group_node_id(group_id))
        if was_header and not tally.headers:
            self._stop_being_chord(group_id, tally, group_node)
        if group_node is not None:
            group_node.state = _aggregate_group_state(tally.state_counts.keys())
            if group_node.node_type == NodeType.CHORD:
                group_node.chord_callback_id = self._named_stamp(tally)
            self._place(group_node)
        elif not tally.members:
            del self._groups[group_id]
        return peers

    def _stop_being_chord(
        self, group_id: str, tally: _GroupTally, group_node: TaskNode | None
    ) -> None:
        """The last header is gone: the others count as plain members again."""
        self._recount(tally)
        if group_node is not None:
            self._set_callback(group_node, group_id, None)
            group_node.chord_callback_id = None
            group_node.node_type = NodeType.GROUP
            group_node.name = "group"
        peers = self._drop_chord_state(group_id)
        for member_id in tally.members:
            member = self.nodes[member_id]
            self._rechain(member)
            self._place(member)
            # It now counts as a member: chords reading it see a group.
            self._retrigger(member_id)
        for peer in sorted(peers):
            self._resolve_callback(peer)

    def _forget_group_node(self, node: TaskNode) -> set[str]:
        """Drop a removed GROUP/CHORD's links (its membership stays).

        Returns:
            Group IDs of other chords whose callback may have changed.
        """
        group_id = node.group_id or ""
        callback_id = node.chord_id
        if callback_id is not None and self._callback_of.get(callback_id) == group_id:
            del self._callback_of[callback_id]
        unlock_id = self._unlock_by_chord.pop(group_id, None)
        if unlock_id is not None and self._unlock_of.get(unlock_id) == group_id:
            del self._unlock_of[unlock_id]
        peers = self._drop_chord_state(group_id)
        tally = self._groups[group_id]
        for member_id in tally.counted():
            self._drop_chain(member_id)
        if not tally.members:
            del self._groups[group_id]
        return peers

    # -- chords ------------------------------------------------------------

    @staticmethod
    def _named_stamp(tally: _GroupTally) -> str | None:
        """The one callback ID the headers name, or None (none, or they differ)."""
        return next(iter(tally.stamps)) if len(tally.stamps) == 1 else None

    def _chord_evidence_changed(self, group_id: str, tally: _GroupTally) -> None:
        """Re-resolve what a change to a group's members or headers affects."""
        peers = self._refresh_stamp_index(group_id, tally)
        group_node = self.nodes.get(_group_node_id(group_id))
        if group_node is not None and group_node.node_type == NodeType.CHORD:
            group_node.chord_callback_id = self._named_stamp(tally)
        self._retrigger(group_id)  # chords that read the group (as a callback)
        for peer in sorted(peers):
            self._resolve_callback(peer)
        self._resolve_callback(group_id)

    def _refresh_stamp_index(self, group_id: str, tally: _GroupTally) -> set[str]:
        """Index the callback ID the chord's headers name, if one and not its own.

        Returns:
            The other chords that name the IDs this one named or names now.
        """
        stamp = self._named_stamp(tally)
        new = stamp if stamp != group_id else None
        old = self._stamp_of.get(group_id)
        if new == old:
            return set()
        peers: set[str] = set()
        if old is not None:
            _drop_from(self._chords_by_stamp, old, group_id)
            peers.update(self._chords_by_stamp.get(old, ()))
            del self._stamp_of[group_id]
        if new is not None:
            peers.update(self._chords_by_stamp.get(new, ()))
            _add_to(self._chords_by_stamp, new, group_id)
            self._stamp_of[group_id] = new
        return peers

    def _refresh_unlock(self, node: TaskNode) -> None:
        """Index what a ``celery.chord_unlock`` task's events say about its chord."""
        old = self._unlocks.get(node.task_id)
        if old is None and node.name != _CHORD_UNLOCK_TASK:
            return
        new = _read_unlock(node) if node.name == _CHORD_UNLOCK_TASK else None
        if new == old:
            return
        # The chord it no longer names lets go of it first.
        chords = sorted(self._drop_unlock(node.task_id)) if old is not None else []
        if new is not None:
            self._unlocks[node.task_id] = new
            if new.group_id is not None:
                _add_to(self._unlocks_by_group, new.group_id, node.task_id)
                chords.append(new.group_id)
        for group_id in chords:
            self._sync_unlock_headers(group_id)
            self._resolve_callback(group_id)

    def _drop_unlock(self, task_id: str) -> set[str]:
        """Forget a ``chord_unlock`` task; returns the chord it named."""
        info = self._unlocks.pop(task_id, None)
        if info is None or info.group_id is None:
            return set()
        _drop_from(self._unlocks_by_group, info.group_id, task_id)
        return {info.group_id}

    def _retrigger(self, node_id: str) -> None:
        """Re-resolve the chords whose callback depends on this node or group."""
        # A copy: resolving a chord adds its reads (``_add_chord_dep``) to
        # ``_chord_deps`` while we iterate.
        for group_id in list(self._chord_deps.get(node_id, ())):  # NOSONAR
            self._resolve_callback(group_id)

    def _add_chord_dep(self, group_id: str, node_id: str) -> None:
        """Record that a chord's callback resolution read this task or group."""
        deps = self._deps_of_chord.setdefault(group_id, set())
        if node_id not in deps:
            deps.add(node_id)
            _add_to(self._chord_deps, node_id, group_id)

    def _drop_chord_state(self, group_id: str) -> set[str]:
        """Forget a chord's reads and claim (its node is gone or no chord).

        Returns:
            The chords that pointed to the same node: they may draw it now.
        """
        for node_id in self._deps_of_chord.pop(group_id, set()):
            _drop_from(self._chord_deps, node_id, group_id)
        candidate = self._candidate.pop(group_id, None)
        if candidate is None:
            return set()
        _drop_from(self._claims, candidate, group_id)
        return set(self._claims.get(candidate, ()))

    def _resolve_callback(self, group_id: str) -> None:
        """Pick the node drawn as a chord's callback, from IDs its events name.

        See ``_evidence_callback``. A node two chords point to is drawn after
        neither; one that would close a cycle is not drawn.
        """
        if group_id in self._resolving:
            return
        self._resolving.add(group_id)
        try:
            self._resolve_callback_now(group_id)
        finally:
            self._resolving.discard(group_id)

    def _resolve_callback_now(self, group_id: str) -> None:
        """Do the work of ``_resolve_callback``."""
        chord_node = self.nodes.get(_group_node_id(group_id))
        tally = self._groups.get(group_id)
        if chord_node is None or tally is None or not tally.headers:
            return  # a group (or a chord whose node is gone): nothing to draw
        unlocks = self._unlocks_of(group_id, tally)
        candidate = self._evidence_callback(group_id, tally, unlocks)
        sole = self._claim(group_id, candidate)
        callback: tuple[str, str | None] | None = None
        if candidate is not None and sole:
            chord_parent = tally.parent()
            if self._loops_back(candidate, chord_node, chord_parent):
                self._cycles_refused += 1
            else:
                callback = (candidate, min(unlocks, default=None))
        self._set_callback(chord_node, group_id, callback)

    def _claim(self, group_id: str, candidate: str | None) -> bool:
        """Record the node a chord's evidence points to; whether it is alone there.

        Another chord that points to the same node is told: neither draws it.
        """
        previous = self._candidate.get(group_id)
        if previous != candidate:
            peers: set[str] = set()
            if previous is not None:
                _drop_from(self._claims, previous, group_id)
                peers.update(self._claims.get(previous, ()))
            if candidate is None:
                del self._candidate[group_id]
            else:
                peers.update(self._claims.get(candidate, ()))
                _add_to(self._claims, candidate, group_id)
                self._candidate[group_id] = candidate
            for peer in sorted(peers):
                self._resolve_callback(peer)
        return candidate is not None and len(self._claims[candidate]) == 1

    def _unlocks_of(self, group_id: str, tally: _GroupTally) -> dict[str, _UnlockInfo]:
        """The ``chord_unlock`` tasks of a chord: named by their first argument.

        One whose ``result`` doesn't list every header that names a stamp or a
        first task isn't this chord's.
        """
        return {
            task_id: info
            for task_id in self._unlocks_by_group.get(group_id, ())
            if (info := self._unlocks[task_id]).result is None
            or not tally.marked.keys() - info.result
        }

    def _evidence_callback(
        self, group_id: str, tally: _GroupTally, unlocks: dict[str, _UnlockInfo]
    ) -> str | None:
        """The node a chord's evidence points to as its callback, if it is clear.

        The first task of the body comes from the headers' events and from the
        ``chord_unlock`` task's body signature; they must agree. Without it,
        the one callback ID the headers name (a stamp) counts when it is a task
        outside the chord's group (the stamp of a chain body is its last task,
        which also carries the group ID) or another group. Anything missing,
        conflicting or ambiguous gives None; a task name is never read here.
        """
        if len(tally.stamps) > 1:
            return None
        known, first = self._body_first_id(tally, unlocks)
        if not known:
            return None
        if first is not None:
            return self._body_task(group_id, tally, first)
        return self._stamped_callback(group_id, tally)

    @staticmethod
    def _body_first_id(
        tally: _GroupTally, unlocks: dict[str, _UnlockInfo]
    ) -> tuple[bool, str | None]:
        """The body's first task ID the headers and unlock tasks name.

        Returns:
            ``(False, None)`` when they conflict, else ``(True, id or None)``.
        """
        if len(tally.firsts) > 1:
            return False, None
        first = next(iter(tally.firsts), None)
        from_unlocks = {info.first for info in unlocks.values()}
        if unlocks and len(from_unlocks) != 1:
            return False, None
        named = next(iter(from_unlocks), None)
        if named is None:
            return True, first
        if first is not None and first != named:
            return False, None
        return True, named

    def _body_task(self, group_id: str, tally: _GroupTally, first: str) -> str | None:
        """``first`` if it is a task with events that isn't one of the headers."""
        self._add_chord_dep(group_id, first)
        usable = (
            first in self.nodes and first != group_id and first not in tally.headers
        )
        return first if usable else None

    def _stamped_callback(self, group_id: str, tally: _GroupTally) -> str | None:
        """The callback the headers' one stamp names, if nothing else claims it."""
        stamp = self._named_stamp(tally)
        if stamp is None or stamp == group_id:
            return None  # a chord stamped with its own ID names no node
        self._add_chord_dep(group_id, stamp)
        if len(self._chords_by_stamp.get(stamp, ())) > 1:
            return None
        node = self.nodes.get(stamp)
        if node is not None and stamp not in self._removing:
            # A member of the group is the callback or a chain body's last task.
            # Until a worker reports the task, its PENDING may not carry the
            # group ID yet (workers before 0.4.0 left it out).
            ambiguous = stamp in tally.members or node.state == TaskState.PENDING
            return None if ambiguous else stamp
        node_id = _group_node_id(stamp)
        self._add_chord_dep(group_id, node_id)
        return node_id if node_id in self.nodes else None

    def _loops_back(
        self, callback_id: str, chord_node: TaskNode, chord_parent: str | None
    ) -> bool:
        """Whether drawing a callback after the chord would close a cycle.

        That is when the chord hangs below it, or the chord's own parent is it
        or hangs below it: the chord's place wins over the callback edge.
        """
        if self._is_ancestor(callback_id, chord_node.task_id):
            return True
        return chord_parent is not None and (
            chord_parent == callback_id or self._is_ancestor(callback_id, chord_parent)
        )

    def _set_callback(
        self,
        chord_node: TaskNode,
        group_id: str,
        callback: tuple[str, str | None] | None,
    ) -> None:
        """Make ``callback`` (and its chord_unlock task) the chord's callback."""
        callback_id, unlock_id = callback if callback is not None else (None, None)
        self._set_unlock(group_id, unlock_id)
        previous = chord_node.chord_id
        if previous != callback_id:
            chord_node.chord_id = callback_id
            if previous is not None:
                self._release_callback(chord_node, previous)
            if callback_id is not None:
                self._claim_callback(group_id, callback_id)
        if callback_id is None:
            return
        self._place(self.nodes[callback_id])
        # Placing it can re-resolve this chord (and drop the callback).
        if (
            chord_node.chord_id == callback_id
            and callback_id not in chord_node.children
        ):
            chord_node.children.append(callback_id)

    def _set_unlock(self, group_id: str, unlock_id: str | None) -> None:
        """Record the chord_unlock task drawn under a chord (None: none)."""
        previous = self._unlock_by_chord.get(group_id)
        if previous == unlock_id:
            return
        if previous is not None:
            del self._unlock_by_chord[group_id]
            del self._unlock_of[previous]
            self._place_if_present(previous)
        if unlock_id is not None:
            self._unlock_by_chord[group_id] = unlock_id
            self._unlock_of[unlock_id] = group_id
            self._place_if_present(unlock_id)

    def _place_if_present(self, node_id: str) -> None:
        """Re-place a node if it is in the graph."""
        node = self.nodes.get(node_id)
        if node is not None:
            self._place(node)

    def _claim_callback(self, group_id: str, callback_id: str) -> None:
        """Record a chord's callback."""
        self._callback_of[callback_id] = group_id
        self._rechain_owner_of(callback_id)  # a callback isn't a chain link

    def _release_callback(self, chord_node: TaskNode, callback_id: str) -> None:
        """Unlink a node that is no longer the chord's callback."""
        del self._callback_of[callback_id]
        callback_node = self.nodes.get(callback_id)
        if callback_node is not None and callback_node.parent_id == chord_node.task_id:
            self._place(callback_node)
        else:
            chord_node.children.remove(callback_id)
        # No longer a callback: it may be a chain link now.
        self._on_children_changed(callback_id)

    def get_node(self, task_id: str) -> TaskNode | None:
        """Get node by ID, or None if not found."""
        return self.nodes.get(task_id)

    def get_group_members(self, group_id: str) -> list[str]:
        """Get task IDs that belong to a group (a chord's headers)."""
        tally = self._groups.get(group_id)
        return list(tally.counted()) if tally is not None else []


def chord_not_run_reasons(
    nodes: Mapping[str, TaskNode],
) -> dict[str, ChordNotRunReason]:
    """Why each chord's callback hasn't run, by CHORD node ID.

    A header (a group task whose events named a callback or a first task) that failed, was
    rejected, revoked or ignored stops Celery running the callback, even while
    other headers run; the worst outcome wins and clears only once a callback
    is drawn. ``nodes`` must hold the chord's whole graph.
    """
    by_group: dict[str, tuple[int, ChordNotRunReason]] = {}
    for node in nodes.values():
        if node.node_type != NodeType.TASK or node.group_id is None:
            continue
        ranked = _HEADER_REASONS.get(node.state)
        current = by_group.get(node.group_id)
        if ranked is None or (current is not None and ranked[0] <= current[0]):
            continue
        if any(
            event.chord_callback_id is not None or event.chord_body_first_id is not None
            for event in node.events
        ):
            by_group[node.group_id] = ranked
    return {
        node_id: by_group[node.group_id][1]
        for node_id, node in nodes.items()
        if node.node_type == NodeType.CHORD
        and node.chord_id is None
        and node.group_id in by_group
    }
