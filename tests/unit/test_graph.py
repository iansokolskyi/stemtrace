"""Tests for task graph models."""

from collections.abc import ItemsView, Iterator, ValuesView
from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest
from pydantic import ValidationError

import stemtrace.core.graph as graph_module
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph, TaskNode
from tests.graph_invariants import assert_graph_invariants


class TestNodeType:
    def test_enum_values(self) -> None:
        assert NodeType.TASK == "TASK"
        assert NodeType.GROUP == "GROUP"
        assert NodeType.CHORD == "CHORD"

    def test_string_inheritance(self) -> None:
        assert isinstance(NodeType.TASK, str)
        assert NodeType.GROUP.lower() == "group"


class TestTaskNode:
    def test_creation_with_required_fields(self) -> None:
        node = TaskNode(
            task_id="task-1",
            name="myapp.tasks.process",
            state=TaskState.STARTED,
        )
        assert node.task_id == "task-1"
        assert node.name == "myapp.tasks.process"
        assert node.state == TaskState.STARTED
        assert node.events == []
        assert node.children == []
        assert node.parent_id is None
        assert node.node_type == NodeType.TASK
        assert node.group_id is None

    def test_mutable_state(self) -> None:
        node = TaskNode(
            task_id="task-1",
            name="test",
            state=TaskState.STARTED,
        )
        node.state = TaskState.SUCCESS
        assert node.state == TaskState.SUCCESS

    def test_mutable_children(self) -> None:
        node = TaskNode(
            task_id="task-1",
            name="test",
            state=TaskState.STARTED,
        )
        node.children.append("child-1")
        node.children.append("child-2")
        assert node.children == ["child-1", "child-2"]

    def test_mutable_events(self) -> None:
        node = TaskNode(
            task_id="task-1",
            name="test",
            state=TaskState.STARTED,
        )
        event = TaskEvent(
            task_id="task-1",
            name="test",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )
        node.events.append(event)
        assert len(node.events) == 1

    def test_validates_state(self) -> None:
        with pytest.raises(ValidationError):
            TaskNode(
                task_id="task-1",
                name="test",
                state="INVALID",  # type: ignore[arg-type]
            )

    def test_creation_with_node_type_and_group_id(self) -> None:
        node = TaskNode(
            task_id="task-1",
            name="myapp.tasks.process",
            state=TaskState.STARTED,
            node_type=NodeType.GROUP,
            group_id="group-abc",
        )
        assert node.node_type == NodeType.GROUP
        assert node.group_id == "group-abc"

    def test_synthetic_group_node(self) -> None:
        node = TaskNode(
            task_id="group:abc-123",
            name="group",
            state=TaskState.STARTED,
            node_type=NodeType.GROUP,
            group_id="abc-123",
            children=["task-1", "task-2"],
        )
        assert node.node_type == NodeType.GROUP
        assert len(node.children) == 2


class TestTaskGraphBasics:
    def test_empty_graph(self) -> None:
        graph = TaskGraph()
        assert graph.nodes == {}
        assert graph.root_ids == []

    def test_add_event_creates_node(self) -> None:
        graph = TaskGraph()
        event = TaskEvent(
            task_id="task-1",
            name="myapp.tasks.process",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )
        graph.add_event(event)
        assert "task-1" in graph.nodes
        assert "task-1" in graph.root_ids
        assert graph.nodes["task-1"].name == "myapp.tasks.process"

    def test_add_event_appends_to_existing_node(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
            )
        )
        assert len(graph.nodes) == 1
        assert len(graph.nodes["task-1"].events) == 2
        assert graph.nodes["task-1"].state == TaskState.SUCCESS

    def test_get_node_returns_none_for_missing(self) -> None:
        graph = TaskGraph()
        assert graph.get_node("nonexistent") is None

    def test_get_node_returns_node(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )
        node = graph.get_node("task-1")
        assert node is not None
        assert node.task_id == "task-1"


class TestTaskGraphParentChild:
    def test_parent_then_child(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="parent-1",
                name="myapp.tasks.main",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                parent_id="parent-1",
            )
        )
        assert "parent-1" in graph.root_ids
        assert "child-1" not in graph.root_ids
        assert "child-1" in graph.nodes["parent-1"].children

    def test_child_before_parent_is_root_until_parent_arrives(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                parent_id="parent-1",
            )
        )
        assert graph.root_ids == ["child-1"]
        assert graph.nodes["child-1"].parent_id is None
        assert "parent-1" not in graph.nodes

    def test_child_before_parent_is_linked_when_parent_arrives(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                parent_id="parent-1",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="parent-1",
                name="myapp.tasks.main",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )
        assert graph.nodes["child-1"].parent_id == "parent-1"
        assert graph.nodes["parent-1"].children == ["child-1"]
        assert graph.root_ids == ["parent-1"]

    def test_multiple_children(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="parent-1",
                name="myapp.tasks.main",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )
        for idx in range(3):
            graph.add_event(
                TaskEvent(
                    task_id=f"child-{idx}",
                    name="myapp.tasks.subtask",
                    state=TaskState.STARTED,
                    timestamp=datetime.now(timezone.utc),
                    parent_id="parent-1",
                )
            )
        assert len(graph.nodes["parent-1"].children) == 3
        assert "child-0" in graph.nodes["parent-1"].children
        assert "child-1" in graph.nodes["parent-1"].children
        assert "child-2" in graph.nodes["parent-1"].children

    def test_late_parent_id_from_later_event(self) -> None:
        """Test that parent_id is updated from later events (e.g., STARTED after PENDING)."""
        graph = TaskGraph()
        # Parent task
        graph.add_event(
            TaskEvent(
                task_id="parent-1",
                name="myapp.tasks.main",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )
        # Child's PENDING event (from task_sent) lacks parent_id
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.PENDING,
                timestamp=datetime.now(timezone.utc),
                parent_id=None,  # task_sent doesn't know parent
            )
        )
        # Initially child appears as root
        assert "child-1" in graph.root_ids
        assert graph.nodes["child-1"].parent_id is None

        # Child's STARTED event (from worker) has parent_id
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                parent_id="parent-1",  # Worker has this info
            )
        )
        # Now child should be linked to parent
        assert "child-1" not in graph.root_ids
        assert graph.nodes["child-1"].parent_id == "parent-1"
        assert "child-1" in graph.nodes["parent-1"].children


class TestTaskGraphNesting:
    def test_three_level_nesting(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="root",
                name="myapp.tasks.root",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="parent",
                name="myapp.tasks.parent",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                parent_id="root",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child",
                name="myapp.tasks.child",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                parent_id="parent",
            )
        )
        assert graph.root_ids == ["root"]
        assert "parent" in graph.nodes["root"].children
        assert "child" in graph.nodes["parent"].children

    def test_multiple_roots(self) -> None:
        graph = TaskGraph()
        for idx in range(3):
            graph.add_event(
                TaskEvent(
                    task_id=f"root-{idx}",
                    name="myapp.tasks.root",
                    state=TaskState.STARTED,
                    timestamp=datetime.now(timezone.utc),
                )
            )
        assert len(graph.root_ids) == 3


class TestTaskNodeStateTransitions:
    def test_full_lifecycle(self) -> None:
        graph = TaskGraph()
        task_id = "task-1"
        states = [
            TaskState.PENDING,
            TaskState.RECEIVED,
            TaskState.STARTED,
            TaskState.SUCCESS,
        ]
        for state in states:
            graph.add_event(
                TaskEvent(
                    task_id=task_id,
                    name="myapp.tasks.process",
                    state=state,
                    timestamp=datetime.now(timezone.utc),
                )
            )
        node = graph.nodes[task_id]
        assert node.state == TaskState.SUCCESS
        assert len(node.events) == 4

    def test_retry_lifecycle(self) -> None:
        graph = TaskGraph()
        task_id = "task-1"
        graph.add_event(
            TaskEvent(
                task_id=task_id,
                name="myapp.tasks.flaky",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                retries=0,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id=task_id,
                name="myapp.tasks.flaky",
                state=TaskState.RETRY,
                timestamp=datetime.now(timezone.utc),
                retries=1,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id=task_id,
                name="myapp.tasks.flaky",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                retries=1,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id=task_id,
                name="myapp.tasks.flaky",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                retries=1,
            )
        )
        node = graph.nodes[task_id]
        assert node.state == TaskState.SUCCESS
        assert len(node.events) == 4
        assert node.events[-1].retries == 1


class TestTaskGraphSerialization:
    def test_to_dict(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )
        data = graph.model_dump()
        assert "nodes" in data
        assert "root_ids" in data
        assert "task-1" in data["nodes"]

    def test_roundtrip(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="parent",
                name="myapp.tasks.main",
                state=TaskState.STARTED,
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child",
                name="myapp.tasks.sub",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
                parent_id="parent",
            )
        )
        data = graph.model_dump(mode="json")
        restored = TaskGraph.model_validate(data)
        assert restored.root_ids == graph.root_ids
        assert "parent" in restored.nodes
        assert "child" in restored.nodes
        assert restored.nodes["parent"].children == ["child"]


class TestGroupIdCapture:
    """Tests for group_id field on events and nodes."""

    def test_event_with_group_id(self) -> None:
        event = TaskEvent(
            task_id="task-1",
            name="myapp.tasks.process",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
            group_id="group-abc",
        )
        assert event.group_id == "group-abc"

    def test_event_without_group_id(self) -> None:
        event = TaskEvent(
            task_id="task-1",
            name="myapp.tasks.process",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )
        assert event.group_id is None

    def test_node_inherits_group_id(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id="group-abc",
            )
        )
        node = graph.get_node("task-1")
        assert node is not None
        assert node.group_id == "group-abc"

    def test_group_id_updated_from_later_event(self) -> None:
        """Test that group_id is updated from later events (e.g., STARTED after PENDING)."""
        graph = TaskGraph()
        # PENDING event (from task_sent) may lack group_id
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.PENDING,
                timestamp=datetime.now(timezone.utc),
                group_id=None,
            )
        )
        assert graph.nodes["task-1"].group_id is None

        # STARTED event (from worker) has group_id
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id="group-abc",
            )
        )
        assert graph.nodes["task-1"].group_id == "group-abc"


class TestSyntheticGroupNodes:
    """Tests for synthetic GROUP node creation."""

    def test_no_group_node_for_single_member(self) -> None:
        """Single task with group_id should not create a GROUP node."""
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id="group-abc",
            )
        )
        # No synthetic node should be created yet
        assert "group:group-abc" not in graph.nodes
        assert "task-1" in graph.root_ids

    def test_group_node_created_on_second_member(self) -> None:
        """GROUP node should be created when second task with same group_id arrives."""
        graph = TaskGraph()
        group_id = "group-abc"

        # First member
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        assert "group:group-abc" not in graph.nodes

        # Second member - triggers GROUP node creation
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        assert "group:group-abc" in graph.nodes
        group_node = graph.nodes["group:group-abc"]
        assert group_node.node_type == NodeType.GROUP
        assert group_node.group_id == group_id
        assert "task-1" in group_node.children
        assert "task-2" in group_node.children

    def test_group_node_as_root(self) -> None:
        """GROUP node should be added to root_ids, members removed from roots."""
        graph = TaskGraph()
        group_id = "group-abc"

        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        # GROUP node should be the only root
        assert "group:group-abc" in graph.root_ids
        assert "task-1" not in graph.root_ids
        assert "task-2" not in graph.root_ids

    def test_group_node_members_parent_id(self) -> None:
        """Member tasks should have parent_id pointing to GROUP node."""
        graph = TaskGraph()
        group_id = "group-abc"

        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        assert graph.nodes["task-1"].parent_id == "group:group-abc"
        assert graph.nodes["task-2"].parent_id == "group:group-abc"

    def test_third_member_added_to_existing_group(self) -> None:
        """Third member should be added to existing GROUP node."""
        graph = TaskGraph()
        group_id = "group-abc"

        for i in range(3):
            graph.add_event(
                TaskEvent(
                    task_id=f"task-{i}",
                    name=f"myapp.tasks.t{i}",
                    state=TaskState.STARTED,
                    timestamp=datetime.now(timezone.utc),
                    group_id=group_id,
                )
            )

        group_node = graph.nodes["group:group-abc"]
        assert len(group_node.children) == 3
        assert "task-0" in group_node.children
        assert "task-1" in group_node.children
        assert "task-2" in group_node.children

    def test_multiple_groups(self) -> None:
        """Multiple independent groups should create separate GROUP nodes."""
        graph = TaskGraph()

        # Group A
        graph.add_event(
            TaskEvent(
                task_id="a1",
                name="myapp.tasks.a",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id="group-a",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="a2",
                name="myapp.tasks.a",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id="group-a",
            )
        )

        # Group B
        graph.add_event(
            TaskEvent(
                task_id="b1",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id="group-b",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="b2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id="group-b",
            )
        )

        assert "group:group-a" in graph.nodes
        assert "group:group-b" in graph.nodes
        assert graph.nodes["group:group-a"].children == ["a1", "a2"]
        assert graph.nodes["group:group-b"].children == ["b1", "b2"]

    def test_get_group_members(self) -> None:
        """Test get_group_members helper method."""
        graph = TaskGraph()
        group_id = "group-abc"

        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        members = graph.get_group_members(group_id)
        assert "task-1" in members
        assert "task-2" in members

    def test_get_group_members_empty(self) -> None:
        """get_group_members returns empty list for unknown group."""
        graph = TaskGraph()
        assert graph.get_group_members("unknown") == []


class TestGroupNodeState:
    """Tests for GROUP node state computation."""

    def test_group_state_all_success(self) -> None:
        """GROUP state should be SUCCESS when all members succeed."""
        graph = TaskGraph()
        group_id = "group-abc"

        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        group_node = graph.nodes["group:group-abc"]
        assert group_node.state == TaskState.SUCCESS

    def test_group_state_any_failure(self) -> None:
        """GROUP state should be FAILURE if any member fails."""
        graph = TaskGraph()
        group_id = "group-abc"

        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.FAILURE,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        group_node = graph.nodes["group:group-abc"]
        assert group_node.state == TaskState.FAILURE

    def test_group_state_any_started(self) -> None:
        """GROUP state should be STARTED if any member is still running."""
        graph = TaskGraph()
        group_id = "group-abc"

        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        group_node = graph.nodes["group:group-abc"]
        assert group_node.state == TaskState.STARTED

    def test_group_state_any_pending(self) -> None:
        """GROUP state should be PENDING if any member is pending."""
        graph = TaskGraph()
        group_id = "group-abc"

        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.PENDING,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        group_node = graph.nodes["group:group-abc"]
        assert group_node.state == TaskState.PENDING

    def test_group_state_updates_on_member_change(self) -> None:
        """GROUP state should update when member states change."""
        graph = TaskGraph()
        group_id = "group-dynamic"

        # Initial: two tasks STARTED
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        group_node = graph.nodes["group:group-dynamic"]
        assert group_node.state == TaskState.STARTED

        # task-1 succeeds
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.a",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        assert group_node.state == TaskState.STARTED  # still one running

        # task-2 succeeds
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        assert group_node.state == TaskState.SUCCESS  # all done

    def test_group_node_linked_to_parent_after_member_parent_update(self) -> None:
        """Group node should be linked to common parent when members get parent_id after group creation.

        This tests the bug fix: when group is created before STARTED events arrive,
        members initially have parent_id=None, so group becomes root. When STARTED
        events arrive with parent_id, group should be updated to link to that parent.
        """
        graph = TaskGraph()
        parent_id = "workflow-task"
        group_id = "group-abc"

        # Create parent task
        graph.add_event(
            TaskEvent(
                task_id=parent_id,
                name="myapp.workflow",
                state=TaskState.PENDING,
                timestamp=datetime.now(timezone.utc),
            )
        )

        # Add PENDING events for group members (no parent_id yet)
        graph.add_event(
            TaskEvent(
                task_id="member-1",
                name="myapp.process",
                state=TaskState.PENDING,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="member-2",
                name="myapp.process",
                state=TaskState.PENDING,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        # Group should be created as root (members have no parent yet)
        group_node_id = f"group:{group_id}"
        assert group_node_id in graph.nodes
        assert group_node_id in graph.root_ids
        assert graph.nodes[group_node_id].parent_id is None

        # Now STARTED events arrive with parent_id
        graph.add_event(
            TaskEvent(
                task_id="member-1",
                name="myapp.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                parent_id=parent_id,
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="member-2",
                name="myapp.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                parent_id=parent_id,
                group_id=group_id,
            )
        )

        # Group should now be linked to parent, not a root
        group_node = graph.nodes[group_node_id]
        assert group_node.parent_id == parent_id
        assert group_node_id not in graph.root_ids
        assert group_node_id in graph.nodes[parent_id].children

    def test_group_node_created_under_common_parent(self) -> None:
        """GROUP node IS created under common parent to differentiate from independent tasks."""
        graph = TaskGraph()
        group_id = "group-with-parent"
        parent_id = "parent-task"

        # Create parent first
        graph.add_event(
            TaskEvent(
                task_id=parent_id,
                name="myapp.tasks.parent",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
            )
        )

        # Add grouped tasks with same parent
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                parent_id=parent_id,
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child-2",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                parent_id=parent_id,
                group_id=group_id,
            )
        )

        # Synthetic GROUP SHOULD exist - differentiates from independent subtasks
        group_node_id = f"group:{group_id}"
        assert group_node_id in graph.nodes

        group_node = graph.nodes[group_node_id]
        # GROUP is a child of the parent task
        assert group_node.parent_id == parent_id
        # GROUP contains the member tasks
        assert "child-1" in group_node.children
        assert "child-2" in group_node.children

        # Parent task has GROUP as child (not the individual tasks)
        parent_node = graph.nodes[parent_id]
        assert group_node_id in parent_node.children
        assert "child-1" not in parent_node.children
        assert "child-2" not in parent_node.children

        # Children point to GROUP, not to parent directly
        assert graph.nodes["child-1"].parent_id == group_node_id
        assert graph.nodes["child-2"].parent_id == group_node_id

        # group_id is still tracked on the nodes
        assert graph.nodes["child-1"].group_id == group_id
        assert graph.nodes["child-2"].group_id == group_id

        # GROUP is NOT a root (it's under parent)
        assert group_node_id not in graph.root_ids


class TestChordNodeCreation:
    """Tests for CHORD node creation and callback linking.

    CHORDs are a special case of GROUPs: parallel header tasks + a callback.
    When header tasks have chord_callback_id, GROUP should upgrade to CHORD.
    """

    def test_chord_created_from_header_with_callback_id(self) -> None:
        """CHORD node is created when header task has chord_callback_id."""
        graph = TaskGraph()
        group_id = "chord-group-abc"
        callback_id = "callback-task-123"

        # First header task with chord info
        graph.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )

        # CHORD node should be created (even with single header)
        chord_node_id = f"group:{group_id}"
        assert chord_node_id in graph.nodes

        chord_node = graph.nodes[chord_node_id]
        assert chord_node.node_type == NodeType.CHORD
        assert chord_node.name == "chord"

    def test_group_upgraded_to_chord(self) -> None:
        """Existing GROUP node is upgraded to CHORD when callback info arrives."""
        graph = TaskGraph()
        group_id = "upgrade-test-group"
        callback_id = "callback-123"

        # First header - creates GROUP (no chord info yet)
        graph.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.tasks.add",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        # Second header - GROUP now exists
        graph.add_event(
            TaskEvent(
                task_id="header-2",
                name="myapp.tasks.add",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        group_node_id = f"group:{group_id}"
        assert graph.nodes[group_node_id].node_type == NodeType.GROUP

        # Header with chord info - should upgrade to CHORD
        graph.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )

        assert graph.nodes[group_node_id].node_type == NodeType.CHORD
        assert graph.nodes[group_node_id].name == "chord"

    def test_callback_linked_to_chord(self) -> None:
        """Callback task is linked to CHORD node when it arrives."""
        graph = TaskGraph()
        group_id = "chord-with-callback"
        callback_id = "aggregate-task"

        # Header tasks with chord info
        for i in range(3):
            graph.add_event(
                TaskEvent(
                    task_id=f"add-{i}",
                    name="myapp.tasks.add",
                    state=TaskState.SUCCESS,
                    timestamp=datetime.now(timezone.utc),
                    group_id=group_id,
                    chord_id=group_id,
                    chord_callback_id=callback_id,
                )
            )

        # Callback task arrives
        graph.add_event(
            TaskEvent(
                task_id=callback_id,
                name="myapp.tasks.aggregate",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,  # Callback has same group_id
            )
        )

        chord_node_id = f"group:{group_id}"
        chord_node = graph.nodes[chord_node_id]
        callback_node = graph.nodes[callback_id]

        # Callback is child of CHORD
        assert callback_id in chord_node.children

        # Callback has parent_id pointing to CHORD
        assert callback_node.parent_id == chord_node_id

        # Callback is NOT in root_ids
        assert callback_id not in graph.root_ids

    def test_callback_not_in_group_members(self) -> None:
        """Callback task should NOT be counted as a group member (it's outside)."""
        graph = TaskGraph()
        group_id = "chord-callback-outside"
        callback_id = "callback-task"

        # Header with chord info
        graph.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )

        # Callback arrives
        graph.add_event(
            TaskEvent(
                task_id=callback_id,
                name="myapp.tasks.aggregate",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        # Callback should NOT be in group members
        members = graph.get_group_members(group_id)
        assert callback_id not in members

    def test_chord_state_reflects_header_tasks_only(self) -> None:
        """CHORD state should reflect header task states, not callback."""
        graph = TaskGraph()
        group_id = "chord-state-test"
        callback_id = "callback"

        # Headers still running
        graph.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.tasks.add",
                state=TaskState.STARTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="header-2",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )

        chord_node = graph.nodes[f"group:{group_id}"]
        # One header still running
        assert chord_node.state == TaskState.STARTED

        # First header completes
        graph.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )

        # All headers done
        assert chord_node.state == TaskState.SUCCESS


class TestGraphCoverageEdgeCases:
    """Targeted tests to cover edge-case branches in TaskGraph."""

    def test_compute_group_state_with_no_known_members_returns_pending(self) -> None:
        """If member IDs aren't present in the graph, group state should be PENDING."""
        graph = TaskGraph()
        assert graph._compute_group_state(["missing-task"]) == TaskState.PENDING

    def test_group_state_any_retry(self) -> None:
        """GROUP state should be RETRY when any member is RETRY and none are PENDING/STARTED."""
        graph = TaskGraph()
        group_id = "group-retry"

        graph.add_event(
            TaskEvent(
                task_id="t1",
                name="myapp.tasks.a",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="t2",
                name="myapp.tasks.b",
                state=TaskState.RETRY,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        assert graph.nodes[f"group:{group_id}"].state == TaskState.RETRY

    def test_group_state_any_revoked(self) -> None:
        """GROUP state should be REVOKED when any member is REVOKED and none are PENDING/STARTED/RETRY."""
        graph = TaskGraph()
        group_id = "group-revoked"

        graph.add_event(
            TaskEvent(
                task_id="t1",
                name="myapp.tasks.a",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="t2",
                name="myapp.tasks.b",
                state=TaskState.REVOKED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        assert graph.nodes[f"group:{group_id}"].state == TaskState.REVOKED

    def test_group_state_any_rejected(self) -> None:
        """A rejected member outranks a successful one: the group didn't finish cleanly."""
        graph = TaskGraph()
        group_id = "group-rejected"

        graph.add_event(
            TaskEvent(
                task_id="t1",
                name="myapp.tasks.a",
                state=TaskState.REJECTED,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="t2",
                name="myapp.tasks.b",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )

        assert graph.nodes[f"group:{group_id}"].state == TaskState.REJECTED

    def test_upgrade_to_chord_removes_preexisting_callback_from_group_members(
        self,
    ) -> None:
        """If callback was tracked as a group member before chord info arrives, it should be removed."""
        graph = TaskGraph()
        group_id = "out-of-order-chord"
        callback_id = "callback"

        # Callback arrives first with group_id => gets tracked as member.
        graph.add_event(
            TaskEvent(
                task_id=callback_id,
                name="myapp.tasks.callback",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
            )
        )
        assert callback_id in graph.get_group_members(group_id)

        # Header arrives later and declares chord callback => upgrades to CHORD and removes callback from members.
        graph.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.tasks.header",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
                chord_callback_id=callback_id,
            )
        )

        assert graph.nodes[f"group:{group_id}"].node_type == NodeType.CHORD
        assert callback_id not in graph.get_group_members(group_id)

    def test_upgrade_to_chord_links_existing_callback_without_group_membership(
        self,
    ) -> None:
        """If callback exists as standalone task, _upgrade_to_chord should link it when chord appears."""
        graph = TaskGraph()
        group_id = "chord-link-existing"
        callback_id = "callback"

        # Callback exists as standalone task (no group_id => not tracked as member)
        graph.add_event(
            TaskEvent(
                task_id=callback_id,
                name="myapp.tasks.callback",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
            )
        )
        assert callback_id in graph.root_ids

        graph.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.tasks.header",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(timezone.utc),
                group_id=group_id,
                chord_callback_id=callback_id,
            )
        )

        chord_node_id = f"group:{group_id}"
        assert graph.nodes[callback_id].parent_id == chord_node_id
        assert callback_id in graph.nodes[chord_node_id].children
        assert callback_id not in graph.root_ids


_BASE_TIME = datetime(2024, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def op_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count event keys, precedence lookups and full event comparisons.

    Lets complexity tests assert on work done instead of wall-clock time.
    """
    counts = {"digest": 0, "precedence": 0, "eq": 0}
    real_digest = graph_module._digest
    real_precedence = graph_module._precedence
    real_eq = TaskEvent.__eq__

    def digest(event: TaskEvent) -> bytes:
        counts["digest"] += 1
        return real_digest(event)

    def precedence(event: TaskEvent) -> tuple[int, int, int, datetime, int]:
        counts["precedence"] += 1
        return real_precedence(event)

    def eq(self: TaskEvent, other: object) -> bool:
        counts["eq"] += 1
        return real_eq(self, other)

    monkeypatch.setattr(graph_module, "_digest", digest)
    monkeypatch.setattr(graph_module, "_precedence", precedence)
    monkeypatch.setattr(TaskEvent, "__eq__", eq)
    return counts


class _ScanCountingDict(dict[str, TaskNode]):
    """Node map that counts full scans (iteration over all nodes)."""

    scans = 0

    def values(self) -> ValuesView[TaskNode]:  # type: ignore[override]  # narrower view type
        self.scans += 1
        return super().values()

    def items(self) -> ItemsView[str, TaskNode]:  # type: ignore[override]  # narrower view type
        self.scans += 1
        return super().items()

    def __iter__(self) -> Iterator[str]:
        self.scans += 1
        return super().__iter__()


def _ordered_event(
    state: TaskState,
    offset_s: float,
    *,
    task_id: str = "task-1",
    retries: int = 0,
    group_id: str | None = None,
) -> TaskEvent:
    """Build an event at a fixed offset from a base time (deterministic ordering)."""
    return TaskEvent(
        task_id=task_id,
        name="myapp.tasks.process",
        state=state,
        timestamp=_BASE_TIME + timedelta(seconds=offset_s),
        retries=retries,
        group_id=group_id,
    )


class TestOutOfOrderEvents:
    """Node state must not regress when events arrive out of order."""

    def test_add_event_pending_after_success_keeps_success(self) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 1))
        graph.add_event(_ordered_event(TaskState.SUCCESS, 2))
        # Sender-side PENDING (task_sent fires after publish) arrives last.
        graph.add_event(_ordered_event(TaskState.PENDING, 0))

        node = graph.nodes["task-1"]
        assert node.state == TaskState.SUCCESS
        assert [e.state for e in node.events] == [
            TaskState.PENDING,
            TaskState.STARTED,
            TaskState.SUCCESS,
        ]

    def test_add_event_pending_with_later_timestamp_after_success_keeps_success(
        self,
    ) -> None:
        # task_sent can be stamped after the worker already started the task.
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 1))
        graph.add_event(_ordered_event(TaskState.SUCCESS, 2))
        graph.add_event(_ordered_event(TaskState.PENDING, 3))

        assert graph.nodes["task-1"].state == TaskState.SUCCESS

    def test_add_event_received_after_started_keeps_started(self) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.PENDING, 0))
        graph.add_event(_ordered_event(TaskState.STARTED, 2))
        graph.add_event(_ordered_event(TaskState.RECEIVED, 1))

        node = graph.nodes["task-1"]
        assert node.state == TaskState.STARTED
        assert [e.state for e in node.events] == [
            TaskState.PENDING,
            TaskState.RECEIVED,
            TaskState.STARTED,
        ]

    def test_add_event_in_order_retry_cycle_shows_retry_then_success(self) -> None:
        graph = TaskGraph()
        seen: list[TaskState] = []
        for event in [
            _ordered_event(TaskState.STARTED, 1, retries=0),
            _ordered_event(TaskState.RETRY, 2, retries=0),
            _ordered_event(TaskState.STARTED, 3, retries=1),
            _ordered_event(TaskState.SUCCESS, 4, retries=1),
        ]:
            graph.add_event(event)
            seen.append(graph.nodes["task-1"].state)

        assert seen == [
            TaskState.STARTED,
            TaskState.RETRY,
            TaskState.STARTED,
            TaskState.SUCCESS,
        ]
        node = graph.nodes["task-1"]
        assert node.events[1].state == TaskState.RETRY
        assert node.events[1].retries == 0

    def test_add_event_interleaved_retry_cycle_ends_in_success(self) -> None:
        # Next attempt's events overtake the RETRY event of the previous attempt.
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 1, retries=0))
        graph.add_event(_ordered_event(TaskState.STARTED, 3, retries=1))
        graph.add_event(_ordered_event(TaskState.SUCCESS, 4, retries=1))
        graph.add_event(_ordered_event(TaskState.RETRY, 2, retries=0))

        node = graph.nodes["task-1"]
        assert node.state == TaskState.SUCCESS
        assert [(e.state, e.retries) for e in node.events] == [
            (TaskState.STARTED, 0),
            (TaskState.RETRY, 0),
            (TaskState.STARTED, 1),
            (TaskState.SUCCESS, 1),
        ]

    def test_add_event_retry_after_next_attempt_started_keeps_started(self) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 1, retries=0))
        graph.add_event(_ordered_event(TaskState.STARTED, 3, retries=1))
        graph.add_event(_ordered_event(TaskState.RETRY, 2, retries=0))

        assert graph.nodes["task-1"].state == TaskState.STARTED

    @pytest.mark.parametrize(
        "terminal",
        [
            TaskState.FAILURE,
            TaskState.REVOKED,
            TaskState.REJECTED,
            TaskState.SUCCESS,
        ],
    )
    def test_add_event_terminal_state_not_overridden_by_same_attempt(
        self, terminal: TaskState
    ) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(terminal, 2))
        graph.add_event(_ordered_event(TaskState.STARTED, 1))
        graph.add_event(_ordered_event(TaskState.RECEIVED, 0.5))
        graph.add_event(_ordered_event(TaskState.PENDING, 0))
        # Even a non-terminal event stamped later in the same attempt loses.
        graph.add_event(_ordered_event(TaskState.STARTED, 5))
        graph.add_event(_ordered_event(TaskState.RETRY, 6))

        assert graph.nodes["task-1"].state == terminal

    def test_add_event_main_process_failure_with_zero_retries_ends_in_failure(
        self,
    ) -> None:
        # Hard time limit / lost worker: Celery's main process emits
        # task_failure with a default request, so retries is 0 even though the
        # task already retried twice.
        events = [
            _ordered_event(TaskState.STARTED, 1, retries=0),
            _ordered_event(TaskState.RETRY, 2, retries=0),
            _ordered_event(TaskState.STARTED, 3, retries=1),
            _ordered_event(TaskState.RETRY, 4, retries=1),
            _ordered_event(TaskState.STARTED, 5, retries=2),
            _ordered_event(TaskState.FAILURE, 6, retries=0),
        ]
        graph = TaskGraph()
        for event in events:
            graph.add_event(event)

        node = graph.nodes["task-1"]
        assert node.state == TaskState.FAILURE
        assert node.events[-1].state == TaskState.FAILURE

    def test_add_event_main_process_failure_any_arrival_order_ends_in_failure(
        self,
    ) -> None:
        events = [
            _ordered_event(TaskState.STARTED, 1, retries=0),
            _ordered_event(TaskState.RETRY, 2, retries=0),
            _ordered_event(TaskState.STARTED, 3, retries=1),
            _ordered_event(TaskState.RETRY, 4, retries=1),
            _ordered_event(TaskState.STARTED, 5, retries=2),
            _ordered_event(TaskState.FAILURE, 6, retries=0),
        ]
        final_states = set()
        for order in permutations(events):
            graph = TaskGraph()
            for event in order:
                graph.add_event(event)
            final_states.add(graph.nodes["task-1"].state)

        assert final_states == {TaskState.FAILURE}

    def test_add_event_terminal_beats_later_non_terminal_from_higher_attempt(
        self,
    ) -> None:
        # A terminal's retries can be wrong (0 from the worker main process),
        # so no non-terminal event overrides it.
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.FAILURE, 2, retries=0))
        graph.add_event(_ordered_event(TaskState.STARTED, 3, retries=2))

        assert graph.nodes["task-1"].state == TaskState.FAILURE

    def test_add_event_connection_loss_redelivery_started_after_retry_wins(
        self,
    ) -> None:
        # worker_cancel_long_running_tasks_on_connection_loss: RETRY(N), then
        # the redelivered message runs with the same retries count.
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 1, retries=1))
        graph.add_event(_ordered_event(TaskState.RETRY, 2, retries=1))
        assert graph.nodes["task-1"].state == TaskState.RETRY

        graph.add_event(_ordered_event(TaskState.STARTED, 3, retries=1))
        assert graph.nodes["task-1"].state == TaskState.STARTED

        graph.add_event(_ordered_event(TaskState.SUCCESS, 4, retries=1))
        assert graph.nodes["task-1"].state == TaskState.SUCCESS

    def test_add_event_connection_loss_redelivery_out_of_order_shows_started(
        self,
    ) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 3, retries=1))
        graph.add_event(_ordered_event(TaskState.RETRY, 2, retries=1))
        graph.add_event(_ordered_event(TaskState.STARTED, 1, retries=1))

        assert graph.nodes["task-1"].state == TaskState.STARTED

    @pytest.mark.parametrize("state", list(TaskState))
    def test_add_event_every_state_is_ranked(self, state: TaskState) -> None:
        # Fails with KeyError if a new TaskState is added without a rank.
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 1))
        graph.add_event(_ordered_event(state, 2))
        graph.add_event(_ordered_event(TaskState.PENDING, 0))

        earlier_phase = {TaskState.PENDING, TaskState.RECEIVED}
        expected = TaskState.STARTED if state in earlier_phase else state
        assert graph.nodes["task-1"].state == expected

    @pytest.mark.parametrize(
        ("sequence", "expected"),
        [
            pytest.param(
                [
                    (TaskState.PENDING, 0, 0),
                    (TaskState.RECEIVED, 0.5, 0),
                    (TaskState.STARTED, 1, 0),
                    (TaskState.SUCCESS, 2, 0),
                ],
                TaskState.SUCCESS,
                id="simple-success",
            ),
            pytest.param(
                [
                    (TaskState.PENDING, 1.5, 0),  # stamped after publish
                    (TaskState.STARTED, 1, 0),
                    (TaskState.SUCCESS, 2, 0),
                ],
                TaskState.SUCCESS,
                id="pending-stamped-late",
            ),
            pytest.param(
                [
                    (TaskState.PENDING, 0, 0),
                    (TaskState.STARTED, 1, 0),
                    (TaskState.RETRY, 2, 0),
                    (TaskState.STARTED, 3, 1),
                    (TaskState.RETRY, 4, 1),
                    (TaskState.SUCCESS, 6, 1),
                ],
                TaskState.SUCCESS,
                id="retries-then-success",
            ),
            pytest.param(
                [
                    (TaskState.STARTED, 1, 0),
                    (TaskState.RETRY, 2, 0),
                    (TaskState.STARTED, 3, 1),
                    (TaskState.FAILURE, 4, 0),  # main-process failure
                ],
                TaskState.FAILURE,
                id="main-process-failure",
            ),
            pytest.param(
                [
                    (TaskState.STARTED, 1, 1),
                    (TaskState.RETRY, 2, 1),  # connection-loss cancel
                    (TaskState.STARTED, 3, 1),  # redelivered run
                ],
                TaskState.STARTED,
                id="connection-loss-redelivery",
            ),
            pytest.param(
                [
                    (TaskState.FAILURE, 10, 0),
                    (TaskState.RETRY, 20, 1),
                    (TaskState.STARTED, 8, 2),  # skewed clock
                ],
                TaskState.FAILURE,
                id="skewed-clocks",
            ),
            pytest.param(
                [
                    (TaskState.STARTED, 1, 0),
                    (TaskState.RETRY, 1, 0),  # same instant
                ],
                TaskState.RETRY,
                id="started-retry-tie",
            ),
        ],
    )
    def test_add_event_final_state_independent_of_arrival_order(
        self,
        sequence: list[tuple[TaskState, float, int]],
        expected: TaskState,
    ) -> None:
        events = [
            _ordered_event(state, offset, retries=retries)
            for state, offset, retries in sequence
        ]
        final_states = set()
        for order in permutations(events):
            graph = TaskGraph()
            for event in order:
                graph.add_event(event)
            final_states.add(graph.nodes["task-1"].state)

        assert final_states == {expected}

    def test_add_event_naive_timestamp_treated_as_utc(self) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.SUCCESS, 2))
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.PENDING,
                timestamp=datetime(2024, 1, 1, 0, 0, 1),  # naive, 1s after base
            )
        )

        node = graph.nodes["task-1"]
        assert node.state == TaskState.SUCCESS
        assert [e.state for e in node.events] == [
            TaskState.PENDING,
            TaskState.SUCCESS,
        ]
        assert node.events[0].timestamp == _BASE_TIME + timedelta(seconds=1)
        assert node.events[0].timestamp.tzinfo is not None

    def test_add_event_after_roundtrip_rebuilds_state_from_history(self) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 1))
        graph.add_event(_ordered_event(TaskState.SUCCESS, 2))
        restored = TaskGraph.model_validate(graph.model_dump())

        restored.add_event(_ordered_event(TaskState.PENDING, 0))

        assert restored.nodes["task-1"].state == TaskState.SUCCESS
        assert len(restored.nodes["task-1"].events) == 3

    @pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
    def test_add_event_many_events_on_one_node_does_constant_work_per_event(
        self, reverse: bool, op_counter: dict[str, int]
    ) -> None:
        graph = TaskGraph()
        events = [_ordered_event(TaskState.RETRY, i, retries=i) for i in range(5000)]
        if reverse:
            events.reverse()

        for event in events:
            graph.add_event(event)
        for event in events[:100]:
            graph.add_event(event)  # duplicates

        node = graph.nodes["task-1"]
        assert len(node.events) == 5000
        assert node.events[0].retries == 0
        assert node.events[-1].retries == 4999
        assert node.state == TaskState.RETRY
        # One key per insert, at most two precedence lookups per new event, and
        # full equality only for the 100 real duplicates: no scans.
        assert op_counter["digest"] == 5100
        assert op_counter["precedence"] <= 2 * 5000
        assert op_counter["eq"] == 100

    def test_add_event_with_chords_never_scans_all_nodes(self) -> None:
        graph = TaskGraph()
        for i in range(200):
            graph.add_event(_ordered_event(TaskState.SUCCESS, i, task_id=f"t{i}"))
        scans = _ScanCountingDict(graph.nodes)
        graph.__dict__["nodes"] = scans  # bypass validation, which would copy it

        for c in range(3):
            for h in range(2):
                graph.add_event(
                    TaskEvent(
                        task_id=f"c{c}-h{h}",
                        name="myapp.tasks.header",
                        state=TaskState.SUCCESS,
                        timestamp=_BASE_TIME,
                        group_id=f"cg{c}",
                        chord_callback_id=f"c{c}-cb",
                    )
                )
            graph.add_event(_ordered_event(TaskState.SUCCESS, 300, task_id=f"c{c}-cb"))
        for i in range(200):
            graph.add_event(_ordered_event(TaskState.STARTED, 400, task_id=f"new{i}"))

        assert scans.scans == 0
        assert graph.nodes["group:cg1"].children == ["c1-h0", "c1-h1", "c1-cb"]
        assert graph.nodes["c1-cb"].parent_id == "group:cg1"

    def test_add_event_same_timestamp_flood_does_constant_work_per_event(
        self, op_counter: dict[str, int]
    ) -> None:
        graph = TaskGraph()
        events = [
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.STARTED,
                timestamp=_BASE_TIME,
                args=[i],
            )
            for i in range(5000)
        ]

        for event in events:
            graph.add_event(event)
        for event in events[:50]:
            graph.add_event(event)  # duplicates

        assert len(graph.nodes["task-1"].events) == 5000
        assert op_counter["eq"] == 50
        assert op_counter["digest"] == 5050

    def test_add_event_equal_timestamps_order_is_deterministic(self) -> None:
        events = [
            _ordered_event(TaskState.SUCCESS, 1),
            _ordered_event(TaskState.RETRY, 1),
            _ordered_event(TaskState.STARTED, 1),
            _ordered_event(TaskState.FAILURE, 1),
        ]
        orders = set()
        for order in permutations(events):
            graph = TaskGraph()
            for event in order:
                graph.add_event(event)
            node = graph.nodes["task-1"]
            orders.add((tuple(e.state for e in node.events), node.state))

        assert orders == {
            (
                (
                    TaskState.STARTED,
                    TaskState.RETRY,
                    TaskState.SUCCESS,
                    TaskState.FAILURE,
                ),
                TaskState.FAILURE,
            )
        }

    def test_add_event_pending_stamped_after_finish_sorts_first(self) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.STARTED, 0.010))
        graph.add_event(_ordered_event(TaskState.SUCCESS, 0.020))
        graph.add_event(_ordered_event(TaskState.PENDING, 0.025))

        node = graph.nodes["task-1"]
        assert [e.state for e in node.events] == [
            TaskState.PENDING,
            TaskState.STARTED,
            TaskState.SUCCESS,
        ]
        # Timestamps are kept as sent.
        assert node.events[0].timestamp == _BASE_TIME + timedelta(seconds=0.025)
        assert node.first_seen == _BASE_TIME + timedelta(seconds=0.010)
        assert node.last_updated == _BASE_TIME + timedelta(seconds=0.020)

    def test_add_event_pending_stamped_after_received_sorts_before_it(self) -> None:
        events = [
            _ordered_event(TaskState.PENDING, 0.060),
            _ordered_event(TaskState.RECEIVED, 0.010),
            _ordered_event(TaskState.STARTED, 0.020),
            _ordered_event(TaskState.SUCCESS, 0.030),
        ]
        results = set()
        for order in permutations(events):
            graph = TaskGraph()
            for event in order:
                graph.add_event(event)
            node = graph.nodes["task-1"]
            results.add((tuple(e.state for e in node.events), node.first_seen))

        assert results == {
            (
                (
                    TaskState.PENDING,
                    TaskState.RECEIVED,
                    TaskState.STARTED,
                    TaskState.SUCCESS,
                ),
                _BASE_TIME + timedelta(seconds=0.010),
            )
        }

    def test_add_event_only_pending_uses_its_timestamps(self) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.PENDING, 3))

        node = graph.nodes["task-1"]
        assert node.first_seen == _BASE_TIME + timedelta(seconds=3)
        assert node.last_updated == _BASE_TIME + timedelta(seconds=3)

    def test_first_seen_and_last_updated_none_without_events(self) -> None:
        node = TaskNode(task_id="group:g", name="group", state=TaskState.PENDING)

        assert node.first_seen is None
        assert node.last_updated is None

    def test_add_event_with_unserializable_args_still_dedupes(self) -> None:
        marker = object()
        event = TaskEvent(
            task_id="task-1",
            name="myapp.tasks.process",
            state=TaskState.STARTED,
            timestamp=_BASE_TIME,
            args=[marker],
        )
        graph = TaskGraph()

        assert graph.add_event(event) is event
        assert graph.add_event(event) is None
        assert graph.nodes["task-1"].events == [event]

    def test_add_event_to_node_built_with_unsorted_events_sorts_them(self) -> None:
        node = TaskNode(
            task_id="task-1",
            name="myapp.tasks.process",
            state=TaskState.PENDING,
            events=[
                _ordered_event(TaskState.SUCCESS, 3),
                _ordered_event(TaskState.PENDING, 0),
                _ordered_event(TaskState.STARTED, 2),
            ],
        )
        graph = TaskGraph(nodes={"task-1": node}, root_ids=["task-1"])

        graph.add_event(_ordered_event(TaskState.RECEIVED, 1))

        assert [e.state for e in graph.nodes["task-1"].events] == [
            TaskState.PENDING,
            TaskState.RECEIVED,
            TaskState.STARTED,
            TaskState.SUCCESS,
        ]
        assert graph.nodes["task-1"].state == TaskState.SUCCESS

    def test_add_event_revoked_not_superseded_by_older_attempt(self) -> None:
        graph = TaskGraph()
        graph.add_event(_ordered_event(TaskState.REVOKED, 5, retries=2))
        graph.add_event(_ordered_event(TaskState.STARTED, 3, retries=1))
        graph.add_event(_ordered_event(TaskState.RETRY, 4, retries=1))

        assert graph.nodes["task-1"].state == TaskState.REVOKED

    def test_add_event_two_terminals_same_attempt_latest_timestamp_wins(
        self,
    ) -> None:
        forward = TaskGraph()
        forward.add_event(_ordered_event(TaskState.FAILURE, 1))
        forward.add_event(_ordered_event(TaskState.REVOKED, 2))
        backward = TaskGraph()
        backward.add_event(_ordered_event(TaskState.REVOKED, 2))
        backward.add_event(_ordered_event(TaskState.FAILURE, 1))

        assert forward.nodes["task-1"].state == TaskState.REVOKED
        assert backward.nodes["task-1"].state == TaskState.REVOKED

    def test_add_event_duplicate_is_idempotent(self) -> None:
        graph = TaskGraph()
        started = _ordered_event(TaskState.STARTED, 1)
        success = _ordered_event(TaskState.SUCCESS, 2)
        graph.add_event(started)
        graph.add_event(success)
        graph.add_event(started)
        graph.add_event(success)

        node = graph.nodes["task-1"]
        assert node.state == TaskState.SUCCESS
        assert node.events == [started, success]
        assert graph.root_ids == ["task-1"]

    def test_add_event_out_of_order_keeps_events_sorted_by_timestamp(self) -> None:
        graph = TaskGraph()
        for offset, state in [
            (3, TaskState.SUCCESS),
            (0, TaskState.PENDING),
            (2, TaskState.STARTED),
            (1, TaskState.RECEIVED),
        ]:
            graph.add_event(_ordered_event(state, offset))

        node = graph.nodes["task-1"]
        assert [e.state for e in node.events] == [
            TaskState.PENDING,
            TaskState.RECEIVED,
            TaskState.STARTED,
            TaskState.SUCCESS,
        ]
        assert node.events[0].timestamp == _BASE_TIME
        assert node.events[-1].timestamp == _BASE_TIME + timedelta(seconds=3)

    def test_add_event_late_pending_in_group_keeps_group_success(self) -> None:
        graph = TaskGraph()
        for task_id in ("member-1", "member-2"):
            graph.add_event(
                _ordered_event(TaskState.STARTED, 1, task_id=task_id, group_id="g1")
            )
            graph.add_event(
                _ordered_event(TaskState.SUCCESS, 2, task_id=task_id, group_id="g1")
            )
        graph.add_event(
            _ordered_event(TaskState.PENDING, 0, task_id="member-1", group_id="g1")
        )

        group = graph.nodes["group:g1"]
        assert group.events == []
        assert graph.nodes["member-1"].state == TaskState.SUCCESS
        assert group.state == TaskState.SUCCESS


# (node_type, parent_id, children) per node; list order matters, it reaches the UI.
_Shape = dict[str, tuple[NodeType, str | None, list[str]]]


def _shape(graph: TaskGraph) -> _Shape:
    """Linking structure of every node, for whole-graph assertions."""
    return {
        node_id: (node.node_type, node.parent_id, list(node.children))
        for node_id, node in graph.nodes.items()
    }


class TestGraphLinkingCharacterization:
    """Pin how parent, group and chord links evolve, including odd corners.

    These describe the linking rules (see ``TaskGraph``) so refactors can't
    change them silently: a member is listed only under its group, the group
    sits under the members' common parent when that parent is known and is a
    root otherwise, and a moved node never stays listed under its old parent.
    """

    @staticmethod
    def _graph(*events: TaskEvent) -> TaskGraph:
        graph = TaskGraph()
        for event in events:
            graph.add_event(event)
            assert_graph_invariants(graph)
        return graph

    @staticmethod
    def _event(
        task_id: str,
        state: TaskState = TaskState.PENDING,
        /,
        *,
        parent_id: str | None = None,
        group_id: str | None = None,
        chord_callback_id: str | None = None,
    ) -> TaskEvent:
        return TaskEvent(
            task_id=task_id,
            name="myapp.tasks.process",
            state=state,
            timestamp=_BASE_TIME,
            parent_id=parent_id,
            group_id=group_id,
            chord_callback_id=chord_callback_id,
        )

    def test_add_event_unknown_late_parent_keeps_task_a_root(self) -> None:
        ev = self._event
        graph = self._graph(
            ev("m1", group_id="g"),
            ev("m1", TaskState.STARTED, parent_id="ghost", group_id="g"),
        )

        assert _shape(graph) == {"m1": (NodeType.TASK, None, [])}
        assert graph.root_ids == ["m1"]
        assert graph.get_group_members("g") == ["m1"]

    def test_add_event_member_real_parent_moves_group_under_it(self) -> None:
        ev = self._event
        graph = self._graph(
            ev("P"),
            ev("m1", parent_id="P", group_id="g"),
            ev("m2", group_id="g"),
            ev("m1", TaskState.STARTED, parent_id="P", group_id="g"),
        )

        assert _shape(graph) == {
            "P": (NodeType.TASK, None, ["group:g"]),
            "m1": (NodeType.TASK, "group:g", []),
            "m2": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, "P", ["m1", "m2"]),
        }
        assert graph.root_ids == ["P"]
        assert graph.nodes["group:g"].state == TaskState.STARTED

    def test_add_event_members_with_different_parents_group_is_root(
        self,
    ) -> None:
        ev = self._event
        graph = self._graph(
            ev("P1"),
            ev("P2"),
            ev("m1", group_id="g"),
            ev("m2", group_id="g"),
            ev("m1", TaskState.STARTED, parent_id="P1", group_id="g"),
            ev("m2", TaskState.STARTED, parent_id="P2", group_id="g"),
        )

        assert _shape(graph) == {
            "P1": (NodeType.TASK, None, []),
            "P2": (NodeType.TASK, None, []),
            "m1": (NodeType.TASK, "group:g", []),
            "m2": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, None, ["m1", "m2"]),
        }
        assert graph.root_ids == ["P1", "P2", "group:g"]

    def test_add_event_member_reporting_other_parent_keeps_group_in_place(
        self,
    ) -> None:
        ev = self._event
        graph = self._graph(
            ev("P0"),
            ev("m1", parent_id="P0", group_id="g"),
            ev("m2", parent_id="P0", group_id="g"),
            ev("m1", TaskState.STARTED, parent_id="P1", group_id="g"),
        )

        assert _shape(graph) == {
            "P0": (NodeType.TASK, None, ["group:g"]),
            "m1": (NodeType.TASK, "group:g", []),
            "m2": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, "P0", ["m1", "m2"]),
        }
        assert graph.root_ids == ["P0"]

    def test_add_event_group_with_unknown_common_parent_is_a_root(
        self,
    ) -> None:
        ev = self._event
        graph = self._graph(
            ev("m1", parent_id="ghost", group_id="g"),
            ev("m2", parent_id="ghost", group_id="g"),
        )

        assert _shape(graph) == {
            "m1": (NodeType.TASK, "group:g", []),
            "m2": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, None, ["m1", "m2"]),
        }
        assert graph.root_ids == ["group:g"]

    def test_add_event_repeated_callback_event_links_it_once(self) -> None:
        ev = self._event
        graph = self._graph(
            ev("h1", group_id="g", chord_callback_id="cb"),
            ev("h2", group_id="g", chord_callback_id="cb"),
            ev("cb", group_id="g"),
            ev("cb", TaskState.STARTED, group_id="g"),
        )

        assert _shape(graph) == {
            "h1": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.CHORD, None, ["h1", "h2", "cb"]),
            "h2": (NodeType.TASK, "group:g", []),
            "cb": (NodeType.TASK, "group:g", []),
        }
        assert graph.root_ids == ["group:g"]
        assert graph.get_group_members("g") == ["h1", "h2"]

    def test_add_event_first_header_with_parent_creates_chord_under_it(
        self,
    ) -> None:
        ev = self._event
        graph = self._graph(
            ev("P"),
            ev("cb", parent_id="P"),
            ev("h1", parent_id="P", group_id="g", chord_callback_id="cb"),
        )

        assert _shape(graph) == {
            "P": (NodeType.TASK, None, ["cb", "group:g"]),
            "cb": (NodeType.TASK, "P", []),
            "h1": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.CHORD, "P", ["h1", "cb"]),
        }
        assert graph.root_ids == ["P"]
        assert graph.get_group_members("g") == ["h1"]

    def test_add_event_late_callback_with_parent_keeps_parent(self) -> None:
        ev = self._event
        graph = self._graph(
            ev("P"),
            ev("h1", group_id="g", chord_callback_id="cb"),
            ev("cb", parent_id="P"),
        )

        assert _shape(graph) == {
            "P": (NodeType.TASK, None, ["cb"]),
            "h1": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.CHORD, None, ["h1", "cb"]),
            "cb": (NodeType.TASK, "P", []),
        }
        assert graph.root_ids == ["P", "group:g"]

    def test_group_state_success_then_rejected_is_rejected(self) -> None:
        ev = self._event
        graph = self._graph(
            ev("a", TaskState.SUCCESS, group_id="g"),
            ev("b", TaskState.REJECTED, group_id="g"),
        )

        assert graph.nodes["group:g"].state == TaskState.REJECTED

    def test_add_event_after_member_evicted_forgets_it(self) -> None:
        ev = self._event
        graph = self._graph(
            ev("P"),
            ev("m1", group_id="g"),
            ev("m2", group_id="g"),
        )
        graph.remove_node("m1")

        graph.add_event(ev("m3", group_id="g"))
        graph.add_event(ev("m2", TaskState.STARTED, parent_id="P", group_id="g"))

        assert _shape(graph) == {
            "P": (NodeType.TASK, None, ["group:g"]),
            "m2": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, "P", ["m2", "m3"]),
            "m3": (NodeType.TASK, "group:g", []),
        }
        assert graph.root_ids == ["P"]
        assert graph.get_group_members("g") == ["m2", "m3"]
        assert graph.nodes["group:g"].state == TaskState.STARTED

    def test_add_event_group_formed_with_evicted_member_under_parent(
        self,
    ) -> None:
        ev = self._event
        graph = self._graph(ev("P"), ev("m0", parent_id="P", group_id="g"))
        graph.remove_node("m0")

        graph.add_event(ev("m1", parent_id="P", group_id="g"))
        graph.add_event(ev("m2", parent_id="P", group_id="g"))

        assert _shape(graph) == {
            "P": (NodeType.TASK, None, ["group:g"]),
            "m1": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, "P", ["m1", "m2"]),
            "m2": (NodeType.TASK, "group:g", []),
        }
        assert graph.root_ids == ["P"]

    def test_add_event_chord_after_member_evicted_forgets_member(self) -> None:
        ev = self._event
        graph = self._graph(ev("m1", group_id="g"))
        graph.remove_node("m1")

        graph.add_event(ev("h1", group_id="g", chord_callback_id="cb"))

        assert _shape(graph) == {
            "h1": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.CHORD, None, ["h1"]),
        }
        assert graph.root_ids == ["group:g"]

    def test_add_event_chord_workflow_with_nested_group_shape(self) -> None:
        """A workflow running a chord whose first header starts a group.

        Events carry the real ``parent_id`` from STARTED on; members stay
        listed only under their group, which moves under that parent.
        """
        ev = self._event
        header = {"parent_id": "W", "group_id": "g", "chord_callback_id": "cb"}
        nested = {"parent_id": "h1", "group_id": "n"}
        graph = self._graph(
            ev("W"),
            ev("W", TaskState.STARTED),
            ev("h1", group_id="g"),
            ev("h2", group_id="g"),
            ev("h3", group_id="g"),
            ev("h1", TaskState.STARTED, **header),
            ev("n1", **nested),
            ev("n2", **nested),
            ev("h2", TaskState.STARTED, **header),
            ev("n1", TaskState.SUCCESS, **nested),
            ev("n2", TaskState.SUCCESS, **nested),
            ev("h1", TaskState.SUCCESS, **header),
            ev("h3", TaskState.STARTED, **header),
            ev("h2", TaskState.SUCCESS, **header),
            ev("h3", TaskState.SUCCESS, **header),
            ev("cb"),
            ev("cb", TaskState.STARTED, parent_id="W"),
            ev("cb", TaskState.SUCCESS, parent_id="W"),
            ev("W", TaskState.SUCCESS),
        )

        assert _shape(graph) == {
            "W": (NodeType.TASK, None, ["group:g", "cb"]),
            "h1": (NodeType.TASK, "group:g", ["group:n"]),
            "h2": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.CHORD, "W", ["h1", "h2", "h3", "cb"]),
            "h3": (NodeType.TASK, "group:g", []),
            "n1": (NodeType.TASK, "group:n", []),
            "n2": (NodeType.TASK, "group:n", []),
            "group:n": (NodeType.GROUP, "h1", ["n1", "n2"]),
            "cb": (NodeType.TASK, "W", []),
        }
        assert graph.root_ids == ["W"]
        assert graph.get_group_members("g") == ["h1", "h2", "h3"]
        assert graph.get_group_members("n") == ["n1", "n2"]
        assert {n.state for n in graph.nodes.values()} == {TaskState.SUCCESS}
        assert graph.nodes["group:g"].chord_callback_id == "cb"


_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _graph_with_states(*states: TaskState) -> tuple[TaskGraph, list[str]]:
    graph = TaskGraph()
    ids = []
    for i, state in enumerate(states):
        task_id = f"m{i}"
        graph.add_event(
            TaskEvent(
                task_id=task_id,
                name="tests.member",
                state=state,
                timestamp=_T0 + timedelta(seconds=i),
            )
        )
        ids.append(task_id)
    return graph, ids


def _ev(
    task_id: str,
    state: TaskState = TaskState.PENDING,
    /,
    *,
    parent_id: str | None = None,
    group_id: str | None = None,
    chord_callback_id: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="myapp.tasks.process",
        state=state,
        timestamp=_BASE_TIME,
        parent_id=parent_id,
        group_id=group_id,
        chord_callback_id=chord_callback_id,
    )


def _build(*events: TaskEvent) -> TaskGraph:
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
        assert_graph_invariants(graph)
    return graph


class TestGraphLinksStayConsistent:
    """No self-links, cycles, double listings or stale links."""

    def test_add_event_members_started_with_parent_listed_only_under_group(
        self,
    ) -> None:
        graph = _build(
            _ev("P"),
            _ev("m1", group_id="g"),
            _ev("m2", group_id="g"),
            _ev("m1", TaskState.STARTED, parent_id="P", group_id="g"),
            _ev("m2", TaskState.STARTED, parent_id="P", group_id="g"),
        )

        assert graph.nodes["P"].children == ["group:g"]
        assert graph.nodes["group:g"].children == ["m1", "m2"]
        assert graph.nodes["m1"].parent_id == "group:g"
        assert graph.nodes["m2"].parent_id == "group:g"

    def test_add_event_children_before_parent_are_linked_in_arrival_order(
        self,
    ) -> None:
        graph = _build(
            _ev("c1", parent_id="P"),
            _ev("c2", TaskState.STARTED, parent_id="P"),
            _ev("x"),
            _ev("P"),
        )

        assert graph.nodes["P"].children == ["c1", "c2"]
        assert graph.root_ids == ["x", "P"]
        assert graph._awaiting_parent == {}

    def test_add_event_group_waiting_for_parent_moves_under_it(self) -> None:
        graph = _build(
            _ev("m1", parent_id="W", group_id="g"),
            _ev("m2", parent_id="W", group_id="g"),
        )
        assert graph.root_ids == ["group:g"]

        graph.add_event(_ev("W"))

        assert_graph_invariants(graph)
        assert _shape(graph) == {
            "m1": (NodeType.TASK, "group:g", []),
            "m2": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, "W", ["m1", "m2"]),
            "W": (NodeType.TASK, None, ["group:g"]),
        }
        assert graph.root_ids == ["W"]

    def test_add_event_task_naming_itself_as_parent_is_a_root(self) -> None:
        graph = _build(_ev("t", TaskState.STARTED, parent_id="t"))

        assert graph.nodes["t"].parent_id is None
        assert graph.nodes["t"].children == []
        assert graph.root_ids == ["t"]

    def test_add_event_tasks_naming_each_other_as_parent_make_no_cycle(
        self,
    ) -> None:
        graph = _build(_ev("a", parent_id="b"), _ev("b", parent_id="a"))

        assert _shape(graph) == {
            "a": (NodeType.TASK, None, ["b"]),
            "b": (NodeType.TASK, "a", []),
        }
        assert graph.root_ids == ["a"]

    def test_add_event_member_parented_by_other_member_makes_no_cycle(
        self,
    ) -> None:
        graph = _build(_ev("m2", group_id="g"), _ev("m1", parent_id="m2", group_id="g"))

        assert _shape(graph) == {
            "m2": (NodeType.TASK, None, ["group:g"]),
            "m1": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, "m2", ["m1"]),
        }
        assert graph.root_ids == ["m2"]

    def test_add_event_chord_naming_new_callback_unlinks_old_one(self) -> None:
        graph = _build(
            _ev("cb1"),
            _ev("h1", group_id="g", chord_callback_id="cb1"),
            _ev("h1", TaskState.STARTED, group_id="g", chord_callback_id="cb2"),
        )

        assert graph.nodes["group:g"].children == ["h1"]
        assert graph.nodes["group:g"].chord_callback_id == "cb2"
        assert graph.nodes["cb1"].parent_id is None
        assert graph.root_ids == ["group:g", "cb1"]

    def test_add_event_callback_claimed_by_second_chord_leaves_first(self) -> None:
        graph = _build(
            _ev("cb"),
            _ev("a1", group_id="a", chord_callback_id="cb"),
            _ev("b1", group_id="b", chord_callback_id="cb"),
        )

        assert graph.nodes["group:a"].children == ["a1"]
        assert graph.nodes["group:a"].chord_callback_id is None
        assert graph.nodes["group:b"].children == ["b1", "cb"]
        assert graph.nodes["cb"].parent_id == "group:b"


class TestRemoveNode:
    def test_remove_node_member_updates_group_children_state_and_members(
        self,
    ) -> None:
        graph = _build(
            _ev("m1", TaskState.FAILURE, group_id="g"),
            _ev("m2", TaskState.SUCCESS, group_id="g"),
        )

        graph.remove_node("m1")

        assert_graph_invariants(graph)
        assert graph.nodes["group:g"].children == ["m2"]
        assert graph.nodes["group:g"].state == TaskState.SUCCESS
        assert graph.get_group_members("g") == ["m2"]

    def test_remove_node_last_member_forgets_group(self) -> None:
        graph = _build(_ev("m1", group_id="g"))

        graph.remove_node("m1")

        assert graph.nodes == {}
        assert graph.root_ids == []
        assert graph._group_members == {}

    def test_remove_node_group_releases_members_to_their_parent(self) -> None:
        graph = _build(
            _ev("P"),
            _ev("m1", parent_id="P", group_id="g"),
            _ev("m2", parent_id="P", group_id="g"),
        )

        graph.remove_node("group:g")

        assert_graph_invariants(graph)
        assert graph.nodes["P"].children == ["m1", "m2"]
        assert graph.nodes["m1"].parent_id == "P"
        assert graph.get_group_members("g") == ["m1", "m2"]

    def test_remove_node_group_then_member_event_recreates_it(self) -> None:
        graph = _build(_ev("m1", group_id="g"), _ev("m2", group_id="g"))
        graph.remove_node("group:g")
        assert graph.root_ids == ["m1", "m2"]

        graph.add_event(_ev("m1", TaskState.STARTED, group_id="g"))

        assert_graph_invariants(graph)
        assert _shape(graph) == {
            "m1": (NodeType.TASK, "group:g", []),
            "m2": (NodeType.TASK, "group:g", []),
            "group:g": (NodeType.GROUP, None, ["m1", "m2"]),
        }
        assert graph.root_ids == ["group:g"]

    def test_remove_node_parent_of_group_makes_group_a_root(self) -> None:
        graph = _build(
            _ev("P"),
            _ev("m1", parent_id="P", group_id="g"),
            _ev("m2", parent_id="P", group_id="g"),
        )

        graph.remove_node("P")

        assert_graph_invariants(graph)
        assert graph.nodes["group:g"].parent_id is None
        assert graph.root_ids == ["group:g"]

    def test_remove_node_parent_task_makes_its_tasks_roots(self) -> None:
        graph = _build(_ev("P"), _ev("c", parent_id="P"))

        graph.remove_node("P")

        assert_graph_invariants(graph)
        assert graph.nodes["c"].parent_id is None
        assert graph.root_ids == ["c"]

    def test_remove_node_parent_then_parent_returns_relinks_tasks(self) -> None:
        graph = _build(_ev("P"), _ev("c", parent_id="P"))
        graph.remove_node("P")

        graph.add_event(_ev("P", TaskState.STARTED))

        assert_graph_invariants(graph)
        assert graph.nodes["P"].children == ["c"]
        assert graph.root_ids == ["P"]

    def test_remove_node_waiting_child_is_forgotten_by_parent(self) -> None:
        graph = _build(_ev("c", parent_id="P"))

        graph.remove_node("c")
        graph.add_event(_ev("P"))

        assert_graph_invariants(graph)
        assert graph.nodes["P"].children == []
        assert graph._awaiting_parent == {}

    def test_remove_node_callback_with_own_parent_leaves_chord_children(
        self,
    ) -> None:
        graph = _build(
            _ev("W"),
            _ev("cb", parent_id="W"),
            _ev("h1", group_id="g", chord_callback_id="cb"),
        )
        assert graph.nodes["group:g"].children == ["h1", "cb"]

        graph.remove_node("cb")

        assert_graph_invariants(graph)
        assert graph.nodes["group:g"].children == ["h1"]
        assert graph.nodes["W"].children == []

    def test_remove_node_chord_forgets_its_callback(self) -> None:
        graph = _build(_ev("h1", group_id="g", chord_callback_id="cb"))

        graph.remove_node("group:g")
        graph.add_event(_ev("cb"))

        assert_graph_invariants(graph)
        assert graph._chord_by_callback == {}
        assert graph.nodes["cb"].parent_id is None

    def test_remove_node_unknown_id_is_ignored(self) -> None:
        graph = _build(_ev("t"))

        graph.remove_node("missing")

        assert list(graph.nodes) == ["t"]
        assert graph.root_ids == ["t"]


class TestGroupStatePriority:
    """Pin each adjacent pair of the group-state priority, in both member orders."""

    @pytest.mark.parametrize(
        ("higher", "lower"),
        [
            (TaskState.FAILURE, TaskState.REJECTED),
            (TaskState.REJECTED, TaskState.STARTED),
            (TaskState.STARTED, TaskState.PENDING),
            (TaskState.PENDING, TaskState.RETRY),
            (TaskState.RETRY, TaskState.REVOKED),
            (TaskState.REVOKED, TaskState.SUCCESS),
        ],
    )
    def test_group_state_with_adjacent_states_picks_higher_priority(
        self, higher: TaskState, lower: TaskState
    ) -> None:
        for order in ((higher, lower), (lower, higher)):
            graph, ids = _graph_with_states(*order)
            assert graph._compute_group_state(ids) == higher, order

    def test_group_state_with_received_member_counts_as_started(self) -> None:
        graph, ids = _graph_with_states(TaskState.SUCCESS, TaskState.RECEIVED)
        assert graph._compute_group_state(ids) == TaskState.STARTED


class TestRealParentIsKept:
    def test_add_event_reporting_other_real_parent_keeps_original_parent(
        self,
    ) -> None:
        graph = TaskGraph()
        for task_id in ("p1", "p2"):
            graph.add_event(
                TaskEvent(
                    task_id=task_id,
                    name="tests.parent",
                    state=TaskState.STARTED,
                    timestamp=_T0,
                )
            )
        graph.add_event(
            TaskEvent(
                task_id="child",
                name="tests.child",
                state=TaskState.STARTED,
                timestamp=_T0 + timedelta(seconds=1),
                parent_id="p1",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child",
                name="tests.child",
                state=TaskState.SUCCESS,
                timestamp=_T0 + timedelta(seconds=2),
                parent_id="p2",
            )
        )

        assert graph.nodes["child"].parent_id == "p1"
        assert graph.nodes["p1"].children == ["child"]
        assert graph.nodes["p2"].children == []
        assert graph.nodes["child"].state == TaskState.SUCCESS
