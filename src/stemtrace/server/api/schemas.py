"""API response schemas for REST endpoints."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stemtrace.core.events import TaskState
from stemtrace.core.graph import ChordNotRunReason, NodeType
from stemtrace.core.groups import GroupSource


class WorkerStatus(str, Enum):
    """Worker lifecycle status. Inherits from str for easy comparison."""

    ONLINE = "online"
    OFFLINE = "offline"


class TaskStatus(str, Enum):
    """Task registry status based on execution and registration state."""

    ACTIVE = "active"  # Has been executed AND registered by workers
    NEVER_RUN = "never_run"  # Registered by workers but never executed
    NOT_REGISTERED = "not_registered"  # Executed but no worker has it registered


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
    output: str | None = None
    exception_type: str | None = None
    hostname: str | None = None
    queue: str | None = None
    routing_key: str | None = None
    # When this attempt was scheduled to run (countdown/eta); see TaskEvent.eta.
    eta: datetime | None = None


class TaskNodeResponse(BaseModel):
    """Task node with events and timing info."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    name: str
    state: TaskState
    node_type: NodeType = NodeType.TASK
    group_id: str | None = None
    chord_id: str | None = None
    # Where the node sits in the graph (a group member: its group node).
    parent_id: str | None = None
    # The parent the task's events reported (None for GROUP/CHORD nodes).
    reported_parent_id: str | None = None
    children: list[str] = Field(default_factory=list)
    # Task group (see core/groups.py), not Celery's group(): that's group_id.
    # None for synthetic GROUP/CHORD nodes.
    task_group: str | None = None
    tags: list[str] = Field(default_factory=list)
    # None when a list was asked for with compact=1.
    events: list[TaskEventResponse] | None = Field(default_factory=list)
    first_seen: datetime | None = None
    last_updated: datetime | None = None
    duration_ms: int | None = None
    # duration_ms split up (see TaskNode): first run's wait, last run, all runs.
    queue_wait_ms: int | None = None
    runtime_ms: int | None = None
    total_runtime_ms: int | None = None
    # The first attempt's ETA (countdown/eta): queue_wait_ms starts there if later.
    eta: datetime | None = None
    # Where the task ran and came from, and what it raised: each from the
    # furthest-along event that reports it (see TaskNode._latest).
    exception_type: str | None = None
    hostname: str | None = None
    queue: str | None = None
    routing_key: str | None = None


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
    flow_root_id: str | None = Field(
        default=None,
        description=(
            "Root of the graph that holds the task: the ID GET /api/graphs/{id} "
            "takes. It follows where the graph places the task, not Celery's "
            "root_id. Null when the walk up from the task reaches no root."
        ),
    )


class FlowSummaryResponse(BaseModel):
    """Totals over a flow's whole graph, for the flows list."""

    task_count: int = Field(
        description="Tasks in the flow (synthetic group and chord nodes excluded)."
    )
    state_counts: dict[str, int] = Field(
        description="Tasks per state, by state value; states with no task are left out."
    )
    not_run_count: int = Field(
        description=(
            "Chords whose callback can't run because a header failed, was rejected, "
            "revoked or ignored (see not_run_reason); 0 while headers are still running."
        )
    )
    state: TaskState = Field(
        description=(
            "Worst task state, in the order FAILURE (also REJECTED), REVOKED, "
            "RETRY, STARTED (also PENDING and RECEIVED), IGNORED, SUCCESS. "
            "The root's own state when the flow has no tasks."
        )
    )


class GraphNodeResponse(BaseModel):
    """Minimal node for graph visualization."""

    task_id: str
    name: str
    state: TaskState
    node_type: NodeType = NodeType.TASK
    group_id: str | None = None
    chord_id: str | None = None
    # Where the node sits in the graph (a group member: its group node).
    parent_id: str | None = None
    # The parent the task's events reported (None for GROUP/CHORD nodes).
    reported_parent_id: str | None = None
    children: list[str] = Field(default_factory=list)
    # Task group (see core/groups.py), not Celery's group(): that's group_id.
    # None for synthetic GROUP/CHORD nodes.
    task_group: str | None = None
    tags: list[str] = Field(default_factory=list)
    duration_ms: int | None = None
    # duration_ms split up (see TaskNode): first run's wait, last run, all runs.
    queue_wait_ms: int | None = None
    runtime_ms: int | None = None
    total_runtime_ms: int | None = None
    # The first attempt's ETA (countdown/eta): queue_wait_ms starts there if later.
    eta: datetime | None = None
    first_seen: datetime | None = None
    last_updated: datetime | None = None
    # Where the task ran and came from, and what it raised: each from the
    # furthest-along event that reports it (see TaskNode._latest).
    exception_type: str | None = None
    hostname: str | None = None
    queue: str | None = None
    routing_key: str | None = None
    # CHORD nodes in a full graph (GET /graphs/{root_id}) only: why the
    # callback hasn't run (see chord_not_run_reasons).
    not_run_reason: ChordNotRunReason | None = None
    # Root nodes in GET /graphs only: totals over the root's whole graph.
    flow: FlowSummaryResponse | None = None


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

    # "ok", or "degraded" when a configured consumer isn't running or can't
    # reach the broker. Always served with HTTP 200.
    status: str = "ok"
    version: str
    consumer_running: bool = False
    # True if the consumer's last broker read succeeded, False while it is
    # failing to reach the broker, None if unknown (not started, first read
    # pending, or the transport doesn't report connectivity).
    consumer_connected: bool | None = None
    websocket_connections: int = 0
    node_count: int = 0
    # Kind of the Celery broker (redis, rabbitmq, memory), from broker_url's
    # scheme only; None when unknown.
    broker: str | None = None


class LivenessResponse(BaseModel):
    """Minimal liveness status, safe to serve without authentication."""

    status: str = "ok"


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
    execution_count: int = 0
    registered_by: list[str] = Field(default_factory=list)
    last_run: datetime | None = None
    status: TaskStatus = TaskStatus.ACTIVE
    task_group: str | None = None
    tags: list[str] = Field(default_factory=list)


class TaskRegistryResponse(BaseModel):
    """List of all registered tasks."""

    tasks: list[RegisteredTaskResponse]
    total: int


# Task group schemas


class TaskGroupResponse(BaseModel):
    """A task group and the task names the server has seen in it."""

    name: str
    source: GroupSource
    tags: list[str] = Field(default_factory=list)
    color: str | None = None
    task_names: list[str] = Field(default_factory=list)


class TaskGroupListResponse(BaseModel):
    """All task groups: code-defined ones (even with no tasks yet) and automatic ones."""

    groups: list[TaskGroupResponse]
    total: int


# Summary schemas (overview, time series, failure groups)


class TimeWindowResponse(BaseModel):
    """The time range an aggregate covers: ``[start, end)``.

    For the latest window, ``end`` is the server's "now" and runs stamped
    later (a worker clock ahead of the server's) still count in it.
    """

    start: datetime
    end: datetime


class KpiResponse(BaseModel):
    """A rate for the window and the same rate for the window before it."""

    value: float
    baseline: float


class KpiCountResponse(BaseModel):
    """A count for the window and the same count for the window before it."""

    value: int
    baseline: int


class WorkerCountsResponse(BaseModel):
    """Workers the server knows of, from worker events (no inspect round trip)."""

    online: int
    total: int


class OverviewResponse(BaseModel):
    """KPIs over a window, each with the previous window of equal length."""

    window: TimeWindowResponse
    baseline_window: TimeWindowResponse
    runs: KpiCountResponse
    failures: KpiCountResponse
    runs_per_minute: KpiResponse
    failure_rate: KpiResponse
    workers: WorkerCountsResponse


class TimeSeriesResponse(BaseModel):
    """Runs by current state per bucket; states with no run in range are left out."""

    window: TimeWindowResponse
    bucket_seconds: int
    starts: list[datetime]
    # Keyed by state name (str, not TaskState: pydantic 2.4 warns serializing
    # enum dict keys).
    counts: dict[str, list[int]]
    by_task_group: dict[str, dict[str, list[int]]] | None = None


class FailureGroupResponse(BaseModel):
    """FAILURE runs that raised the same exception class."""

    exception_type: str | None
    count: int
    first_seen: datetime
    last_seen: datetime
    task_names: list[str]
    sample_task_ids: list[str]


class FailureGroupListResponse(BaseModel):
    """The window's failures, grouped by exception class."""

    window: TimeWindowResponse
    group_by: str
    total: int
    groups: list[FailureGroupResponse]


# Worker Registry schemas


class WorkerResponse(BaseModel):
    """Information about a registered worker."""

    model_config = ConfigDict(from_attributes=True)

    hostname: str
    pid: int
    registered_tasks: list[str] = Field(default_factory=list)
    status: WorkerStatus
    registered_at: datetime
    last_seen: datetime


class WorkerListResponse(BaseModel):
    """List of all registered workers."""

    workers: list[WorkerResponse]
    total: int
