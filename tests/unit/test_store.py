"""Tests for GraphStore."""

import json
import math
import random
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph, TaskNode
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.store import (
    GraphStore,
    WorkerRegistry,
    _eviction_units,
    _path_up,
)
from tests.conftest import InterleavingLock
from tests.graph_invariants import assert_graph_invariants

_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _task_event(
    task_id: str,
    state: TaskState,
    seconds: float,
    parent_id: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="test.task",
        state=state,
        timestamp=_T0 + timedelta(seconds=seconds),
        parent_id=parent_id,
    )


class TestGraphStoreConsistency:
    """Readers must see one consistent snapshot while the consumer writes."""

    def test_get_nodes_state_filter_consistent_with_interleaved_writer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = GraphStore()
        for i in range(5):
            store.add_event(_task_event(f"task-{i}", TaskState.STARTED, i))
        to_finish = [f"task-{i}" for i in range(5)]

        def finish_next_task() -> None:
            if to_finish:
                store.add_event(_task_event(to_finish.pop(0), TaskState.SUCCESS, 10))

        lock = InterleavingLock(store._lock, finish_next_task)
        monkeypatch.setattr(store, "_lock", lock)

        nodes, total = store.get_nodes(state=TaskState.STARTED)

        assert lock.interleavings >= 1
        assert {n.state for n in nodes} == {TaskState.STARTED}
        assert total == len(nodes)
        assert "task-0" not in {n.task_id for n in nodes}

    def test_get_nodes_state_filter_consistent_under_concurrent_writer(
        self,
    ) -> None:
        store = GraphStore()
        stop = threading.Event()
        mismatches: list[str] = []

        def writer() -> None:
            i = 0
            while not stop.is_set():
                store.add_event(_task_event(f"task-{i}", TaskState.STARTED, i))
                store.add_event(_task_event(f"task-{i}", TaskState.SUCCESS, i + 0.5))
                i += 1

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(200):
                nodes, total = store.get_nodes(state=TaskState.STARTED, limit=1000)
                mismatches.extend(
                    n.task_id for n in nodes if n.state != TaskState.STARTED
                )
                assert total == len(nodes)
        finally:
            stop.set()
            thread.join()

        assert mismatches == []

    def test_get_node_with_children_consistent_with_interleaved_writer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = GraphStore()
        store.add_event(_task_event("parent", TaskState.STARTED, 0))
        counter = iter(range(100))

        def add_child() -> None:
            n = next(counter)
            store.add_event(
                _task_event(f"child-{n}", TaskState.STARTED, 1 + n, parent_id="parent")
            )

        lock = InterleavingLock(store._lock, add_child)
        monkeypatch.setattr(store, "_lock", lock)

        found = store.get_node_with_children("parent")

        assert found is not None
        node, children = found
        assert node.children == [c.task_id for c in children]
        assert lock.interleavings == 1

    def test_get_node_with_children_missing_returns_none(self) -> None:
        assert GraphStore().get_node_with_children("missing") is None

    def test_get_root_nodes_with_children_returns_roots_and_children(self) -> None:
        store = GraphStore()
        store.add_event(_task_event("root", TaskState.STARTED, 0))
        store.add_event(_task_event("child", TaskState.SUCCESS, 1, parent_id="root"))
        store.add_event(_task_event("other-root", TaskState.SUCCESS, 2))

        roots, related, total = store.get_root_nodes_with_children(limit=1)

        assert [r.task_id for r in roots] == ["other-root"]
        assert set(related) == {"other-root"}
        assert total == 2
        roots, related, _ = store.get_root_nodes_with_children(limit=1, offset=1)
        assert [r.task_id for r in roots] == ["root"]
        assert set(related) == {"root", "child"}
        assert related["child"].state == TaskState.SUCCESS


@pytest.fixture
def store() -> GraphStore:
    """Create a fresh GraphStore for each test."""
    return GraphStore()


class MakeEvent:
    """Factory for creating events with incrementing timestamps."""

    _counter = 0
    _base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

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

    def test_get_nodes_name_exact_matches_whole_name_case_sensitively(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", name="app.add"))
        store.add_event(make_event.create("task-2", name="app.add_numbers"))
        store.add_event(make_event.create("task-3", name="x.app.add"))
        store.add_event(make_event.create("task-4", name="app.Add"))

        nodes, total = store.get_nodes(name_exact="app.add")

        assert [n.task_id for n in nodes] == ["task-1"]
        assert total == 1

    def test_get_nodes_name_exact_with_name_contains_requires_both(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", name="app.add"))
        store.add_event(make_event.create("task-2", name="app.add_numbers"))

        nodes, _ = store.get_nodes(name_exact="app.add", name_contains="numbers")
        assert nodes == []

        nodes, _ = store.get_nodes(name_exact="app.add", name_contains="ADD")
        assert [n.task_id for n in nodes] == ["task-1"]

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


class TestGraphStoreOutOfOrderEvents:
    """Store queries must use event time, not arrival order."""

    def test_get_nodes_late_pending_does_not_change_state_or_sort_order(
        self, store: GraphStore, make_event: type
    ) -> None:
        base_time = make_event._base_time

        def event(task_id: str, state: TaskState, hours: int) -> TaskEvent:
            return TaskEvent(
                task_id=task_id,
                name="test.task",
                state=state,
                timestamp=base_time + timedelta(hours=hours),
            )

        store.add_event(event("older", TaskState.STARTED, 1))
        store.add_event(event("older", TaskState.SUCCESS, 2))
        store.add_event(event("newer", TaskState.SUCCESS, 3))
        # Late sender-side PENDING for "older" arrives last.
        store.add_event(event("older", TaskState.PENDING, 0))

        nodes, total = store.get_nodes()
        assert [n.task_id for n in nodes] == ["newer", "older"]
        assert total == 2

        success, success_total = store.get_nodes(state=TaskState.SUCCESS)
        assert [n.task_id for n in success] == ["newer", "older"]
        assert success_total == 2
        _, pending_total = store.get_nodes(state=TaskState.PENDING)
        assert pending_total == 0

        # to_date uses the first event, which is now the late-arriving PENDING.
        early, _ = store.get_nodes(to_date=base_time + timedelta(minutes=30))
        assert [n.task_id for n in early] == ["older"]
        # from_date uses the last event, which is still SUCCESS at hour 2.
        late, _ = store.get_nodes(from_date=base_time + timedelta(hours=2))
        assert sorted(n.task_id for n in late) == ["newer", "older"]

        assert store.get_last_execution_time("test.task") == base_time + timedelta(
            hours=3
        )

    def test_add_event_duplicate_not_broadcast_to_listeners(
        self, store: GraphStore, make_event: type
    ) -> None:
        received: list[TaskEvent] = []
        store.add_listener(received.append)
        event = make_event.create("task-1", TaskState.SUCCESS)

        store.add_event(event)
        store.add_event(event)

        assert received == [event]
        node = store.get_node("task-1")
        assert node is not None
        assert len(node.events) == 1

    def test_add_event_naive_timestamp_listener_receives_stored_event(
        self, store: GraphStore
    ) -> None:
        received: list[TaskEvent] = []
        store.add_listener(received.append)

        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="test.task",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, 12, 0),  # naive
            )
        )

        node = store.get_node("task-1")
        assert node is not None
        assert received == node.events
        assert received[0].timestamp == datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

    def test_get_node_returns_snapshot_unaffected_by_later_inserts(
        self, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", TaskState.SUCCESS))
        snapshot = store.get_node("task-1")
        assert snapshot is not None

        store.add_event(make_event.create("task-1", TaskState.STARTED))
        store.add_event(make_event.create("child-1", parent_id="task-1"))

        assert len(snapshot.events) == 1
        assert snapshot.children == []
        live = store.get_node("task-1")
        assert live is not None
        assert len(live.events) == 2
        assert live.children == ["child-1"]

    def test_eviction_uses_earliest_event_after_out_of_order_arrival(self) -> None:
        store = GraphStore(max_nodes=2)
        base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

        def event(task_id: str, state: TaskState, hours: int) -> TaskEvent:
            return TaskEvent(
                task_id=task_id,
                name="test.task",
                state=state,
                timestamp=base_time + timedelta(hours=hours),
            )

        store.add_event(event("a", TaskState.SUCCESS, 5))
        store.add_event(event("b", TaskState.SUCCESS, 2))
        # "a" actually started first; its PENDING arrives late.
        store.add_event(event("a", TaskState.PENDING, 1))
        # Third node triggers eviction down to int(2 * 0.9) == 1 node.
        store.add_event(event("c", TaskState.SUCCESS, 4))

        assert store.get_node("a") is None
        assert store.get_node("b") is None
        assert store.get_node("c") is not None
        assert store.node_count == 1


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
        base_time = make_event._base_time  # This is timezone-aware (timezone.utc)
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
        base_time = make_event._base_time  # 2024-01-01 00:00:00 timezone.utc
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
        base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

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
        base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

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
        ts1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        ts2 = datetime(2024, 1, 1, 0, 0, 10, tzinfo=timezone.utc)

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

    def test_mark_node_alive_noops_when_node_unknown(
        self, registry: WorkerRegistry
    ) -> None:
        """mark_node_alive should return cleanly if the node isn't registered."""
        registry.mark_node_alive("missing.example.com")
        registry.mark_node_alive("missing.example.com", 12345)
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

    def test_mark_node_alive_updates_status_and_last_seen(
        self, registry: WorkerRegistry
    ) -> None:
        """A node that answered a ping comes back online with a fresh last_seen."""
        registry.register_worker(
            hostname="worker-1.example.com",
            pid=12345,
            tasks=["myapp.tasks.send"],
        )
        registry.mark_offline("worker-1.example.com", 12345)

        before = registry.get_worker("worker-1.example.com", 12345)
        assert before is not None
        before_last_seen = before.last_seen

        registry.mark_node_alive("worker-1.example.com")
        after = registry.get_worker("worker-1.example.com", 12345)
        assert after is not None
        assert after.status == WorkerStatus.ONLINE
        assert after.last_seen >= before_last_seen

    def test_mark_shutdown_sets_shutdown_reported(
        self, registry: WorkerRegistry
    ) -> None:
        """worker_shutdown is recorded; mark_offline (a missed reply) is not."""
        registry.register_worker("w1@devbox", 1, [])
        registry.register_worker("w2@devbox", 2, [])

        registry.mark_shutdown("w1@devbox", 1)
        registry.mark_offline("w2@devbox", 2)

        w1 = registry.get_worker("w1@devbox", 1)
        w2 = registry.get_worker("w2@devbox", 2)
        assert w1 is not None
        assert w2 is not None
        assert (w1.status, w1.shutdown_reported) == (WorkerStatus.OFFLINE, True)
        assert (w2.status, w2.shutdown_reported) == (WorkerStatus.OFFLINE, False)

    def test_mark_shutdown_and_mark_offline_noop_when_worker_missing(
        self, registry: WorkerRegistry
    ) -> None:
        """Events and inspect results for unknown workers create nothing."""
        registry.mark_shutdown("missing@devbox", 1)
        registry.mark_offline("missing@devbox", 1)
        assert registry.get_all_workers() == []

    def test_register_worker_clears_shutdown_reported(
        self, registry: WorkerRegistry
    ) -> None:
        """A new worker_ready for a reused pid makes the entry live again."""
        registry.register_worker("w1@devbox", 1, [])
        registry.mark_shutdown("w1@devbox", 1)

        registry.register_worker("w1@devbox", 1, [])

        w1 = registry.get_worker("w1@devbox", 1)
        assert w1 is not None
        assert (w1.status, w1.shutdown_reported) == (WorkerStatus.ONLINE, False)

    def test_mark_node_alive_without_pid_skips_shutdown_reported_entry(
        self, registry: WorkerRegistry
    ) -> None:
        """A pid-less reply can't undo a worker_shutdown event."""
        registry.register_worker("w1@devbox", 1, [])
        registry.mark_shutdown("w1@devbox", 1)

        registry.mark_node_alive("w1@devbox")

        w1 = registry.get_worker("w1@devbox", 1)
        assert w1 is not None
        assert w1.status == WorkerStatus.OFFLINE

    def test_mark_node_alive_with_pid_revives_and_retires_others(
        self, registry: WorkerRegistry
    ) -> None:
        """A reply naming the pid is authoritative for that node only."""
        registry.register_worker("w1@devbox", 1, [])
        registry.mark_shutdown("w1@devbox", 1)
        registry.register_worker("w1@devbox", 2, [])
        registry.register_worker("w2@devbox", 3, [])

        registry.mark_node_alive("w1@devbox", 1)

        statuses = {(w.hostname, w.pid): w.status for w in registry.get_all_workers()}
        assert statuses == {
            ("w1@devbox", 1): WorkerStatus.ONLINE,
            ("w1@devbox", 2): WorkerStatus.OFFLINE,
            ("w2@devbox", 3): WorkerStatus.ONLINE,
        }

    def test_removed_stale_newest_worker_is_not_revived_by_ping(
        self, registry: WorkerRegistry
    ) -> None:
        """Once the newest entry is cleaned up, its node has nothing to revive."""
        registry.register_worker(
            "w1@devbox",
            1,
            [],
            event_timestamp=datetime.now(timezone.utc) - timedelta(minutes=60),
        )
        registry.remove_stale_workers(
            stale_timeout_minutes=3, cleanup_timeout_minutes=30
        )

        registry.mark_node_alive("w1@devbox")

        assert registry.get_all_workers() == []

    def test_removed_stale_older_worker_keeps_newest_revivable_by_ping(
        self, registry: WorkerRegistry
    ) -> None:
        """Cleaning up an older pid leaves the node's newest pid as the one to revive."""
        registry.register_worker(
            "w1@devbox",
            1,
            [],
            event_timestamp=datetime.now(timezone.utc) - timedelta(minutes=60),
        )
        registry.register_worker("w1@devbox", 2, [])
        registry.remove_stale_workers(
            stale_timeout_minutes=3, cleanup_timeout_minutes=30
        )
        registry.mark_offline("w1@devbox", 2)

        registry.mark_node_alive("w1@devbox")

        workers = [(w.hostname, w.pid, w.status) for w in registry.get_all_workers()]
        assert workers == [("w1@devbox", 2, WorkerStatus.ONLINE)]

    def test_register_worker_retires_the_nodes_other_pids(
        self, registry: WorkerRegistry
    ) -> None:
        """worker_ready(B) means A is gone, even if A never sent worker_shutdown."""
        registry.register_worker("w1@devbox", 100, ["a"])
        registry.register_worker("w2@devbox", 300, ["a"])

        registry.register_worker("w1@devbox", 200, ["a"])

        statuses = {(w.hostname, w.pid): w.status for w in registry.get_all_workers()}
        assert statuses == {
            ("w1@devbox", 100): WorkerStatus.OFFLINE,
            ("w1@devbox", 200): WorkerStatus.ONLINE,
            ("w2@devbox", 300): WorkerStatus.ONLINE,
        }

    def test_register_worker_retired_pid_is_not_marked_shutdown_reported(
        self, registry: WorkerRegistry
    ) -> None:
        """Only a worker_shutdown event is final; a retired pid can be revived."""
        registry.register_worker("w1@devbox", 100, ["a"])
        registry.register_worker("w1@devbox", 200, ["a"])

        old = registry.get_worker("w1@devbox", 100)

        assert old is not None
        assert old.shutdown_reported is False

    def test_register_worker_same_pid_again_stays_online(
        self, registry: WorkerRegistry
    ) -> None:
        """Re-registering the live pid (worker_ready replay) retires nothing."""
        registry.register_worker("w1@devbox", 100, ["a"])
        registry.register_worker("w1@devbox", 100, ["a", "b"])

        statuses = {(w.hostname, w.pid): w.status for w in registry.get_all_workers()}
        assert statuses == {("w1@devbox", 100): WorkerStatus.ONLINE}

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
            except Exception as e:  # noqa: BLE001  collected and asserted empty by the test thread
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

        # A backdated event timestamp makes the worker stale without waiting: relying
        # on the clock advancing between register and check depended on timer
        # resolution.
        registry.register_worker(
            hostname="old-worker.example.com",
            pid=11111,
            tasks=["old.task"],
            event_timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

        registry.remove_stale_workers(stale_timeout_minutes=3)

        workers = registry.get_all_workers()
        # Worker should still exist but marked offline
        assert len(workers) == 1
        assert workers[0].hostname == "old-worker.example.com"
        assert workers[0].status == WorkerStatus.OFFLINE

    def test_remove_stale_workers_keeps_recent_worker_online(
        self, registry: WorkerRegistry
    ) -> None:
        """A worker seen within the stale timeout stays online and registered."""
        registry.register_worker(
            hostname="fresh-worker.example.com",
            pid=22222,
            tasks=["fresh.task"],
        )

        registry.remove_stale_workers(stale_timeout_minutes=3)

        workers = registry.get_all_workers()
        assert len(workers) == 1
        assert workers[0].hostname == "fresh-worker.example.com"
        assert workers[0].status == WorkerStatus.ONLINE

    def test_cleanup_old_offline_workers(self, registry: WorkerRegistry) -> None:
        """Very old offline workers should be removed entirely."""
        registry.register_worker(
            hostname="old-worker.example.com",
            pid=11111,
            tasks=["old.task"],
            event_timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        # First mark it offline but keep it (cleanup timeout far in the future)...
        registry.remove_stale_workers(
            stale_timeout_minutes=3, cleanup_timeout_minutes=24 * 60
        )
        workers = registry.get_all_workers()
        assert [w.status for w in workers] == [WorkerStatus.OFFLINE]

        # ...then cleanup removes workers offline for > cleanup_timeout_minutes
        registry.remove_stale_workers(
            stale_timeout_minutes=3, cleanup_timeout_minutes=30
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
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
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
            store._graph._roots[group_node_id] = None

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
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
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
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

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
        """A child that arrived before its parent is linked, then evicted cleanly."""
        store = GraphStore(max_nodes=3)
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        # Child arrives first; it is linked once its parent arrives.
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
        assert parent_node.children == ["child"]

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
        parent_node = store.get_node("parent")
        assert parent_node is not None
        assert parent_node.children == []

    def test_eviction_of_parent_lists_its_children_as_roots(self) -> None:
        store = GraphStore(max_nodes=3)
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        store.add_event(
            TaskEvent(
                task_id="parent", name="p", state=TaskState.SUCCESS, timestamp=base
            )
        )
        for i in (1, 2):
            store.add_event(
                TaskEvent(
                    task_id=f"child-{i}",
                    name="c",
                    state=TaskState.SUCCESS,
                    timestamp=base + timedelta(seconds=i),
                    parent_id="parent",
                )
            )
        store.add_event(  # 4 nodes -> 2: the parent and child-1 go
            TaskEvent(
                task_id="other",
                name="o",
                state=TaskState.SUCCESS,
                timestamp=base + timedelta(seconds=3),
            )
        )

        roots, total = store.get_root_nodes()
        assert sorted(r.task_id for r in roots) == ["child-2", "other"]
        assert total == 2
        child = store.get_node("child-2")
        assert child is not None
        assert child.parent_id is None


class TestGraphStoreEvictionGroupLinks:
    @staticmethod
    def _event(
        task_id: str, seconds: int, state: TaskState = TaskState.SUCCESS, **kw: str
    ) -> TaskEvent:
        return TaskEvent(
            task_id=task_id,
            name="tests.task",
            state=state,
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=seconds),
            **kw,
        )

    def test_add_event_after_eviction_never_links_group_under_itself(
        self,
    ) -> None:
        store = GraphStore(max_nodes=20)
        for i in range(1, 4):
            store.add_event(self._event(f"m{i}", i, group_id="g"))
        for i in range(17):
            store.add_event(self._event(f"x{i}", 10 + i))  # 21 nodes -> 17
        assert store.get_node("group:g") is None

        store.add_event(self._event("m2", 101, TaskState.FAILURE, group_id="g"))
        store.add_event(self._event("m3", 102, TaskState.SUCCESS, group_id="g"))

        group = store._graph.nodes["group:g"]
        assert group.parent_id is None
        assert group.children == ["m2", "m3"]
        assert "group:g" in store._graph.root_ids
        assert group.state == TaskState.FAILURE
        assert_graph_invariants(store._graph)

    def test_eviction_keeps_group_whole_while_its_newest_member_lives(self) -> None:
        store = GraphStore(max_nodes=20)
        store.add_event(self._event("m1", 1, group_id="g"))
        store.add_event(self._event("m2", 100, group_id="g"))
        store.add_event(self._event("m3", 101, group_id="g"))
        for i in range(17):
            store.add_event(self._event(f"x{i}", 10 + i))

        # 21 nodes -> down to 18: the group is as new as m3, so the three
        # oldest plain tasks go and m1, though older, stays with its group.
        assert [store.get_node(f"x{i}") for i in range(3)] == [None, None, None]
        group = store.get_node("group:g")
        assert group is not None
        assert group.children == ["m1", "m2", "m3"]
        assert_graph_invariants(store._graph)

    def test_eviction_keeps_group_state_of_an_evicted_failure(self) -> None:
        """Evicting a group's failed member alone made the group show SUCCESS."""
        store = GraphStore(max_nodes=10)
        store.add_event(self._event("m0", 1, TaskState.FAILURE, group_id="g"))
        for i in range(1, 4):
            store.add_event(self._event(f"m{i}", 1 + i, group_id="g"))
        for i in range(6):
            store.add_event(self._event(f"x{i}", 10 + i))

        # 11 nodes: the group (newest member at 4s) is the oldest unit and goes whole.
        assert sorted(store._graph.nodes) == [f"x{i}" for i in range(6)]
        assert_graph_invariants(store._graph)

    def test_eviction_keeps_a_group_of_chains_whole(self) -> None:
        """group(chain(a, b) ...): Celery stamps the group on each b only, and
        b hangs under its a. Evicting a failed b alone made the group SUCCESS."""
        store = GraphStore(max_nodes=10)
        for i in range(1, 5):
            store.add_event(self._event(f"a{i}", i))
        for i in range(1, 5):
            state = TaskState.FAILURE if i == 1 else TaskState.SUCCESS
            store.add_event(
                self._event(f"b{i}", 10 + i, state, group_id="g", parent_id=f"a{i}")
            )
        assert store._graph.nodes["group:g"].state == TaskState.FAILURE

        store.add_event(self._event("x0", 100))
        store.add_event(self._event("x1", 101))  # 11 nodes: the group goes whole

        assert sorted(store._graph.nodes) == ["x0", "x1"]
        assert_graph_invariants(store._graph)

    def test_eviction_of_a_chord_takes_its_callback_with_it(self) -> None:
        """Before, the oldest task (the callback) went alone, leaving the
        chord without it."""
        store = GraphStore(max_nodes=5)
        store.add_event(self._event("P", 50))
        store.add_event(self._event("cb", 1, parent_id="P"))
        store.add_event(
            self._event("h1", 60, group_id="g", chord_callback_id="cb", parent_id="P")
        )
        store.add_event(self._event("x", 70))
        # 5 nodes. The callback is the oldest task, but it belongs to the
        # chord, which is as new as h1 (60s): P goes first, then the chord
        # with h1 and cb.
        store.add_event(self._event("y", 80))  # 6 nodes -> 4, then 2

        assert sorted(store._graph.nodes) == ["x", "y"]
        assert_graph_invariants(store._graph)

    def test_eviction_splits_a_group_bigger_than_the_store_oldest_first(self) -> None:
        """A group over 90% of max_nodes can't be held whole: its oldest members go."""
        store = GraphStore(max_nodes=10)
        # Newest first, so arrival order isn't age order.
        for i in range(11):
            store.add_event(self._event(f"m{i}", 20 - i, group_id="g"))

        # At 11 nodes (10 members and the group) -> 9: the two oldest, m9
        # (11s) and m8 (12s), go; m10 brings it back to 10, the limit.
        assert sorted(store._graph.nodes) == sorted(
            ["group:g", "m10", *(f"m{i}" for i in range(8))]
        )
        assert_graph_invariants(store._graph)


_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _minutes_ago(minutes: float) -> datetime:
    return _NOW - timedelta(minutes=minutes)


class TestWorkerSeenByTaskEvents:
    """A worker's task events show it is alive (busy workers stay online)."""

    @pytest.fixture
    def registry(self) -> WorkerRegistry:
        return WorkerRegistry()

    def test_busy_worker_stays_online_past_stale_window(
        self, registry: WorkerRegistry
    ) -> None:
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(10)
        )
        for minutes in (9, 6, 3, 1):
            registry.mark_seen("celery@w1", _minutes_ago(minutes))

        registry.remove_stale_workers(now=_NOW)

        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert worker.status == WorkerStatus.ONLINE
        assert worker.last_seen == _minutes_ago(1)

    def test_worker_whose_events_stopped_goes_offline(
        self, registry: WorkerRegistry
    ) -> None:
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(10)
        )
        registry.mark_seen("celery@w1", _minutes_ago(5))

        registry.remove_stale_workers(now=_NOW)

        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert worker.status == WorkerStatus.OFFLINE

    def test_event_from_another_nodename_does_not_refresh(
        self, registry: WorkerRegistry
    ) -> None:
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(10)
        )
        registry.register_worker(
            "celery@w2", 200, ["t"], event_timestamp=_minutes_ago(10)
        )
        registry.mark_seen("celery@w2", _minutes_ago(1))

        registry.remove_stale_workers(now=_NOW)

        statuses = {w.hostname: w.status for w in registry.get_all_workers()}
        assert statuses == {
            "celery@w1": WorkerStatus.OFFLINE,
            "celery@w2": WorkerStatus.ONLINE,
        }

    def test_restarted_node_refreshes_only_its_newest_pid(
        self, registry: WorkerRegistry
    ) -> None:
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(10)
        )
        registry.register_worker(
            "celery@w1", 101, ["t"], event_timestamp=_minutes_ago(8)
        )
        registry.mark_seen("celery@w1", _minutes_ago(1))

        registry.remove_stale_workers(now=_NOW)

        old = registry.get_worker("celery@w1", 100)
        new = registry.get_worker("celery@w1", 101)
        assert old is not None
        assert new is not None
        assert (old.status, old.last_seen) == (WorkerStatus.OFFLINE, _minutes_ago(10))
        assert (new.status, new.last_seen) == (WorkerStatus.ONLINE, _minutes_ago(1))

    def test_task_event_after_worker_shutdown_does_not_revive(
        self, registry: WorkerRegistry
    ) -> None:
        """A task's last event can reach the stream after worker_shutdown."""
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(2)
        )
        registry.mark_shutdown("celery@w1", 100)
        registry.mark_seen("celery@w1", _minutes_ago(1))

        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert worker.status == WorkerStatus.OFFLINE
        assert worker.shutdown_reported is True

    def test_recent_event_brings_stale_worker_back_online(
        self, registry: WorkerRegistry
    ) -> None:
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(10)
        )
        registry.remove_stale_workers(now=_NOW)

        registry.mark_seen("celery@w1", _minutes_ago(0.5), now=_NOW)

        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert worker.status == WorkerStatus.ONLINE

    def test_replayed_old_event_keeps_stale_worker_offline(
        self, registry: WorkerRegistry
    ) -> None:
        """A server reading back an old stream must not revive dead workers."""
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(10)
        )
        registry.remove_stale_workers(now=_NOW)

        registry.mark_seen("celery@w1", _minutes_ago(6), now=_NOW)

        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert worker.status == WorkerStatus.OFFLINE
        assert worker.last_seen == _minutes_ago(6)

    def test_last_seen_never_moves_back(self, registry: WorkerRegistry) -> None:
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(10)
        )
        registry.mark_seen("celery@w1", _minutes_ago(1))
        registry.mark_seen("celery@w1", _minutes_ago(5))

        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert worker.last_seen == _minutes_ago(1)

    def test_unknown_nodename_is_ignored(self, registry: WorkerRegistry) -> None:
        registry.mark_seen("celery@ghost", _minutes_ago(1))

        assert registry.get_all_workers() == []

    @pytest.mark.parametrize("seed", range(200))
    def test_status_matches_reference_for_any_event_order_and_timing(
        self, seed: int
    ) -> None:
        """Random task events (in any order) and an optional shutdown, against
        a model: online iff no shutdown and the newest of worker_ready and the
        task events is within the stale window."""
        rng = random.Random(seed)
        registry = WorkerRegistry()
        ready = rng.uniform(0, 20)
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(ready)
        )
        seen = [rng.uniform(0, 20) for _ in range(rng.randint(0, 6))]
        shutdown_at = rng.choice([None, None, rng.randint(0, len(seen))])
        for i, minutes in enumerate(seen):
            if shutdown_at == i:
                registry.mark_shutdown("celery@w1", 100)
            registry.mark_seen("celery@w1", _minutes_ago(minutes), now=_NOW)
            if rng.random() < 0.3:
                registry.remove_stale_workers(now=_NOW)
        if shutdown_at == len(seen):
            registry.mark_shutdown("celery@w1", 100)
        registry.remove_stale_workers(now=_NOW)

        newest = min([ready, *seen])
        expected_online = shutdown_at is None and newest < 3
        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert (worker.status == WorkerStatus.ONLINE) is expected_online, (
            seed,
            ready,
            seen,
            shutdown_at,
        )
        assert worker.last_seen == _minutes_ago(newest)

    def test_event_from_clock_ahead_counts_as_now(
        self, registry: WorkerRegistry
    ) -> None:
        """A worker clock 10 minutes ahead must not keep a stopped worker
        online for 10 more minutes."""
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(1)
        )
        registry.mark_seen("celery@w1", _NOW + timedelta(minutes=10), now=_NOW)

        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert worker.last_seen == _NOW
        registry.remove_stale_workers(now=_NOW + timedelta(minutes=4))
        assert worker.status == WorkerStatus.OFFLINE

    def test_naive_event_time_is_utc(self, registry: WorkerRegistry) -> None:
        registry.register_worker(
            "celery@w1", 100, ["t"], event_timestamp=_minutes_ago(10)
        )
        naive = _minutes_ago(1).replace(tzinfo=None)

        registry.mark_seen("celery@w1", naive, now=_NOW)

        worker = registry.get_worker("celery@w1", 100)
        assert worker is not None
        assert worker.last_seen == _minutes_ago(1)


_Snapshot = dict[str, tuple[NodeType, str | None, tuple[str, ...], str | None]]


def _snapshot(nodes: dict[str, TaskNode]) -> _Snapshot:
    """Each node's type, group, children and parent, copied (add_event mutates)."""
    return {
        node_id: (node.node_type, node.group_id, tuple(node.children), node.parent_id)
        for node_id, node in nodes.items()
    }


def _drawn_in(snap: _Snapshot) -> dict[str, str]:
    """Each task drawn under a GROUP/CHORD (through tasks only) -> that container."""
    owner: dict[str, str] = {}
    for node_id, (kind, _, children, _) in snap.items():
        if kind == NodeType.TASK:
            continue
        todo = [c for c in children if c in snap]
        while todo:
            current = todo.pop()
            if snap[current][0] != NodeType.TASK or current in owner:
                continue
            owner[current] = node_id
            todo.extend(c for c in snap[current][2] if c in snap)
    return owner


def _expected_unit(snap: _Snapshot, container: str) -> set[str]:
    """What goes with a GROUP/CHORD, worked out top-down (the store walks up):
    its group's member tasks, its direct children (a nested container with its
    own unit), and the tasks under it on the way down to a member."""
    group_id = snap[container][1]
    drawn_in = _drawn_in(snap)
    # A task stamped with this group but drawn inside another container
    # belongs to that one (Celery stamps a pipeline's last task with its first
    # group's ID); one drawn at a root still belongs here (a replaced member).
    members = {
        node_id
        for node_id, (kind, group, _, _) in snap.items()
        if kind == NodeType.TASK
        and group == group_id
        and drawn_in.get(node_id, container) == container
    }

    def task_children(node_id: str) -> list[str]:
        return [
            c for c in snap[node_id][2] if c in snap and snap[c][0] == NodeType.TASK
        ]

    def leads_to_member(node_id: str, depth: int = 0) -> bool:
        return node_id in members or (
            depth < 64
            and any(leads_to_member(c, depth + 1) for c in task_children(node_id))
        )

    unit = {container} | members
    todo: list[str] = []
    for child in snap[container][2]:
        if child not in snap:
            continue
        if snap[child][0] != NodeType.TASK:
            unit |= _expected_unit(snap, child)
        else:
            unit.add(child)
            todo.append(child)
    while todo:
        for child in task_children(todo.pop()):
            if child not in unit and leads_to_member(child):
                unit.add(child)
                todo.append(child)
    return unit


def _top_units(snap: _Snapshot) -> list[set[str]]:
    """The expected unit of each GROUP/CHORD not drawn directly inside another."""
    return [
        _expected_unit(snap, node_id)
        for node_id, (kind, _, _, parent) in snap.items()
        if kind != NodeType.TASK
        and not (parent in snap and snap[parent][0] != NodeType.TASK)
    ]


def _added_unevicted(store: GraphStore, event: TaskEvent) -> _Snapshot:
    """The store's graph with ``event`` added, before any eviction."""
    graph = store._graph.model_copy(deep=True)
    graph.add_event(event)
    return _snapshot(graph.nodes)


def _assert_no_group_split(before: _Snapshot, after: _Snapshot, keep: int) -> None:
    """Eviction kept or removed each unit that fits whole (``before`` is the
    graph with the new event, before eviction)."""
    for unit in _top_units(before):
        if len(unit) > keep:
            continue  # too big to hold whole: it may lose its oldest nodes
        kept = unit & after.keys()
        assert kept in (set(), unit), (sorted(unit - kept), sorted(kept))


def _event_at(task_id: str, i: int, rng: random.Random, **kw: str) -> TaskEvent:
    state = rng.choice([TaskState.SUCCESS, TaskState.FAILURE, TaskState.STARTED])
    return TaskEvent(
        task_id=task_id,
        name="tests.task",
        state=state,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc)
        + timedelta(seconds=i + rng.choice([0, 0, -5, 5])),
        **kw,
    )


_KINDS = [
    "task",
    "child",
    "member",
    "chain_member",
    "chain3_member",
    "member_spawns_group",
    "header",
    "callback",
    "callback_starts_chord",
    "stamped_callback",
]


def _chord_events(kind: str, rng: random.Random, i: int, c: int) -> list[TaskEvent]:
    """A chord step for ``_random_events``."""
    events: list[TaskEvent]
    match kind:
        case "header":
            events = [
                _event_at(f"t{i}", i, rng, group_id=f"c{c}", chord_callback_id=f"cb{c}")
            ]
        case "callback":
            events = [_event_at(f"cb{c}", i, rng, group_id=f"c{c}")]
        case "stamped_callback":
            # chain(group, task, group, task): the last task is the new chord's
            # callback, stamped with an earlier chord's group ID.
            events = [
                _event_at(
                    f"t{i}h{n}", i, rng, group_id=f"s{i}", chord_callback_id=f"t{i}cb"
                )
                for n in range(2)
            ]
            events.append(
                _event_at(f"t{i}cb", i, rng, group_id=f"c{c}", parent_id=f"t{i}h1")
            )
        case "callback_starts_chord":
            # The callback's workflow: the next chord's headers hang from it.
            events = [
                _event_at(
                    f"t{i}h{n}",
                    i,
                    rng,
                    group_id=f"n{i}",
                    chord_callback_id=f"ncb{i}",
                    parent_id=f"cb{c}",
                )
                for n in range(2)
            ]
    return events


def _random_events(rng: random.Random, i: int, ids: list[str]) -> list[TaskEvent]:
    """One random workflow step, from the shapes Celery canvases produce."""
    kind = rng.choice(_KINDS)
    g = f"g{rng.randrange(4)}"
    c = rng.randrange(2)
    events: list[TaskEvent]
    match kind:
        case "child" if ids:
            events = [_event_at(f"t{i}", i, rng, parent_id=rng.choice(ids))]
        case "member":
            events = [_event_at(f"t{i}", i, rng, group_id=g)]
        case "chain_member" | "chain3_member":
            # group(chain(..., member)): Celery stamps the group on the last step.
            links = 2 if kind == "chain_member" else 3
            events = [_event_at(f"t{i}s0", i, rng)]
            for n in range(1, links):
                kw = {"parent_id": f"t{i}s{n - 1}"}
                if n == links - 1:
                    kw["group_id"] = g
                events.append(_event_at(f"t{i}s{n}", i, rng, **kw))
        case "member_spawns_group":
            # A member starts a group of its own: a follow-on, not part of g.
            events = [_event_at(f"t{i}", i, rng, group_id=g)] + [
                _event_at(f"t{i}i{n}", i, rng, group_id=f"in{i}", parent_id=f"t{i}")
                for n in range(2)
            ]
        case "header" | "callback" | "stamped_callback" | "callback_starts_chord":
            events = _chord_events(kind, rng, i, c)
        case _:
            events = [_event_at(f"t{i}", i, rng)]
    return events


@pytest.mark.parametrize("seed", range(150))
def test_eviction_never_splits_a_group_that_fits(seed: int) -> None:
    """Over random stores of tasks, groups (of plain tasks and of chains), groups
    started by members, and chords whose callback starts the next one: eviction
    keeps or removes each group with all that shows its state, unless that is
    more than eviction keeps (90% of max_nodes)."""
    rng = random.Random(seed)
    max_nodes = rng.randrange(8, 40)
    keep = int(max_nodes * 0.9)
    store = GraphStore(max_nodes=max_nodes)
    ids: list[str] = []
    for i in range(80):
        for event in _random_events(rng, i, ids):
            before = _added_unevicted(store, event)
            store.add_event(event)
            ids.append(event.task_id)

            assert len(store._graph.nodes) <= max_nodes, seed
            _assert_no_group_split(before, _snapshot(store._graph.nodes), keep)
            assert_graph_invariants(store._graph)


_FLOWS: dict[str, list[dict[str, object]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)


@pytest.mark.parametrize("flow", sorted(_FLOWS))
def test_eviction_never_splits_a_real_workflow_group(flow: str) -> None:
    """Every captured Celery workflow, then newer tasks until it is evicted."""
    events = [TaskEvent.model_validate(e) for e in _FLOWS[flow]]
    full = GraphStore()
    for event in events:
        full.add_event(event)
    largest = max(map(len, _top_units(_snapshot(full._graph.nodes))), default=0)
    # Just room for the largest unit, so each eviction takes a few nodes.
    max_nodes = max(10, math.ceil((largest + 2) / 0.9))
    keep = int(max_nodes * 0.9)
    store = GraphStore(max_nodes=max_nodes)
    for event in events:
        before = _added_unevicted(store, event)
        store.add_event(event)
        _assert_no_group_split(before, _snapshot(store._graph.nodes), keep)
    last = max(e.timestamp for e in events)
    for i in range(2 * max_nodes):
        filler = TaskEvent(
            task_id=f"x{i}",
            name="tests.filler",
            state=TaskState.SUCCESS,
            timestamp=last + timedelta(seconds=1 + i),
        )
        before = _added_unevicted(store, filler)
        store.add_event(filler)
        _assert_no_group_split(before, _snapshot(store._graph.nodes), keep)
    assert all(node_id.startswith("x") for node_id in store._graph.nodes)


_NESTED_FLOWS = [
    "chord_in_chord_body",
    "chord_header_fanout_group",
    "chord_group_body",
    "chain_group_group",
    "chord_group_body@celery5.2",
    "chain_group_group@celery5.2",
]


@pytest.mark.parametrize("flow", _NESTED_FLOWS)
def test_eviction_unit_of_a_nested_group_depends_on_what_holds_it(flow: str) -> None:
    """A group drawn directly inside another (a chord's group body) goes with it;
    a group a task started (a header that spawns one) is a unit of its own."""
    store = GraphStore()
    for event in _FLOWS[flow]:
        store.add_event(TaskEvent.model_validate(event))
    graph = store._graph
    unit_of = {
        n.task_id: i for i, unit in enumerate(_eviction_units(graph)) for n in unit
    }

    nested = [
        n for n in graph.nodes.values() if n.node_type != NodeType.TASK and n.parent_id
    ]
    assert nested
    for node in nested:
        parent = graph.nodes[node.parent_id]  # type: ignore[index]  # filtered above
        if parent.node_type != NodeType.TASK:
            assert unit_of[node.task_id] == unit_of[parent.task_id], flow
        else:
            assert unit_of[node.task_id] != unit_of[parent.task_id], flow


def _chord_pipeline(store: GraphStore, chords: int, *, unrelated: bool) -> None:
    """Chords whose callback starts the next one; chord 4's first header fails."""
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def add(task_id: str, second: int, **kw: str) -> None:
        state = TaskState.FAILURE if task_id == "h4_0" else TaskState.SUCCESS
        store.add_event(
            TaskEvent(
                task_id=task_id,
                name="tests.task",
                state=state,
                timestamp=t0 + timedelta(seconds=second),
                **kw,
            )
        )

    for k in range(chords):
        start = {"parent_id": f"cb{k - 1}"} if k else {}
        for n in range(3):
            add(
                f"h{k}_{n}",
                10 * k + n,
                group_id=f"c{k}",
                chord_callback_id=f"cb{k}",
                **start,
            )
        add(f"cb{k}", 10 * k + 3, group_id=f"c{k}", parent_id=f"h{k}_2")
        if unrelated:
            add(f"u{k}_0", 10 * k + 4)
            add(f"u{k}_1", 10 * k + 5)


def test_eviction_keeps_each_chord_of_a_pipeline_whole() -> None:
    """Each chord's callback starts the next: the chain of chords isn't one unit,
    so the store never drops a chord's failed header and keeps the rest."""
    store = GraphStore(max_nodes=40)
    _chord_pipeline(store, 12, unrelated=False)

    nodes = store._graph.nodes
    for k in range(12):
        chord = {f"group:c{k}", f"h{k}_0", f"h{k}_1", f"h{k}_2", f"cb{k}"}
        assert chord & nodes.keys() in (set(), chord), k
    if "group:c4" in nodes:
        assert nodes["group:c4"].state == TaskState.FAILURE


def test_eviction_takes_a_pipeline_s_oldest_chords_before_newer_tasks() -> None:
    """A long pipeline doesn't pin its first chords over newer unrelated tasks."""
    store = GraphStore(max_nodes=40)
    _chord_pipeline(store, 12, unrelated=True)

    timeline = [
        node_id for k in range(12) for node_id in (f"cb{k}", f"u{k}_0", f"u{k}_1")
    ]
    kept = [node_id in store._graph.nodes for node_id in timeline]
    assert kept == sorted(kept)  # evicted ones all come before kept ones


def test_eviction_keeps_each_inner_group_of_a_fan_out_whole() -> None:
    """Outer group members each start an inner group whose oldest member fails:
    each inner group is its own unit, so every inner group still shown is whole
    (a member arriving after its group was evicted is stored on its own)."""
    store = GraphStore(max_nodes=20)
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    second = 0
    for n in range(4):
        for i in range(5):
            if n == 0:
                store.add_event(
                    TaskEvent(
                        task_id=f"o{i}",
                        name="tests.task",
                        state=TaskState.SUCCESS,
                        timestamp=t0 + timedelta(seconds=second),
                        group_id="outer",
                    )
                )
            store.add_event(
                TaskEvent(
                    task_id=f"i{i}m{n}",
                    name="tests.task",
                    state=TaskState.FAILURE if n == 0 else TaskState.SUCCESS,
                    timestamp=t0 + timedelta(seconds=second + 1),
                    group_id=f"inner{i}",
                    parent_id=f"o{i}",
                )
            )
            second += 2

    nodes = store._graph.nodes
    shown = [i for i in range(5) if f"group:inner{i}" in nodes]
    assert shown
    for i in shown:
        assert {f"i{i}m{n}" for n in range(4)} <= nodes.keys(), i
        assert nodes[f"group:inner{i}"].state == TaskState.FAILURE


def _node(task_id: str, kind: NodeType = NodeType.TASK, **kw: str) -> TaskNode:
    return TaskNode(
        task_id=task_id,
        name="tests.task",
        state=TaskState.SUCCESS,
        node_type=kind,
        **kw,
    )


def _units_of(graph: TaskGraph) -> dict[str, int]:
    return {n.task_id: i for i, unit in enumerate(_eviction_units(graph)) for n in unit}


@pytest.mark.parametrize(
    "top", ["group:g", "group:h"], ids=["own_group", "other_group"]
)
def test_eviction_unit_of_a_member_is_the_container_it_is_drawn_in(top: str) -> None:
    """A task of group g drawn (through two chain steps) inside group h's
    container belongs to h, steps included; inside g's, to g."""
    graph = TaskGraph()
    for node in (
        _node("group:g", NodeType.GROUP, group_id="g"),
        _node("group:h", NodeType.GROUP, group_id="h"),
        _node("s1", parent_id=top),
        _node("s2", parent_id="s1"),
        _node("m", parent_id="s2", group_id="g"),
    ):
        graph.nodes[node.task_id] = node

    assert _path_up(graph, graph.nodes["m"]) == (["s2", "s1"], top)
    units = _units_of(graph)
    assert units["m"] == units["s2"] == units["s1"] == units[top]
    other = "group:h" if top == "group:g" else "group:g"
    assert units["m"] != units[other]


@pytest.mark.parametrize(
    "chain",
    [
        # s1's parent isn't stored (evicted, or never seen).
        [_node("s1", parent_id="gone"), _node("m", parent_id="s1", group_id="g")],
        # Malformed parents that loop back: the walk stops.
        [
            _node("s1", parent_id="s2"),
            _node("s2", parent_id="s1"),
            _node("m", parent_id="s2", group_id="g"),
        ],
    ],
    ids=["parent_not_stored", "parent_loop"],
)
def test_eviction_unit_keeps_a_member_drawn_under_no_container_in_its_group(
    chain: list[TaskNode],
) -> None:
    graph = TaskGraph()
    for node in [_node("group:g", NodeType.GROUP, group_id="g"), *chain]:
        graph.nodes[node.task_id] = node

    assert _path_up(graph, graph.nodes["m"]) == ([], None)
    units = _units_of(graph)
    assert units["m"] == units["group:g"]
    assert units["s1"] != units["group:g"]


def test_eviction_unit_of_a_pipeline_s_last_task_is_the_chord_it_is_drawn_in() -> None:
    """Real capture of chain(group, tsum, group, tsum, group, tsum): Celery
    stamps the last tsum with the first group's ID, and the graph draws it as
    the last chord's callback. It belongs to the last chord, so the first and
    last chords stay separate units."""
    store = GraphStore()
    for event in _FLOWS["chain_groups_tasks"]:
        store.add_event(TaskEvent.model_validate(event))
    graph = store._graph
    unit_of = {
        n.task_id: i for i, unit in enumerate(_eviction_units(graph)) for n in unit
    }
    chords = sorted(
        (n for n in graph.nodes.values() if n.node_type == NodeType.CHORD),
        key=lambda n: n.first_seen or datetime.min.replace(tzinfo=timezone.utc),
    )
    first, last = chords[0], chords[-1]
    (stamped,) = [
        n
        for n in graph.nodes.values()
        if n.node_type == NodeType.TASK
        and n.group_id == first.group_id
        and n.parent_id == last.task_id
    ]

    assert len({unit_of[c.task_id] for c in chords}) == len(chords)
    assert unit_of[stamped.task_id] == unit_of[last.task_id]
