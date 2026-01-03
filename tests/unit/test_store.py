"""Tests for GraphStore."""

from datetime import UTC, datetime, timedelta

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskNode
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.store import GraphStore, WorkerRegistry


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

    def test_get_nodes_excludes_synthetic_group(
        self, store: GraphStore, make_event: type
    ) -> None:
        """get_nodes should exclude synthetic GROUP/CHORD nodes.

        Synthetic nodes are for graph visualization only, not for the
        Tasks tab which lists actual task executions.
        """
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

        # Verify synthetic node was created
        group_node = store.get_node(f"group:{group_id}")
        assert group_node is not None
        assert group_node.node_type == NodeType.GROUP

        # get_nodes should exclude synthetic nodes
        nodes, total = store.get_nodes()
        assert len(nodes) == 2  # Only real tasks, not the synthetic group
        assert total == 2
        node_ids = [n.task_id for n in nodes]
        assert f"group:{group_id}" not in node_ids

    def test_get_unique_task_names_excludes_synthetic_nodes(
        self, store: GraphStore, make_event: type
    ) -> None:
        """get_unique_task_names should exclude synthetic node names.

        Synthetic nodes have names like 'group' or 'chord' which should
        not appear in the Registry tab.
        """
        group_id = "registry-test-group"
        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.real_task",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time,
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.real_task",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time,
                group_id=group_id,
            )
        )

        # Verify synthetic node was created
        group_node = store.get_node(f"group:{group_id}")
        assert group_node is not None
        assert group_node.name == "group"

        # get_unique_task_names should exclude synthetic names
        task_names = store.get_unique_task_names()
        assert "myapp.real_task" in task_names
        assert "group" not in task_names
        assert "chord" not in task_names

    def test_get_nodes_excludes_synthetic_chord(
        self, store: GraphStore, make_event: type
    ) -> None:
        """get_nodes should exclude CHORD nodes as well as GROUP nodes."""
        group_id = "chord-test-group"
        callback_id = "callback-task"

        # Create header tasks in chord
        store.add_event(
            TaskEvent(
                task_id="header-1",
                name="myapp.header_task",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time,
                group_id=group_id,
                chord_callback_id=callback_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="header-2",
                name="myapp.header_task",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time,
                group_id=group_id,
            )
        )

        # Create callback task
        store.add_event(
            TaskEvent(
                task_id=callback_id,
                name="myapp.callback_task",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time,
            )
        )

        # Verify CHORD node was created
        chord_node = store.get_node(f"group:{group_id}")
        assert chord_node is not None
        assert chord_node.node_type == NodeType.CHORD
        assert chord_node.name == "chord"

        # get_nodes should exclude the CHORD synthetic node
        nodes, total = store.get_nodes()
        assert total == 3  # header-1, header-2, callback - no CHORD
        node_ids = [n.task_id for n in nodes]
        assert f"group:{group_id}" not in node_ids
        assert "header-1" in node_ids
        assert "header-2" in node_ids
        assert callback_id in node_ids

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


class TestWorkerRegistry:
    """Tests for WorkerRegistry worker lifecycle tracking."""

    @pytest.fixture
    def registry(self) -> WorkerRegistry:
        """Create fresh WorkerRegistry for each test."""
        return WorkerRegistry()

    def test_register_new_worker(self, registry: WorkerRegistry) -> None:
        """Register a new worker with task list."""
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["myapp.tasks.send", "myapp.tasks.process"],
        )

        worker = registry.get_worker("worker-1.example.com", 12345)
        assert worker is not None
        assert worker.hostname == "worker-1.example.com"
        assert worker.pid == 12345
        assert len(worker.registered_tasks) == 2
        assert "myapp.tasks.send" in worker.registered_tasks
        assert "myapp.tasks.process" in worker.registered_tasks
        assert worker.status == WorkerStatus.ONLINE
        assert isinstance(worker.registered_at, datetime)

    def test_register_existing_worker_updates(self, registry: WorkerRegistry) -> None:
        """Re-registering an existing worker should update its state."""
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["myapp.tasks.send"],
        )

        # Wait a bit
        import time

        time.sleep(0.01)

        # Re-register with new PID (simulated restart)
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=54321,
            tasks=["myapp.tasks.send", "myapp.tasks.process"],
        )

        worker = registry.get_worker("worker-1.example.com", 54321)
        assert worker.pid == 54321
        assert worker.status == WorkerStatus.ONLINE  # Should still be online

    def test_register_same_worker_id_updates_in_place(
        self, registry: WorkerRegistry
    ) -> None:
        """Re-registering the same hostname+pid should update that worker entry."""
        ts1 = datetime(2024, 1, 1, tzinfo=UTC)
        ts2 = datetime(2024, 1, 1, 0, 0, 10, tzinfo=UTC)

        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["task.a"],
            event_timestamp=ts1,
        )
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["task.b"],
            event_timestamp=ts2,
        )

        worker = registry.get_worker("worker-1.example.com", 12345)
        assert worker is not None
        assert worker.registered_tasks == ["task.b"]
        assert worker.last_seen == ts2
        assert worker.status == WorkerStatus.ONLINE

    def test_mark_online_noops_when_worker_missing(
        self, registry: WorkerRegistry
    ) -> None:
        """mark_online should return cleanly if the worker isn't registered."""
        registry.mark_online("missing.example.com", 12345)
        assert registry.get_all_workers() == []

    def test_get_nonexistent_worker(self, registry: WorkerRegistry) -> None:
        """Getting a nonexistent worker returns None."""
        worker = registry.get_worker("nonexistent.example.com", 99999)
        assert worker is None

    def test_get_registered_tasks(self, registry: WorkerRegistry) -> None:
        """Get registered tasks for a specific worker."""
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["myapp.tasks.send", "myapp.tasks.process", "myapp.tasks.analyze"],
        )

        tasks = registry.get_registered_tasks("worker-1.example.com", 12345)
        assert len(tasks) == 3
        assert "myapp.tasks.send" in tasks
        assert "myapp.tasks.process" in tasks
        assert "myapp.tasks.analyze" in tasks

        # Returned list should be a copy: external mutation must not affect registry state.
        tasks.append("evil.mutate")
        tasks2 = registry.get_registered_tasks("worker-1.example.com", 12345)
        assert "evil.mutate" not in tasks2

    def test_get_all_workers_empty(self, registry: WorkerRegistry) -> None:
        """Getting all workers when none registered returns empty list."""
        workers = registry.get_all_workers()
        assert workers == []

    def test_get_all_workers_multiple(self, registry: WorkerRegistry) -> None:
        """Get all workers with multiple workers."""
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["myapp.tasks.send"],
        )
        # Small delay ensures worker-2 has later timestamp
        import time

        time.sleep(0.01)
        registry.register_worker(
            hostname="worker-2.example.com",
            pid=54321,
            tasks=["myapp.tasks.process"],
        )

        workers = registry.get_all_workers()
        assert len(workers) == 2
        # worker-2 should be first (registered later)
        assert workers[0].hostname == "worker-2.example.com"
        assert workers[1].hostname == "worker-1.example.com"

    def test_workers_sorted_by_last_seen(self, registry: WorkerRegistry) -> None:
        """Workers are sorted by last_seen (most recent first)."""
        import time

        registry.register_worker(
            hostname="worker-1.example.com",
            pid=11111,
            tasks=["task-a"],
        )
        time.sleep(0.1)  # Ensure time difference
        registry.register_worker(
            hostname="worker-2.example.com",
            pid=22222,
            tasks=["task-b"],
        )
        time.sleep(0.01)

        workers = registry.get_all_workers()
        assert workers[0].hostname == "worker-2.example.com"  # Most recent
        assert workers[1].hostname == "worker-1.example.com"  # Older

    def test_mark_shutdown(self, registry: WorkerRegistry) -> None:
        """Marking a worker as offline."""
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["myapp.tasks.send"],
        )

        registry.mark_shutdown("worker-1.example.com", 12345)

        worker = registry.get_worker("worker-1.example.com", 12345)
        assert worker is not None
        assert worker.status == WorkerStatus.OFFLINE

    def test_mark_online_updates_status_and_last_seen(
        self, registry: WorkerRegistry
    ) -> None:
        """Marking an existing worker as online updates status and last_seen."""
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["myapp.tasks.send"],
        )
        registry.mark_shutdown("worker-1.example.com", 12345)

        before = registry.get_worker("worker-1.example.com", 12345)
        assert before is not None
        before_last_seen = before.last_seen

        registry.mark_online("worker-1.example.com", 12345)
        after = registry.get_worker("worker-1.example.com", 12345)
        assert after is not None
        assert after.status == WorkerStatus.ONLINE
        assert after.last_seen >= before_last_seen

    def test_get_registered_tasks_after_shutdown(
        self, registry: WorkerRegistry
    ) -> None:
        """Registered tasks are still available after worker shutdown."""
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["myapp.tasks.send"],
        )
        registry.mark_shutdown("worker-1.example.com", 12345)

        # Tasks should still be accessible
        tasks = registry.get_registered_tasks("worker-1.example.com", 12345)
        assert len(tasks) == 1
        assert "myapp.tasks.send" in tasks

    def test_thread_safety_with_concurrent_access(
        self, registry: WorkerRegistry
    ) -> None:
        """Registry handles concurrent access safely."""
        import threading

        results = []
        errors = []

        def register_worker(hostname: str, pid: int) -> None:
            try:
                registry.register_worker(
                    hostname=hostname,
                    pid=pid,
                    tasks=[f"{hostname}.task"],
                )
                results.append(True)
            except Exception as e:
                errors.append(e)

        # Launch threads concurrently
        threads = [
            threading.Thread(
                target=register_worker, args=(f"worker-{i}.com", 10000 + i)
            )
            for i in range(10)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        # All should succeed without errors
        assert len(results) == 10
        assert len(errors) == 0

    def test_remove_stale_workers(self, registry: WorkerRegistry) -> None:
        """Mark workers as stale if not seen recently."""

        registry.register_worker(
            hostname="old-worker.example.com",
            pid=11111,
            tasks=["old.task"],
        )

        # Mark as stale (timeout = 0 for immediate effect)
        registry.remove_stale_workers(stale_timeout_minutes=0)

        workers = registry.get_all_workers()
        # Worker should still exist but marked offline
        assert len(workers) == 1
        assert workers[0].hostname == "old-worker.example.com"
        assert workers[0].status == "offline"

    def test_cleanup_old_offline_workers(self, registry: WorkerRegistry) -> None:
        """Very old offline workers should be removed entirely."""
        from datetime import timedelta, timezone

        registry.register_worker(
            hostname="old-worker.example.com",
            pid=11111,
            tasks=["old.task"],
        )

        # Manually set last_seen to a long time ago
        with registry._lock:
            worker = registry._workers["old-worker.example.com:11111"]
            worker.last_seen = datetime.now(timezone.utc) - timedelta(hours=2)
            worker.status = WorkerStatus.OFFLINE

        # Cleanup should remove workers offline for > cleanup_timeout_minutes
        registry.remove_stale_workers(
            stale_timeout_minutes=0, cleanup_timeout_minutes=0
        )

        workers = registry.get_all_workers()
        # Worker should be completely removed
        assert len(workers) == 0

    def test_register_worker_ignores_invalid_pid(
        self, registry: WorkerRegistry
    ) -> None:
        """Workers with invalid PID (0 or negative) should be ignored."""
        registry.register_worker(hostname="bad-worker", pid=0, tasks=["some.task"])
        registry.register_worker(hostname="bad-worker2", pid=-1, tasks=["some.task"])

        workers = registry.get_all_workers()
        assert len(workers) == 0


class TestGraphStoreLastExecutionTime:
    """Tests for get_last_execution_time method."""

    def test_last_execution_time_empty_store(self, store: GraphStore) -> None:
        """Return None for a task that doesn't exist."""
        result = store.get_last_execution_time("nonexistent.task")
        assert result is None

    def test_last_execution_time_single_execution(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Return timestamp for a single execution."""
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))

        result = store.get_last_execution_time("myapp.tasks.process")
        assert result is not None
        # Should be close to the base time + 1 second (first event)
        assert result.year == 2024

    def test_last_execution_time_multiple_executions(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Return the most recent timestamp across multiple executions."""
        # Create three executions of the same task (incrementing timestamps)
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))
        store.add_event(make_event.create("task-2", name="myapp.tasks.process"))
        store.add_event(make_event.create("task-3", name="myapp.tasks.process"))

        result = store.get_last_execution_time("myapp.tasks.process")
        assert result is not None

        # Should be the most recent (task-3's timestamp)
        node = store.get_node("task-3")
        assert node is not None
        assert result == node.events[-1].timestamp

    def test_last_execution_time_with_updates(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Return latest event timestamp when task has multiple events."""
        # Task goes through PENDING -> STARTED -> SUCCESS
        store.add_event(
            make_event.create("task-1", TaskState.PENDING, name="myapp.tasks.process")
        )
        store.add_event(
            make_event.create("task-1", TaskState.STARTED, name="myapp.tasks.process")
        )
        store.add_event(
            make_event.create("task-1", TaskState.SUCCESS, name="myapp.tasks.process")
        )

        result = store.get_last_execution_time("myapp.tasks.process")
        assert result is not None

        # Should be the SUCCESS event timestamp
        node = store.get_node("task-1")
        assert node is not None
        assert result == node.events[-1].timestamp  # Last event

    def test_last_execution_time_excludes_synthetic_nodes(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Synthetic GROUP/CHORD nodes should not affect last execution time."""
        group_id = "test-group"
        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="myapp.grouped_task",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time,
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="task-2",
                name="myapp.grouped_task",
                state=TaskState.SUCCESS,
                timestamp=make_event._base_time + timedelta(seconds=1),
                group_id=group_id,
            )
        )

        # Verify synthetic node was created
        group_node = store.get_node(f"group:{group_id}")
        assert group_node is not None
        assert group_node.name == "group"

        # get_last_execution_time for "group" should return None
        # (synthetic nodes shouldn't count as executions)
        result = store.get_last_execution_time("group")
        assert result is None

        # Real task should have a last execution time
        result = store.get_last_execution_time("myapp.grouped_task")
        assert result is not None

    def test_last_execution_time_different_tasks(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Different task names return different timestamps."""
        store.add_event(make_event.create("task-a", name="myapp.task_a"))
        store.add_event(make_event.create("task-b", name="myapp.task_b"))
        store.add_event(
            make_event.create("task-c", name="myapp.task_a")
        )  # Another execution

        result_a = store.get_last_execution_time("myapp.task_a")
        result_b = store.get_last_execution_time("myapp.task_b")

        assert result_a is not None
        assert result_b is not None
        assert result_a > result_b  # task_a had a later execution


class TestGraphStoreRootNodesSyntheticDateFiltering:
    def test_get_root_nodes_to_date_filters_synthetic_by_earliest_child_timestamp(
        self, store: GraphStore
    ) -> None:
        """Synthetic GROUP roots should be filtered by the earliest child timestamp."""
        base = datetime(2024, 1, 1, tzinfo=UTC)
        group_id = "date-filter-group"

        # Two grouped tasks that both start on Jan 2 (so Jan 1 filter should exclude the group)
        store.add_event(
            TaskEvent(
                task_id="m1",
                name="myapp.tasks.member",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(days=1, hours=10),
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="m2",
                name="myapp.tasks.member",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(days=1, hours=12),
                group_id=group_id,
            )
        )

        group_node_id = f"group:{group_id}"
        roots, _ = store.get_root_nodes()
        assert group_node_id in [r.task_id for r in roots]

        jan1_midnight = datetime(2024, 1, 1, 0, 0, 0)
        roots, total = store.get_root_nodes(to_date=jan1_midnight)
        assert total == 0
        assert roots == []

    def test_get_root_nodes_handles_synthetic_with_missing_children(
        self, store: GraphStore
    ) -> None:
        """Synthetic nodes with missing children should not break sorting or filtering."""
        group_id = "bad"
        group_node_id = f"group:{group_id}"

        with store._lock:
            store._graph.nodes[group_node_id] = TaskNode(
                task_id=group_node_id,
                name="group",
                state=TaskState.PENDING,
                node_type=NodeType.GROUP,
                group_id=group_id,
                children=["missing-child"],
            )
            store._graph.root_ids.append(group_node_id)

        roots, total = store.get_root_nodes()
        assert total == 1
        assert roots[0].task_id == group_node_id

        roots, total = store.get_root_nodes(to_date=datetime(2024, 1, 1, 0, 0, 0))
        assert total == 1
        assert roots[0].task_id == group_node_id


class TestGraphStoreGetGraphFromRootEdgeCases:
    def test_get_graph_from_root_skips_missing_nodes_and_prevents_cycles(
        self, store: GraphStore
    ) -> None:
        """get_graph_from_root should ignore missing children and avoid infinite loops."""
        base = datetime(2024, 1, 1, tzinfo=UTC)
        store.add_event(
            TaskEvent(
                task_id="root",
                name="root.task",
                state=TaskState.SUCCESS,
                timestamp=base,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="child",
                name="child.task",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(seconds=1),
                parent_id="root",
            )
        )

        root_node = store.get_node("root")
        assert root_node is not None
        root_node.children.append("ghost")
        root_node.children.append("root")

        graph = store.get_graph_from_root("root")
        assert "root" in graph
        assert "child" in graph
        assert "ghost" not in graph


class TestGraphStoreEvictionParentCleanup:
    def test_eviction_removes_evicted_child_from_parent_children(self) -> None:
        """When a child is evicted, it should be removed from its parent's children list."""
        store = GraphStore(max_nodes=3)
        base = datetime(2024, 1, 1, tzinfo=UTC)

        store.add_event(
            TaskEvent(
                task_id="oldest",
                name="oldest.task",
                state=TaskState.SUCCESS,
                timestamp=base,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="parent",
                name="parent.task",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(seconds=10),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="child",
                name="child.task",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(seconds=1),
                parent_id="parent",
            )
        )

        parent_node = store.get_node("parent")
        assert parent_node is not None
        assert "child" in parent_node.children

        store.add_event(
            TaskEvent(
                task_id="newest",
                name="newest.task",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(seconds=20),
            )
        )

        assert store.get_node("child") is None
        parent_node = store.get_node("parent")
        assert parent_node is not None
        assert "child" not in parent_node.children

    def test_eviction_does_not_require_child_in_parent_children(self) -> None:
        """Eviction should handle nodes with parent_id that were never linked as children."""
        store = GraphStore(max_nodes=3)
        base = datetime(2024, 1, 1, tzinfo=UTC)

        # Child arrives first referencing a missing parent => no backlinking occurs.
        store.add_event(
            TaskEvent(
                task_id="child",
                name="child.task",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(seconds=1),
                parent_id="parent",
            )
        )
        store.add_event(
            TaskEvent(
                task_id="parent",
                name="parent.task",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(seconds=10),
            )
        )

        parent_node = store.get_node("parent")
        assert parent_node is not None
        assert "child" not in parent_node.children

        store.add_event(
            TaskEvent(
                task_id="oldest",
                name="oldest.task",
                state=TaskState.SUCCESS,
                timestamp=base,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="newest",
                name="newest.task",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(seconds=20),
            )
        )

        assert store.get_node("child") is None
