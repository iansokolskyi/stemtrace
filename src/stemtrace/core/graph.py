"""Task graph models for representing task execution flows."""

import bisect
import hashlib
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic_core import PydanticSerializationError

from stemtrace.core.events import TaskEvent, TaskState

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

# A group's state is the result of the first rule matching any member state.
# SUCCESS and REJECTED aren't ranked (see ``TaskGraph._compute_group_state``).
_GROUP_STATE_RULES: tuple[tuple[frozenset[TaskState], TaskState], ...] = (
    (frozenset({TaskState.FAILURE}), TaskState.FAILURE),
    (frozenset({TaskState.STARTED, TaskState.RECEIVED}), TaskState.STARTED),
    (frozenset({TaskState.PENDING}), TaskState.PENDING),
    (frozenset({TaskState.RETRY}), TaskState.RETRY),
    (frozenset({TaskState.REVOKED}), TaskState.REVOKED),
)


def _group_node_id(group_id: str) -> str:
    """ID of the synthetic GROUP/CHORD node for a Celery ``group_id``."""
    return f"{_GROUP_NODE_PREFIX}{group_id}"


def _parent_upgrade(current: str | None, reported: str | None) -> str | None:
    """Parent a node should switch to after an event, or None to keep its own.

    A missing parent is filled in: the sender's PENDING carries no
    ``parent_id``. A synthetic group parent gives way to a different real
    parent, because the group can be created before the member's STARTED
    arrives with its ``parent_id``. A real parent is never replaced.
    """
    if reported is None or reported == current:
        return None
    if current is None or current.startswith(_GROUP_NODE_PREFIX):
        return reported
    return None


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

    Parent-child linking only occurs when parent exists at child insertion time.
    Out-of-order events won't back-link.

    Synthetic nodes (GROUP, CHORD) are created when tasks share a group_id.

    Nodes are keyed by their ``task_id``. Node IDs in ``children``,
    ``parent_id``, ``root_ids`` and group membership may point at nodes that
    are gone (the server store evicts old nodes), so lookups through them
    check that the node exists.
    """

    model_config = ConfigDict(validate_assignment=True)

    nodes: dict[str, TaskNode] = Field(default_factory=dict)
    root_ids: list[str] = Field(default_factory=list)

    # Track which tasks belong to each group_id (private, not serialized)
    _group_members: dict[str, list[str]] = PrivateAttr(default_factory=dict)

    def add_event(self, event: TaskEvent) -> TaskEvent | None:
        """Add event, creating node if needed. Links child to parent if parent exists.

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

        node._fill_missing_ids(event)
        new_parent = _parent_upgrade(node.parent_id, event.parent_id)
        if new_parent is not None:
            self._move_to_real_parent(node, new_parent)

        if event.group_id is not None:
            self._track_group_member(event.task_id, event.group_id)
            # Only a chord's header tasks carry the callback's ID.
            if event.chord_callback_id is not None:
                self._upgrade_to_chord(event.group_id, event.chord_callback_id)
        self._link_chord_callback_if_needed(event.task_id)
        return event

    def _insert_node(self, event: TaskEvent) -> TaskNode:
        """Create the node for a task's first event and hang it in the graph.

        A task without a parent becomes a root; a task whose parent isn't in
        the graph yet stays unlinked.
        """
        node = TaskNode(
            task_id=event.task_id,
            name=event.name,
            state=event.state,
            parent_id=event.parent_id,
            group_id=event.group_id,
            chord_id=event.chord_id,
        )
        self.nodes[event.task_id] = node
        if event.parent_id is None:
            self.root_ids.append(event.task_id)
        elif event.parent_id in self.nodes:
            self.nodes[event.parent_id].children.append(event.task_id)
        node._record_event(event)
        return node

    def _move_to_real_parent(self, node: TaskNode, parent_id: str) -> None:
        """Re-parent a node to the parent a later event reported.

        The node stops being a root, leaves the children of a synthetic group
        it was parked under, and joins the new parent's children if that
        parent is known. Its group may then follow it (see
        ``_move_group_to_members_parent``).
        """
        old_parent = node.parent_id
        self._set_parent(node, parent_id)
        if old_parent is not None and old_parent.startswith(_GROUP_NODE_PREFIX):
            self._remove_child(old_parent, node.task_id)
        self._add_child(parent_id, node.task_id)
        if node.group_id is not None:
            self._move_group_to_members_parent(node.group_id)

    def _move_group_to_members_parent(self, group_id: str) -> None:
        """Move a synthetic group under its members' real parent.

        Only members that already left the group for a real parent count. If
        they all name the same one, the group moves there and is listed under
        it in place of its members.
        """
        group_node_id = _group_node_id(group_id)
        group_node = self.nodes.get(group_node_id)
        if group_node is None:
            return
        members = self._group_members.get(group_id, [])
        parent_id = self._members_real_parent(members, group_node_id)
        if parent_id is None or group_node.parent_id == parent_id:
            return
        self._set_parent(group_node, parent_id)
        parent_node = self.nodes.get(parent_id)
        if parent_node is not None:
            self._nest_group(parent_node, group_node_id, members)

    def _members_real_parent(
        self, member_ids: list[str], group_node_id: str
    ) -> str | None:
        """The one real parent shared by members that have one, else None.

        Members without a parent or still parented to the group are ignored.
        """
        parents = {
            member.parent_id
            for member_id in member_ids
            if (member := self.nodes.get(member_id)) is not None
            and member.parent_id is not None
            and member.parent_id != group_node_id
        }
        return parents.pop() if len(parents) == 1 else None

    def _track_group_member(self, task_id: str, group_id: str) -> None:
        """Track task as member of a group and create synthetic node if needed.

        Creates a synthetic GROUP node when 2+ tasks share a group_id.
        If tasks share a common parent, the GROUP becomes a child of that parent.
        Skips adding callback tasks (they're outside the group container).
        """
        group_node_id = _group_node_id(group_id)
        group_node = self.nodes.get(group_node_id)
        if (
            group_node is not None
            and group_node.node_type == NodeType.CHORD
            and group_node.chord_id == task_id
        ):
            # The callback shares the header's group_id but isn't a member.
            self._attach_chord_callback(group_node, self.nodes[task_id])
            return

        members = self._group_members.setdefault(group_id, [])
        if task_id not in members:
            members.append(task_id)

        if group_node is None:
            if not self._should_create_group_node(members):
                return
            self._create_group_node(group_id)
            group_node = self.nodes[group_node_id]

        if task_id not in group_node.children:
            group_node.children.append(task_id)
        group_node.state = self._compute_group_state(members)
        self._adopt_if_orphan(self.nodes[task_id], group_node_id)

    def _should_create_group_node(self, member_ids: list[str]) -> bool:
        """Determine if a synthetic GROUP node should be created.

        Always returns True when there are 2+ members - we want to visualize
        groups even when they have a common parent, to differentiate from
        independent subtasks.
        """
        return len(member_ids) >= 2

    def _get_common_parent(self, member_ids: list[str]) -> str | None:
        """Get common parent_id if all members share the same parent.

        Returns the parent_id if all members have the same parent,
        or None if they have different parents or are standalone.
        """
        parents: set[str | None] = set()
        for mid in member_ids:
            if mid in self.nodes:
                parents.add(self.nodes[mid].parent_id)

        # Single shared non-None parent => common parent.
        if len(parents) == 1 and None not in parents:
            return parents.pop()
        return None

    def _create_group_node(self, group_id: str) -> None:
        """Create a synthetic GROUP node for tasks sharing a group_id.

        If all members share a common parent task, the GROUP becomes a child
        of that parent. Otherwise, the GROUP is a root node.
        """
        group_node_id = _group_node_id(group_id)
        members = self._group_members.get(group_id, [])
        common_parent = self._get_common_parent(members)

        self.nodes[group_node_id] = TaskNode(
            task_id=group_node_id,
            name="group",
            state=self._compute_group_state(members),
            node_type=NodeType.GROUP,
            group_id=group_id,
            children=list(members),
            parent_id=common_parent,
        )

        if common_parent is None:
            self._add_root(group_node_id)
        else:
            parent_node = self.nodes.get(common_parent)
            if parent_node is not None:
                self._nest_group(parent_node, group_node_id, members)
        self._move_members_into(group_node_id, members)

    def _compute_group_state(self, member_ids: list[str]) -> TaskState:
        """Compute aggregate state for a group based on member states.

        Priority: FAILURE > STARTED/RECEIVED > PENDING > RETRY > REVOKED > SUCCESS
        (see ``_GROUP_STATE_RULES``). REJECTED is not ranked.
        """
        states = [self.nodes[mid].state for mid in member_ids if mid in self.nodes]
        if not states:
            return TaskState.PENDING

        present = set(states)
        for triggers, group_state in _GROUP_STATE_RULES:
            if present & triggers:
                return group_state
        # Only SUCCESS and REJECTED are left. All SUCCESS yields SUCCESS; REJECTED
        # isn't ranked, so a mix takes the first member's state.
        return states[0]

    def _upgrade_to_chord(self, group_id: str, callback_id: str) -> None:
        """Upgrade a GROUP node to CHORD and register the callback.

        Called when a header task has chord info (chord_callback_id set).
        The callback will be linked outside the container.
        """
        chord_node = self.nodes.get(_group_node_id(group_id))
        if chord_node is None:
            chord_node = self._create_chord_node(group_id, callback_id)
        else:
            chord_node._become_chord(callback_id)

        callback_node = self.nodes.get(callback_id)
        if callback_node is None:
            return
        self._attach_chord_callback(chord_node, callback_node)
        # A callback seen before the chord was tracked as a member; it isn't one.
        members = self._group_members.get(group_id)
        if members is not None and callback_id in members:
            members.remove(callback_id)

    def _create_chord_node(self, group_id: str, callback_id: str) -> TaskNode:
        """Create a CHORD node directly, before a GROUP node exists.

        Unlike a GROUP, it always starts as a root, even when its members
        share a parent.
        """
        chord_node_id = _group_node_id(group_id)
        members = self._group_members.get(group_id, [])
        chord_node = TaskNode(
            task_id=chord_node_id,
            name="chord",
            state=TaskState.PENDING,
            node_type=NodeType.CHORD,
            group_id=group_id,
            chord_id=callback_id,
            chord_callback_id=callback_id,
            children=list(members),
        )
        self.nodes[chord_node_id] = chord_node
        self._add_root(chord_node_id)
        self._move_members_into(chord_node_id, members)
        return chord_node

    def _link_chord_callback_if_needed(self, task_id: str) -> None:
        """Link a task to a CHORD node if it's the callback for that chord.

        Called when a new task arrives. Checks if any existing CHORD node
        is waiting for this task as its callback.
        """
        callback_node = self.nodes.get(task_id)
        if callback_node is None:
            return
        for node in self.nodes.values():
            if node.node_type == NodeType.CHORD and node.chord_callback_id == task_id:
                self._attach_chord_callback(node, callback_node)
                return  # Only one CHORD can have this callback

    def _attach_chord_callback(
        self, chord_node: TaskNode, callback_node: TaskNode
    ) -> None:
        """Hang a chord's callback off the CHORD node, outside its container.

        A callback that already has a parent keeps it. It is listed among the
        chord's children either way, which is what draws the chord's edge to it.
        """
        self._adopt_if_orphan(callback_node, chord_node.task_id)
        if callback_node.task_id not in chord_node.children:
            chord_node.children.append(callback_node.task_id)

    def _move_members_into(self, group_node_id: str, member_ids: list[str]) -> None:
        """Re-parent every known member to a newly created group node."""
        for member_id in member_ids:
            member = self.nodes.get(member_id)
            if member is not None:
                self._set_parent(member, group_node_id)

    @staticmethod
    def _nest_group(
        parent_node: TaskNode, group_node_id: str, member_ids: list[str]
    ) -> None:
        """List a group among its parent's children in place of its members."""
        if group_node_id not in parent_node.children:
            parent_node.children.append(group_node_id)
        for member_id in member_ids:
            if member_id in parent_node.children:
                parent_node.children.remove(member_id)

    def _adopt_if_orphan(self, node: TaskNode, parent_id: str) -> None:
        """Give a node a parent unless it already has one."""
        if node.parent_id is None:
            self._set_parent(node, parent_id)

    def _set_parent(self, node: TaskNode, parent_id: str) -> None:
        """Set a node's parent; a node with a parent is never a root."""
        node.parent_id = parent_id
        if node.task_id in self.root_ids:
            self.root_ids.remove(node.task_id)

    def _add_root(self, node_id: str) -> None:
        """Add a node to the roots unless it is already there."""
        if node_id not in self.root_ids:
            self.root_ids.append(node_id)

    def _add_child(self, parent_id: str, child_id: str) -> None:
        """List a child under its parent, if the parent is known, at most once."""
        parent = self.nodes.get(parent_id)
        if parent is not None and child_id not in parent.children:
            parent.children.append(child_id)

    def _remove_child(self, parent_id: str, child_id: str) -> None:
        """Drop a child from its parent's children, if both are there."""
        parent = self.nodes.get(parent_id)
        if parent is not None and child_id in parent.children:
            parent.children.remove(child_id)

    def get_node(self, task_id: str) -> TaskNode | None:
        """Get node by ID, or None if not found."""
        return self.nodes.get(task_id)

    def get_group_members(self, group_id: str) -> list[str]:
        """Get task IDs that belong to a group."""
        return self._group_members.get(group_id, [])
