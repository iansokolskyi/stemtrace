"""REST API endpoints for stemtrace."""

from __future__ import annotations

import contextlib
from datetime import datetime  # noqa: TC003  # FastAPI evaluates annotations at runtime
from typing import TYPE_CHECKING, Annotated

from celery import Celery
from fastapi import APIRouter, HTTPException, Query

from stemtrace.server.api.schemas import (
    ErrorResponse,
    GraphListResponse,
    GraphNodeResponse,
    GraphResponse,
    HealthResponse,
    RegisteredTaskResponse,
    TaskDetailResponse,
    TaskEventResponse,
    TaskListResponse,
    TaskNodeResponse,
    TaskRegistryResponse,
    TaskStatus,
    WorkerListResponse,
    WorkerResponse,
)

if TYPE_CHECKING:
    from celery.app.control import Inspect

    from stemtrace.core.events import TaskState
    from stemtrace.core.graph import TaskNode
    from stemtrace.server.consumer import AsyncEventConsumer
    from stemtrace.server.store import GraphStore, WorkerRegistry
    from stemtrace.server.websocket import WebSocketManager


def _node_to_response(node: TaskNode) -> TaskNodeResponse:
    """Convert TaskNode to API response model.

    Args:
        node: Task node from graph store.

    Returns:
        TaskNodeResponse with timestamp and duration.
    """
    first_seen = node.events[0].timestamp if node.events else None
    last_updated = node.events[-1].timestamp if node.events else None

    duration_ms = None
    if first_seen and last_updated and first_seen != last_updated:
        duration_ms = int((last_updated - first_seen).total_seconds() * 1000)

    return TaskNodeResponse(
        task_id=node.task_id,
        name=node.name,
        state=node.state,
        node_type=node.node_type,
        group_id=node.group_id,
        chord_id=node.chord_id,
        parent_id=node.parent_id,
        children=node.children,
        events=[TaskEventResponse.model_validate(e) for e in node.events],
        first_seen=first_seen,
        last_updated=last_updated,
        duration_ms=duration_ms,
    )


def _node_to_graph_response(
    node: TaskNode,
    all_nodes: dict[str, TaskNode] | None = None,
) -> GraphNodeResponse:
    """Convert TaskNode to graph response model.

    For synthetic nodes (GROUP/CHORD), compute timing from children.

    Args:
        node: Task node from graph store.
        all_nodes: All nodes dict for child lookup.

    Returns:
        GraphNodeResponse with timing from children.
    """
    first_seen = node.events[0].timestamp if node.events else None
    last_updated = node.events[-1].timestamp if node.events else None

    # For synthetic nodes (GROUP/CHORD), compute timing from children
    if not node.events and node.children and all_nodes:
        child_first_seen: list[datetime] = []
        child_last_updated: list[datetime] = []
        for child_id in node.children:
            child = all_nodes.get(child_id)
            if child and child.events:
                child_first_seen.append(child.events[0].timestamp)
                child_last_updated.append(child.events[-1].timestamp)
        if child_first_seen:
            first_seen = min(child_first_seen)
        if child_last_updated:
            last_updated = max(child_last_updated)

    duration_ms = None
    if first_seen and last_updated and first_seen != last_updated:
        duration_ms = int((last_updated - first_seen).total_seconds() * 1000)

    return GraphNodeResponse(
        task_id=node.task_id,
        name=node.name,
        state=node.state,
        node_type=node.node_type,
        group_id=node.group_id,
        chord_id=node.chord_id,
        parent_id=node.parent_id,
        children=node.children,
        duration_ms=duration_ms,
        first_seen=first_seen,
        last_updated=last_updated,
    )


def _get_inspector(
    broker_url: str | None,
) -> Inspect | None:
    """Get Celery inspector for on-demand worker status.

    Args:
        broker_url: Broker URL to create minimal Celery app.

    Returns:
        Inspect instance, or None if broker_url not provided.
    """
    if broker_url is None:
        return None
    # Create minimal Celery app for inspection only
    app = Celery("stemtrace", "inspect")
    app.conf.broker_url = broker_url
    return app.control.inspect()


def _refresh_worker_status(
    worker_registry: WorkerRegistry,
    inspector: Inspect,
) -> None:
    """Update worker status based on Celery inspect results.

    Args:
        worker_registry: Worker registry to update.
        inspector: Celery inspector to query.
    """
    active = inspector.ping()
    if not active:
        return

    # Extract active hostnames from Celery response
    # Celery format: "worker1@hostname" or "hostname"
    active_hostnames = set()
    for worker_key in active:
        # Celery format: "worker1@hostname" or "hostname"
        hostname = worker_key.split("@")[1] if "@" in worker_key else worker_key
        active_hostnames.add(hostname)

    # Update registry: mark workers ONLINE if active in Celery.
    # Note: Never mutate WorkerInfo objects returned from get_all_workers() since
    # that bypasses WorkerRegistry locking and can race with the consumer thread.
    workers = worker_registry.get_all_workers()
    for worker in workers:
        if worker.hostname in active_hostnames:
            worker_registry.mark_online(worker.hostname, worker.pid)


def create_api_router(
    store: GraphStore,
    consumer: AsyncEventConsumer | None = None,
    ws_manager: WebSocketManager | None = None,
    worker_registry: WorkerRegistry | None = None,
    broker_url: str | None = None,
) -> APIRouter:
    """Create REST API router with task and graph endpoints.

    Args:
        store: Graph store for task/graph data.
        consumer: Optional async event consumer.
        ws_manager: Optional WebSocket manager.
        worker_registry: Optional worker registry for lifecycle tracking.
        broker_url: Optional Celery broker URL for on-demand inspection.

    Returns:
        Configured API router.
    """
    router = APIRouter(prefix="/api", tags=["stemtrace"])

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Return server health status and connection counts."""
        from stemtrace import __version__

        return HealthResponse(
            status="ok",
            version=__version__,
            consumer_running=consumer.is_running if consumer else False,
            websocket_connections=ws_manager.connection_count if ws_manager else 0,
            node_count=store.node_count,
        )

    @router.get(
        "/tasks",
        response_model=TaskListResponse,
        responses={400: {"model": ErrorResponse}},
    )
    async def list_tasks(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        state: Annotated[str | None, Query(description="Filter by task state")] = None,
        name: Annotated[
            str | None, Query(description="Filter by name substring")
        ] = None,
        from_date: Annotated[
            datetime | None, Query(description="Filter by start date (ISO format)")
        ] = None,
        to_date: Annotated[
            datetime | None, Query(description="Filter by end date (ISO format)")
        ] = None,
    ) -> TaskListResponse:
        """List tasks with optional filtering by state, name, and date range."""
        from stemtrace.core.events import TaskState as TS

        task_state: TaskState | None = None
        if state is not None:
            with contextlib.suppress(ValueError):
                task_state = TS(state)

        nodes, total = store.get_nodes(
            limit=limit,
            offset=offset,
            state=task_state,
            name_contains=name,
            from_date=from_date,
            to_date=to_date,
        )
        return TaskListResponse(
            tasks=[_node_to_response(n) for n in nodes],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/tasks/registry",
        response_model=TaskRegistryResponse,
    )
    async def get_task_registry(
        query: Annotated[
            str | None, Query(description="Filter by task name substring")
        ] = None,
        status: Annotated[
            str | None,
            Query(
                description="Filter by status: 'active', 'never_run', 'not_registered'"
            ),
        ] = None,
    ) -> TaskRegistryResponse:
        """List all discovered task definitions with optional filtering.

        Status values:
        - active: Has been executed AND registered by workers
        - never_run: Registered by workers but never executed
        - not_registered: Executed but no worker has it registered
        """
        # Get all observed task names (from executions)
        observed_names = store.get_unique_task_names()

        # Get all registered task names (from workers)
        # Use sets to avoid duplicates when same hostname has multiple workers (restarts)
        registered_tasks_by_worker: dict[str, set[str]] = {}
        if worker_registry is not None:
            workers = worker_registry.get_all_workers()
            for worker in workers:
                for task_name in worker.registered_tasks:
                    if task_name not in registered_tasks_by_worker:
                        registered_tasks_by_worker[task_name] = set()
                    registered_tasks_by_worker[task_name].add(worker.hostname)

        # Combine observed and registered tasks
        all_task_names = observed_names | set(registered_tasks_by_worker.keys())

        tasks: list[RegisteredTaskResponse] = []
        for name in sorted(all_task_names):
            # Apply text search filter
            if query and query.lower() not in name.lower():
                continue

            # Get execution count and last run time
            execution_count = store.get_task_execution_count(name)
            last_run = store.get_last_execution_time(name)

            # Get workers that registered this task (convert set to sorted list)
            registered_by_set = registered_tasks_by_worker.get(name, set())
            registered_by = sorted(registered_by_set) if registered_by_set else []

            # Compute status
            if execution_count > 0 and registered_by:
                task_status = TaskStatus.ACTIVE
            elif execution_count == 0 and registered_by:
                task_status = TaskStatus.NEVER_RUN
            else:
                task_status = TaskStatus.NOT_REGISTERED

            # Apply status filter
            if status and task_status.value != status:
                continue

            definition = (
                worker_registry.get_task_definition(name) if worker_registry else None
            )

            parts = name.rsplit(".", 1)
            fallback_module = parts[0] if len(parts) > 1 else None
            module = (
                definition.module
                if definition and definition.module
                else fallback_module
            )

            tasks.append(
                RegisteredTaskResponse(
                    name=name,
                    module=module,
                    signature=definition.signature if definition else None,
                    docstring=definition.docstring if definition else None,
                    bound=definition.bound if definition else False,
                    execution_count=execution_count,
                    registered_by=registered_by,
                    last_run=last_run,
                    status=task_status,
                )
            )

        return TaskRegistryResponse(tasks=tasks, total=len(tasks))

    @router.get(
        "/tasks/{task_id}",
        response_model=TaskDetailResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_task(task_id: str) -> TaskDetailResponse:
        """Get detailed information for a specific task including children."""
        node = store.get_node(task_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        children = store.get_children(task_id)
        return TaskDetailResponse(
            task=_node_to_response(node),
            children=[_node_to_response(c) for c in children],
        )

    @router.get(
        "/tasks/{task_id}/children",
        response_model=list[TaskNodeResponse],
        responses={404: {"model": ErrorResponse}},
    )
    async def get_task_children(task_id: str) -> list[TaskNodeResponse]:
        """Get child tasks for a specific task."""
        node = store.get_node(task_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        children = store.get_children(task_id)
        return [_node_to_response(c) for c in children]

    @router.get("/graphs", response_model=GraphListResponse)
    async def list_graphs(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        from_date: Annotated[
            datetime | None, Query(description="Filter by start date (ISO format)")
        ] = None,
        to_date: Annotated[
            datetime | None, Query(description="Filter by end date (ISO format)")
        ] = None,
    ) -> GraphListResponse:
        """List task execution graphs (root tasks) with pagination and date filtering."""
        roots, total = store.get_root_nodes(
            limit=limit,
            offset=offset,
            from_date=from_date,
            to_date=to_date,
        )
        # Build nodes dict for synthetic node timing computation
        all_nodes: dict[str, TaskNode] = {}
        for root in roots:
            all_nodes[root.task_id] = root
            for child in store.get_children(root.task_id):
                all_nodes[child.task_id] = child
        return GraphListResponse(
            graphs=[_node_to_graph_response(r, all_nodes) for r in roots],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/graphs/{root_id}",
        response_model=GraphResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_graph(root_id: str) -> GraphResponse:
        """Get complete task graph starting from a root task."""
        graph = store.get_graph_from_root(root_id)
        if not graph:
            raise HTTPException(status_code=404, detail=f"Graph {root_id} not found")

        all_nodes: dict[str, TaskNode] = {}
        for node_id, node in graph.items():
            all_nodes[node_id] = node

        return GraphResponse(
            root_id=root_id,
            nodes={
                tid: _node_to_graph_response(n, all_nodes) for tid, n in graph.items()
            },
        )

    @router.get(
        "/workers",
        response_model=WorkerListResponse,
    )
    async def list_workers(
        refresh: Annotated[
            bool, Query(description="Refresh worker status from Celery")
        ] = False,
    ) -> WorkerListResponse:
        """List all registered workers, optionally refreshing from Celery.

        Args:
            refresh: If True, query Celery for current worker status and update registry.
        """
        if worker_registry is None:
            return WorkerListResponse(workers=[], total=0)

        # On-demand worker status refresh using Celery inspect
        if refresh:
            inspector = _get_inspector(broker_url)
            if inspector is not None:
                _refresh_worker_status(worker_registry, inspector)

        workers = worker_registry.get_all_workers()
        return WorkerListResponse(
            workers=[WorkerResponse.model_validate(w) for w in workers],
            total=len(workers),
        )

    @router.get(
        "/workers/{hostname}",
        response_model=WorkerListResponse,
    )
    async def get_workers_by_hostname(hostname: str) -> WorkerListResponse:
        """Get all workers matching a hostname.

        Args:
            hostname: Worker hostname to filter by.
        """
        if worker_registry is None:
            return WorkerListResponse(workers=[], total=0)

        workers = worker_registry.get_workers_by_hostname(hostname)
        return WorkerListResponse(
            workers=[WorkerResponse.model_validate(w) for w in workers],
            total=len(workers),
        )

    return router
