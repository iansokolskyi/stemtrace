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
        exception: Exception message (FAILURE/RETRY states).
        traceback: Full traceback string (FAILURE/RETRY states).
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


class WorkerEvent(BaseModel):
    """Worker lifecycle event.

    Captures worker startup, shutdown, and task registration information
    from Celery's worker_ready and worker_shutdown signals.

    Attributes:
        event_type: Type of worker event (ready or shutdown).
        hostname: Worker hostname for identification.
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
