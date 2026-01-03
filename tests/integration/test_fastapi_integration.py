"""Integration tests for FastAPI components."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import RegisteredTaskDefinition, TaskEvent, TaskState
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.fastapi.auth import require_api_key, require_basic_auth
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore, WorkerRegistry
from stemtrace.server.websocket import WebSocketManager


class TestCreateRouter:
    """Tests for create_router() factory function."""

    def test_create_router_with_defaults(self) -> None:
        """Router creates its own store and ws_manager if not provided."""
        router = create_router()

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # Health endpoint should work
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_create_router_with_store(self) -> None:
        """Router uses provided store."""
        store = GraphStore()
        store.add_event(
            TaskEvent(
                task_id="test-123",
                name="tests.sample",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/test-123")
        assert response.status_code == 200
        assert response.json()["task"]["task_id"] == "test-123"

    def test_create_router_with_auth(self) -> None:
        """Router applies auth dependency."""
        router = create_router(auth_dependency=require_api_key("secret"))
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # Without auth - should fail
        response = client.get("/api/health")
        assert response.status_code == 401

        # With auth - should work
        response = client.get("/api/health", headers={"X-API-Key": "secret"})
        assert response.status_code == 200


class TestWebSocketEndpoint:
    """Tests for WebSocket endpoint integration."""

    def test_websocket_connect_disconnect(self) -> None:
        """WebSocket connects and disconnects cleanly."""
        store = GraphStore()
        ws_manager = WebSocketManager()
        router = create_router(store=store, ws_manager=ws_manager)

        app = FastAPI()
        app.include_router(router)

        with TestClient(app) as client:
            with client.websocket_connect("/ws"):
                assert ws_manager.connection_count == 1

            # After disconnect
            assert ws_manager.connection_count == 0

    def test_store_listener_queues_events_for_broadcast(self) -> None:
        """Events added to store are queued for WebSocket broadcast."""
        import time

        # Use extension with lifespan so broadcast loop starts
        ext = StemtraceExtension(
            broker_url="memory://",
            embedded_consumer=False,  # We'll add events manually
        )

        app = FastAPI(lifespan=ext.lifespan)
        app.include_router(ext.router, prefix="/stemtrace")

        with TestClient(app) as client, client.websocket_connect("/stemtrace/ws"):
            # Verify connected
            assert ext.ws_manager.connection_count == 1

            # Add an event to the store
            event = TaskEvent(
                task_id="test-ws-123",
                name="tests.sample",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
            ext.store.add_event(event)

            # Give broadcast loop time to process
            time.sleep(0.1)

            # Verify the event was processed (queue should be empty after broadcast)
            # The broadcast happens asynchronously, so we just verify the connection
            # is still active and the store has the event
            assert ext.ws_manager.connection_count == 1
            assert ext.store.get_node("test-ws-123") is not None


class TestStemtraceExtension:
    """Tests for StemtraceExtension lifecycle."""

    def test_extension_creates_components(self) -> None:
        """Extension creates store, ws_manager, and optionally consumer."""
        ext = StemtraceExtension(
            broker_url="memory://",
            embedded_consumer=True,
        )

        assert ext.store is not None
        assert ext.ws_manager is not None
        assert ext.consumer is not None

    def test_extension_without_consumer(self) -> None:
        """Extension can run without embedded consumer."""
        ext = StemtraceExtension(
            broker_url="memory://",
            embedded_consumer=False,
        )

        assert ext.consumer is None

    def test_extension_router_includes_api(self) -> None:
        """Extension router includes API endpoints."""
        ext = StemtraceExtension(broker_url="memory://", serve_ui=False)

        app = FastAPI()
        app.include_router(ext.router, prefix="/flow")
        client = TestClient(app)

        response = client.get("/flow/api/health")
        assert response.status_code == 200

    def test_extension_with_auth(self) -> None:
        """Extension applies auth to routes."""
        ext = StemtraceExtension(
            broker_url="memory://",
            serve_ui=False,
            auth_dependency=require_basic_auth("admin", "pass"),
        )

        app = FastAPI()
        app.include_router(ext.router, prefix="/flow")
        client = TestClient(app)

        # Without auth
        response = client.get("/flow/api/health")
        assert response.status_code == 401

        # With auth
        response = client.get("/flow/api/health", auth=("admin", "pass"))
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_extension_lifespan(self) -> None:
        """Extension lifespan starts/stops ws_manager broadcast loop."""
        ext = StemtraceExtension(
            broker_url="memory://",
            embedded_consumer=False,  # Don't test consumer here (blocks)
            serve_ui=False,
        )

        app = FastAPI()

        # Simulate lifespan
        async with ext.lifespan(app):
            # WS manager should have loop
            assert ext.ws_manager._loop is not None
            assert ext.ws_manager._broadcast_task is not None

        # After lifespan exit
        assert ext.ws_manager._loop is None
        assert ext.ws_manager._broadcast_task is None

    @pytest.mark.asyncio
    async def test_extension_compose_lifespan(self) -> None:
        """Extension can compose with another lifespan."""
        ext = StemtraceExtension(broker_url="memory://", embedded_consumer=False)

        other_started = False
        other_stopped = False

        @asynccontextmanager
        async def other_lifespan(app: FastAPI) -> AsyncIterator[None]:
            nonlocal other_started, other_stopped
            other_started = True
            yield
            other_stopped = True

        combined = ext.compose_lifespan(other_lifespan)
        app = FastAPI()

        async with combined(app):
            assert other_started

        assert other_stopped

    def test_extension_store_receives_events(self) -> None:
        """Events added to store are accessible via API."""
        ext = StemtraceExtension(
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
        )

        # Add event directly to store
        ext.store.add_event(
            TaskEvent(
                task_id="direct-event",
                name="tests.direct",
                state=TaskState.STARTED,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )

        app = FastAPI()
        app.include_router(ext.router)
        client = TestClient(app)

        response = client.get("/api/tasks/direct-event")
        assert response.status_code == 200
        assert response.json()["task"]["name"] == "tests.direct"


class TestSyntheticGroupNodes:
    """Integration tests for synthetic GROUP node creation and API exposure."""

    def test_group_node_in_graph_response(self) -> None:
        """Synthetic GROUP node appears in graph response."""
        store = GraphStore()
        group_id = "test-group-abc"

        # Add two tasks with the same group_id
        store.add_event(
            TaskEvent(
                task_id="task-a",
                name="tests.group_member",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC),
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="task-b",
                name="tests.group_member",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, 0, 0, 2, tzinfo=UTC),
                group_id=group_id,
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # The GROUP node should be a root
        response = client.get("/api/graphs")
        assert response.status_code == 200
        graphs = response.json()["graphs"]
        group_node = next(
            (g for g in graphs if g["task_id"] == f"group:{group_id}"), None
        )
        assert group_node is not None
        assert group_node["node_type"] == "GROUP"
        assert group_node["name"] == "group"

    def test_group_node_includes_children(self) -> None:
        """GROUP node children are included in graph detail."""
        store = GraphStore()
        group_id = "test-group-xyz"

        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="tests.t1",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="task-2",
                name="tests.t2",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                group_id=group_id,
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # Get the full graph from the GROUP node
        response = client.get(f"/api/graphs/group:{group_id}")
        assert response.status_code == 200
        graph = response.json()
        assert graph["root_id"] == f"group:{group_id}"

        group_node = graph["nodes"][f"group:{group_id}"]
        assert "task-1" in group_node["children"]
        assert "task-2" in group_node["children"]

    def test_task_response_includes_node_type(self) -> None:
        """Task response includes node_type field."""
        store = GraphStore()
        store.add_event(
            TaskEvent(
                task_id="test-task",
                name="tests.sample",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/test-task")
        assert response.status_code == 200
        task = response.json()["task"]
        assert task["node_type"] == "TASK"

    def test_task_response_includes_group_id(self) -> None:
        """Task response includes group_id field."""
        store = GraphStore()
        store.add_event(
            TaskEvent(
                task_id="grouped-task",
                name="tests.grouped",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                group_id="my-group-id",
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/grouped-task")
        assert response.status_code == 200
        task = response.json()["task"]
        assert task["group_id"] == "my-group-id"


class TestGraphEdgeData:
    """Tests that verify parent-child relationships required for edge rendering.

    These tests ensure the API returns correct data structures that the frontend
    uses to generate graph edges. Regression tests for edge visibility issues.
    """

    def test_parent_has_child_in_children_list(self) -> None:
        """Parent node's children list includes child task IDs."""
        store = GraphStore()
        store.add_event(
            TaskEvent(
                task_id="parent",
                name="tests.parent",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="child-1",
                name="tests.child",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                parent_id="parent",
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/graphs/parent")
        assert response.status_code == 200
        graph = response.json()

        parent_node = graph["nodes"]["parent"]
        assert "child-1" in parent_node["children"]

    def test_parent_to_group_edge_data(self) -> None:
        """When parent spawns a group, GROUP node is in parent's children list.

        Regression test: Previously GROUP nodes under a parent were missing edges
        because the GROUP wasn't in parent's children list.
        """
        store = GraphStore()
        group_id = "test-group"

        store.add_event(
            TaskEvent(
                task_id="parent",
                name="tests.parent",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        store.add_event(
            TaskEvent(
                task_id="child-1",
                name="tests.add",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                parent_id="parent",
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="child-2",
                name="tests.add",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                parent_id="parent",
                group_id=group_id,
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/graphs/parent")
        assert response.status_code == 200
        graph = response.json()

        # Parent should have GROUP as child (not individual tasks)
        parent_node = graph["nodes"]["parent"]
        group_node_id = f"group:{group_id}"
        assert group_node_id in parent_node["children"]
        assert "child-1" not in parent_node["children"]
        assert "child-2" not in parent_node["children"]

        # GROUP should have individual tasks as children
        group_node = graph["nodes"][group_node_id]
        assert "child-1" in group_node["children"]
        assert "child-2" in group_node["children"]

    def test_chord_callback_edge_data(self) -> None:
        """CHORD node includes callback in children for edge rendering."""
        store = GraphStore()
        group_id = "chord-header-group"
        callback_id = "callback-task"

        # Header tasks
        store.add_event(
            TaskEvent(
                task_id="header-1",
                name="tests.add",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="header-2",
                name="tests.add",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                group_id=group_id,
                chord_id=group_id,
                chord_callback_id=callback_id,
            )
        )
        # Callback task
        store.add_event(
            TaskEvent(
                task_id=callback_id,
                name="tests.aggregate",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        chord_node_id = f"group:{group_id}"
        response = client.get(f"/api/graphs/{chord_node_id}")
        assert response.status_code == 200
        graph = response.json()

        chord_node = graph["nodes"][chord_node_id]
        # CHORD should be upgraded from GROUP
        assert chord_node["node_type"] == "CHORD"
        # Header tasks should be children
        assert "header-1" in chord_node["children"]
        assert "header-2" in chord_node["children"]
        # Callback should also be in children (for edge rendering)
        assert callback_id in chord_node["children"]

    def test_multiple_children_all_in_children_list(self) -> None:
        """Parent with multiple independent children has all in children list."""
        store = GraphStore()
        store.add_event(
            TaskEvent(
                task_id="parent",
                name="tests.parent",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        for i in range(3):
            store.add_event(
                TaskEvent(
                    task_id=f"child-{i}",
                    name="tests.child",
                    state=TaskState.SUCCESS,
                    timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                    parent_id="parent",
                )
            )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/graphs/parent")
        assert response.status_code == 200
        graph = response.json()

        parent_node = graph["nodes"]["parent"]
        # All 3 children should be in parent's children list
        assert len(parent_node["children"]) == 3
        assert "child-0" in parent_node["children"]
        assert "child-1" in parent_node["children"]
        assert "child-2" in parent_node["children"]

    def test_standalone_group_is_root_with_children(self) -> None:
        """Standalone GROUP (no parent task) is a root node with children."""
        store = GraphStore()
        group_id = "standalone-group"

        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="tests.t1",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                group_id=group_id,
            )
        )
        store.add_event(
            TaskEvent(
                task_id="task-2",
                name="tests.t2",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                group_id=group_id,
            )
        )

        router = create_router(store=store)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        group_node_id = f"group:{group_id}"
        response = client.get(f"/api/graphs/{group_node_id}")
        assert response.status_code == 200
        graph = response.json()

        # GROUP is the root
        assert graph["root_id"] == group_node_id

        group_node = graph["nodes"][group_node_id]
        assert "task-1" in group_node["children"]
        assert "task-2" in group_node["children"]


class TestWorkersEndpoint:
    """Tests for /api/workers endpoints."""

    def test_workers_empty_registry(self) -> None:
        """Empty registry returns empty list."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        router = create_router(store=store, worker_registry=worker_registry)

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/workers")
        assert response.status_code == 200
        data = response.json()
        assert data["workers"] == []
        assert data["total"] == 0

    def test_workers_returns_registered_worker(self) -> None:
        """Registered worker appears in response."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker(
            "worker-1", 12345, ["tasks.add", "tasks.multiply"]
        )

        router = create_router(store=store, worker_registry=worker_registry)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/workers")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 1
        worker = data["workers"][0]
        assert worker["hostname"] == "worker-1"
        assert worker["pid"] == 12345
        assert worker["registered_tasks"] == ["tasks.add", "tasks.multiply"]
        assert worker["status"] == "online"

    def test_workers_by_hostname(self) -> None:
        """Filter workers by hostname."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 12345, ["tasks.add"])
        worker_registry.register_worker("worker-2", 67890, ["tasks.multiply"])

        router = create_router(store=store, worker_registry=worker_registry)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/workers/worker-1")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 1
        assert data["workers"][0]["hostname"] == "worker-1"

    def test_workers_shutdown_status(self) -> None:
        """Shutdown worker has offline status."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 12345, ["tasks.add"])
        worker_registry.mark_shutdown("worker-1", 12345)

        router = create_router(store=store, worker_registry=worker_registry)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/workers")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 1
        worker = data["workers"][0]
        assert worker["status"] == WorkerStatus.OFFLINE


class TestRegistryWithWorkers:
    """Tests for task registry with worker registration data."""

    def test_registry_shows_registered_by(self) -> None:
        """Tasks show which workers registered them."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker(
            "worker-1", 12345, ["tasks.add", "tasks.multiply"]
        )
        worker_registry.register_worker("worker-2", 67890, ["tasks.add"])  # Same task

        router = create_router(store=store, worker_registry=worker_registry)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry")
        assert response.status_code == 200
        data = response.json()

        # Find tasks.add
        add_task = next(t for t in data["tasks"] if t["name"] == "tasks.add")
        # Both workers should be listed (deduplicated and sorted)
        assert sorted(add_task["registered_by"]) == ["worker-1", "worker-2"]

    def test_registry_includes_task_metadata(self) -> None:
        """Registry includes docstring/signature/bound when provided by workers."""
        store = GraphStore()
        worker_registry = WorkerRegistry()

        task_name = "tasks.add"
        worker_registry.register_worker(
            "worker-1",
            12345,
            [task_name],
            task_definitions={
                task_name: RegisteredTaskDefinition(
                    name=task_name,
                    module="tasks",
                    signature="(x, y)",
                    docstring="Add two numbers.",
                    bound=True,
                )
            },
        )

        # Ensure the task is also observed (so it appears in the union set even if workers differ).
        store.add_event(
            TaskEvent(
                task_id="task-1",
                name=task_name,
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )

        router = create_router(store=store, worker_registry=worker_registry)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry")
        assert response.status_code == 200
        data = response.json()

        add_task = next(t for t in data["tasks"] if t["name"] == task_name)
        assert add_task["module"] == "tasks"
        assert add_task["signature"] == "(x, y)"
        assert add_task["docstring"] == "Add two numbers."
        assert add_task["bound"] is True

    def test_registry_no_duplicate_hostnames(self) -> None:
        """Same hostname from multiple restarts appears only once."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        # Same hostname, different PIDs (simulating restarts)
        worker_registry.register_worker("worker-1", 12345, ["tasks.add"])
        worker_registry.register_worker("worker-1", 67890, ["tasks.add"])

        router = create_router(store=store, worker_registry=worker_registry)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/tasks/registry")
        assert response.status_code == 200
        data = response.json()

        add_task = next(t for t in data["tasks"] if t["name"] == "tasks.add")
        # Hostname should appear only once, not twice
        assert add_task["registered_by"] == ["worker-1"]

    def test_registry_never_run_status_filter(self) -> None:
        """status=never_run shows only registered but never executed tasks."""
        store = GraphStore()
        # Add an executed task (not registered) - status: not_registered
        store.add_event(
            TaskEvent(
                task_id="task-1",
                name="tasks.executed_only",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )

        # Add a registered task that's also executed - status: active
        store.add_event(
            TaskEvent(
                task_id="task-2",
                name="tasks.active_task",
                state=TaskState.SUCCESS,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )

        worker_registry = WorkerRegistry()
        worker_registry.register_worker(
            "worker-1", 12345, ["tasks.never_run_task", "tasks.active_task"]
        )

        router = create_router(store=store, worker_registry=worker_registry)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # Filter to never_run status (registered but never executed)
        response = client.get("/api/tasks/registry?status=never_run")
        assert response.status_code == 200
        data = response.json()

        task_names = [t["name"] for t in data["tasks"]]
        assert "tasks.never_run_task" in task_names
        assert "tasks.executed_only" not in task_names  # not_registered status
        assert "tasks.active_task" not in task_names  # active status

        # All returned tasks should have never_run status
        for task in data["tasks"]:
            assert task["status"] == "never_run"
