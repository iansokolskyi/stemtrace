"""Tests for REST API routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore


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
