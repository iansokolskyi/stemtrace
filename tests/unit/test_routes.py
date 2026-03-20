"""Tests for REST API routes."""

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import stemtrace.server.api.routes as routes
from stemtrace.core.events import RegisteredTaskDefinition, TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskNode
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
        assert worker["hostname"] == "worker-1"
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
        worker_registry.register_worker("worker-1", 1111, ["a"])
        worker_registry.mark_shutdown("worker-1", 1111)

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
        assert workers[0].hostname == "worker-1"
        assert workers[0].pid == 1111
        assert workers[0].status == "online"

    def test_refresh_worker_registry_keeps_existing_tasks_when_registered_unavailable(
        self,
    ) -> None:
        """If registered() fails, we keep the existing task list for that worker."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 1111, ["myapp.tasks.process"])

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
        assert workers[0].hostname == "worker-1"
        assert workers[0].registered_tasks == ["myapp.tasks.process"]

    def test_refresh_worker_registry_marks_missing_workers_offline(self) -> None:
        """Workers not present in stats responders are marked offline."""
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 1111, ["a"])
        worker_registry.register_worker("worker-2", 2222, ["b"])

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
        assert status_by_host["worker-1"] == "online"
        assert status_by_host["worker-2"] == "offline"

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
        assert workers[0].hostname == "worker-3"
        assert workers[0].pid == 3333
        assert workers[0].registered_tasks == []


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
            return None

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
        t0 = datetime(2024, 1, 1, tzinfo=UTC)
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
            group_node, all_nodes={"child-1": child1, "child-2": child2}
        )
        assert resp.first_seen == t1
        assert resp.last_updated == t2
        assert resp.duration_ms == int((t2 - t1).total_seconds() * 1000)

    def test_graph_node_response_children_sorted_tasks_first(self) -> None:
        """Children are ordered with TASK nodes first, GROUP/CHORD nodes last."""
        t0 = datetime(2024, 1, 1, tzinfo=UTC)

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
        resp = routes._node_to_graph_response(parent, all_nodes=all_nodes)

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
                    timestamp=datetime(2024, 1, 1, tzinfo=UTC),
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
        resp = routes._node_to_graph_response(node, node_alias_key="operator_type")
        assert resp.name == "sync_table"

    def test_graph_response_without_alias_uses_task_name(self) -> None:
        """_node_to_graph_response without alias key uses task name."""
        node = self._make_node(kwargs={"operator_type": "sync_table"})
        resp = routes._node_to_graph_response(node)
        assert resp.name == "myapp.tasks.process"
