"""Task graph models for representing task execution flows."""

import bisect
import hashlib
from collections import Counter
from collections.abc import Collection
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    computed_field,
    model_validator,
)
from pydantic_core import PydanticSerializationError

from stemtrace.core.events import TaskEvent, TaskState

if TYPE_CHECKING:
    # Only an annotation: pydantic exports it from 2.11 on, below our minimum.
    from pydantic import ModelWrapValidatorHandler

_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCESS, TaskState.FAILURE, TaskState.REVOKED, TaskState.REJECTED}
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
)


def _aggregate_group_state(states: Collection[TaskState]) -> TaskState:
    """A group's state from its members' states (see ``_GROUP_STATE_RULES``).

    Priority: FAILURE > REJECTED > STARTED/RECEIVED > PENDING > RETRY >
    REVOKED > SUCCESS; a group without known members is PENDING.
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


def _is_pre_start(event: TaskEvent) -> bool:
    """Whether the event belongs before the first run of the task.

    The sender's PENDING (``task_sent`` fires after publish) can be stamped
    after the worker already started or even finished the task, so its
    timestamp can't place it.
    """
    return event.retries == 0 and event.state in (TaskState.PENDING, TaskState.RECEIVED)


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
    * Terminal vs terminal: the later timestamp wins.
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
    if event.state in _TERMINAL_STATES:
        return (1, 0, 0, event.timestamp, tiebreak)
    return (0, event.retries, _PHASE[event.state], event.timestamp, tiebreak)


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

    def _note_reported_parent(self, parent_id: str | None) -> bool:
        """Remember the first parent the task's events reported.

        The sender's PENDING carries no ``parent_id``; later events do. A
        reported parent is never replaced, and a task can't be its own parent
        (Celery reports a retried task as its own parent).

        Returns:
            True if this set the reported parent.
        """
        if (
            self.reported_parent_id is None
            and parent_id is not None
            and parent_id != self.task_id
        ):
            self.reported_parent_id = parent_id
            return True
        return False


class _EventIndex:
    """Sort keys, duplicate lookup and state-deciding event for one node."""

    __slots__ = ("by_key", "keys", "state_event")

    def __init__(self, events: list[TaskEvent]) -> None:
        """Sort ``events`` in place into lifecycle order and index them."""
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
    them carried a ``chord_callback_id`` the group is a chord, and only those
    (``headers``) count as members: Celery also stamps a chord's group ID on
    its callback and on the last task of a chain body, which aren't headers.
    The counts (each counted member's vote for the group's parent, and their
    states) keep the group's parent and state O(1) per event.
    """

    __slots__ = (
        "headers",
        "members",
        "parent_votes",
        "state_counts",
        "unparented",
        "vote_of",
    )

    def __init__(self) -> None:
        """Start an empty group."""
        self.members: dict[str, None] = {}
        self.headers: dict[str, None] = {}
        self.parent_votes: Counter[str] = Counter()
        self.state_counts: Counter[TaskState] = Counter()
        self.vote_of: dict[str, str] = {}
        # Counted members that haven't reported a parent (yet).
        self.unparented: set[str] = set()

    def counted(self) -> dict[str, None]:
        """The members that make up the group (headers, for a chord)."""
        return self.headers if self.headers else self.members

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
    reachable from a root. The final shape doesn't depend on event order.

    * A task sits under the parent its events reported (``reported_parent_id``,
      the first one wins). While that parent isn't in the graph (not seen yet,
      or evicted) the task is a root; it moves under the parent when the
      parent's node is created.
    * Tasks sharing a ``group_id`` get a synthetic GROUP node (from 2
      members) or CHORD node (from the first header carrying a
      ``chord_callback_id``; then only headers are members). A member sits
      inside its group; the group sits under its members' reported parent
      (the most common one if they disagree), or is a root while that parent
      isn't in the graph.
    * Chains inside a group: a member's reported parent whose only child is
      that member (and which belongs to no group and isn't a chord callback)
      is a link of the member's chain, drawn inside the group, and so on up
      the chain. The group then sits under the chain's first task's parent.
    * A CHORD's callback is the node its body starts with, taken only from
      evidence: walking up reported parents from the callback the headers
      name, or from a task stamped with the chord's group ID (a chain body's
      last task), until a task whose parent is a header (or Celery's
      ``chord_unlock`` task). A walk that stops at a missing task, or one
      that hasn't reported its parent yet, resumes when that changes; until
      then the chord has no callback. The CHORD lists the callback among its
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
    # Celery group ID -> its members and counts.
    _groups: dict[str, _GroupTally] = PrivateAttr(default_factory=dict)
    # Reported parent ID -> tasks that reported it, in arrival order.
    _reported_children: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    # Group member -> its chain links inside the group (bottom-up), and the
    # reverse: link -> member.
    _chain_of: dict[str, list[str]] = PrivateAttr(default_factory=dict)
    _link_owner: dict[str, str] = PrivateAttr(default_factory=dict)
    # Chord callback ID the headers name -> the chord's group ID.
    _chord_by_callback: dict[str, str] = PrivateAttr(default_factory=dict)
    # Node drawn as a chord's callback -> that chord's group ID; and the
    # chord_unlock task that fired it -> the chord's group ID.
    _callback_of: dict[str, str] = PrivateAttr(default_factory=dict)
    _unlock_of: dict[str, str] = PrivateAttr(default_factory=dict)
    _unlock_by_chord: dict[str, str] = PrivateAttr(default_factory=dict)
    # Node IDs a chord's callback resolution read -> chord group IDs, and the
    # reverse; a change to one of those nodes re-resolves the chord.
    _chord_deps: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    _deps_of_chord: dict[str, set[str]] = PrivateAttr(default_factory=dict)
    # Missing parent ID -> nodes (in arrival order) to move under it once its
    # node is created, and the reverse map to keep both in sync.
    _awaiting_parent: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    _awaited_by: dict[str, str] = PrivateAttr(default_factory=dict)

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
        if node is None:
            node = self._insert_node(event)
            new_parent = node.reported_parent_id is not None
            old_state = None
        else:
            old_state = node.state
            if not node._record_event(event):
                return None
            node._fill_missing_ids(event)
            new_parent = node._note_reported_parent(event.parent_id)

        tally = self._groups.get(node.group_id) if node.group_id else None
        if (
            tally is not None
            and old_state is not None
            and old_state != node.state
            and node.task_id in tally.counted()
        ):
            _bump(tally.state_counts, old_state, -1)
            _bump(tally.state_counts, node.state, 1)
        if new_parent:
            self._note_reported_child(node)
            if tally is not None and node.task_id in tally.unparented:
                self._mark_parented(tally, node)
        if event.group_id is not None and event.group_id == node.group_id:
            self._join_group(node, event.group_id, event.chord_callback_id)
        if is_new:
            self._on_children_changed(node.task_id)
        self._place(node)
        self._refresh_group_of(node)
        if is_new or new_parent:
            self._retrigger(node.task_id)
        return event

    def remove_node(self, task_id: str) -> None:
        """Remove a node and every link to it; unknown IDs are ignored.

        The node leaves its parent's children (and a CHORD's, as its
        callback), the roots, its group and any chain. Its children become
        roots, waiting for it like for any missing parent; a removed
        GROUP/CHORD releases its members to their own parents (or the roots)
        but keeps their membership, so a later member event can recreate it.

        Args:
            task_id: ID of the node to remove.
        """
        node = self.nodes.get(task_id)
        if node is None:
            return
        self._unlink(node)
        self._set_awaited(task_id, None)
        chords = self._drop_chord_roles(task_id)
        if node.node_type == NodeType.TASK:
            chords.update(self._forget_task(node))
        else:
            self._forget_group_node(node)
        del self.nodes[task_id]
        owner = self._link_owner.pop(task_id, None)
        if owner is not None and owner in self.nodes:
            self._rechain(self.nodes[owner])  # the chain now ends below it
        for child_id in node.children:
            child = self.nodes.get(child_id)
            if child is not None and child.parent_id == task_id:
                self._place(child)
        self._retrigger(task_id)
        for group_id in chords:
            self._resolve_callback(group_id)

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
                if task_id in chord_node.children:
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
        node._note_reported_parent(event.parent_id)
        node._record_event(event)
        self.nodes[event.task_id] = node
        node.parent_id = self._safe_parent(node, self._desired_parent(node))
        self._link(node)
        self._adopt_waiting_children(node.task_id)
        return node

    def _adopt_waiting_children(self, parent_id: str) -> None:
        """Move the nodes that were waiting for this parent under it."""
        for child_id in self._awaiting_parent.pop(parent_id, {}):
            del self._awaited_by[child_id]
            child = self.nodes.get(child_id)
            if child is not None:
                self._place(child)

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
        member = self.nodes.get(member_id)
        group_node_id = self._member_group_node_id(member) if member else None
        if group_node_id is None:
            return None
        chain = self._chain_of[member_id]
        position = chain.index(node.task_id)
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

        If a member can't join its group because the group sits below it, the
        group's link is the one broken: the group becomes a root.
        """
        desired = self._desired_parent(node)
        if desired == node.parent_id:
            return
        parent_id = self._safe_parent(node, desired)
        if parent_id is None and desired is not None:
            group_node_id = self._member_group_node_id(node)
            if desired == group_node_id:
                self._move(self.nodes[desired], None)
                parent_id = self._safe_parent(node, desired)
        if parent_id != node.parent_id:
            self._move(node, parent_id)

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
        if node.task_id in parent.children:
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

    def _is_header(self, task_id: str, group_id: str) -> bool:
        """Whether a task is one of a chord's headers."""
        tally = self._groups.get(group_id)
        return tally is not None and task_id in tally.headers

    def _join_group(
        self, node: TaskNode, group_id: str, callback_id: str | None
    ) -> None:
        """Track a task as a member (a header, if it names a chord callback).

        Creates the GROUP node at 2 members and the CHORD node at the first
        header; refreshes the group's counts.
        """
        tally = self._groups.setdefault(group_id, _GroupTally())
        was_chord = bool(tally.headers)
        changed = self._add_membership(node, group_id, tally, callback_id)
        if changed:
            # Membership ends a chain link's claim and may start a chain.
            self._rechain_owner_of(node.task_id)
            self._retrigger(node.task_id)
            if was_chord and node.task_id not in tally.headers:
                self._resolve_callback(group_id)  # a body stamped with the ID

        group_node = self.nodes.get(_group_node_id(group_id))
        if group_node is None and (tally.headers or len(tally.members) >= 2):
            self._create_group_node(group_id, tally)
        elif group_node is not None:
            group_node.state = _aggregate_group_state(tally.state_counts.keys())
            if changed:
                self._rechain(node)
        if callback_id is not None:
            self._name_chord_callback(group_id, callback_id)

    def _add_membership(
        self,
        node: TaskNode,
        group_id: str,
        tally: _GroupTally,
        callback_id: str | None,
    ) -> bool:
        """Add a task to a group's members (and headers); True if that's new."""
        was_chord = bool(tally.headers)
        changed = False
        if node.task_id not in tally.members:
            tally.members[node.task_id] = None
            changed = True
            if not was_chord:
                self._count_member(tally, node)
        if callback_id is not None and node.task_id not in tally.headers:
            tally.headers[node.task_id] = None
            changed = True
            if was_chord:
                self._count_member(tally, node)
            else:
                self._become_chord_group(group_id, tally)
        return changed

    def _count_member(self, tally: _GroupTally, node: TaskNode) -> None:
        """Add a newly counted member's state and parent vote."""
        _bump(tally.state_counts, node.state, 1)
        tally.set_vote(node.task_id, self._member_vote(node))
        if node.reported_parent_id is None and not tally.unparented:
            tally.unparented.add(node.task_id)
            self._drop_group_chains(tally)
        elif node.reported_parent_id is None:
            tally.unparented.add(node.task_id)

    def _mark_parented(self, tally: _GroupTally, node: TaskNode) -> None:
        """A counted member reported its parent; maybe its group's chains can form."""
        tally.unparented.discard(node.task_id)
        tally.set_vote(node.task_id, self._member_vote(node))
        if not tally.unparented:
            for member_id in list(tally.counted()):
                member = self.nodes.get(member_id)
                if member is not None:
                    self._rechain(member)

    def _drop_group_chains(self, tally: _GroupTally) -> None:
        """Release every chain of a group (a member's parent isn't known)."""
        for member_id in list(tally.counted()):
            if member_id in self._chain_of:
                member = self.nodes.get(member_id)
                if member is not None:
                    self._rechain(member)

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
        tally.unparented.clear()
        for member_id in tally.counted():
            member = self.nodes.get(member_id)
            if member is not None:
                self._count_member(tally, member)

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

    def _create_group_node(self, group_id: str, tally: _GroupTally) -> None:
        """Create the synthetic GROUP/CHORD node and move its members into it."""
        is_chord = bool(tally.headers)
        group_node = TaskNode(
            task_id=_group_node_id(group_id),
            name="chord" if is_chord else "group",
            state=_aggregate_group_state(tally.state_counts.keys()),
            node_type=NodeType.CHORD if is_chord else NodeType.GROUP,
            group_id=group_id,
        )
        self.nodes[group_node.task_id] = group_node
        group_node.parent_id = self._safe_parent(
            group_node, self._desired_parent(group_node)
        )
        self._link(group_node)
        for member_id in list(tally.counted()):
            member = self.nodes.get(member_id)
            if member is not None:
                self._rechain(member)
                self._place(member)
        self._adopt_waiting_children(group_node.task_id)
        self._place(group_node)
        self._retrigger(group_node.task_id)

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
        # Its own chain can now extend upwards.
        self._rechain_owner_of(node.task_id)

    def _single_child(self, parent_id: str) -> str | None:
        """The only task that reported ``parent_id`` as parent, if just one did."""
        children = self._reported_children.get(parent_id)
        if children is None or len(children) != 1:
            return None
        return next(iter(children))

    def _on_children_changed(self, parent_id: str) -> None:
        """Re-check chains near a task whose reported children (or node) changed.

        Only going to or from exactly one child changes whether it is a link.
        """
        children = self._reported_children.get(parent_id)
        if not children or len(children) > 2:
            return
        for child_id in children:
            self._rechain_owner_of(child_id)

    def _rechain_owner_of(self, task_id: str) -> None:
        """Recompute the chain of the member a task is (or is a link of)."""
        member = self.nodes.get(self._link_owner.get(task_id, task_id))
        if member is not None:
            self._rechain(member)

    def _can_link(self, task_id: str, child_id: str) -> bool:
        """Whether a task is a chain link above ``child_id`` (see class docstring)."""
        node = self.nodes.get(task_id)
        if node is None or node.node_type != NodeType.TASK:
            return False
        if self._single_child(task_id) != child_id:
            return False
        tally = self._groups.get(node.group_id) if node.group_id else None
        if tally is not None and task_id in tally.members:
            return False
        if task_id in self._callback_of or task_id in self._unlock_of:
            return False
        return node.name != _CHORD_UNLOCK_TASK

    def _rechain(self, member: TaskNode) -> None:
        """Recompute a group member's chain links and re-place what moved."""
        group_node_id = self._member_group_node_id(member)
        tally = self._groups.get(member.group_id or "")
        chain = self._chain_links(member, tally) if group_node_id else []
        previous = self._chain_of.get(member.task_id, [])
        if chain != previous:
            self._set_chain(member.task_id, chain, previous)
        elif not self._refresh_vote(member, tally):
            return  # nothing moved
        # The group before and after the links: its new parent may be a link
        # that is only now leaving the group, or one that is joining it.
        if group_node_id is not None:
            self._place(self.nodes[group_node_id])
        if chain != previous:
            for node_id in [*reversed(chain), *previous, member.task_id]:
                self._place_if_present(node_id)
            if group_node_id is not None:
                self._place(self.nodes[group_node_id])

    def _chain_links(self, member: TaskNode, tally: _GroupTally | None) -> list[str]:
        """A member's chain links, bottom-up (see the class docstring).

        Until every member has reported its parent, a parent with one child
        so far may just be the group's own parent: no links yet.
        """
        if tally is None or tally.unparented:
            return []
        chain: list[str] = []
        seen = {member.task_id}
        current = member
        while (
            current.reported_parent_id is not None
            and current.reported_parent_id not in seen
            and self._can_link(current.reported_parent_id, current.task_id)
        ):
            current = self.nodes[current.reported_parent_id]
            seen.add(current.task_id)
            chain.append(current.task_id)
        return chain

    def _set_chain(self, member_id: str, chain: list[str], previous: list[str]) -> None:
        """Record a member's new chain links and its vote for the group parent."""
        for link_id in previous:
            if self._link_owner.get(link_id) == member_id:
                del self._link_owner[link_id]
        for link_id in chain:
            self._link_owner[link_id] = member_id
        if chain:
            self._chain_of[member_id] = chain
        else:
            self._chain_of.pop(member_id, None)
        member = self.nodes[member_id]
        self._refresh_vote(member, self._groups.get(member.group_id or ""))

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
            if self._link_owner.get(link_id) == member_id:
                del self._link_owner[link_id]
                link = self.nodes.get(link_id)
                if link is not None:
                    self._place(link)

    def _forget_task(self, node: TaskNode) -> set[str]:
        """Drop a removed task from its group, chains and the parent index.

        Returns:
            Group IDs of chords whose callback may have changed.
        """
        chords: set[str] = set()
        owner = self._link_owner.pop(node.task_id, None)
        self._drop_chain(node.task_id)
        parent_id = node.reported_parent_id
        if parent_id is not None:
            _drop_from(self._reported_children, parent_id, node.task_id)
            self._on_children_changed(parent_id)
        callback_group = self._chord_by_callback.get(node.task_id)
        if callback_group is not None:
            chords.add(callback_group)
        if node.group_id is not None and self._leave_group(node, node.group_id):
            chords.add(node.group_id)
        if owner is not None:
            # Recomputed once the node is gone (see remove_node).
            self._link_owner[node.task_id] = owner
        return chords

    def _leave_group(self, node: TaskNode, group_id: str) -> bool:
        """Drop a removed task from its group's members and counts.

        Returns:
            True if the task was a member.
        """
        tally = self._groups.get(group_id)
        if tally is None or node.task_id not in tally.members:
            return False
        if node.task_id in tally.counted():
            _bump(tally.state_counts, node.state, -1)
            tally.set_vote(node.task_id, None)
            if node.task_id in tally.unparented:
                self._mark_parented(tally, node)
        del tally.members[node.task_id]
        was_header = node.task_id in tally.headers
        tally.headers.pop(node.task_id, None)
        group_node = self.nodes.get(_group_node_id(group_id))
        if was_header and not tally.headers:
            # The last header is gone: the others count as plain members again.
            self._recount(tally)
            if group_node is not None:
                self._set_callback(group_node, group_id, None)
                group_node.node_type = NodeType.GROUP
                group_node.name = "group"
            for member_id in tally.members:
                member = self.nodes.get(member_id)
                if member is not None:
                    self._rechain(member)
                    self._place(member)
        if group_node is not None:
            group_node.state = _aggregate_group_state(tally.state_counts.keys())
            self._place(group_node)
        elif not tally.members:
            del self._groups[group_id]
        return True

    def _forget_group_node(self, node: TaskNode) -> None:
        """Drop a removed GROUP/CHORD's links (its membership stays)."""
        group_id = node.group_id or ""
        callback_id = node.chord_id
        if callback_id is not None and self._callback_of.get(callback_id) == group_id:
            del self._callback_of[callback_id]
        unlock_id = self._unlock_by_chord.pop(group_id, None)
        if unlock_id is not None and self._unlock_of.get(unlock_id) == group_id:
            del self._unlock_of[unlock_id]
        self._set_chord_deps(group_id, set())
        tally = self._groups.get(group_id)
        if tally is None:
            return
        for member_id in tally.counted():
            self._drop_chain(member_id)
        if not tally.members:
            del self._groups[group_id]

    # -- chords ------------------------------------------------------------

    def _name_chord_callback(self, group_id: str, callback_id: str) -> None:
        """Record the callback ID a chord's header named.

        A callback ID belongs to one chord: another chord naming it loses it.
        """
        chord_node = self.nodes[_group_node_id(group_id)]
        if chord_node.chord_callback_id == callback_id:
            return
        previous = chord_node.chord_callback_id
        if previous is not None and self._chord_by_callback.get(previous) == group_id:
            del self._chord_by_callback[previous]
        other_group = self._chord_by_callback.get(callback_id)
        self._chord_by_callback[callback_id] = group_id
        chord_node.chord_callback_id = callback_id
        if other_group is not None and other_group != group_id:
            other = self.nodes.get(_group_node_id(other_group))
            if other is not None:
                other.chord_callback_id = None
            self._resolve_callback(other_group)
        self._resolve_callback(group_id)

    def _retrigger(self, node_id: str) -> None:
        """Re-resolve the chords whose callback depends on this node."""
        for group_id in list(self._chord_deps.get(node_id, ())):
            self._resolve_callback(group_id)

    def _set_chord_deps(self, group_id: str, deps: set[str]) -> None:
        """Replace the nodes a chord's callback resolution depends on."""
        for node_id in self._deps_of_chord.pop(group_id, set()) - deps:
            _drop_from(self._chord_deps, node_id, group_id)
        for node_id in deps:
            _add_to(self._chord_deps, node_id, group_id)
        if deps:
            self._deps_of_chord[group_id] = deps

    def _resolve_callback(self, group_id: str) -> None:
        """Pick the node drawn as a chord's callback, from evidence only.

        Walks up from the callback the headers name, then from the tasks
        stamped with the chord's group ID that aren't headers (a chain body's
        last task), until a task whose parent is a header or Celery's
        ``chord_unlock`` task: that task (or the group it is a member of)
        starts the body. A named group whose members hang from a header is the
        body too. Nodes the walks read are recorded, so a missing one arriving
        or reporting its parent resolves again.
        """
        chord_node = self.nodes.get(_group_node_id(group_id))
        tally = self._groups.get(group_id)
        if chord_node is None or tally is None or not tally.headers:
            return
        deps: set[str] = set()
        named = chord_node.chord_callback_id
        callback: tuple[str, str | None] | None = None
        if named is not None:
            callback = self._walk_to_body(named, tally, deps)
            if callback is None:
                callback = self._named_group_body(named, group_id, deps)
        for tail_id in tally.members:
            if callback is not None:
                break
            if tail_id not in tally.headers:
                callback = self._walk_to_body(tail_id, tally, deps)
        self._set_chord_deps(group_id, deps)
        if callback is not None and self._is_ancestor(callback[0], chord_node.task_id):
            callback = None  # the chord already hangs below it
        self._set_callback(chord_node, group_id, callback)

    def _walk_to_body(
        self, start_id: str, tally: _GroupTally, deps: set[str]
    ) -> tuple[str, str | None] | None:
        """Walk up from a body task to the node that starts the chord's body.

        Returns:
            (body start node ID, chord_unlock task ID or None), or None if the
            walk stopped at a missing task or one without a reported parent.
        """
        current_id = start_id
        seen: set[str] = set()
        while current_id not in seen:
            seen.add(current_id)
            deps.add(current_id)
            node = self.nodes.get(current_id)
            if node is None or current_id in tally.headers:
                return None
            parent_id = node.reported_parent_id
            if parent_id is None:
                return None
            unlock = self.nodes.get(parent_id)
            fired_by_unlock = unlock is not None and unlock.name == _CHORD_UNLOCK_TASK
            if parent_id in tally.headers or fired_by_unlock:
                deps.add(parent_id)
                start = self._body_node(node, deps)
                if start is None:
                    return None
                return start, parent_id if fired_by_unlock else None
            current_id = parent_id
        return None

    def _body_node(self, node: TaskNode, deps: set[str]) -> str | None:
        """The node a body starting at this task is drawn as: it, or its group.

        A body that is itself a chord or group starts with one of its
        members; the group node stands for it (None while it doesn't exist).
        """
        if node.group_id is None:
            return node.task_id
        tally = self._groups.get(node.group_id)
        if tally is None or node.task_id not in tally.counted():
            return node.task_id
        group_node_id = _group_node_id(node.group_id)
        deps.add(group_node_id)
        return group_node_id if group_node_id in self.nodes else None

    def _named_group_body(
        self, named: str, group_id: str, deps: set[str]
    ) -> tuple[str, str | None] | None:
        """A named callback ID that is another group: the body is that group.

        Celery names the inner chord's group ID when a chord's body is a
        chord; it counts once its members' parent is one of our headers.
        """
        group_node_id = _group_node_id(named)
        deps.add(group_node_id)
        tally = self._groups.get(named)
        if named == group_id or group_node_id not in self.nodes or tally is None:
            return None
        parent_id = tally.parent()
        if parent_id is not None and self._is_header(parent_id, group_id):
            return group_node_id, None
        return None

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
                self._release_callback(chord_node, group_id, previous)
            if callback_id is not None:
                self._claim_callback(group_id, callback_id)
        if callback_id is None:
            return
        self._place(self.nodes[callback_id])
        if callback_id not in chord_node.children:
            chord_node.children.append(callback_id)

    def _set_unlock(self, group_id: str, unlock_id: str | None) -> None:
        """Record the chord_unlock task that fired a chord's body (None: none)."""
        previous = self._unlock_by_chord.get(group_id)
        if previous == unlock_id:
            return
        if previous is not None:
            del self._unlock_by_chord[group_id]
            if self._unlock_of.get(previous) == group_id:
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
        """Record a chord's callback; another chord drawing it lets it go."""
        other_group = self._callback_of.get(callback_id)
        self._callback_of[callback_id] = group_id
        self._rechain_owner_of(callback_id)  # a callback isn't a chain link
        if other_group is None or other_group == group_id:
            return
        other = self.nodes.get(_group_node_id(other_group))
        if other is not None and other.chord_id == callback_id:
            other.chord_id = None
            self._release_callback(other, other_group, callback_id)

    def _release_callback(
        self, chord_node: TaskNode, group_id: str, callback_id: str
    ) -> None:
        """Unlink a node that is no longer the chord's callback."""
        if self._callback_of.get(callback_id) == group_id:
            del self._callback_of[callback_id]
        callback_node = self.nodes.get(callback_id)
        if callback_node is not None and callback_node.parent_id == chord_node.task_id:
            self._place(callback_node)
        elif callback_id in chord_node.children:
            chord_node.children.remove(callback_id)

    def get_node(self, task_id: str) -> TaskNode | None:
        """Get node by ID, or None if not found."""
        return self.nodes.get(task_id)

    def get_group_members(self, group_id: str) -> list[str]:
        """Get task IDs that belong to a group (a chord's headers)."""
        tally = self._groups.get(group_id)
        return list(tally.counted()) if tally is not None else []
