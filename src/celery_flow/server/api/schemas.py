"""API response schemas for REST endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from celery_flow.core.events import TaskState


class TaskEventResponse(BaseModel):
    """Single task event."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    name: str
    state: TaskState
    timestamp: datetime
    parent_id: str | None = None
    root_id: str | None = None
    trace_id: str | None = None
    retries: int = 0


class TaskNodeResponse(BaseModel):
    """Task node with events and timing info."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    name: str
    state: TaskState
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
    parent_id: str | None = None
    children: list[str] = Field(default_factory=list)


class GraphResponse(BaseModel):
    """Full task graph from root."""

    root_id: str
    nodes: dict[str, GraphNodeResponse]


class GraphListResponse(BaseModel):
    """List of root graphs."""

    graphs: list[GraphNodeResponse]
    total: int


class HealthResponse(BaseModel):
    """Health check status."""

    status: str = "ok"
    consumer_running: bool = False
    websocket_connections: int = 0
    node_count: int = 0


class ErrorResponse(BaseModel):
    """API error."""

    detail: str
    error_code: str | None = None
