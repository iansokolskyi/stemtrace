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
    # Only an annotation: pydantic exports it from 2.11 on, the floor is 2.0.
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


class _GroupTally:
    """One Celery group's members, with running counts over those that count.

    Every task that carried the ``group_id`` is in ``members``. Once any of
    them carried a ``chord_callback_id`` the group is a chord, and only those
    (``headers``) count as members: Celery also stamps a chord's group ID on
    its callback and on the last task of a chain body, which aren't headers.
    The counts (reported parents and states of the counted members) keep the
    group's parent and state O(1) per event.
    """

    __slots__ = ("headers", "members", "parent_votes", "state_counts")

    def __init__(self) -> None:
        """Start an empty group."""
        self.members: dict[str, None] = {}
        self.headers: dict[str, None] = {}
        self.parent_votes: Counter[str] = Counter()
        self.state_counts: Counter[TaskState] = Counter()

    def counted(self) -> dict[str, None]:
        """The members that make up the group (headers, for a chord)."""
        return self.headers if self.headers else self.members

    def count(self, node: TaskNode, sign: int) -> None:
        """Add (+1) or remove (-1) a counted member's state and parent."""
        _bump(self.state_counts, node.state, sign)
        if node.reported_parent_id is not None:
            _bump(self.parent_votes, node.reported_parent_id, sign)

    def recount(self, nodes: dict[str, TaskNode]) -> None:
        """Rebuild the counts, e.g. after the group turned out to be a chord."""
        self.parent_votes.clear()
        self.state_counts.clear()
        for member_id in self.counted():
            member = nodes.get(member_id)
            if member is not None:
                self.count(member, 1)

    def parent(self) -> str | None:
        """The parent most counted members reported (ties: the smallest ID).

        Members of a group normally agree. When they don't (e.g. a group of
        chains, where only each chain's last task carries the group ID), a
        fixed choice keeps the graph independent of event order.
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
      only under its group; the group sits under its members' reported
      parent (the most common one if they disagree), or is a root while that
      parent isn't in the graph.
    * A CHORD's callback is the node its body starts with: the task (or
      group) whose reported parent is one of the chord's headers, preferring
      the ID the headers name. A chain body's later tasks follow it. The
      CHORD lists the callback among its children and names it in
      ``chord_id``; the callback sits under the CHORD unless it reported a
      parent outside the chord.
    * A node is never linked under itself or one of its descendants; such a
      cycle is broken at a group's link rather than a member's.

    Nodes are keyed by their ``task_id``. Remove nodes with ``remove_node``
    so these links stay consistent.
    """

    model_config = ConfigDict(validate_assignment=True)

    nodes: dict[str, TaskNode] = Field(default_factory=dict)

    # Root IDs in the order they became roots: a dict used as an ordered set,
    # so adding and removing a root is O(1). Exposed as ``root_ids``.
    _roots: dict[str, None] = PrivateAttr(default_factory=dict)
    # Celery group ID -> its members and counts.
    _groups: dict[str, _GroupTally] = PrivateAttr(default_factory=dict)
    # Chord callback ID the headers name -> the chord's group ID.
    _chord_by_callback: dict[str, str] = PrivateAttr(default_factory=dict)
    # Node drawn as a chord's callback -> that chord's group ID.
    _callback_of: dict[str, str] = PrivateAttr(default_factory=dict)
    # Reported parent ID -> tasks that reported it, in arrival order.
    _reported_children: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    # Missing parent ID -> nodes (in arrival order) to move under it once its
    # node is created, and the reverse map to keep both in sync.
    _awaiting_parent: dict[str, dict[str, None]] = PrivateAttr(default_factory=dict)
    _awaited_by: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def _restore_roots(
        cls, data: object, handler: "ModelWrapValidatorHandler[TaskGraph]"
    ) -> "TaskGraph":
        """Take ``root_ids`` from the input (e.g. a ``model_dump``) as the roots."""
        graph = handler(data)
        if isinstance(data, dict) and data.get("root_ids") is not None:
            graph._roots = dict.fromkeys(data["root_ids"])
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
        if tally is not None and node.task_id in tally.counted():
            if old_state is not None and old_state != node.state:
                _bump(tally.state_counts, old_state, -1)
                _bump(tally.state_counts, node.state, 1)
            if new_parent and node.reported_parent_id is not None:
                _bump(tally.parent_votes, node.reported_parent_id, 1)
        if new_parent:
            self._note_reported_child(node)

        if event.group_id is not None and event.group_id == node.group_id:
            self._join_group(node, event.group_id, event.chord_callback_id)
        self._place(node)
        self._refresh_group_of(node)
        self._refresh_chords_near(node)
        return event

    def remove_node(self, task_id: str) -> None:
        """Remove a node and every link to it; unknown IDs are ignored.

        The node leaves its parent's children (and a CHORD's, as its
        callback), the roots and its group. Its children become roots, waiting
        for it like for any missing parent; a removed GROUP/CHORD releases its
        members to their own parents (or the roots) but keeps their
        membership, so a later member event can recreate it.

        Args:
            task_id: ID of the node to remove.
        """
        node = self.nodes.get(task_id)
        if node is None:
            return
        self._unlink(node)
        self._set_awaited(task_id, None)
        chords = set()
        callback_of = self._callback_of.pop(task_id, None)
        if callback_of is not None:
            chords.add(callback_of)
            chord_node = self.nodes.get(_group_node_id(callback_of))
            if chord_node is not None:
                chord_node.chord_id = None
                if task_id in chord_node.children:
                    chord_node.children.remove(task_id)
        if node.node_type == NodeType.TASK:
            chords.update(self._forget_task(node))
        else:
            self._forget_group_node(node)
        del self.nodes[task_id]
        for child_id in node.children:
            child = self.nodes.get(child_id)
            if child is not None and child.parent_id == task_id:
                self._place(child)
        for group_id in chords:
            self._resolve_callback(group_id)

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
        if node.node_type == NodeType.TASK:
            group_node_id = self._member_group_node_id(node)
            if group_node_id is not None:
                self._set_awaited(node.task_id, None)
                return group_node_id
            reported = node.reported_parent_id
        else:
            tally = self._groups.get(node.group_id or "")
            reported = tally.parent() if tally is not None else None

        callback_of = self._callback_of.get(node.task_id)
        if callback_of is not None and (
            reported is None
            or reported not in self.nodes
            or self._is_header(reported, callback_of)
        ):
            self._set_awaited(node.task_id, None)
            return _group_node_id(callback_of)
        if reported is not None and reported in self.nodes:
            self._set_awaited(node.task_id, None)
            return reported
        self._set_awaited(node.task_id, reported)
        return None

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
            group_node = self.nodes[desired]
            if desired == self._member_group_node_id(node):
                self._move(group_node, None)
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
            waiting = self._awaiting_parent[previous]
            del waiting[node_id]
            if not waiting:
                del self._awaiting_parent[previous]
            del self._awaited_by[node_id]
        if parent_id is not None:
            self._awaiting_parent.setdefault(parent_id, {})[node_id] = None
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
        if node.task_id not in tally.members:
            tally.members[node.task_id] = None
            if not was_chord:
                tally.count(node, 1)
        if callback_id is not None and node.task_id not in tally.headers:
            tally.headers[node.task_id] = None
            if was_chord:
                tally.count(node, 1)
            else:
                self._become_chord_group(group_id, tally)
            self._add_header_candidates(node.task_id, group_id)

        group_node_id = _group_node_id(group_id)
        group_node = self.nodes.get(group_node_id)
        if group_node is None and (tally.headers or len(tally.members) >= 2):
            self._create_group_node(group_id, tally)
        elif group_node is not None:
            group_node.state = _aggregate_group_state(tally.state_counts.keys())
        if callback_id is not None:
            self._name_chord_callback(group_id, callback_id)

    def _become_chord_group(self, group_id: str, tally: _GroupTally) -> None:
        """Count only headers from now on, releasing the other members."""
        tally.recount(self.nodes)
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
        for member_id in tally.counted():
            member = self.nodes.get(member_id)
            if member is not None:
                self._place(member)
        self._adopt_waiting_children(group_node.task_id)
        # The new node may be the body of a chord whose header it hangs from.
        parent_id = tally.parent()
        parent = self.nodes.get(parent_id) if parent_id is not None else None
        if parent is not None and parent.group_id is not None:
            self._resolve_callback(parent.group_id)

    def _refresh_group_of(self, node: TaskNode) -> None:
        """Re-place the synthetic node of a task's group, if there is one."""
        if node.group_id is None:
            return
        group_node = self.nodes.get(_group_node_id(node.group_id))
        if group_node is not None:
            self._place(group_node)

    def _forget_task(self, node: TaskNode) -> set[str]:
        """Drop a removed task from its group and the parent index.

        Returns:
            Group IDs of chords whose callback may have changed.
        """
        chords: set[str] = set()
        parent_id = node.reported_parent_id
        if parent_id is not None:
            siblings = self._reported_children.get(parent_id)
            if siblings is not None:
                siblings.pop(node.task_id, None)
                if not siblings:
                    del self._reported_children[parent_id]
            parent = self.nodes.get(parent_id)
            if parent is not None and parent.group_id is not None:
                chords.add(parent.group_id)
        callback_group = self._chord_by_callback.get(node.task_id)
        if callback_group is not None:
            chords.add(callback_group)
        if node.group_id is not None and self._leave_group(node, node.group_id):
            chords.add(node.group_id)
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
            tally.count(node, -1)
        del tally.members[node.task_id]
        was_header = node.task_id in tally.headers
        tally.headers.pop(node.task_id, None)
        group_node = self.nodes.get(_group_node_id(group_id))
        if was_header and not tally.headers:
            # The last header is gone: the others count as plain members again.
            tally.recount(self.nodes)
            if group_node is not None:
                self._set_callback(group_node, group_id, None)
                group_node.node_type = NodeType.GROUP
                group_node.name = "group"
            for member_id in tally.members:
                member = self.nodes.get(member_id)
                if member is not None:
                    self._place(member)
        if group_node is not None:
            group_node.state = _aggregate_group_state(tally.state_counts.keys())
            self._place(group_node)
        elif not tally.members:
            del self._groups[group_id]
        return True

    def _forget_group_node(self, node: TaskNode) -> None:
        """Drop a removed GROUP/CHORD's callback links (membership stays)."""
        callback_id = node.chord_id
        if (
            callback_id is not None
            and self._callback_of.get(callback_id) == node.group_id
        ):
            del self._callback_of[callback_id]
        tally = self._groups.get(node.group_id or "")
        if tally is not None and not tally.members:
            del self._groups[node.group_id or ""]

    # -- chords ------------------------------------------------------------

    def _name_chord_callback(self, group_id: str, callback_id: str) -> None:
        """Record the callback ID a chord's header named.

        A callback belongs to one chord: another chord naming it loses it.
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

    def _note_reported_child(self, node: TaskNode) -> None:
        """Index a task under the parent it reported."""
        parent_id = node.reported_parent_id
        if parent_id is None:
            return
        self._reported_children.setdefault(parent_id, {})[node.task_id] = None

    def _add_header_candidates(self, header_id: str, group_id: str) -> None:
        """A new header: tasks that reported it as parent may be the body."""
        if header_id in self._reported_children:
            self._resolve_callback(group_id)

    def _refresh_chords_near(self, node: TaskNode) -> None:
        """Re-resolve the chords whose callback this task's event may decide.

        That is the chord whose header it reported as parent, the chord that
        names it as callback, and the chord whose group ID it carries.
        """
        groups: set[str] = set()
        parent = self.nodes.get(node.reported_parent_id or "")
        if (
            parent is not None
            and parent.group_id is not None
            and self._is_header(parent.task_id, parent.group_id)
        ):
            groups.add(parent.group_id)
        named = self._chord_by_callback.get(node.task_id)
        if named is not None:
            groups.add(named)
        if node.group_id is not None:
            tally = self._groups.get(node.group_id)
            if tally is not None and tally.headers:
                groups.add(node.group_id)
        for group_id in groups:
            self._resolve_callback(group_id)

    def _callback_candidates(self, group_id: str, tally: _GroupTally) -> list[str]:
        """Nodes hanging from the chord's headers: the body starts there.

        A task that is a member of another group stands for that group's node.
        """
        candidates: dict[str, None] = {}
        for header_id in tally.headers:
            for child_id in self._reported_children.get(header_id, ()):
                child = self.nodes.get(child_id)
                if child is None or child_id in tally.headers:
                    continue
                candidates[self._member_group_node_id(child) or child_id] = None
        # A node the chord already hangs below can't also follow it.
        chord_node_id = _group_node_id(group_id)
        return [
            node_id
            for node_id in candidates
            if node_id != chord_node_id
            and not self._is_ancestor(node_id, chord_node_id)
        ]

    def _resolve_callback(self, group_id: str) -> None:
        """Pick the node drawn as a chord's callback.

        Candidates are the nodes hanging from the chord's headers (where
        Celery starts the body). In order: the candidate the headers name (or
        the group they name, for a chord body); the candidate a body's last
        task leads up to; the named task itself, wherever it hangs; any
        candidate.
        """
        chord_node = self.nodes.get(_group_node_id(group_id))
        tally = self._groups.get(group_id)
        if chord_node is None or tally is None or not tally.headers:
            return
        named = chord_node.chord_callback_id
        candidates = self._callback_candidates(group_id, tally)
        callback: str | None = None
        if named is not None and named in candidates:
            callback = named
        elif named is not None and _group_node_id(named) in candidates:
            callback = _group_node_id(named)
        else:
            callback = self._callback_from_tails(group_id, tally, candidates)
        if (
            callback is None
            and named is not None
            and self._can_follow(named, chord_node, tally)
        ):
            callback = named
        if callback is None and candidates:
            callback = min(candidates)
        self._set_callback(chord_node, group_id, callback)

    def _can_follow(
        self, node_id: str, chord_node: TaskNode, tally: _GroupTally
    ) -> bool:
        """Whether a node can be drawn after the chord (not a header or above it)."""
        return (
            node_id in self.nodes
            and node_id not in tally.headers
            and not self._is_ancestor(node_id, chord_node.task_id)
        )

    def _callback_from_tails(
        self, group_id: str, tally: _GroupTally, candidates: list[str]
    ) -> str | None:
        """The candidate a chord body's last task descends from, if any.

        Celery stamps the chord's group ID on a chain body's last task (and
        may name it as the callback); a header's own subtasks don't lead
        there.
        """
        if not candidates:
            return None
        tails = [m for m in tally.members if m not in tally.headers]
        named = self.nodes[_group_node_id(group_id)].chord_callback_id
        if named is not None:
            tails.append(named)
        wanted = set(candidates)
        for tail_id in tails:
            seen: set[str] = set()
            current = self.nodes.get(tail_id)
            while current is not None and current.task_id not in seen:
                seen.add(current.task_id)
                for node_id in (current.task_id, self._member_group_node_id(current)):
                    if node_id in wanted:
                        return node_id
                current = self.nodes.get(current.reported_parent_id or "")
        return None

    def _set_callback(
        self, chord_node: TaskNode, group_id: str, callback: str | None
    ) -> None:
        """Make ``callback`` the chord's callback, releasing the previous one."""
        previous = chord_node.chord_id
        if previous != callback:
            chord_node.chord_id = callback
            if previous is not None:
                self._release_callback(chord_node, group_id, previous)
            if callback is not None:
                self._claim_callback(group_id, callback)
        if callback is None:
            return
        # Its place can change even if it stays the callback (e.g. its
        # reported parent just turned out to be a header).
        self._place(self.nodes[callback])
        if callback not in chord_node.children:
            chord_node.children.append(callback)

    def _claim_callback(self, group_id: str, callback_id: str) -> None:
        """Record a chord's callback; another chord drawing it lets it go."""
        other_group = self._callback_of.get(callback_id)
        self._callback_of[callback_id] = group_id
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
