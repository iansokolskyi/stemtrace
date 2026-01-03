"""Tests for REST API routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import RegisteredTaskDefinition, TaskEvent, TaskState
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry


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


class TestHealthEndpoint:
    def test_health_basic(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert data["node_count"] == 0

    def test_health_with_consumer_and_ws(self, store: GraphStore) -> None:
        mock_consumer = MagicMock()
        mock_consumer.is_running = True

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
        assert data["websocket_connections"] == 5


class TestTaskListEndpoint:
    def test_list_tasks_empty(self, client: TestClient) -> None:
        response = client.get("/api/tasks")
        assert response.status_code == 200

        data = response.json()
        assert data["tasks"] == []
        assert data["total"] == 0

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

    def test_get_task_with_children(
        self, client: TestClient, store: GraphStore, make_event: type
    ) -> None:
        store.add_event(make_event.create("parent"))
        store.add_event(make_event.create("child-1", parent_id="parent"))
        store.add_event(make_event.create("child-2", parent_id="parent"))

        response = client.get("/api/tasks/parent")
        data = response.json()

        assert len(data["children"]) == 2


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
        start_time = datetime(2024, 1, 1, tzinfo=UTC)
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

        # Active: registered + executed
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
