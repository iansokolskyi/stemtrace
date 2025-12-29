"""Tests for task graph models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from celery_flow.core.events import TaskEvent, TaskState
from celery_flow.core.graph import NodeType, TaskGraph, TaskNode


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
            timestamp=datetime.now(UTC),
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
            timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
                parent_id="parent-1",
            )
        )
        assert "parent-1" in graph.root_ids
        assert "child-1" not in graph.root_ids
        assert "child-1" in graph.nodes["parent-1"].children

    def test_child_before_parent_orphaned(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
                parent_id="parent-1",
            )
        )
        assert "child-1" not in graph.root_ids
        assert "child-1" in graph.nodes
        assert graph.nodes["child-1"].parent_id == "parent-1"
        assert "parent-1" not in graph.nodes

    def test_child_before_parent_no_backlink(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
                parent_id="parent-1",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="parent-1",
                name="myapp.tasks.main",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
            )
        )
        # Child knows parent, but parent doesn't know child (no back-linking)
        assert graph.nodes["child-1"].parent_id == "parent-1"
        assert "child-1" not in graph.nodes["parent-1"].children

    def test_multiple_children(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="parent-1",
                name="myapp.tasks.main",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
            )
        )
        for idx in range(3):
            graph.add_event(
                TaskEvent(
                    task_id=f"child-{idx}",
                    name="myapp.tasks.subtask",
                    state=TaskState.STARTED,
                    timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
            )
        )
        # Child's PENDING event (from task_sent) lacks parent_id
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.subtask",
                state=TaskState.PENDING,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="parent",
                name="myapp.tasks.parent",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
                parent_id="root",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child",
                name="myapp.tasks.child",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                    timestamp=datetime.now(UTC),
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
                    timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                retries=0,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id=task_id,
                name="myapp.tasks.flaky",
                state=TaskState.RETRY,
                timestamp=datetime.now(UTC),
                retries=1,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id=task_id,
                name="myapp.tasks.flaky",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
                retries=1,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id=task_id,
                name="myapp.tasks.flaky",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child",
                name="myapp.tasks.sub",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
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
            timestamp=datetime.now(UTC),
            group_id="group-abc",
        )
        assert event.group_id == "group-abc"

    def test_event_without_group_id(self) -> None:
        event = TaskEvent(
            task_id="task-1",
            name="myapp.tasks.process",
            state=TaskState.STARTED,
            timestamp=datetime.now(UTC),
        )
        assert event.group_id is None

    def test_node_inherits_group_id(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.tasks.process",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                    timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id="group-a",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="a2",
                name="myapp.tasks.a",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
                group_id="group-a",
            )
        )

        # Group B
        graph.add_event(
            TaskEvent(
                task_id="b1",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
                group_id="group-b",
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="b2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.FAILURE,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.PENDING,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.tasks.b",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        assert group_node.state == TaskState.SUCCESS  # all done

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
                timestamp=datetime.now(UTC),
            )
        )

        # Add grouped tasks with same parent
        graph.add_event(
            TaskEvent(
                task_id="child-1",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(UTC),
                parent_id=parent_id,
                group_id=group_id,
            )
        )
        graph.add_event(
            TaskEvent(
                task_id="child-2",
                name="myapp.tasks.add",
                state=TaskState.SUCCESS,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
            )
        )
        # Second header - GROUP now exists
        graph.add_event(
            TaskEvent(
                task_id="header-2",
                name="myapp.tasks.add",
                state=TaskState.STARTED,
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                    timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )

        # All headers done
        assert chord_node.state == TaskState.SUCCESS
