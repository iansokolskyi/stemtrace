"""Task graph models for representing task execution flows."""

import bisect
import hashlib
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

    # Insert index (not serialized; rebuilt on first use, e.g. after
    # deserialization or when ``events`` was changed directly).
    _index: "_EventIndex | None" = PrivateAttr(default=None)
    # First parent the task's events reported (not serialized). A group
    # member's ``parent_id`` is its group node; this keeps the real parent,
    # which places the group.
    _reported_parent_id: str | None = PrivateAttr(default=None)

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

    def _note_reported_parent(self, parent_id: str | None) -> None:
        """Remember the first parent the task's events reported.

        The sender's PENDING carries no ``parent_id``; later events do. A
        reported parent is never replaced, and a task can't be its own parent.
        """
        if (
            self._reported_parent_id is None
            and parent_id is not None
            and parent_id != self.task_id
        ):
            self._reported_parent_id = parent_id

    def _become_chord(self, callback_id: str) -> None:
        """Make this synthetic node a CHORD that fires ``callback_id``.

        A GROUP is converted; an existing CHORD just takes the callback ID.
        """
        if self.node_type == NodeType.GROUP:
            self.node_type = NodeType.CHORD
            self.name = "chord"
        self.chord_id = callback_id
        self.chord_callback_id = callback_id


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


class TaskGraph(BaseModel):
    """DAG of task executions, built incrementally from events.

    Linking rules: a node's ``parent_id`` is None (it is a root) or names a
    node in the graph that lists it among its ``children``, so every node is
    reachable from a root.

    * A task sits under the parent its events reported; the first reported
      parent is kept. While that parent isn't in the graph (not seen yet, or
      evicted) the task is a root, and it moves under the parent when the
      parent's node is created.
    * Synthetic GROUP/CHORD nodes are created when tasks share a
      ``group_id`` (a CHORD from its first header). A member sits only under
      its group, and the group sits under its members' common reported
      parent; while that parent isn't in the graph, the group is a root.
    * A chord's callback is listed among the CHORD's children (that draws the
      chord's edge to it) and sits under the chord unless it reported a
      parent of its own.
    * A node is never linked under itself or one of its descendants.

    Nodes are keyed by their ``task_id``. Remove nodes with ``remove_node``
    so these links stay consistent.
    """

    model_config = ConfigDict(validate_assignment=True)

    nodes: dict[str, TaskNode] = Field(default_factory=dict)

    # Root IDs in the order they became roots: a dict used as an ordered set,
    # so adding and removing a root is O(1). Exposed as ``root_ids``.
    _roots: dict[str, None] = PrivateAttr(default_factory=dict)

    # Track which tasks belong to each group_id (private, not serialized)
    _group_members: dict[str, list[str]] = PrivateAttr(default_factory=dict)
    # Chord callback task ID -> its CHORD node's ID, so linking a callback
    # doesn't scan every node on each event.
    _chord_by_callback: dict[str, str] = PrivateAttr(default_factory=dict)
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
        elif not node._record_event(event):
            return None
        else:
            node._fill_missing_ids(event)
            node._note_reported_parent(event.parent_id)

        if event.group_id is not None:
            self._track_group_member(node, event.group_id)
            # Only a chord's header tasks carry the callback's ID.
            if event.chord_callback_id is not None:
                self._upgrade_to_chord(event.group_id, event.chord_callback_id)
        self._link_chord_callback_if_needed(node)
        self._place(node)
        self._place_group_of(node)
        return event

    def remove_node(self, task_id: str) -> None:
        """Remove a node and every link to it; unknown IDs are ignored.

        The node leaves its parent's children, the roots, its group's members
        and, for a chord callback, the CHORD's children. Its children become
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
        chord_node = self._chord_of_callback(task_id)
        if chord_node is not None and task_id in chord_node.children:
            chord_node.children.remove(task_id)
        if node.node_type == NodeType.TASK:
            self._forget_member(node)
        elif (
            node.chord_callback_id is not None
            and self._chord_by_callback.get(node.chord_callback_id) == task_id
        ):
            del self._chord_by_callback[node.chord_callback_id]
        del self.nodes[task_id]
        for child_id in node.children:
            child = self.nodes.get(child_id)
            if child is not None and child.parent_id == task_id:
                self._place(child)
        if node.group_id is not None and not self._group_members.get(node.group_id):
            self._group_members.pop(node.group_id, None)

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
        reported = self._reported_parent(node)
        if reported is not None and reported in self.nodes:
            self._set_awaited(node.task_id, None)
        else:
            self._set_awaited(node.task_id, reported)
            reported = None
        if node.node_type != NodeType.TASK:
            return reported
        group_node_id = self._member_group_node_id(node)
        if group_node_id is not None:
            return group_node_id
        if reported is not None:
            return reported
        chord_node = self._chord_of_callback(node.task_id)
        return chord_node.task_id if chord_node is not None else None

    def _reported_parent(self, node: TaskNode) -> str | None:
        """A task's first reported parent, or its group members' common one."""
        if node.node_type == NodeType.TASK:
            return node._reported_parent_id
        return self._members_real_parent(node.group_id)

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

    def _safe_parent(self, node: TaskNode, parent_id: str | None) -> str | None:
        """``parent_id`` unless linking under it would create a cycle."""
        if parent_id is None or self._is_self_or_ancestor(node.task_id, parent_id):
            return None
        return parent_id

    def _is_self_or_ancestor(self, candidate_id: str, node_id: str) -> bool:
        """Whether ``candidate_id`` is ``node_id`` or one of its ancestors."""
        seen: set[str] = set()
        current: str | None = node_id
        while current is not None and current not in seen:
            if current == candidate_id:
                return True
            seen.add(current)
            current_node = self.nodes.get(current)
            current = current_node.parent_id if current_node is not None else None
        return False

    def _place(self, node: TaskNode) -> None:
        """Move a node to the parent the linking rules give it, if that changed."""
        parent_id = self._desired_parent(node)
        if parent_id != node.parent_id:
            parent_id = self._safe_parent(node, parent_id)
        if parent_id == node.parent_id:
            return
        self._unlink(node)
        node.parent_id = parent_id
        self._link(node)

    def _place_group_of(self, node: TaskNode) -> None:
        """Re-place the synthetic node of a task's group, if there is one."""
        if node.group_id is None:
            return
        group_node = self.nodes.get(_group_node_id(node.group_id))
        if group_node is not None:
            self._place(group_node)

    def _link(self, node: TaskNode) -> None:
        """List a node under its ``parent_id``, or as a root if it has none.

        Only call it for a node that isn't listed yet (new or just unlinked).
        """
        if node.parent_id is None:
            self._roots[node.task_id] = None
            return
        parent = self.nodes[node.parent_id]
        # Only a CHORD can already list the node: as its callback's edge.
        if parent.chord_callback_id == node.task_id and node.task_id in parent.children:
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
        if parent is None or node.task_id not in parent.children:
            return
        if parent.chord_callback_id == node.task_id:
            return
        parent.children.remove(node.task_id)

    def _member_group_node_id(self, node: TaskNode) -> str | None:
        """ID of the synthetic node a task sits in as a group member, if any."""
        if node.group_id is None:
            return None
        group_node_id = _group_node_id(node.group_id)
        if group_node_id not in self.nodes:
            return None
        if node.task_id not in self._group_members.get(node.group_id, ()):
            return None
        return group_node_id

    def _members_real_parent(self, group_id: str | None) -> str | None:
        """The one parent a group's members reported, in the graph or not.

        Members that reported no parent are ignored; if the others disagree,
        there is no common parent.
        """
        parents = {
            member._reported_parent_id
            for member_id in self._group_members.get(group_id or "", ())
            if (member := self.nodes.get(member_id)) is not None
            and member._reported_parent_id is not None
        }
        return parents.pop() if len(parents) == 1 else None

    def _track_group_member(self, node: TaskNode, group_id: str) -> None:
        """Track a task as a member of its group, creating the group node.

        A GROUP node is created once 2+ tasks share the ``group_id``. A chord's
        callback shares its header's ``group_id`` but isn't a member.
        """
        group_node = self.nodes.get(_group_node_id(group_id))
        if (
            group_node is not None
            and group_node.node_type == NodeType.CHORD
            and group_node.chord_callback_id == node.task_id
        ):
            self._attach_chord_callback(group_node, node)
            return

        members = self._group_members.setdefault(group_id, [])
        if node.task_id not in members:
            members.append(node.task_id)

        if group_node is None:
            if len(members) >= 2:
                self._create_group_node(group_id, NodeType.GROUP)
            return
        self._place(node)
        group_node.state = self._compute_group_state(members)

    def _create_group_node(self, group_id: str, node_type: NodeType) -> TaskNode:
        """Create a synthetic GROUP/CHORD node and move its members into it."""
        group_node_id = _group_node_id(group_id)
        members = self._group_members.get(group_id, [])
        group_node = TaskNode(
            task_id=group_node_id,
            name="chord" if node_type == NodeType.CHORD else "group",
            state=self._compute_group_state(members),
            node_type=node_type,
            group_id=group_id,
        )
        self.nodes[group_node_id] = group_node
        group_node.parent_id = self._safe_parent(
            group_node, self._desired_parent(group_node)
        )
        self._link(group_node)
        for member_id in members:
            member = self.nodes.get(member_id)
            if member is not None:
                self._place(member)
        return group_node

    def _compute_group_state(self, member_ids: list[str]) -> TaskState:
        """Compute aggregate state for a group based on member states.

        Priority: FAILURE > REJECTED > STARTED/RECEIVED > PENDING > RETRY >
        REVOKED > SUCCESS (see ``_GROUP_STATE_RULES``). The result doesn't
        depend on member order.
        """
        present = {self.nodes[mid].state for mid in member_ids if mid in self.nodes}
        if not present:
            return TaskState.PENDING
        for triggers, group_state in _GROUP_STATE_RULES:
            if present & triggers:
                return group_state
        return TaskState.SUCCESS

    def _upgrade_to_chord(self, group_id: str, callback_id: str) -> None:
        """Make the group a CHORD (creating it if needed) firing ``callback_id``.

        Called when a header task has chord info (chord_callback_id set).
        The callback is linked outside the container.
        """
        chord_node = self.nodes.get(_group_node_id(group_id))
        if chord_node is None:
            chord_node = self._create_group_node(group_id, NodeType.CHORD)
        if chord_node.chord_callback_id != callback_id:
            # A callback belongs to one chord: a chord naming a new callback,
            # or a callback claimed by another chord, is released first.
            self._release_callback(chord_node)
            other_chord = self._chord_of_callback(callback_id)
            if other_chord is not None:
                self._release_callback(other_chord)
        chord_node._become_chord(callback_id)
        self._chord_by_callback[callback_id] = chord_node.task_id

        callback_node = self.nodes.get(callback_id)
        if callback_node is None:
            return
        # A callback seen before the chord was tracked as a member; it isn't one.
        members = self._group_members.get(group_id)
        if members is not None and callback_id in members:
            members.remove(callback_id)
            chord_node.state = self._compute_group_state(members)
        self._attach_chord_callback(chord_node, callback_node)

    def _release_callback(self, chord_node: TaskNode) -> None:
        """Make a CHORD forget its callback, unlinking the callback from it."""
        callback_id = chord_node.chord_callback_id
        if callback_id is None:
            return
        if self._chord_by_callback.get(callback_id) == chord_node.task_id:
            del self._chord_by_callback[callback_id]
        chord_node.chord_id = None
        chord_node.chord_callback_id = None
        callback_node = self.nodes.get(callback_id)
        if callback_node is None or callback_node.parent_id != chord_node.task_id:
            if callback_id in chord_node.children:
                chord_node.children.remove(callback_id)
        else:
            self._place(callback_node)

    def _chord_of_callback(self, task_id: str) -> TaskNode | None:
        """The CHORD node that fires ``task_id`` as its callback, if any."""
        chord_node_id = self._chord_by_callback.get(task_id)
        return self.nodes.get(chord_node_id) if chord_node_id is not None else None

    def _link_chord_callback_if_needed(self, node: TaskNode) -> None:
        """Link a task to the CHORD node it is the callback of, if any.

        Called on every event, so it looks the chord up by callback ID.
        """
        chord_node = self._chord_of_callback(node.task_id)
        if chord_node is not None:
            self._attach_chord_callback(chord_node, node)

    def _attach_chord_callback(
        self, chord_node: TaskNode, callback_node: TaskNode
    ) -> None:
        """Hang a chord's callback off the CHORD node, outside its container.

        A callback that reported a parent stays under it. It is listed among
        the chord's children either way, which is what draws the chord's edge
        to it.
        """
        self._place(callback_node)
        if callback_node.task_id not in chord_node.children:
            chord_node.children.append(callback_node.task_id)

    def _forget_member(self, node: TaskNode) -> None:
        """Drop a task from its group's members and refresh the group node."""
        if node.group_id is None:
            return
        members = self._group_members.get(node.group_id)
        if members is None or node.task_id not in members:
            return
        members.remove(node.task_id)
        group_node = self.nodes.get(_group_node_id(node.group_id))
        if group_node is not None:
            group_node.state = self._compute_group_state(members)
            self._place(group_node)

    def get_node(self, task_id: str) -> TaskNode | None:
        """Get node by ID, or None if not found."""
        return self.nodes.get(task_id)

    def get_group_members(self, group_id: str) -> list[str]:
        """Get task IDs that belong to a group."""
        return self._group_members.get(group_id, [])
