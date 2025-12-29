"""Tests for task graph models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from celery_flow.core.events import TaskEvent, TaskState
from celery_flow.core.graph import TaskGraph, TaskNode


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
