"""Task event definitions."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskState(str, Enum):
    """Celery task states. Inherits from str for easy comparison."""

    PENDING = "PENDING"
    RECEIVED = "RECEIVED"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    REVOKED = "REVOKED"
    REJECTED = "REJECTED"
    RETRY = "RETRY"
    # The task raised Celery's Ignore (not a replace): it ended without a result.
    IGNORED = "IGNORED"


class WorkerEventType(str, Enum):
    """Worker lifecycle event types."""

    WORKER_READY = "worker_ready"
    WORKER_SHUTDOWN = "worker_shutdown"


class RegisteredTaskDefinition(BaseModel):
    """Celery task definition metadata captured from workers.

    This data is collected from a running worker's task registry and is used
    to enrich the task registry API (`/api/tasks/registry`) with human-friendly
    information like docstrings and signatures.

    Attributes:
        name: Fully qualified task name (e.g., "myapp.tasks.add").
        module: Python module where the task implementation lives.
        signature: Human-readable call signature (best-effort).
        docstring: Task documentation (best-effort).
        bound: Whether the task is bound (`bind=True`) and receives `self`.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    module: str | None = None
    signature: str | None = None
    docstring: str | None = None
    bound: bool = False


class TaskEvent(BaseModel):
    """Immutable task lifecycle event.

    Frozen model that can be hashed and compared. Captures a single
    state transition in a Celery task's lifecycle.

    Attributes:
        task_id: Unique identifier for the task execution.
        name: Fully qualified task name (e.g., 'myapp.tasks.add').
        state: Current state of the task.
        timestamp: When this event occurred.
        parent_id: ID of the parent task that spawned this one.
        root_id: ID of the root task in the workflow.
        group_id: ID shared by tasks in the same group/chord.
        chord_id: ID of the group for which this header task's chord will complete.
        chord_callback_id: Task ID of the chord callback (only set on header tasks).
        trace_id: Optional distributed tracing ID.
        retries: Number of retry attempts so far.
        args: Positional arguments passed to the task (scrubbed).
        kwargs: Keyword arguments passed to the task (scrubbed).
        result: Return value of the task (SUCCESS state only).
        exception: Exception message (FAILURE/RETRY states), or the reason a
            task gave ``Reject`` (REJECTED).
        exception_type: Exception class (FAILURE/RETRY states, and REJECTED
            when the reason is an exception): ``module.Class``, or just
            ``Class`` for builtins (e.g. ``ValueError``). For a retry, the
            exception that caused it.
        traceback: Full traceback string (FAILURE/RETRY states).
        output: What the attempt printed and logged, in order (SUCCESS,
            FAILURE, RETRY, IGNORED and REJECTED events, when output capture
            is enabled).
        hostname: Nodename of the worker that handled the task (``name@host``,
            as in ``WorkerEvent.hostname``). Worker events only.
        queue: Queue the task message was sent to, when Celery published it
            straight to a queue (the default); None when it went through a
            named exchange, which may route it to any number of queues.
        routing_key: Routing key the task message was published with.
        eta: When the attempt was scheduled to run (``countdown`` or ``eta``),
            in UTC; None when it could run at once. A retry's events carry the
            retry's ETA.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    name: str
    state: TaskState
    timestamp: datetime
    parent_id: str | None = None
    root_id: str | None = None
    group_id: str | None = None
    chord_id: str | None = None
    chord_callback_id: str | None = None
    trace_id: str | None = None
    retries: int = 0

    # New fields for enhanced event data
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
    eta: datetime | None = None


class WorkerEvent(BaseModel):
    """Worker lifecycle event.

    Captures worker startup, shutdown, and task registration information
    from Celery's worker_ready and worker_shutdown signals.

    Attributes:
        event_type: Type of worker event (ready or shutdown).
        hostname: Celery nodename (``name@host``, e.g. ``celery@web-1``).
        pid: Worker process ID (with hostname, creates unique ID).
        timestamp: When this event occurred.
        registered_tasks: List of task names registered by this worker.
        shutdown_time: When worker shut down (shutdown event only).
    """

    model_config = ConfigDict(frozen=True)

    event_type: WorkerEventType
    hostname: str
    pid: int
    timestamp: datetime
    registered_tasks: list[str] = Field(default_factory=list)
    task_definitions: dict[str, RegisteredTaskDefinition] = Field(default_factory=dict)
    shutdown_time: datetime | None = None


__all__ = [
    "RegisteredTaskDefinition",
    "TaskEvent",
    "TaskState",
    "WorkerEvent",
    "WorkerEventType",
]
