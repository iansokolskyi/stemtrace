"""API response schemas for REST endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stemtrace.core.events import TaskState
from stemtrace.core.graph import NodeType


class TaskEventResponse(BaseModel):
    """Single task event with full details."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    name: str
    state: TaskState
    timestamp: datetime
    parent_id: str | None = None
    root_id: str | None = None
    group_id: str | None = None
    trace_id: str | None = None
    retries: int = 0

    # Enhanced event data
    args: list[Any] | None = None
    kwargs: dict[str, Any] | None = None
    result: Any | None = None
    exception: str | None = None
    traceback: str | None = None


class TaskNodeResponse(BaseModel):
    """Task node with events and timing info."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    name: str
    state: TaskState
    node_type: NodeType = NodeType.TASK
    group_id: str | None = None
    chord_id: str | None = None
    parent_id: str | None = None
    children: list[str] = Field(default_factory=list)
    events: list[TaskEventResponse] = Field(default_factory=list)
    first_seen: datetime | None = None
    last_updated: datetime | None = None
    duration_ms: int | None = None


class TaskListResponse(BaseModel):
    """Paginated list of tasks."""

    tasks: list[TaskNodeResponse]
    total: int
    limit: int
    offset: int


class TaskDetailResponse(BaseModel):
    """Task with its children."""

    task: TaskNodeResponse
    children: list[TaskNodeResponse] = Field(default_factory=list)


class GraphNodeResponse(BaseModel):
    """Minimal node for graph visualization."""

    task_id: str
    name: str
    state: TaskState
    node_type: NodeType = NodeType.TASK
    group_id: str | None = None
    chord_id: str | None = None
    parent_id: str | None = None
    children: list[str] = Field(default_factory=list)
    duration_ms: int | None = None
    first_seen: datetime | None = None
    last_updated: datetime | None = None


class GraphResponse(BaseModel):
    """Full task graph from root."""

    root_id: str
    nodes: dict[str, GraphNodeResponse]


class GraphListResponse(BaseModel):
    """Paginated list of root graphs."""

    graphs: list[GraphNodeResponse]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    """Health check status."""

    status: str = "ok"
    version: str
    consumer_running: bool = False
    websocket_connections: int = 0
    node_count: int = 0


class ErrorResponse(BaseModel):
    """API error."""

    detail: str
    error_code: str | None = None


# Task Registry schemas


class RegisteredTaskResponse(BaseModel):
    """A registered Celery task definition."""

    name: str
    signature: str | None = None
    docstring: str | None = None
    module: str | None = None
    bound: bool = False


class TaskRegistryResponse(BaseModel):
    """List of all registered tasks."""

    tasks: list[RegisteredTaskResponse]
    total: int
