"""Protocol definitions for dependency inversion."""

from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol

from typing_extensions import Self

if TYPE_CHECKING:
    from stemtrace.core.events import TaskEvent


class EventTransport(Protocol):
    """Broker-agnostic transport. publish() must be fire-and-forget (never raise)."""

    def publish(self, event: "TaskEvent") -> None:
        """Publish event. Fire-and-forget: log errors, don't raise."""
        ...

    def consume(self) -> Iterator["TaskEvent"]:
        """Yield events as they arrive. May block waiting for new events."""
        ...

    @classmethod
    def from_url(cls, url: str) -> Self:
        """Create transport from broker URL. Raises UnsupportedBrokerError if unknown."""
        ...


class TaskRepository(Protocol):
    """Read-only task data access for API endpoints."""

    def get(self, task_id: str) -> "TaskEvent | None":
        """Get latest event for a task."""
        ...

    def list_recent(self, limit: int = 100) -> list["TaskEvent"]:
        """List recent events, most recent first."""
        ...

    def get_children(self, parent_id: str) -> list["TaskEvent"]:
        """Get events for tasks spawned by parent."""
        ...
