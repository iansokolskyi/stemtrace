"""Tests for GraphStore."""

from datetime import UTC, datetime, timedelta

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType
from stemtrace.server.store import GraphStore


@pytest.fixture
def store() -> GraphStore:
    """Create a fresh GraphStore for each test."""
    return GraphStore()


class MakeEvent:
    """Factory for creating events with incrementing timestamps."""

    _counter = 0
    _base_time = datetime(2024, 1, 1, tzinfo=UTC)

    @classmethod
    def reset(cls) -> None:
        cls._counter = 0

    @classmethod
    def create(
        cls,
        task_id: str,
        state: TaskState = TaskState.STARTED,
        name: str = "tests.sample",
        parent_id: str | None = None,
    ) -> TaskEvent:
        cls._counter += 1
        return TaskEvent(
            task_id=task_id,
            name=name,
            state=state,
            timestamp=cls._base_time + timedelta(seconds=cls._counter),
            parent_id=parent_id,
        )


@pytest.fixture
def make_event() -> type[MakeEvent]:
    """Factory for creating events with incrementing timestamps."""
    MakeEvent.reset()
    return MakeEvent


class TestGraphStoreBasics:
    def test_empty_store(self, store: GraphStore) -> None:
        assert store.node_count == 0
        assert store.get_node("nonexistent") is None

    def test_add_event_creates_node(self, store: GraphStore, make_event: type) -> None:
        event = make_event.create("task-1")
        store.add_event(event)
        assert store.node_count == 1
        assert store.get_node("task-1") is not None

    def test_add_multiple_events_same_task(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", TaskState.PENDING))
        store.add_event(make_event.create("task-1", TaskState.STARTED))
        store.add_event(make_event.create("task-1", TaskState.SUCCESS))

        assert store.node_count == 1
        node = store.get_node("task-1")
        assert node is not None
        assert len(node.events) == 3
        assert node.state == TaskState.SUCCESS


class TestGraphStoreGetNodes:
    def test_get_nodes_empty(self, store: GraphStore) -> None:
        nodes, total = store.get_nodes()
        assert nodes == []
        assert total == 0

    def test_get_nodes_returns_list(self, store: GraphStore, make_event: type) -> None:
        for i in range(5):
            store.add_event(make_event.create(f"task-{i}"))

        nodes, total = store.get_nodes()
        assert len(nodes) == 5
        assert total == 5

    def test_get_nodes_limit(self, store: GraphStore, make_event: type) -> None:
        for i in range(10):
            store.add_event(make_event.create(f"task-{i}"))

        nodes, total = store.get_nodes(limit=3)
        assert len(nodes) == 3
        assert total == 10

    def test_get_nodes_offset(self, store: GraphStore, make_event: type) -> None:
        for i in range(10):
            store.add_event(make_event.create(f"task-{i}"))

        all_nodes, _ = store.get_nodes(limit=10)
        offset_nodes, total = store.get_nodes(limit=5, offset=5)

        # Should get last 5 of the 10 nodes
        assert len(offset_nodes) == 5
        assert total == 10
        assert offset_nodes[0].task_id == all_nodes[5].task_id

    def test_get_nodes_filter_by_state(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", TaskState.SUCCESS))
        store.add_event(make_event.create("task-2", TaskState.FAILURE))
        store.add_event(make_event.create("task-3", TaskState.SUCCESS))

        success_nodes, success_total = store.get_nodes(state=TaskState.SUCCESS)
        assert len(success_nodes) == 2
        assert success_total == 2

        failure_nodes, failure_total = store.get_nodes(state=TaskState.FAILURE)
        assert len(failure_nodes) == 1
        assert failure_total == 1

    def test_get_nodes_filter_by_name(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))
        store.add_event(make_event.create("task-2", name="myapp.tasks.send_email"))
        store.add_event(make_event.create("task-3", name="otherapp.tasks.process"))

        nodes, total = store.get_nodes(name_contains="myapp")
        assert len(nodes) == 2
        assert total == 2

        nodes, total = store.get_nodes(name_contains="send")
        assert len(nodes) == 1
        assert total == 1

    def test_get_nodes_filter_case_insensitive(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", name="MyApp.Tasks.Process"))
        nodes, _ = store.get_nodes(name_contains="myapp")
        assert len(nodes) == 1

    def test_get_nodes_sorted_by_timestamp_desc(
        self, store: GraphStore, make_event: type
    ) -> None:
        # Add in order task-0, task-1, task-2
        for i in range(3):
            store.add_event(make_event.create(f"task-{i}"))

        nodes, _ = store.get_nodes()
        # Most recent should be first (task-2)
        assert nodes[0].task_id == "task-2"
        assert nodes[-1].task_id == "task-0"


class TestGraphStoreRoots:
    def test_get_root_nodes_empty(self, store: GraphStore) -> None:
        roots, total = store.get_root_nodes()
        assert roots == []
        assert total == 0

    def test_get_root_nodes(self, store: GraphStore, make_event: type) -> None:
        store.add_event(make_event.create("root-1"))
        store.add_event(make_event.create("root-2"))
        store.add_event(make_event.create("child-1", parent_id="root-1"))

        roots, total = store.get_root_nodes()
        root_ids = [r.task_id for r in roots]
        assert "root-1" in root_ids
        assert "root-2" in root_ids
        assert "child-1" not in root_ids
        assert total == 2

    def test_get_root_nodes_limit(self, store: GraphStore, make_event: type) -> None:
        for i in range(10):
            store.add_event(make_event.create(f"root-{i}"))

        roots, total = store.get_root_nodes(limit=3)
        assert len(roots) == 3
        assert total == 10


class TestGraphStoreDateFiltering:
    """Tests for date filtering functionality."""

    def test_get_nodes_filter_by_from_date(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Filter nodes that have events after from_date."""
        base_time = make_event._base_time
        store.add_event(
            TaskEvent(
                task_id="old-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=1),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="new-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=5),
            )
        )

        # Filter for tasks after hour 3
        nodes, total = store.get_nodes(from_date=base_time + timedelta(hours=3))
        assert len(nodes) == 1
        assert total == 1
        assert nodes[0].task_id == "new-task"

    def test_get_nodes_filter_by_to_date(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Filter nodes that have events before to_date."""
        base_time = make_event._base_time
        store.add_event(
            TaskEvent(
                task_id="old-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=1),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="new-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=5),
            )
        )

        # Filter for tasks before hour 3
        nodes, total = store.get_nodes(to_date=base_time + timedelta(hours=3))
        assert len(nodes) == 1
        assert total == 1
        assert nodes[0].task_id == "old-task"

    def test_get_nodes_filter_by_date_range(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Filter nodes within a date range."""
        base_time = make_event._base_time
        store.add_event(
            TaskEvent(
                task_id="old-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=1),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="mid-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=3),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="new-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=5),
            )
        )

        # Filter for tasks between hour 2 and hour 4
        nodes, total = store.get_nodes(
            from_date=base_time + timedelta(hours=2),
            to_date=base_time + timedelta(hours=4),
        )
        assert len(nodes) == 1
        assert total == 1
        assert nodes[0].task_id == "mid-task"

    def test_get_root_nodes_filter_by_from_date(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Filter root nodes by from_date."""
        base_time = make_event._base_time
        store.add_event(
            TaskEvent(
                task_id="old-root",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=1),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="new-root",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=5),
            )
        )

        roots, total = store.get_root_nodes(from_date=base_time + timedelta(hours=3))
        assert len(roots) == 1
        assert total == 1
        assert roots[0].task_id == "new-root"

    def test_get_root_nodes_filter_by_to_date(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Filter root nodes by to_date."""
        base_time = make_event._base_time
        store.add_event(
            TaskEvent(
                task_id="old-root",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=1),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="new-root",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=5),
            )
        )

        roots, total = store.get_root_nodes(to_date=base_time + timedelta(hours=3))
        assert len(roots) == 1
        assert total == 1
        assert roots[0].task_id == "old-root"

    def test_get_nodes_with_naive_datetime_filter(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Naive datetime filter should work with timezone-aware events.

        This simulates what happens when API receives YYYY-MM-DD dates
        which parse as naive datetimes.
        """
        base_time = make_event._base_time  # This is timezone-aware (UTC)
        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=2),
            )
        )

        # Create naive datetime (simulating API input from YYYY-MM-DD)
        naive_from = datetime(2024, 1, 1, 0, 0, 0)  # No tzinfo
        naive_to = datetime(2024, 1, 1, 23, 59, 59)  # No tzinfo

        # Should not raise TypeError
        nodes, total = store.get_nodes(from_date=naive_from, to_date=naive_to)
        assert len(nodes) == 1
        assert total == 1

    def test_get_root_nodes_with_naive_datetime_filter(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Naive datetime filter should work with timezone-aware events for roots."""
        base_time = make_event._base_time
        store.add_event(
            TaskEvent(
                task_id="root-1",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=2),
            )
        )

        naive_from = datetime(2024, 1, 1, 0, 0, 0)
        naive_to = datetime(2024, 1, 1, 23, 59, 59)

        # Should not raise TypeError
        roots, total = store.get_root_nodes(from_date=naive_from, to_date=naive_to)
        assert len(roots) == 1
        assert total == 1

    def test_to_date_midnight_includes_full_day(
        self, store: GraphStore, make_event: type
    ) -> None:
        """to_date at midnight (YYYY-MM-DD) should include events from that day.

        When API receives ?to_date=2024-01-01, it parses to 2024-01-01 00:00:00.
        But a task that ran at 2024-01-01 14:00:00 should still be included.
        """
        base_time = make_event._base_time  # 2024-01-01 00:00:00 UTC
        store.add_event(
            TaskEvent(
                task_id="afternoon-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=14),  # 2:00 PM
            )
        )

        # to_date at midnight (what YYYY-MM-DD parsing gives)
        midnight_to = datetime(2024, 1, 1, 0, 0, 0)

        # Should include the afternoon task
        nodes, total = store.get_nodes(to_date=midnight_to)
        assert len(nodes) == 1
        assert total == 1
        assert nodes[0].task_id == "afternoon-task"

    def test_to_date_midnight_excludes_next_day(
        self, store: GraphStore, make_event: type
    ) -> None:
        """to_date=2024-01-01 should exclude tasks from 2024-01-02."""
        base_time = make_event._base_time
        store.add_event(
            TaskEvent(
                task_id="jan1-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=10),  # Jan 1, 10 AM
            )
        )
        store.add_event(
            TaskEvent(
                task_id="jan2-task",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(days=1, hours=10),  # Jan 2, 10 AM
            )
        )

        # to_date=Jan 1
        jan1_midnight = datetime(2024, 1, 1, 0, 0, 0)

        nodes, total = store.get_nodes(to_date=jan1_midnight)
        assert len(nodes) == 1
        assert total == 1
        assert nodes[0].task_id == "jan1-task"


class TestGraphStoreChildren:
    def test_get_children_empty(self, store: GraphStore, make_event: type) -> None:
        store.add_event(make_event.create("parent"))
        children = store.get_children("parent")
        assert children == []

    def test_get_children_nonexistent_parent(self, store: GraphStore) -> None:
        children = store.get_children("nonexistent")
        assert children == []

    def test_get_children(self, store: GraphStore, make_event: type) -> None:
        store.add_event(make_event.create("parent"))
        store.add_event(make_event.create("child-1", parent_id="parent"))
        store.add_event(make_event.create("child-2", parent_id="parent"))

        children = store.get_children("parent")
        child_ids = [c.task_id for c in children]
        assert len(children) == 2
        assert "child-1" in child_ids
        assert "child-2" in child_ids


class TestGraphStoreSubgraph:
    def test_get_graph_from_root_nonexistent(self, store: GraphStore) -> None:
        graph = store.get_graph_from_root("nonexistent")
        assert graph == {}

    def test_get_graph_from_root_single_node(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("root"))
        graph = store.get_graph_from_root("root")
        assert len(graph) == 1
        assert "root" in graph

    def test_get_graph_from_root_tree(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("root"))
        store.add_event(make_event.create("child-1", parent_id="root"))
        store.add_event(make_event.create("child-2", parent_id="root"))
        store.add_event(make_event.create("grandchild", parent_id="child-1"))

        graph = store.get_graph_from_root("root")
        assert len(graph) == 4
        assert "root" in graph
        assert "child-1" in graph
        assert "child-2" in graph
        assert "grandchild" in graph

    def test_get_graph_from_root_excludes_other_roots(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("root-1"))
        store.add_event(make_event.create("child", parent_id="root-1"))
        store.add_event(make_event.create("root-2"))

        graph = store.get_graph_from_root("root-1")
        assert len(graph) == 2
        assert "root-2" not in graph


class TestGraphStoreListeners:
    def test_add_listener(self, store: GraphStore, make_event: type) -> None:
        events_received: list[TaskEvent] = []

        def listener(event: TaskEvent) -> None:
            events_received.append(event)

        store.add_listener(listener)
        event = make_event.create("task-1")
        store.add_event(event)

        assert len(events_received) == 1
        assert events_received[0].task_id == "task-1"

    def test_multiple_listeners(self, store: GraphStore, make_event: type) -> None:
        listener1_calls = 0
        listener2_calls = 0

        def listener1(event: TaskEvent) -> None:
            nonlocal listener1_calls
            listener1_calls += 1

        def listener2(event: TaskEvent) -> None:
            nonlocal listener2_calls
            listener2_calls += 1

        store.add_listener(listener1)
        store.add_listener(listener2)
        store.add_event(make_event.create("task-1"))

        assert listener1_calls == 1
        assert listener2_calls == 1

    def test_remove_listener(self, store: GraphStore, make_event: type) -> None:
        call_count = 0

        def listener(event: TaskEvent) -> None:
            nonlocal call_count
            call_count += 1

        store.add_listener(listener)
        store.add_event(make_event.create("task-1"))
        assert call_count == 1

        store.remove_listener(listener)
        store.add_event(make_event.create("task-2"))
        assert call_count == 1  # Should not increase

    def test_remove_nonexistent_listener(self, store: GraphStore) -> None:
        # Should not raise
        store.remove_listener(lambda e: None)

    def test_listener_exception_suppressed(
        self, store: GraphStore, make_event: type
    ) -> None:
        call_count = 0

        def bad_listener(event: TaskEvent) -> None:
            raise RuntimeError("Listener error")

        def good_listener(event: TaskEvent) -> None:
            nonlocal call_count
            call_count += 1

        store.add_listener(bad_listener)
        store.add_listener(good_listener)

        # Should not raise and second listener should be called
        store.add_event(make_event.create("task-1"))
        assert call_count == 1


class TestGraphStoreEviction:
    def test_eviction_under_limit(self, make_event: type) -> None:
        store = GraphStore(max_nodes=10)
        for i in range(5):
            store.add_event(make_event.create(f"task-{i}"))
        assert store.node_count == 5

    def test_eviction_at_limit(self, make_event: type) -> None:
        store = GraphStore(max_nodes=10)
        for i in range(10):
            store.add_event(make_event.create(f"task-{i}"))
        assert store.node_count == 10

    def test_eviction_over_limit(self, make_event: type) -> None:
        store = GraphStore(max_nodes=10)
        for i in range(15):
            store.add_event(make_event.create(f"task-{i}"))

        # Should evict down to 90% (9 nodes)
        assert store.node_count == 9

    def test_eviction_removes_oldest(self, make_event: type) -> None:
        store = GraphStore(max_nodes=10)
        for i in range(15):
            store.add_event(make_event.create(f"task-{i}"))

        # Oldest (task-0 through task-5) should be evicted
        assert store.get_node("task-0") is None
        assert store.get_node("task-5") is None
        # Newest should remain
        assert store.get_node("task-14") is not None


class TestGraphStoreSyntheticNodes:
    """Tests for synthetic GROUP/CHORD nodes in the store."""

    def test_get_root_nodes_with_synthetic_group(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Synthetic GROUP nodes (no events) should not break sorting."""
        group_id = "test-group-id"
        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.add",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time,
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.add",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time,
                group_id=group_id,
            )
        )

        # This triggers GROUP node creation
        group_node = store.get_node(f"group:{group_id}")
        assert group_node is not None
        assert group_node.node_type == NodeType.GROUP
        assert group_node.events == []  # Synthetic nodes have no events

        # get_root_nodes should not raise TypeError when sorting
        roots, _ = store.get_root_nodes()
        root_ids = [r.task_id for r in roots]
        assert f"group:{group_id}" in root_ids

    def test_get_nodes_with_synthetic_group(
        self, store: GraphStore, make_event: type
    ) -> None:
        """get_nodes should handle synthetic nodes with no events."""
        group_id = "test-group-2"
        store.add_event(
            TaskEvent(
                task_id="task-a",
                name="myapp.mul",
                state=TaskState.STARTED,
                timestamp=make_event._base_time,
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="task-b",
                name="myapp.mul",
                state=TaskState.STARTED,
                timestamp=make_event._base_time,
                group_id=group_id,
            )
        )

        # Should not raise TypeError
        nodes, _ = store.get_nodes()
        assert len(nodes) == 3  # 2 tasks + 1 synthetic group

    def test_eviction_with_synthetic_nodes(self, make_event: type) -> None:
        """Eviction sorting should handle synthetic nodes."""
        store = GraphStore(max_nodes=10)
        group_id = "eviction-group"

        # Add grouped tasks
        for idx in range(3):
            store.add_event(
                TaskEvent(
                    task_id=f"grouped-{idx}",
                    name="myapp.task",
                    state=TaskState.SUCCESS,
                    timestamp=make_event._base_time,
                    group_id=group_id,
                )
            )

        # Add more tasks to trigger eviction
        for idx in range(10):
            store.add_event(make_event.create(f"other-{idx}"))

        # Should not raise during eviction
        assert store.node_count <= 10

    def test_synthetic_nodes_sorted_by_children_timestamps(
        self, store: GraphStore
    ) -> None:
        """Synthetic nodes should sort by their children's most recent timestamp."""
        base_time = datetime(2024, 1, 1, tzinfo=UTC)

        # Create an old regular task (first)
        store.add_event(
            TaskEvent(
                task_id="old-task",
                name="old.task",
                state=TaskState.SUCCESS,
                timestamp=base_time,
            )
        )

        # Create a group with OLD children
        old_group_id = "old-group"
        store.add_event(
            TaskEvent(
                task_id="old-member-1",
                name="grouped.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(seconds=1),
                group_id=old_group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="old-member-2",
                name="grouped.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(seconds=2),
                group_id=old_group_id,
            )
        )

        # Create a group with NEW children (should appear first)
        new_group_id = "new-group"
        store.add_event(
            TaskEvent(
                task_id="new-member-1",
                name="grouped.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=1),
                group_id=new_group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="new-member-2",
                name="grouped.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=1, seconds=10),
                group_id=new_group_id,
            )
        )

        roots, _ = store.get_root_nodes()
        root_ids = [r.task_id for r in roots]

        # Most recent activity should be first (new-group)
        assert root_ids[0] == f"group:{new_group_id}"
        assert root_ids[1] == f"group:{old_group_id}"
        assert root_ids[2] == "old-task"

    def test_newly_updated_synthetic_node_appears_first(
        self, store: GraphStore
    ) -> None:
        """When a group member gets updated, the group should move to the top."""
        base_time = datetime(2024, 1, 1, tzinfo=UTC)

        # Create a group
        group_id = "my-group"
        store.add_event(
            TaskEvent(
                task_id="member-1",
                name="grouped.task",
                state=TaskState.STARTED,
                timestamp=base_time,
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="member-2",
                name="grouped.task",
                state=TaskState.STARTED,
                timestamp=base_time + timedelta(seconds=1),
                group_id=group_id,
            )
        )

        # Create a regular task later (should initially be first)
        store.add_event(
            TaskEvent(
                task_id="regular-task",
                name="regular.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=1),
            )
        )

        roots, _ = store.get_root_nodes()
        assert roots[0].task_id == "regular-task"

        # Now update a group member (much later)
        store.add_event(
            TaskEvent(
                task_id="member-1",
                name="grouped.task",
                state=TaskState.SUCCESS,
                timestamp=base_time + timedelta(hours=2),
                group_id=group_id,
            )
        )

        # Group should now be first
        roots, _ = store.get_root_nodes()
        assert roots[0].task_id == f"group:{group_id}"
