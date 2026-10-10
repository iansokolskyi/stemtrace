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
    """

    model_config = ConfigDict(validate_assignment=True)

    nodes: dict[str, TaskNode] = Field(default_factory=dict)
    root_ids: list[str] = Field(default_factory=list)

    # Track which tasks belong to each group_id (private, not serialized)
    _group_members: dict[str, list[str]] = PrivateAttr(default_factory=dict)

    def add_event(self, event: TaskEvent) -> TaskEvent | None:  # noqa: C901, PLR0912, PLR0915
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
        existing = self.nodes.get(event.task_id)
        if existing is not None and not existing._record_event(event):
            return None

        if existing is None:
            self.nodes[event.task_id] = TaskNode(
                task_id=event.task_id,
                name=event.name,
                state=event.state,
                parent_id=event.parent_id,
                group_id=event.group_id,
                chord_id=event.chord_id,
            )
            if event.parent_id is None:
                self.root_ids.append(event.task_id)
            elif event.parent_id in self.nodes:
                self.nodes[event.parent_id].children.append(event.task_id)
            self.nodes[event.task_id]._record_event(event)

        node = self.nodes[event.task_id]

        # Update group_id if we didn't have it before
        if node.group_id is None and event.group_id is not None:
            node.group_id = event.group_id

        # Update chord_id if we didn't have it before
        if node.chord_id is None and event.chord_id is not None:
            node.chord_id = event.chord_id

        # Update parent_id if we didn't have it before (PENDING lacks parent_id)
        # Also update if current parent is a group node but event has a different parent
        # (this happens when group is created before STARTED event arrives with real parent_id)
        should_update_parent = (
            node.parent_id is None and event.parent_id is not None
        ) or (
            node.parent_id is not None
            and event.parent_id is not None
            and node.parent_id.startswith("group:")
            and node.parent_id != event.parent_id
        )

        if should_update_parent:
            old_parent = node.parent_id
            node.parent_id = event.parent_id
            if event.task_id in self.root_ids:
                self.root_ids.remove(event.task_id)

            # Remove from old parent's children if it was a group node
            if (
                old_parent
                and old_parent.startswith("group:")
                and old_parent in self.nodes
            ):
                old_group = self.nodes[old_parent]
                if event.task_id in old_group.children:
                    old_group.children.remove(event.task_id)

            # Add to new parent's children
            if event.parent_id in self.nodes:
                parent = self.nodes[event.parent_id]
                if event.task_id not in parent.children:
                    parent.children.append(event.task_id)

            # Check if this task is a group member and if group needs parent update
            if node.group_id is not None:
                group_node_id = f"group:{node.group_id}"
                if group_node_id in self.nodes:
                    group_node = self.nodes[group_node_id]
                    members = self._group_members.get(node.group_id, [])
                    # Collect real parents of all members (skip group_node_id and None)
                    # After updating this member, check if all members now share a common real parent
                    member_real_parents: set[str | None] = set()
                    for member_id in members:
                        if member_id in self.nodes:
                            member = self.nodes[member_id]
                            # If member's parent is the group node, it doesn't have a real parent yet
                            # Otherwise, use its current parent_id (which is the real parent)
                            if (
                                member.parent_id != group_node_id
                                and member.parent_id is not None
                            ):
                                member_real_parents.add(member.parent_id)

                    # If all members share the same real parent, update group
                    if (
                        len(member_real_parents) == 1
                        and None not in member_real_parents
                    ):
                        common_parent = member_real_parents.pop()
                        if (
                            common_parent != group_node_id  # noqa: PLR1714
                            and group_node.parent_id != common_parent
                        ):
                            # Update group node's parent
                            group_node.parent_id = common_parent
                            # Remove group from root_ids if it was there
                            if group_node_id in self.root_ids:
                                self.root_ids.remove(group_node_id)
                            # Add group to parent's children
                            if common_parent in self.nodes:
                                parent_node = self.nodes[common_parent]
                                if group_node_id not in parent_node.children:
                                    parent_node.children.append(group_node_id)
                                # Remove member tasks from parent's direct children (they're in group)
                                for member_id in members:
                                    if member_id in parent_node.children:
                                        parent_node.children.remove(member_id)

        # Track group membership for synthetic node creation
        if event.group_id is not None:
            self._track_group_member(event.task_id, event.group_id)

        # Track chord - when header task has chord_callback_id, upgrade GROUP to CHORD
        if event.chord_callback_id is not None and event.group_id is not None:
            # This is a HEADER task with chord info - upgrade GROUP to CHORD
            self._upgrade_to_chord(event.group_id, event.chord_callback_id)

        # Check if this task is a callback for any existing CHORD
        self._link_chord_callback_if_needed(event.task_id)
        return event

    def _track_group_member(self, task_id: str, group_id: str) -> None:  # noqa: C901
        """Track task as member of a group and create synthetic node if needed.

        Creates a synthetic GROUP node when 2+ tasks share a group_id.
        If tasks share a common parent, the GROUP becomes a child of that parent.
        Skips adding callback tasks (they're outside the group container).
        """
        # Check if this task is the callback for a CHORD - link but don't add to members
        group_node_id = f"group:{group_id}"
        if group_node_id in self.nodes:
            chord_node = self.nodes[group_node_id]
            if (
                chord_node.node_type == NodeType.CHORD
                and chord_node.chord_id == task_id
            ):
                # This is the callback - link it to CHORD but not as group member
                task_node = self.nodes[task_id]
                if task_node.parent_id is None:
                    task_node.parent_id = group_node_id
                    if task_id in self.root_ids:
                        self.root_ids.remove(task_id)
                if task_id not in chord_node.children:
                    chord_node.children.append(task_id)
                return  # Don't add to _group_members

        if group_id not in self._group_members:
            self._group_members[group_id] = []

        if task_id not in self._group_members[group_id]:
            self._group_members[group_id].append(task_id)

        # Only create GROUP node for standalone groups (no parent task)
        group_node_id = f"group:{group_id}"
        members = self._group_members[group_id]

        should_create = (
            len(members) >= 2
            and group_node_id not in self.nodes
            and self._should_create_group_node(members)
        )
        if should_create:
            self._create_group_node(group_id)

        # Update group node if it exists
        if group_node_id in self.nodes:
            group_node = self.nodes[group_node_id]
            if task_id not in group_node.children:
                group_node.children.append(task_id)

            group_node.state = self._compute_group_state(members)

            # Update task's parent to point to group node (if no other parent)
            task_node = self.nodes[task_id]
            if task_node.parent_id is None:
                task_node.parent_id = group_node_id
                if task_id in self.root_ids:
                    self.root_ids.remove(task_id)

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
        group_node_id = f"group:{group_id}"
        members = self._group_members.get(group_id, [])

        # Check if members share a common parent
        common_parent = self._get_common_parent(members)

        # Determine the group's state based on member states
        group_state = self._compute_group_state(members)

        self.nodes[group_node_id] = TaskNode(
            task_id=group_node_id,
            name="group",
            state=group_state,
            node_type=NodeType.GROUP,
            group_id=group_id,
            children=list(members),
            parent_id=common_parent,
        )

        if common_parent is not None:
            # GROUP is child of common parent
            parent_node = self.nodes.get(common_parent)
            if parent_node and group_node_id not in parent_node.children:
                parent_node.children.append(group_node_id)
            # Remove member tasks from parent's children (they're now in GROUP)
            if parent_node:
                for member_id in members:
                    if member_id in parent_node.children:
                        parent_node.children.remove(member_id)
        elif group_node_id not in self.root_ids:
            # GROUP is a root node
            self.root_ids.append(group_node_id)

        # Update member nodes to point to group as parent
        for member_id in members:
            if member_id in self.nodes:
                member = self.nodes[member_id]
                # Store original parent before updating (for later members)
                member.parent_id = group_node_id
                # Remove from root_ids since it now has a parent
                if member_id in self.root_ids:
                    self.root_ids.remove(member_id)

    def _compute_group_state(self, member_ids: list[str]) -> TaskState:  # noqa: PLR0911
        """Compute aggregate state for a group based on member states.

        Priority: FAILURE > STARTED/RECEIVED > PENDING > RETRY > REVOKED > SUCCESS
        """
        states = [self.nodes[mid].state for mid in member_ids if mid in self.nodes]
        if not states:
            return TaskState.PENDING

        state_set = set(states)

        if TaskState.FAILURE in state_set:
            return TaskState.FAILURE
        if state_set & {TaskState.STARTED, TaskState.RECEIVED}:
            return TaskState.STARTED
        if TaskState.PENDING in state_set:
            return TaskState.PENDING
        if TaskState.RETRY in state_set:
            return TaskState.RETRY
        if TaskState.REVOKED in state_set:
            return TaskState.REVOKED
        if all(s == TaskState.SUCCESS for s in states):
            return TaskState.SUCCESS

        return states[0]

    def _upgrade_to_chord(self, group_id: str, callback_id: str) -> None:  # noqa: C901
        """Upgrade a GROUP node to CHORD and register the callback.

        Called when a header task has chord info (chord_callback_id set).
        The callback will be linked outside the container.
        """
        group_node_id = f"group:{group_id}"
        members = self._group_members.get(group_id, [])

        # If no GROUP node exists yet, create it as CHORD
        if group_node_id not in self.nodes:
            self.nodes[group_node_id] = TaskNode(
                task_id=group_node_id,
                name="chord",
                state=TaskState.PENDING,
                node_type=NodeType.CHORD,
                group_id=group_id,
                chord_id=callback_id,  # Store callback reference
                chord_callback_id=callback_id,
                children=list(members),  # Add existing members as children
            )
            if group_node_id not in self.root_ids:
                self.root_ids.append(group_node_id)
            # Update member nodes to point to CHORD as parent
            for member_id in members:
                if member_id in self.nodes:
                    member = self.nodes[member_id]
                    member.parent_id = group_node_id
                    if member_id in self.root_ids:
                        self.root_ids.remove(member_id)
        else:
            # Upgrade existing GROUP to CHORD
            group_node = self.nodes[group_node_id]
            if group_node.node_type == NodeType.GROUP:
                group_node.node_type = NodeType.CHORD
                group_node.name = "chord"
            group_node.chord_id = callback_id  # Store callback reference
            group_node.chord_callback_id = callback_id

        # Link callback to CHORD if it already exists
        group_node = self.nodes[group_node_id]
        callback_node = self.nodes.get(callback_id)
        if callback_node:
            # Callback should be OUTSIDE container, linked as child for edge
            if callback_node.parent_id is None:
                callback_node.parent_id = group_node_id
                if callback_id in self.root_ids:
                    self.root_ids.remove(callback_id)
            # Ensure callback is in children (for edge rendering)
            if callback_id not in group_node.children:
                group_node.children.append(callback_id)
            # Remove callback from group members (it's not a header task)
            if (
                group_id in self._group_members
                and callback_id in self._group_members[group_id]
            ):
                self._group_members[group_id].remove(callback_id)

    def _link_chord_callback_if_needed(self, task_id: str) -> None:
        """Link a task to a CHORD node if it's the callback for that chord.

        Called when a new task arrives. Checks if any existing CHORD node
        is waiting for this task as its callback.
        """
        for node in self.nodes.values():
            if node.node_type == NodeType.CHORD and node.chord_callback_id == task_id:
                callback_node = self.nodes.get(task_id)
                if callback_node:
                    # Link callback to CHORD
                    if callback_node.parent_id is None:
                        callback_node.parent_id = node.task_id
                        if task_id in self.root_ids:
                            self.root_ids.remove(task_id)
                    # Add to children for edge rendering
                    if task_id not in node.children:
                        node.children.append(task_id)
                return  # Only one CHORD can have this callback

    def get_node(self, task_id: str) -> TaskNode | None:
        """Get node by ID, or None if not found."""
        return self.nodes.get(task_id)

    def get_group_members(self, group_id: str) -> list[str]:
        """Get task IDs that belong to a group."""
        return self._group_members.get(group_id, [])
