"""Task event definitions."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


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
