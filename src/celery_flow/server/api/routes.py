"""REST API endpoints for celery-flow."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query

if TYPE_CHECKING:
    from datetime import datetime

from celery_flow.server.api.schemas import (
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
)

if TYPE_CHECKING:
    from celery_flow.core.events import TaskState
    from celery_flow.core.graph import TaskNode
    from celery_flow.server.consumer import AsyncEventConsumer
    from celery_flow.server.store import GraphStore
    from celery_flow.server.websocket import WebSocketManager


def _node_to_response(node: TaskNode) -> TaskNodeResponse:
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


def create_api_router(
    store: GraphStore,
    consumer: AsyncEventConsumer | None = None,
    ws_manager: WebSocketManager | None = None,
) -> APIRouter:
    """Create REST API router with task and graph endpoints."""
    router = APIRouter(prefix="/api", tags=["celery-flow"])

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Return server health status and connection counts."""
        return HealthResponse(
            status="ok",
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
    ) -> TaskListResponse:
        """List tasks with optional filtering by state and name."""
        from celery_flow.core.events import TaskState as TS

        task_state: TaskState | None = None
        if state is not None:
            with contextlib.suppress(ValueError):
                task_state = TS(state)

        nodes = store.get_nodes(
            limit=limit,
            offset=offset,
            state=task_state,
            name_contains=name,
        )
        return TaskListResponse(
            tasks=[_node_to_response(n) for n in nodes],
            total=store.node_count,
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
    ) -> TaskRegistryResponse:
        """List all discovered task definitions."""
        unique_names = store.get_unique_task_names()

        tasks: list[RegisteredTaskResponse] = []
        for name in sorted(unique_names):
            if query and query.lower() not in name.lower():
                continue

            parts = name.rsplit(".", 1)
            module = parts[0] if len(parts) > 1 else None

            tasks.append(
                RegisteredTaskResponse(
                    name=name,
                    module=module,
                    signature=None,
                    docstring=None,
                    bound=False,
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
    ) -> GraphListResponse:
        """List task execution graphs (root tasks)."""
        roots = store.get_root_nodes(limit=limit)
        # Build nodes dict for synthetic node timing computation
        all_nodes: dict[str, TaskNode] = {}
        for root in roots:
            all_nodes[root.task_id] = root
            for child in store.get_children(root.task_id):
                all_nodes[child.task_id] = child
        return GraphListResponse(
            graphs=[_node_to_graph_response(r, all_nodes) for r in roots],
            total=len(roots),
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

        return GraphResponse(
            root_id=root_id,
            nodes={tid: _node_to_graph_response(n, graph) for tid, n in graph.items()},
        )

    return router
