"""Task graph models for representing task execution flows."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from celery_flow.core.events import TaskEvent, TaskState


class NodeType(str, Enum):
    """Type of node in the task graph."""

    TASK = "TASK"
    GROUP = "GROUP"
    CHORD = "CHORD"


class TaskNode(BaseModel):
    """Mutable node in the task graph. Tracks event history and child relationships."""

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

    def add_event(self, event: TaskEvent) -> None:
        """Add event, creating node if needed. Links child to parent if parent exists.

        Also tracks group membership and creates synthetic GROUP/CHORD nodes.
        """
        if event.task_id not in self.nodes:
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

        node = self.nodes[event.task_id]
        node.events.append(event)
        node.state = event.state

        # Update group_id if we didn't have it before
        if node.group_id is None and event.group_id is not None:
            node.group_id = event.group_id

        # Update chord_id if we didn't have it before
        if node.chord_id is None and event.chord_id is not None:
            node.chord_id = event.chord_id

        # Update parent_id if we didn't have it before (PENDING lacks parent_id)
        if node.parent_id is None and event.parent_id is not None:
            node.parent_id = event.parent_id
            if event.task_id in self.root_ids:
                self.root_ids.remove(event.task_id)
            if event.parent_id in self.nodes:
                parent = self.nodes[event.parent_id]
                if event.task_id not in parent.children:
                    parent.children.append(event.task_id)

        # Track group membership for synthetic node creation
        if event.group_id is not None:
            self._track_group_member(event.task_id, event.group_id)

        # Track chord - when header task has chord_callback_id, upgrade GROUP to CHORD
        if event.chord_callback_id is not None and event.group_id is not None:
            # This is a HEADER task with chord info - upgrade GROUP to CHORD
            self._upgrade_to_chord(event.group_id, event.chord_callback_id)

        # Check if this task is a callback for any existing CHORD
        self._link_chord_callback_if_needed(event.task_id)

    def _track_group_member(self, task_id: str, group_id: str) -> None:
        """Track task as member of a group and create synthetic node if needed.

        Only creates a synthetic GROUP node when grouped tasks are orphans.
        If tasks already share a common parent, that parent visualizes the group.
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

        # Only create GROUP node for orphan groups (no common parent)
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
        or None if they have different parents or are orphans.
        """
        parents: set[str | None] = set()
        for mid in member_ids:
            if mid in self.nodes:
                parents.add(self.nodes[mid].parent_id)

        # Return the common parent if there's exactly one non-None parent
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
        else:
            # GROUP is a root node
            if group_node_id not in self.root_ids:
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

    def _compute_group_state(self, member_ids: list[str]) -> TaskState:
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

    def _upgrade_to_chord(self, group_id: str, callback_id: str) -> None:
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
            if (
                node.node_type == NodeType.CHORD
                and node.chord_callback_id == task_id
            ):
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
