"""Tests for REST API routes."""

import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from stemtrace.core.events import RegisteredTaskDefinition, TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskNode
from stemtrace.core.groups import GroupConfig
from stemtrace.server.api import routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.api.schemas import TaskEventResponse
from stemtrace.server.store import GraphStore, WorkerRegistry
from tests.conftest import InterleavingLock

_AUTO_GROUP_OF = routes._group_lookup(GroupConfig())


@pytest.fixture
def store() -> GraphStore:
    """Create a fresh GraphStore for each test."""
    return GraphStore()


@pytest.fixture
def client(store: GraphStore) -> TestClient:
    """Create a TestClient with the API router."""
    app = FastAPI()
    router = create_api_router(store)
    app.include_router(router)
    return TestClient(app)


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
        output: str | None = None,
    ) -> TaskEvent:
        cls._counter += 1
        return TaskEvent(
            task_id=task_id,
            name=name,
            state=state,
            timestamp=cls._base_time + timedelta(seconds=cls._counter),
            parent_id=parent_id,
            output=output,
        )


@pytest.fixture
def make_event() -> type[MakeEvent]:
    """Factory for creating events with incrementing timestamps."""
    MakeEvent.reset()
    return MakeEvent


class FakeHealthConsumer:
    """Consumer stand-in exposing only what the health endpoints read."""

    def __init__(
        self,
        *,
        running: bool,
        connected: bool | None = None,
        finished: bool = False,
        stopped: bool = False,
    ) -> None:
        self.is_running = running
        self.is_connected = connected
        self.is_finished = finished
        self.is_stopped = stopped


def _live_client(store: GraphStore, consumer: FakeHealthConsumer | None) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(store, consumer=consumer))  # type: ignore[arg-type]  # duck-typed fake
    return TestClient(app)


class TestLivenessEndpoint:
    def test_live_without_consumer_returns_ok(self, store: GraphStore) -> None:
        resp = _live_client(store, None).get("/api/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_live_with_running_consumer_returns_ok(self, store: GraphStore) -> None:
        resp = _live_client(store, FakeHealthConsumer(running=True)).get(
            "/api/health/live"
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_live_with_stopped_consumer_returns_200_degraded(
        self, store: GraphStore
    ) -> None:
        resp = _live_client(store, FakeHealthConsumer(running=False)).get(
            "/api/health/live"
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "degraded"}


class TestHealthStatusAgreement:
    """``/health`` and ``/health/live`` report the same consumer status."""

    @pytest.mark.parametrize("path", ["/api/health", "/api/health/live"])
    @pytest.mark.parametrize(
        ("consumer", "expected_status"),
        [
            (FakeHealthConsumer(running=True, connected=True), "ok"),
            (FakeHealthConsumer(running=True, connected=False), "degraded"),
            (FakeHealthConsumer(running=False), "degraded"),
            (FakeHealthConsumer(running=False, stopped=True), "ok"),
            (FakeHealthConsumer(running=False, finished=True), "ok"),
        ],
        ids=["connected", "broker-down", "crashed", "stopped", "finished"],
    )
    def test_health_status_with_consumer_state_matches_on_both_endpoints(
        self,
        store: GraphStore,
        path: str,
        consumer: FakeHealthConsumer,
        expected_status: str,
    ) -> None:
        resp = _live_client(store, consumer).get(path)

        assert resp.status_code == 200
        assert resp.json()["status"] == expected_status


class TestHealthEndpoint:
    def test_health_basic(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert data["node_count"] == 0
        assert data["consumer_connected"] is None

    def test_health_with_consumer_and_ws(self, store: GraphStore) -> None:
        mock_consumer = MagicMock()
        mock_consumer.is_running = True
        mock_consumer.is_connected = False

        mock_ws_manager = MagicMock()
        mock_ws_manager.connection_count = 5

        app = FastAPI()
        router = create_api_router(
            store, consumer=mock_consumer, ws_manager=mock_ws_manager
        )
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/health")
        data = response.json()

        assert data["consumer_running"] is True
        assert data["consumer_connected"] is False
        assert data["websocket_connections"] == 5

    @pytest.mark.parametrize(
        ("running", "connected", "finished", "expected_status"),
        [
            (True, True, False, "ok"),
            (True, None, False, "ok"),
            (False, None, False, "degraded"),
            (True, False, False, "degraded"),
            (False, None, True, "ok"),
        ],
        ids=[
            "running-connected",
            "running-unknown",
            "not-running",
            "disconnected",
            "source-finished",
        ],
    )
    def test_health_status_with_consumer_state_reports_status(
        self,
        store: GraphStore,
        running: bool,
        connected: bool | None,
        finished: bool,
        expected_status: str,
    ) -> None:
        consumer = FakeHealthConsumer(
            running=running, connected=connected, finished=finished
        )
        app = FastAPI()
        app.include_router(create_api_router(store, consumer=consumer))  # type: ignore[arg-type]  # structural fake

        response = TestClient(app).get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == expected_status
        assert response.json()["consumer_running"] is running
        assert response.json()["consumer_connected"] is connected


class TestTaskListEndpoint:
    def test_list_tasks_empty(self, client: TestClient) -> None:
        response = client.get("/api/tasks")
        assert response.status_code == 200

        data = response.json()
        assert data["tasks"] == []
        assert data["total"] == 0

    def test_list_tasks_omits_output(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Output can be large; only the task detail endpoint returns it."""
        store.add_event(
            make_event.create("task-1", TaskState.SUCCESS, output="x" * 100)
        )

        data = client.get("/api/tasks").json()

        assert [e["output"] for e in data["tasks"][0]["events"]] == [None]

    def test_list_tasks(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        for i in range(5):
            store.add_event(make_event.create(f"task-{i}"))

        response = client.get("/api/tasks")
        data = response.json()

        assert len(data["tasks"]) == 5
        assert data["total"] == 5

    def test_list_tasks_with_limit(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        for i in range(10):
            store.add_event(make_event.create(f"task-{i}"))

        response = client.get("/api/tasks?limit=3")
        data = response.json()

        assert len(data["tasks"]) == 3
        assert data["limit"] == 3

    def test_list_tasks_with_offset(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        for i in range(10):
            store.add_event(make_event.create(f"task-{i}"))

        response = client.get("/api/tasks?limit=5&offset=5")
        data = response.json()

        assert data["offset"] == 5

    def test_list_tasks_filter_by_state(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", TaskState.SUCCESS))
        store.add_event(make_event.create("task-2", TaskState.FAILURE))
        store.add_event(make_event.create("task-3", TaskState.SUCCESS))

        response = client.get("/api/tasks?state=SUCCESS")
        data = response.json()

        assert len(data["tasks"]) == 2

    def test_list_tasks_filter_by_name(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))
        store.add_event(make_event.create("task-2", name="myapp.tasks.send_email"))
        store.add_event(make_event.create("task-3", name="otherapp.tasks.run"))

        response = client.get("/api/tasks?name=myapp")
        data = response.json()

        assert len(data["tasks"]) == 2

    def test_list_tasks_name_exact_excludes_names_that_extend_it(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", name="app.add"))
        store.add_event(make_event.create("task-2", name="app.add_numbers"))
        store.add_event(make_event.create("task-3", name="other.app.add"))
        store.add_event(make_event.create("task-4", name="app.Add"))
        store.add_event(make_event.create("task-5", name="app.add"))

        response = client.get("/api/tasks?name_exact=app.add")
        data = response.json()

        assert data["total"] == 2
        assert sorted(t["task_id"] for t in data["tasks"]) == ["task-1", "task-5"]
        assert {t["name"] for t in data["tasks"]} == {"app.add"}

    def test_list_tasks_name_substring_still_matches_longer_names(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", name="app.add"))
        store.add_event(make_event.create("task-2", name="app.add_numbers"))

        response = client.get("/api/tasks?name=app.add")

        assert response.json()["total"] == 2

    def test_list_tasks_name_exact_combines_with_state(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", TaskState.SUCCESS, name="app.add"))
        store.add_event(make_event.create("task-2", TaskState.FAILURE, name="app.add"))

        response = client.get("/api/tasks?name_exact=app.add&state=FAILURE")

        assert [t["task_id"] for t in response.json()["tasks"]] == ["task-2"]


class TestTaskDetailEndpoint:
    def test_get_task(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1"))

        response = client.get("/api/tasks/task-1")
        assert response.status_code == 200

        data = response.json()
        assert data["task"]["task_id"] == "task-1"

    def test_get_task_not_found(self, client: TestClient) -> None:
        response = client.get("/api/tasks/nonexistent")
        assert response.status_code == 404

    def test_get_task_includes_output_per_attempt(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1"))
        store.add_event(make_event.create("task-1", TaskState.RETRY, output="try 0"))
        store.add_event(make_event.create("task-1", TaskState.SUCCESS, output="try 1"))

        data = client.get("/api/tasks/task-1").json()

        outputs = [(e["state"], e["output"]) for e in data["task"]["events"]]
        assert outputs == [("STARTED", None), ("RETRY", "try 0"), ("SUCCESS", "try 1")]

    def test_get_task_omits_output_of_children(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("parent"))
        store.add_event(
            make_event.create(
                "child-1", TaskState.SUCCESS, parent_id="parent", output="child out"
            )
        )

        data = client.get("/api/tasks/parent").json()

        assert [e["output"] for e in data["children"][0]["events"]] == [None]

    def test_get_task_with_children(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("parent"))
        store.add_event(make_event.create("child-1", parent_id="parent"))
        store.add_event(make_event.create("child-2", parent_id="parent"))

        response = client.get("/api/tasks/parent")
        data = response.json()

        assert len(data["children"]) == 2

    def test_get_task_insert_during_serialization_returns_consistent_snapshot(
        self,
        client: TestClient,
        store: GraphStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        def event(state: TaskState, offset_ms: int) -> TaskEvent:
            return TaskEvent(
                task_id="task-1",
                name="tests.sample",
                state=state,
                timestamp=base + timedelta(milliseconds=offset_ms),
            )

        store.add_event(event(TaskState.STARTED, 100))
        store.add_event(event(TaskState.SUCCESS, 1100))
        late_pending = event(TaskState.PENDING, 0)
        inserted: list[bool] = []

        class InsertingEventResponse(TaskEventResponse):
            """Simulates the consumer inserting an event mid-serialization."""

            @classmethod
            def model_validate(cls, obj: object, **kwargs: object) -> TaskEventResponse:  # type: ignore[override]  # test hook narrows the signature
                if not inserted:
                    inserted.append(True)
                    # Lands at index 0 of the live list, shifting the rest.
                    store.add_event(late_pending)
                return TaskEventResponse.model_validate(obj)

        monkeypatch.setattr(routes, "TaskEventResponse", InsertingEventResponse)

        task = client.get("/api/tasks/task-1").json()["task"]

        assert inserted == [True]
        assert [e["state"] for e in task["events"]] == ["STARTED", "SUCCESS"]
        assert task["first_seen"] == "2024-01-01T00:00:00.100000Z"
        assert task["last_updated"] == "2024-01-01T00:00:01.100000Z"
        assert task["duration_ms"] == 1000
        # The store itself did take the late event.
        node = store.get_node("task-1")
        assert node is not None
        assert [e.state for e in node.events] == [
            TaskState.PENDING,
            TaskState.STARTED,
            TaskState.SUCCESS,
        ]

    def test_get_task_pending_stamped_after_finish_sorts_first(
        self, client: TestClient, store: GraphStore
    ) -> None:
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        def event(state: TaskState, offset_ms: int) -> TaskEvent:
            return TaskEvent(
                task_id="task-1",
                name="tests.sample",
                state=state,
                timestamp=base + timedelta(milliseconds=offset_ms),
            )

        # after_task_publish fires after publish, so PENDING can be stamped last.
        store.add_event(event(TaskState.STARTED, 10))
        store.add_event(event(TaskState.SUCCESS, 20))
        store.add_event(event(TaskState.PENDING, 25))

        task = client.get("/api/tasks/task-1").json()["task"]

        assert task["state"] == "SUCCESS"
        assert [e["state"] for e in task["events"]] == [
            "PENDING",
            "STARTED",
            "SUCCESS",
        ]
        assert task["events"][0]["timestamp"] == "2024-01-01T00:00:00.025000Z"
        assert task["first_seen"] == "2024-01-01T00:00:00.010000Z"
        assert task["last_updated"] == "2024-01-01T00:00:00.020000Z"
        assert task["duration_ms"] == 10

    def test_get_task_children_consistent_with_interleaved_writer(
        self,
        client: TestClient,
        store: GraphStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        store.add_event(
            TaskEvent(
                task_id="parent",
                name="tests.sample",
                state=TaskState.STARTED,
                timestamp=base,
            )
        )
        counter = iter(range(100))

        def add_child() -> None:
            n = next(counter)
            store.add_event(
                TaskEvent(
                    task_id=f"child-{n}",
                    name="tests.sample",
                    state=TaskState.STARTED,
                    timestamp=base + timedelta(seconds=n + 1),
                    parent_id="parent",
                )
            )

        monkeypatch.setattr(store, "_lock", InterleavingLock(store._lock, add_child))

        data = client.get("/api/tasks/parent").json()

        assert data["task"]["children"] == [c["task_id"] for c in data["children"]]
        assert data["children"] != []

    def test_get_task_with_late_pending_event_returns_terminal_state_and_timing(
        self, client: TestClient, store: GraphStore
    ) -> None:
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        def event(state: TaskState, offset_ms: int) -> TaskEvent:
            return TaskEvent(
                task_id="task-1",
                name="tests.sample",
                state=state,
                timestamp=base + timedelta(milliseconds=offset_ms),
            )

        store.add_event(event(TaskState.STARTED, 100))
        store.add_event(event(TaskState.SUCCESS, 1100))
        # Sender-side PENDING reaches the stream after the worker finished.
        store.add_event(event(TaskState.PENDING, 0))

        task = client.get("/api/tasks/task-1").json()["task"]

        assert task["state"] == "SUCCESS"
        assert [e["state"] for e in task["events"]] == [
            "PENDING",
            "STARTED",
            "SUCCESS",
        ]
        assert task["first_seen"] == "2024-01-01T00:00:00Z"
        assert task["last_updated"] == "2024-01-01T00:00:01.100000Z"
        assert task["duration_ms"] == 1100

        listed = client.get("/api/tasks?state=SUCCESS").json()
        assert [t["task_id"] for t in listed["tasks"]] == ["task-1"]
        assert listed["tasks"][0]["duration_ms"] == 1100
        assert client.get("/api/tasks?state=PENDING").json()["total"] == 0


class TestTaskChildrenEndpoint:
    def test_get_children(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("parent"))
        store.add_event(make_event.create("child-1", parent_id="parent"))

        response = client.get("/api/tasks/parent/children")
        assert response.status_code == 200

        data = response.json()
        assert len(data) == 1
        assert data[0]["task_id"] == "child-1"

    def test_get_children_not_found(self, client: TestClient) -> None:
        response = client.get("/api/tasks/nonexistent/children")
        assert response.status_code == 404


class TestGraphListEndpoint:
    def test_list_graphs_empty(self, client: TestClient) -> None:
        response = client.get("/api/graphs")
        assert response.status_code == 200

        data = response.json()
        assert data["graphs"] == []
        assert data["total"] == 0

    def test_list_graphs(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("root-1"))
        store.add_event(make_event.create("root-2"))
        store.add_event(make_event.create("child", parent_id="root-1"))

        response = client.get("/api/graphs")
        data = response.json()

        # Should only show root nodes
        assert data["total"] == 2
        root_ids = [g["task_id"] for g in data["graphs"]]
        assert "root-1" in root_ids
        assert "root-2" in root_ids
        assert "child" not in root_ids


class TestGraphDetailEndpoint:
    def test_get_graph(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("root"))
        store.add_event(make_event.create("child", parent_id="root"))

        response = client.get("/api/graphs/root")
        assert response.status_code == 200

        data = response.json()
        assert data["root_id"] == "root"
        assert "root" in data["nodes"]
        assert "child" in data["nodes"]

    def test_get_graph_not_found(self, client: TestClient) -> None:
        response = client.get("/api/graphs/nonexistent")
        assert response.status_code == 404


class TestTaskNodeResponse:
    def test_task_response_includes_duration(
        self, client: TestClient, store: GraphStore
    ) -> None:
        """Test that duration is calculated for completed tasks."""
        start_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end_time = start_time + timedelta(seconds=5)

        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="tests.sample",
                state=TaskState.STARTED,
                timestamp=start_time,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="tests.sample",
                state=TaskState.SUCCESS,
                timestamp=end_time,
            )
        )

        response = client.get("/api/tasks/task-1")
        data = response.json()

        # 5 seconds = 5000ms
        assert data["task"]["duration_ms"] == 5000

    def test_task_response_events_included(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("task-1", TaskState.PENDING))
        store.add_event(make_event.create("task-1", TaskState.STARTED))
        store.add_event(make_event.create("task-1", TaskState.SUCCESS))

        response = client.get("/api/tasks/task-1")
        data = response.json()

        assert len(data["task"]["events"]) == 3


class TestTaskRegistryEndpoint:
    """Tests for the task registry endpoint."""

    def test_registry_empty(self, client: TestClient) -> None:
        """Empty store returns empty registry."""
        response = client.get("/api/tasks/registry")
        assert response.status_code == 200

        data = response.json()
        assert data["tasks"] == []
        assert data["total"] == 0

    def test_registry_returns_unique_tasks(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Registry returns unique task names from events."""
        store.add_event(make_event.create("task-1", name="myapp.tasks.add"))
        store.add_event(make_event.create("task-2", name="myapp.tasks.multiply"))
        store.add_event(
            make_event.create("task-3", name="myapp.tasks.add")
        )  # Duplicate

        response = client.get("/api/tasks/registry")
        data = response.json()

        assert data["total"] == 2
        names = [t["name"] for t in data["tasks"]]
        assert "myapp.tasks.add" in names
        assert "myapp.tasks.multiply" in names

    def test_registry_extracts_module(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Registry extracts module from task name."""
        store.add_event(make_event.create("task-1", name="myapp.tasks.process_data"))

        response = client.get("/api/tasks/registry")
        data = response.json()

        assert data["tasks"][0]["module"] == "myapp.tasks"

    def test_registry_filter_by_query(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Registry filters tasks by query string."""
        store.add_event(make_event.create("task-1", name="myapp.tasks.add"))
        store.add_event(make_event.create("task-2", name="myapp.tasks.multiply"))
        store.add_event(make_event.create("task-3", name="otherapp.tasks.process"))

        response = client.get("/api/tasks/registry?query=myapp")
        data = response.json()

        assert data["total"] == 2
        for task in data["tasks"]:
            assert "myapp" in task["name"]

    def test_registry_sorted_alphabetically(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Registry returns tasks sorted alphabetically."""
        store.add_event(make_event.create("task-1", name="z_task"))
        store.add_event(make_event.create("task-2", name="a_task"))
        store.add_event(make_event.create("task-3", name="m_task"))

        response = client.get("/api/tasks/registry")
        data = response.json()

        names = [t["name"] for t in data["tasks"]]
        assert names == ["a_task", "m_task", "z_task"]

    def test_registry_includes_last_run(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Registry includes last_run timestamp for each task."""
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))

        response = client.get("/api/tasks/registry")
        data = response.json()

        assert data["total"] == 1
        task = data["tasks"][0]
        assert task["last_run"] is not None
        assert "2024" in task["last_run"]  # Year from base_time

    def test_registry_includes_status(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Registry includes computed status for each task."""
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))

        response = client.get("/api/tasks/registry")
        data = response.json()

        task = data["tasks"][0]
        # Without worker registry, tasks with executions are "not_registered"
        assert task["status"] == "not_registered"

    def test_registry_filter_by_status_not_registered(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Registry filters by not_registered status."""
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))
        store.add_event(make_event.create("task-2", name="myapp.tasks.analyze"))

        response = client.get("/api/tasks/registry?status=not_registered")
        data = response.json()

        assert data["total"] == 2
        for task in data["tasks"]:
            assert task["status"] == "not_registered"

    def test_registry_filter_by_status_active(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        """Registry filters by active status (no results without workers)."""
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))

        response = client.get("/api/tasks/registry?status=active")
        data = response.json()

        # Without worker registry, there are no "active" tasks
        assert data["total"] == 0


class TestTaskRegistryWithWorkers:
    """Tests for registry endpoint with WorkerRegistry integration."""

    def test_registry_active_status_with_worker(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Task with executions AND registered by worker has 'active' status."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker(
            hostname="worker-1",
            pid=12345,
            tasks=["myapp.tasks.process"],
        )

        # Add execution
        store.add_event(make_event.create("task-1", name="myapp.tasks.process"))

        # Create client with worker registry
        app = FastAPI()
        router = create_api_router(store, worker_registry=worker_registry)
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry")
        data = response.json()

        task = data["tasks"][0]
        assert task["name"] == "myapp.tasks.process"
        assert task["status"] == "active"
        assert "worker-1" in task["registered_by"]

    def test_registry_includes_task_metadata_from_worker_registry(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Registry includes docstring/signature/bound when provided by workers."""
        worker_registry = WorkerRegistry()
        task_name = "myapp.tasks.process"
        worker_registry.register_worker(
            hostname="worker-1",
            pid=12345,
            tasks=[task_name],
            task_definitions={
                task_name: RegisteredTaskDefinition(
                    name=task_name,
                    module="myapp.tasks",
                    signature="(x, y)",
                    docstring="Process data.",
                    bound=True,
                )
            },
        )

        # Add execution so task is visible in registry
        store.add_event(make_event.create("task-1", name=task_name))

        app = FastAPI()
        router = create_api_router(store, worker_registry=worker_registry)
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry")
        data = response.json()

        task = next(t for t in data["tasks"] if t["name"] == task_name)
        assert task["module"] == "myapp.tasks"
        assert task["signature"] == "(x, y)"
        assert task["docstring"] == "Process data."
        assert task["bound"] is True

    def test_registry_never_run_status_with_worker(self, store: GraphStore) -> None:
        """Task registered by worker but never executed has 'never_run' status."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker(
            hostname="worker-1",
            pid=12345,
            tasks=["myapp.tasks.new_feature"],
        )

        # No executions for this task

        app = FastAPI()
        router = create_api_router(store, worker_registry=worker_registry)
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry")
        data = response.json()

        task = data["tasks"][0]
        assert task["name"] == "myapp.tasks.new_feature"
        assert task["status"] == "never_run"
        assert task["execution_count"] == 0
        assert task["last_run"] is None

    def test_registry_filter_never_run(
        self, store: GraphStore, make_event: type
    ) -> None:
        """Filter to show only never_run tasks."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker(
            hostname="worker-1",
            pid=12345,
            tasks=["myapp.tasks.executed", "myapp.tasks.not_executed"],
        )

        # Only one task has executions
        store.add_event(make_event.create("task-1", name="myapp.tasks.executed"))

        app = FastAPI()
        router = create_api_router(store, worker_registry=worker_registry)
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry?status=never_run")
        data = response.json()

        assert data["total"] == 1
        assert data["tasks"][0]["name"] == "myapp.tasks.not_executed"
        assert data["tasks"][0]["status"] == "never_run"

    def test_registry_mixed_statuses(self, store: GraphStore, make_event: type) -> None:
        """Registry shows all three status types correctly."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker(
            hostname="worker-1",
            pid=12345,
            tasks=["myapp.tasks.active_task", "myapp.tasks.never_run_task"],
        )

        # Active task: registered and executed
        store.add_event(make_event.create("task-1", name="myapp.tasks.active_task"))
        # Not registered: executed but not registered
        store.add_event(make_event.create("task-2", name="myapp.tasks.orphan_task"))
        # Never run: registered but not executed (myapp.tasks.never_run_task)

        app = FastAPI()
        router = create_api_router(store, worker_registry=worker_registry)
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry")
        data = response.json()

        assert data["total"] == 3

        status_map = {t["name"]: t["status"] for t in data["tasks"]}
        assert status_map["myapp.tasks.active_task"] == "active"
        assert status_map["myapp.tasks.orphan_task"] == "not_registered"
        assert status_map["myapp.tasks.never_run_task"] == "never_run"


class _FakeInspect:
    """Celery Inspect stub that returns predictable worker/task state."""

    def __init__(
        self,
        *,
        ping_result: dict[str, object] | None,
        stats_result: dict[str, object] | None,
        registered_result: dict[str, object] | None,
    ) -> None:
        self._ping_result = ping_result
        self._stats_result = stats_result
        self._registered_result = registered_result

    def ping(self, timeout: float | None = None) -> dict[str, object] | None:
        del timeout
        return self._ping_result

    def stats(self, timeout: float | None = None) -> dict[str, object] | None:
        del timeout
        return self._stats_result

    def registered(self, timeout: float | None = None) -> dict[str, object] | None:
        del timeout
        return self._registered_result


class TestWorkersAndRegistryOnDemandInspect:
    """Tests for on-demand inspect refresh behavior (order-independent)."""

    def test_workers_endpoint_discovers_workers_from_inspect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Workers list is populated via inspect even if registry starts empty."""
        store = GraphStore()
        worker_registry = WorkerRegistry()

        fake = _FakeInspect(
            ping_result={"celery@worker-1": {"ok": "pong"}},
            stats_result={"celery@worker-1": {"pid": 1111}},
            registered_result={
                "celery@worker-1": ["myapp.tasks.process", "celery.backend_cleanup"]
            },
        )
        monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: fake)

        app = FastAPI()
        router = create_api_router(
            store,
            worker_registry=worker_registry,
            broker_url="amqp://guest:guest@localhost:5672//",
        )
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/workers")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        worker = data["workers"][0]
        assert worker["hostname"] == "celery@worker-1"
        assert worker["pid"] == 1111
        assert worker["status"] == "online"
        assert worker["registered_tasks"] == ["myapp.tasks.process"]

    def test_registry_endpoint_includes_never_run_tasks_from_inspect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registry shows tasks from inspect even without any executed tasks."""
        store = GraphStore()
        worker_registry = WorkerRegistry()

        fake = _FakeInspect(
            ping_result={"celery@worker-1": {"ok": "pong"}},
            stats_result={"celery@worker-1": {"pid": 1111}},
            registered_result={"celery@worker-1": ["myapp.tasks.process"]},
        )
        monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: fake)

        app = FastAPI()
        router = create_api_router(
            store,
            worker_registry=worker_registry,
            broker_url="redis://localhost:6379/0",
        )
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        task = data["tasks"][0]
        assert task["name"] == "myapp.tasks.process"
        assert task["status"] == "never_run"


class TestGetInspector:
    """Tests for _get_inspector() construction safety."""

    def test_get_inspector_returns_none_when_broker_url_missing(self) -> None:
        """_get_inspector returns None when broker_url is not provided."""
        assert routes._get_inspector(None) is None

    def test_get_inspector_passes_broker_by_keyword(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_get_inspector must not pass broker_url as Celery loader arg."""

        created: dict[str, object] = {}

        class _StubControl:
            def inspect(self) -> object:
                created["inspect_called"] = True
                return object()

        class _StubCelery:
            def __init__(self, *args: object, **kwargs: object) -> None:
                created["args"] = args
                created["kwargs"] = kwargs
                self._control = _StubControl()

            @property
            def control(self) -> _StubControl:
                return self._control

        monkeypatch.setattr(routes, "Celery", _StubCelery)

        broker_url = "amqp://guest:guest@localhost:5672//"
        inspector = routes._get_inspector(broker_url)
        assert inspector is not None
        assert created.get("inspect_called") is True

        kwargs = created["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs.get("broker") == broker_url

    def test_get_inspector_falls_back_when_timeout_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If Inspect(timeout=...) is not supported, fall back to Inspect()."""

        calls: list[object] = []

        class _StubControl:
            def inspect(self, **kwargs: object) -> object:
                calls.append(kwargs)
                if "timeout" in kwargs:
                    raise TypeError("timeout not supported")
                return object()

        class _StubCelery:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self._control = _StubControl()

            @property
            def control(self) -> _StubControl:
                return self._control

        monkeypatch.setattr(routes, "Celery", _StubCelery)

        inspector = routes._get_inspector("amqp://guest:guest@localhost:5672//")
        assert inspector is not None
        # First try uses timeout, second try is without args
        assert calls == [{"timeout": 0.5}, {}]

    def test_get_inspector_returns_none_when_constructor_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_get_inspector should never raise."""

        class _BoomCelery:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("boom")

        monkeypatch.setattr(routes, "Celery", _BoomCelery)

        assert routes._get_inspector("redis://localhost:6379/0") is None


class TestInspectRefreshBranches:
    """Targeted tests for inspect refresh edge cases (coverage on new code)."""

    def test_refresh_worker_registry_ping_fallback_returns_when_ping_empty(
        self,
    ) -> None:
        """If stats is unavailable and ping() returns nothing, refresh is a no-op."""
        worker_registry = WorkerRegistry()

        class _Inspect:
            def stats(self) -> None:
                return None

            def registered(self) -> None:
                return None

            def ping(self) -> None:
                return None

        routes._refresh_worker_registry_from_inspect(worker_registry, _Inspect())  # type: ignore[arg-type]
        assert worker_registry.get_all_workers() == []

    def test_refresh_worker_registry_ping_fallback_marks_workers_online(self) -> None:
        """If stats is unavailable but ping() responds, mark known workers online."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("celery@worker-1", 1111, ["a"])
        worker_registry.mark_offline("celery@worker-1", 1111)

        class _Inspect:
            def stats(self) -> None:
                return None

            def registered(self) -> None:
                return None

            def ping(self) -> dict[str, object]:
                return {"celery@worker-1": {"ok": "pong"}}

        routes._refresh_worker_registry_from_inspect(worker_registry, _Inspect())  # type: ignore[arg-type]

        workers = worker_registry.get_all_workers()
        assert len(workers) == 1
        assert workers[0].hostname == "celery@worker-1"
        assert workers[0].pid == 1111
        assert workers[0].status == "online"

    def test_refresh_worker_registry_keeps_existing_tasks_when_registered_unavailable(
        self,
    ) -> None:
        """If registered() fails, we keep the existing task list for that worker."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker(
            "celery@worker-1", 1111, ["myapp.tasks.process"]
        )

        class _Inspect:
            def stats(self) -> dict[str, object]:
                return {"celery@worker-1": {"pid": 1111}}

            def registered(self) -> None:
                return None

            def ping(self) -> None:
                return None

        routes._refresh_worker_registry_from_inspect(worker_registry, _Inspect())  # type: ignore[arg-type]

        workers = worker_registry.get_all_workers()
        assert len(workers) == 1
        assert workers[0].hostname == "celery@worker-1"
        assert workers[0].registered_tasks == ["myapp.tasks.process"]

    def test_refresh_worker_registry_marks_missing_workers_offline(self) -> None:
        """Workers not present in stats responders are marked offline."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("celery@worker-1", 1111, ["a"])
        worker_registry.register_worker("celery@worker-2", 2222, ["b"])

        class _Inspect:
            def stats(self) -> dict[str, object]:
                return {"celery@worker-1": {"pid": 1111}}

            def registered(self) -> dict[str, object]:
                return {"celery@worker-1": ["a"]}

            def ping(self) -> None:
                return None

        routes._refresh_worker_registry_from_inspect(worker_registry, _Inspect())  # type: ignore[arg-type]

        status_by_host = {
            w.hostname: w.status for w in worker_registry.get_all_workers()
        }
        assert status_by_host["celery@worker-1"] == "online"
        assert status_by_host["celery@worker-2"] == "offline"

    def test_refresh_worker_registry_handles_bad_stats_payloads(self) -> None:
        """Non-dict stats payloads and invalid pids are ignored."""
        worker_registry = WorkerRegistry()

        class _Inspect:
            def stats(self) -> dict[str, object]:
                return {
                    "celery@worker-1": "not-a-dict",
                    "celery@worker-2": {"pid": 0},
                    "celery@worker-3": {"pid": 3333},
                }

            def registered(self) -> dict[str, object]:
                # registered() payload for worker-3 is not a list -> treated as empty list
                return {"celery@worker-3": "not-a-list"}

            def ping(self) -> None:
                return None

        routes._refresh_worker_registry_from_inspect(worker_registry, _Inspect())  # type: ignore[arg-type]

        workers = worker_registry.get_all_workers()
        assert len(workers) == 1
        assert workers[0].hostname == "celery@worker-3"
        assert workers[0].pid == 3333
        assert workers[0].registered_tasks == []


class TestInspectRefreshUsesCeleryNodenames:
    """Refresh keys workers by Celery nodename, the name worker events carry.

    ``worker_ready``/``worker_shutdown`` publish ``WorkController.hostname``,
    which Celery 5.6.3 always sets to a nodename (``default_nodename`` in
    celery/worker/worker.py), e.g. ``w1@devbox`` for ``celery multi start w1``.
    Inspect replies are keyed by the same nodename. Values below are what a
    real ``celery multi start w1 w2`` reported.
    """

    def test_refresh_with_multi_nodes_keeps_one_online_entry_per_node(self) -> None:
        """Each node stays a single online worker, never a bare-host duplicate."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("w1@devbox", 296481, ["app.add"])
        worker_registry.register_worker("w2@devbox", 297962, ["app.add"])
        fake = _FakeInspect(
            ping_result=None,
            stats_result={
                "w1@devbox": {"pid": 296481},
                "w2@devbox": {"pid": 297962},
            },
            registered_result={"w1@devbox": ["app.add"], "w2@devbox": ["app.add"]},
        )

        routes._refresh_worker_registry_from_inspect(worker_registry, fake)  # type: ignore[arg-type]

        workers = {
            (w.hostname, w.pid): (w.status, w.registered_tasks)
            for w in worker_registry.get_all_workers()
        }
        assert workers == {
            ("w1@devbox", 296481): ("online", ["app.add"]),
            ("w2@devbox", 297962): ("online", ["app.add"]),
        }

    def test_refresh_without_registered_keeps_event_task_list(self) -> None:
        """A failed registered() reply keeps the tasks worker_ready reported."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("w1@devbox", 296481, ["app.add", "app.mul"])
        fake = _FakeInspect(
            ping_result=None,
            stats_result={"w1@devbox": {"pid": 296481}},
            registered_result=None,
        )

        routes._refresh_worker_registry_from_inspect(worker_registry, fake)  # type: ignore[arg-type]

        workers = [
            (w.hostname, w.status, w.registered_tasks)
            for w in worker_registry.get_all_workers()
        ]
        assert workers == [("w1@devbox", "online", ["app.add", "app.mul"])]

    def test_refresh_marks_only_the_silent_node_on_a_shared_host_offline(
        self,
    ) -> None:
        """Nodes sharing a host are told apart: only the missing one goes offline."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("w1@devbox", 296481, ["app.add"])
        worker_registry.register_worker("w2@devbox", 297962, ["app.add"])
        fake = _FakeInspect(
            ping_result=None,
            stats_result={"w1@devbox": {"pid": 296481}},
            registered_result={"w1@devbox": ["app.add"]},
        )

        routes._refresh_worker_registry_from_inspect(worker_registry, fake)  # type: ignore[arg-type]

        status_by_node = {
            w.hostname: w.status for w in worker_registry.get_all_workers()
        }
        assert status_by_node == {"w1@devbox": "online", "w2@devbox": "offline"}

    def test_ping_fallback_marks_node_online_by_nodename(self) -> None:
        """Without stats, a ping reply from the node brings it back online."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("w1@devbox", 296481, ["app.add"])
        worker_registry.mark_offline("w1@devbox", 296481)
        fake = _FakeInspect(
            ping_result={"w1@devbox": {"ok": "pong"}},
            stats_result=None,
            registered_result=None,
        )

        routes._refresh_worker_registry_from_inspect(worker_registry, fake)  # type: ignore[arg-type]

        workers = [(w.hostname, w.status) for w in worker_registry.get_all_workers()]
        assert workers == [("w1@devbox", "online")]

    def test_ping_fallback_leaves_silent_node_on_same_host_offline(self) -> None:
        """Without stats, only the node that answered the ping comes back online."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("w1@devbox", 296481, ["app.add"])
        worker_registry.register_worker("w2@devbox", 296482, ["app.add"])
        worker_registry.mark_offline("w1@devbox", 296481)
        worker_registry.mark_offline("w2@devbox", 296482)
        fake = _FakeInspect(
            ping_result={"w1@devbox": {"ok": "pong"}},
            stats_result=None,
            registered_result=None,
        )

        routes._refresh_worker_registry_from_inspect(worker_registry, fake)  # type: ignore[arg-type]

        status_by_node = {
            w.hostname: w.status for w in worker_registry.get_all_workers()
        }
        assert status_by_node == {"w1@devbox": "online", "w2@devbox": "offline"}


class TestRefreshOneProcessPerNodename:
    """Registry entries are keyed ``(nodename, pid)`` but inspect only names the node.

    A Celery nodename has at most one live process, so a restart leaves older
    pids of the same node dead. ``stats()`` carries the live pid; ``ping()``
    carries none, so only the entry registered last can be the live one.
    """

    @staticmethod
    def _statuses(registry: WorkerRegistry) -> dict[tuple[str, int], str]:
        return {(w.hostname, w.pid): w.status for w in registry.get_all_workers()}

    @staticmethod
    def _refresh(
        registry: WorkerRegistry,
        *,
        ping: dict[str, object] | None = None,
        stats: dict[str, object] | None = None,
    ) -> None:
        fake = _FakeInspect(
            ping_result=ping, stats_result=stats, registered_result=None
        )
        routes._refresh_worker_registry_from_inspect(registry, fake)  # type: ignore[arg-type]

    @staticmethod
    def _registry_after_graceful_restart() -> WorkerRegistry:
        """pid 100 got worker_shutdown, then pid 200 started on the same node."""
        registry = WorkerRegistry()
        registry.register_worker("celery@web-1", 100, ["app.add"])
        registry.mark_shutdown("celery@web-1", 100)
        registry.register_worker("celery@web-1", 200, ["app.add"])
        return registry

    def test_ping_after_graceful_restart_keeps_shut_down_pid_offline(self) -> None:
        """A ping reply has no pid and must not revive the pid worker_shutdown closed."""
        registry = self._registry_after_graceful_restart()

        self._refresh(registry, ping={"celery@web-1": {"ok": "pong"}})

        assert self._statuses(registry) == {
            ("celery@web-1", 100): "offline",
            ("celery@web-1", 200): "online",
        }

    def test_ping_after_crash_restart_revives_only_the_newest_pid(self) -> None:
        """pid 100 died without worker_shutdown; only pid 200 answers the ping."""
        registry = WorkerRegistry()
        registry.register_worker("celery@web-1", 100, ["app.add"])
        registry.register_worker("celery@web-1", 200, ["app.add"])
        registry.mark_offline("celery@web-1", 200)  # e.g. missed an earlier refresh

        self._refresh(registry, ping={"celery@web-1": {"ok": "pong"}})

        assert self._statuses(registry) == {
            ("celery@web-1", 100): "offline",
            ("celery@web-1", 200): "online",
        }

    def test_ping_when_newest_pid_reported_shutdown_revives_nothing(self) -> None:
        """worker_shutdown for the newest pid is final: a pid-less ping can't undo it."""
        registry = WorkerRegistry()
        registry.register_worker("celery@web-1", 100, ["app.add"])
        registry.mark_shutdown("celery@web-1", 100)

        self._refresh(registry, ping={"celery@web-1": {"ok": "pong"}})

        assert self._statuses(registry) == {("celery@web-1", 100): "offline"}

    def test_ping_revives_a_pid_that_inspect_marked_offline(self) -> None:
        """A node that missed one stats reply comes back on the next ping."""
        registry = WorkerRegistry()
        registry.register_worker("celery@web-1", 100, ["app.add"])
        registry.mark_offline("celery@web-1", 100)

        self._refresh(registry, ping={"celery@web-1": {"ok": "pong"}})

        assert self._statuses(registry) == {("celery@web-1", 100): "online"}

    def test_ping_newest_is_registration_order_not_pid_or_clock(self) -> None:
        """A restart can get a lower pid, and event clocks can be skewed."""
        registry = WorkerRegistry()
        skewed_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        registry.register_worker(
            "celery@web-1", 900, ["app.add"], event_timestamp=skewed_future
        )
        registry.register_worker("celery@web-1", 50, ["app.add"])
        registry.mark_offline("celery@web-1", 50)

        self._refresh(registry, ping={"celery@web-1": {"ok": "pong"}})

        assert self._statuses(registry) == {
            ("celery@web-1", 900): "offline",
            ("celery@web-1", 50): "online",
        }

    def test_stats_marks_crashed_pid_offline_when_node_restarted(self) -> None:
        """pid 100 died without worker_shutdown; stats names pid 200 for the node."""
        registry = WorkerRegistry()
        registry.register_worker("celery@web-1", 100, ["app.add"])

        self._refresh(registry, stats={"celery@web-1": {"pid": 200}})

        assert self._statuses(registry) == {
            ("celery@web-1", 100): "offline",
            ("celery@web-1", 200): "online",
        }

    def test_stats_with_graceful_restart_keeps_one_online_pid(self) -> None:
        """Stats agrees with the events: the shut-down pid stays offline."""
        registry = self._registry_after_graceful_restart()

        self._refresh(registry, stats={"celery@web-1": {"pid": 200}})

        assert self._statuses(registry) == {
            ("celery@web-1", 100): "offline",
            ("celery@web-1", 200): "online",
        }

    def test_stats_revives_pid_reused_after_shutdown(self) -> None:
        """The OS handed pid 100 to the restarted node: stats proves it is live."""
        registry = WorkerRegistry()
        registry.register_worker("celery@web-1", 100, ["app.add"])
        registry.mark_shutdown("celery@web-1", 100)

        self._refresh(registry, stats={"celery@web-1": {"pid": 100}})

        assert self._statuses(registry) == {("celery@web-1", 100): "online"}

    def test_stats_with_single_pid_leaves_it_online(self) -> None:
        """The plain case: one node, one pid, answers stats."""
        registry = WorkerRegistry()
        registry.register_worker("celery@web-1", 100, ["app.add"])

        self._refresh(registry, stats={"celery@web-1": {"pid": 100}})

        assert self._statuses(registry) == {("celery@web-1", 100): "online"}

    def test_ping_with_single_pid_leaves_it_online(self) -> None:
        """The plain case, ping only: one node, one pid."""
        registry = WorkerRegistry()
        registry.register_worker("celery@web-1", 100, ["app.add"])

        self._refresh(registry, ping={"celery@web-1": {"ok": "pong"}})

        assert self._statuses(registry) == {("celery@web-1", 100): "online"}

    def test_stats_with_two_multi_nodes_restarting_one_keeps_the_other(self) -> None:
        """``celery multi`` w1 and w2 share a host; restarting w1 must not touch w2."""
        registry = WorkerRegistry()
        registry.register_worker("w1@devbox", 100, ["app.add"])
        registry.register_worker("w2@devbox", 101, ["app.add"])

        self._refresh(
            registry, stats={"w1@devbox": {"pid": 200}, "w2@devbox": {"pid": 101}}
        )

        assert self._statuses(registry) == {
            ("w1@devbox", 100): "offline",
            ("w1@devbox", 200): "online",
            ("w2@devbox", 101): "online",
        }

    def test_ping_with_two_multi_nodes_restarting_one_keeps_the_other(self) -> None:
        """Ping-only multi nodes: each node's newest pid is revived independently."""
        registry = WorkerRegistry()
        registry.register_worker("w1@devbox", 100, ["app.add"])
        registry.mark_shutdown("w1@devbox", 100)
        registry.register_worker("w1@devbox", 200, ["app.add"])
        registry.register_worker("w2@devbox", 101, ["app.add"])
        registry.mark_offline("w1@devbox", 200)
        registry.mark_offline("w2@devbox", 101)

        self._refresh(registry, ping={"w1@devbox": {"ok": "pong"}})

        assert self._statuses(registry) == {
            ("w1@devbox", 100): "offline",
            ("w1@devbox", 200): "online",
            ("w2@devbox", 101): "offline",
        }

    def test_stats_without_pid_treats_node_like_a_ping_reply(self) -> None:
        """A node that answers stats but names no pid is alive; only its newest pid is."""
        registry = self._registry_after_graceful_restart()
        registry.mark_offline("celery@web-1", 200)

        self._refresh(registry, stats={"celery@web-1": {"total": {}}})

        assert self._statuses(registry) == {
            ("celery@web-1", 100): "offline",
            ("celery@web-1", 200): "online",
        }

    def test_stats_without_pid_keeps_other_silent_node_offline(self) -> None:
        """The pid-less answerer doesn't keep a node that stayed silent online."""
        registry = WorkerRegistry()
        registry.register_worker("w1@devbox", 100, ["app.add"])
        registry.register_worker("w2@devbox", 101, ["app.add"])

        self._refresh(registry, stats={"w1@devbox": {"total": {}}})

        assert self._statuses(registry) == {
            ("w1@devbox", 100): "online",
            ("w2@devbox", 101): "offline",
        }


class _InspectApplyingEvents(_FakeInspect):
    """Inspect whose slow call lets worker events reach the registry meanwhile.

    A real refresh waits up to 0.5 s for ``stats()`` and again for
    ``registered()`` (``_INSPECT_TIMEOUT_SECONDS``), while the consumer thread
    keeps applying worker events. ``during`` runs inside the named call and the
    reply it then returns is what was true before those events.
    """

    def __init__(
        self,
        *,
        stats_result: dict[str, object] | None,
        during: Callable[[], None],
        in_call: str = "stats",
    ) -> None:
        super().__init__(
            ping_result=None, stats_result=stats_result, registered_result=None
        )
        self._during = during
        self._in_call = in_call

    def stats(self, timeout: float | None = None) -> dict[str, object] | None:
        if self._in_call == "stats":
            self._during()
        return super().stats(timeout)

    def registered(self, timeout: float | None = None) -> dict[str, object] | None:
        if self._in_call == "registered":
            self._during()
        return super().registered(timeout)


class TestStaleInspectReplyLosesToNewerEvents:
    """A reply describes the node as of when inspect asked, not when it arrived.

    Source precedence per nodename: a worker event applied after the refresh
    started beats the reply (the reply predates it); a reply beats events
    applied before the refresh started (it is the fresher observation, e.g. a
    pid the OS reused after ``worker_shutdown``).
    """

    NODE = "celery@web-1"

    @staticmethod
    def _statuses(registry: WorkerRegistry) -> dict[tuple[str, int], str]:
        return {(w.hostname, w.pid): w.status for w in registry.get_all_workers()}

    @pytest.mark.parametrize("in_call", ["stats", "registered"])
    def test_stale_stats_after_restart_events_keeps_new_pid_live(
        self, in_call: str
    ) -> None:
        """worker_shutdown(A) and worker_ready(B) land while stats() is pending."""
        registry = WorkerRegistry()
        registry.register_worker(self.NODE, 100, ["app.add"])

        def restart_events() -> None:
            registry.mark_shutdown(self.NODE, 100)
            registry.register_worker(self.NODE, 200, ["app.add"])

        stale = _InspectApplyingEvents(
            stats_result={self.NODE: {"pid": 100}},
            during=restart_events,
            in_call=in_call,
        )
        routes._refresh_worker_registry_from_inspect(registry, stale)  # type: ignore[arg-type]

        expected = {(self.NODE, 100): "offline", (self.NODE, 200): "online"}
        assert self._statuses(registry) == expected

        # The stale reply must not have made A the node's newest entry either:
        # a ping-only refresh afterwards revives B, not A.
        registry.mark_offline(self.NODE, 200)
        ping = _FakeInspect(
            ping_result={self.NODE: {"ok": "pong"}},
            stats_result=None,
            registered_result=None,
        )
        routes._refresh_worker_registry_from_inspect(registry, ping)  # type: ignore[arg-type]

        assert self._statuses(registry) == expected

    def test_stale_stats_does_not_revive_a_pid_shut_down_mid_inspect(self) -> None:
        """worker_shutdown alone lands while stats() is pending: no restart yet."""
        registry = WorkerRegistry()
        registry.register_worker(self.NODE, 100, ["app.add"])

        stale = _InspectApplyingEvents(
            stats_result={self.NODE: {"pid": 100}},
            during=lambda: registry.mark_shutdown(self.NODE, 100),
        )
        routes._refresh_worker_registry_from_inspect(registry, stale)  # type: ignore[arg-type]

        assert self._statuses(registry) == {(self.NODE, 100): "offline"}

    def test_stale_stats_does_not_mark_a_node_that_just_started_offline(self) -> None:
        """A node whose worker_ready arrived mid-inspect is missing from the reply."""
        registry = WorkerRegistry()
        registry.register_worker("w1@devbox", 100, ["app.add"])

        def second_node_ready() -> None:
            registry.register_worker("w2@devbox", 101, ["app.add"])

        stale = _InspectApplyingEvents(
            stats_result={"w1@devbox": {"pid": 100}}, during=second_node_ready
        )
        routes._refresh_worker_registry_from_inspect(registry, stale)  # type: ignore[arg-type]

        assert self._statuses(registry) == {
            ("w1@devbox", 100): "online",
            ("w2@devbox", 101): "online",
        }

    def test_reply_still_wins_over_events_applied_before_the_refresh(self) -> None:
        """No event during the call: stats naming a reused pid revives it."""
        registry = WorkerRegistry()
        registry.register_worker(self.NODE, 100, ["app.add"])
        registry.mark_shutdown(self.NODE, 100)

        fresh = _FakeInspect(
            ping_result=None,
            stats_result={self.NODE: {"pid": 100}},
            registered_result=None,
        )
        routes._refresh_worker_registry_from_inspect(registry, fresh)  # type: ignore[arg-type]

        assert self._statuses(registry) == {(self.NODE, 100): "online"}

    def test_events_for_another_node_do_not_discard_the_reply(self) -> None:
        """The guard is per nodename: w2's events leave w1's reply applicable."""
        registry = WorkerRegistry()
        registry.register_worker("w1@devbox", 100, ["app.add"])
        registry.mark_offline("w1@devbox", 100)

        def other_node_ready() -> None:
            registry.register_worker("w2@devbox", 101, ["app.add"])

        inspect = _InspectApplyingEvents(
            stats_result={"w1@devbox": {"pid": 100}, "w2@devbox": {"pid": 101}},
            during=other_node_ready,
        )
        routes._refresh_worker_registry_from_inspect(registry, inspect)  # type: ignore[arg-type]

        assert self._statuses(registry) == {
            ("w1@devbox", 100): "online",
            ("w2@devbox", 101): "online",
        }


class TestInspectRateLimiting:
    """Tests for rate limiting and early-return branches in API refresh."""

    def test_workers_endpoint_refresh_is_rate_limited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Back-to-back requests should not stampede inspect."""
        store = GraphStore()
        worker_registry = WorkerRegistry()

        calls = {"n": 0}

        fake = _FakeInspect(
            ping_result={"celery@worker-1": {"ok": "pong"}},
            stats_result={"celery@worker-1": {"pid": 1111}},
            registered_result={"celery@worker-1": ["myapp.tasks.process"]},
        )

        def _fake_get_inspector(_url: str, **_: object) -> _FakeInspect:
            calls["n"] += 1
            return fake

        monkeypatch.setattr(routes, "_get_inspector", _fake_get_inspector)

        app = FastAPI()
        router = create_api_router(
            store,
            worker_registry=worker_registry,
            broker_url="redis://localhost:6379/0",
        )
        app.include_router(router)
        client = TestClient(app)

        r1 = client.get("/api/workers")
        r2 = client.get("/api/workers")

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert calls["n"] == 1

    def test_workers_endpoint_refresh_skips_when_inner_interval_check_trips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the inner rate-limit check trips, refresh should return before calling inspect."""
        store = GraphStore()
        worker_registry = WorkerRegistry()

        called = {"n": 0}

        def _fake_get_inspector(_url: str, **_: object) -> None:
            called["n"] += 1

        # Outer check passes (2.1s since last=0), inner check fails (<2s).
        values = iter([2.1, 0.1])
        monkeypatch.setattr(routes, "_monotonic", lambda: next(values))
        monkeypatch.setattr(routes, "_get_inspector", _fake_get_inspector)

        app = FastAPI()
        router = create_api_router(
            store,
            worker_registry=worker_registry,
            broker_url="redis://localhost:6379/0",
        )
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/workers")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert called["n"] == 0

    def test_workers_endpoint_refresh_skips_when_inspector_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If _get_inspector returns None, refresh should be a no-op."""
        store = GraphStore()
        worker_registry = WorkerRegistry()

        monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: None)

        app = FastAPI()
        router = create_api_router(
            store,
            worker_registry=worker_registry,
            broker_url="redis://localhost:6379/0",
        )
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/workers")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_workers_endpoint_does_not_refresh_when_refresh_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When refresh=false, the endpoint must not trigger Celery inspect."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 1111, ["myapp.tasks.process"])

        def _boom(_url: str, **_: object) -> None:
            raise AssertionError(
                "_get_inspector should not be called when refresh=false"
            )

        monkeypatch.setattr(routes, "_get_inspector", _boom)

        app = FastAPI()
        router = create_api_router(
            store,
            worker_registry=worker_registry,
            broker_url="redis://localhost:6379/0",
        )
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/workers?refresh=false")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_workers_endpoint_refresh_skips_when_lock_is_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the refresh lock is held by another request, a second request shouldn't block."""
        store = GraphStore()
        worker_registry = WorkerRegistry()

        calls = {"n": 0}
        entered = threading.Event()
        release = threading.Event()

        fake = _FakeInspect(
            ping_result={"celery@worker-1": {"ok": "pong"}},
            stats_result={"celery@worker-1": {"pid": 1111}},
            registered_result={"celery@worker-1": ["myapp.tasks.process"]},
        )

        def _blocking_get_inspector(_url: str, **_: object) -> _FakeInspect:
            calls["n"] += 1
            entered.set()
            # Hold the refresh lock long enough for a second request to arrive.
            release.wait(timeout=2.0)
            return fake

        monkeypatch.setattr(routes, "_get_inspector", _blocking_get_inspector)

        app = FastAPI()
        router = create_api_router(
            store,
            worker_registry=worker_registry,
            broker_url="redis://localhost:6379/0",
        )
        app.include_router(router)

        results: dict[str, object] = {}

        def _req1() -> None:
            client = TestClient(app)
            results["r1"] = client.get("/api/workers")

        t1 = threading.Thread(target=_req1)
        t1.start()

        assert entered.wait(timeout=2.0) is True

        # Second request should see the lock is held and return without blocking.
        client2 = TestClient(app)
        r2 = client2.get("/api/workers")

        release.set()
        t1.join(timeout=2.0)
        assert not t1.is_alive(), "First request thread did not complete"

        r1 = results.get("r1")
        assert r1 is not None, "First request did not complete"
        # Starlette TestClient returns httpx.Response; validate the request succeeded.
        assert r1.status_code == 200

        assert r2.status_code == 200
        assert calls["n"] == 1


class TestWorkersEndpointsNoRegistry:
    """Coverage for workers endpoints when worker_registry is disabled."""

    def test_workers_returns_empty_when_registry_missing(self) -> None:
        store = GraphStore()
        app = FastAPI()
        router = create_api_router(store, worker_registry=None)
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/workers")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_workers_by_hostname_returns_empty_when_registry_missing(self) -> None:
        store = GraphStore()
        app = FastAPI()
        router = create_api_router(store, worker_registry=None)
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/workers/worker-1")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestWorkersEndpointsWithRegistry:
    """Coverage for workers endpoints when worker_registry is enabled."""

    def test_workers_by_hostname_filters_when_registry_present(self) -> None:
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 1111, ["a"])
        worker_registry.register_worker("worker-2", 2222, ["b"])

        app = FastAPI()
        router = create_api_router(store, worker_registry=worker_registry)
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/workers/worker-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["workers"][0]["hostname"] == "worker-1"


class TestTaskRegistryRegisteredByAggregation:
    """Coverage for task registry registered_by aggregation edge cases."""

    def test_task_registry_merges_registered_by_across_workers(self) -> None:
        """If multiple workers register the same task, registered_by includes all hostnames."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 1111, ["myapp.tasks.process"])
        worker_registry.register_worker("worker-2", 2222, ["myapp.tasks.process"])

        app = FastAPI()
        router = create_api_router(store, worker_registry=worker_registry)
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/tasks/registry?refresh=false")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["tasks"][0]["name"] == "myapp.tasks.process"
        assert data["tasks"][0]["status"] == "never_run"
        assert data["tasks"][0]["registered_by"] == ["worker-1", "worker-2"]


class TestGraphNodeTimingResponse:
    """Coverage for synthetic node timing computation in graph responses."""

    def test_graph_node_response_timing_computed_from_children(self) -> None:
        """Synthetic nodes with no events compute timing/duration from child nodes."""
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=10)
        t2 = t0 + timedelta(seconds=25)

        child1 = TaskNode(
            task_id="child-1",
            name="myapp.tasks.child1",
            state=TaskState.SUCCESS,
            events=[
                TaskEvent(
                    task_id="child-1",
                    name="myapp.tasks.child1",
                    state=TaskState.SUCCESS,
                    timestamp=t1,
                )
            ],
        )
        child2 = TaskNode(
            task_id="child-2",
            name="myapp.tasks.child2",
            state=TaskState.SUCCESS,
            events=[
                TaskEvent(
                    task_id="child-2",
                    name="myapp.tasks.child2",
                    state=TaskState.SUCCESS,
                    timestamp=t2,
                )
            ],
        )
        group_node = TaskNode(
            task_id="group:g1",
            name="group:g1",
            state=TaskState.SUCCESS,
            node_type=NodeType.GROUP,
            children=["child-1", "child-2"],
        )

        resp = routes._node_to_graph_response(
            group_node,
            all_nodes={"child-1": child1, "child-2": child2},
            group_of=_AUTO_GROUP_OF,
        )
        assert resp.first_seen == t1
        assert resp.last_updated == t2
        assert resp.duration_ms == int((t2 - t1).total_seconds() * 1000)

    def test_graph_node_response_children_sorted_tasks_first(self) -> None:
        """Children are ordered with TASK nodes first, GROUP/CHORD nodes last."""
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)

        task_a = TaskNode(
            task_id="task-a",
            name="myapp.tasks.a",
            state=TaskState.SUCCESS,
            events=[
                TaskEvent(
                    task_id="task-a",
                    name="myapp.tasks.a",
                    state=TaskState.SUCCESS,
                    timestamp=t0,
                )
            ],
        )
        task_b = TaskNode(
            task_id="task-b",
            name="myapp.tasks.b",
            state=TaskState.SUCCESS,
            events=[
                TaskEvent(
                    task_id="task-b",
                    name="myapp.tasks.b",
                    state=TaskState.SUCCESS,
                    timestamp=t0,
                )
            ],
        )
        chord_node = TaskNode(
            task_id="group:chord-1",
            name="chord",
            state=TaskState.SUCCESS,
            node_type=NodeType.CHORD,
            children=[],
        )

        # Parent has children in "wrong" order: chord first, then tasks
        parent = TaskNode(
            task_id="root",
            name="myapp.tasks.process_uploads",
            state=TaskState.SUCCESS,
            node_type=NodeType.TASK,
            children=["group:chord-1", "task-a", "task-b"],
        )

        all_nodes = {"task-a": task_a, "task-b": task_b, "group:chord-1": chord_node}
        resp = routes._node_to_graph_response(
            parent, all_nodes=all_nodes, group_of=_AUTO_GROUP_OF
        )

        assert resp.children == ["task-a", "task-b", "group:chord-1"]


class TestResolveNodeAlias:
    """Tests for _resolve_node_alias helper."""

    def _make_node(
        self,
        args: list[object] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> TaskNode:
        """Create a TaskNode with a single event carrying given args/kwargs."""
        return TaskNode(
            task_id="test-1",
            name="myapp.tasks.process",
            state=TaskState.SUCCESS,
            events=[
                TaskEvent(
                    task_id="test-1",
                    name="myapp.tasks.process",
                    state=TaskState.SUCCESS,
                    timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    args=args,
                    kwargs=kwargs,
                )
            ],
        )

    def test_none_key_returns_task_name(self) -> None:
        """When key is None, returns the task name unchanged."""
        node = self._make_node(args=["ignored"])
        assert routes._resolve_node_alias(node, None) == "myapp.tasks.process"

    def test_digit_key_returns_args_by_index(self) -> None:
        """Digit string key extracts positional argument by index."""
        node = self._make_node(args=["upload.csv", 42])
        assert routes._resolve_node_alias(node, "0") == "upload.csv"
        assert routes._resolve_node_alias(node, "1") == "42"

    def test_string_key_returns_kwargs_value(self) -> None:
        """String key extracts keyword argument by name."""
        node = self._make_node(kwargs={"operator_type": "run_query"})
        assert routes._resolve_node_alias(node, "operator_type") == "run_query"

    def test_digit_key_out_of_range_returns_task_name(self) -> None:
        """Falls back to task name when args index is out of range."""
        node = self._make_node(args=["only_one"])
        assert routes._resolve_node_alias(node, "5") == "myapp.tasks.process"

    def test_string_key_missing_returns_task_name(self) -> None:
        """Falls back to task name when kwargs key doesn't exist."""
        node = self._make_node(kwargs={"other": "value"})
        assert routes._resolve_node_alias(node, "missing_key") == "myapp.tasks.process"

    def test_no_events_returns_task_name(self) -> None:
        """Falls back to task name when node has no events."""
        node = TaskNode(
            task_id="test-1",
            name="myapp.tasks.process",
            state=TaskState.PENDING,
        )
        assert routes._resolve_node_alias(node, "0") == "myapp.tasks.process"

    def test_none_args_returns_task_name(self) -> None:
        """Falls back to task name when args is None."""
        node = self._make_node(args=None)
        assert routes._resolve_node_alias(node, "0") == "myapp.tasks.process"

    def test_none_kwargs_returns_task_name(self) -> None:
        """Falls back to task name when kwargs is None."""
        node = self._make_node(kwargs=None)
        assert routes._resolve_node_alias(node, "some_key") == "myapp.tasks.process"

    def test_non_string_value_converted_to_str(self) -> None:
        """Non-string args/kwargs values are converted to string."""
        node = self._make_node(args=[123], kwargs={"count": 456})
        assert routes._resolve_node_alias(node, "0") == "123"
        assert routes._resolve_node_alias(node, "count") == "456"

    def test_graph_response_uses_alias(self) -> None:
        """_node_to_graph_response passes alias key through."""
        node = self._make_node(kwargs={"operator_type": "sync_table"})
        resp = routes._node_to_graph_response(
            node, node_alias_key="operator_type", group_of=_AUTO_GROUP_OF
        )
        assert resp.name == "sync_table"

    def test_alias_skips_leading_events_without_arguments(self) -> None:
        """Events without args/kwargs (e.g. RECEIVED) don't hide later ones."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        node = TaskNode(
            task_id="test-1",
            name="myapp.tasks.process",
            state=TaskState.STARTED,
            events=[
                TaskEvent(
                    task_id="test-1",
                    name="myapp.tasks.process",
                    state=state,
                    timestamp=base + timedelta(milliseconds=offset),
                    args=args,
                    kwargs=kwargs,
                )
                for state, offset, args, kwargs in [
                    (TaskState.RECEIVED, 10, None, None),
                    (TaskState.STARTED, 20, ["invoice-42"], {"customer": "acme"}),
                ]
            ],
        )

        assert routes._resolve_node_alias(node, "0") == "invoice-42"
        assert routes._resolve_node_alias(node, "customer") == "acme"

    def test_graph_api_alias_with_pending_stamped_after_received(
        self, store: GraphStore
    ) -> None:
        """The sender's late-stamped PENDING carries the args the alias needs."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for state, offset, args in [
            (TaskState.RECEIVED, 10, None),
            (TaskState.PENDING, 60, ["invoice-42"]),
        ]:
            store.add_event(
                TaskEvent(
                    task_id="root",
                    name="myapp.tasks.bill",
                    state=state,
                    timestamp=base + timedelta(milliseconds=offset),
                    args=args,
                )
            )
        app = FastAPI()
        app.include_router(create_api_router(store, node_alias_from_arguments="0"))

        response = TestClient(app).get("/api/graphs/root")

        assert response.status_code == 200
        assert response.json()["nodes"]["root"]["name"] == "invoice-42"

    def test_graph_response_without_alias_uses_task_name(self) -> None:
        """_node_to_graph_response without alias key uses task name."""
        node = self._make_node(kwargs={"operator_type": "sync_table"})
        resp = routes._node_to_graph_response(node, group_of=_AUTO_GROUP_OF)
        assert resp.name == "myapp.tasks.process"


class TestApiRouteTable:
    """The router exposes the same routes, in the same order."""

    def test_create_api_router_registers_routes_in_order(self) -> None:
        # Order matters: /tasks/registry must precede /tasks/{task_id}, or
        # "registry" would be captured as a task id.
        router = create_api_router(GraphStore(), worker_registry=WorkerRegistry())

        table = [
            (route.path, sorted(route.methods))
            for route in router.routes
            if isinstance(route, APIRoute)
        ]

        get_paths = [
            "/api/health",
            "/api/health/live",
            "/api/tasks",
            "/api/tasks/registry",
            "/api/tasks/{task_id}",
            "/api/tasks/{task_id}/children",
            "/api/graphs",
            "/api/graphs/{root_id}",
            "/api/groups",
            "/api/workers",
            "/api/workers/{hostname}",
        ]
        # Each GET route, then a HEAD route for it (kept out of the schema).
        assert table == [(path, ["GET"]) for path in get_paths] + [
            (path, ["HEAD"]) for path in get_paths
        ]
        assert len(table) == len(router.routes)
