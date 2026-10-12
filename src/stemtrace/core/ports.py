"""Protocol definitions for dependency inversion."""

import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from typing_extensions import Self

if TYPE_CHECKING:
    from stemtrace.core.events import TaskEvent, WorkerEvent


class EventTransport(Protocol):
    """Broker-agnostic transport. publish() must be fire-and-forget (never raise)."""

    def publish(self, event: "TaskEvent | WorkerEvent") -> None:
        """Publish event. Fire-and-forget: log errors, don't raise."""
        ...

    def consume(self) -> Iterator["TaskEvent | WorkerEvent"]:
        """Yield events as they arrive. May block waiting for new events."""
        ...

    @classmethod
    def from_url(cls, url: str) -> Self:
        """Create transport from broker URL. Raises UnsupportedBrokerError if unknown."""
        ...


@runtime_checkable
class ConnectionAwareTransport(Protocol):
    """Optional transport capability: report whether the broker is reachable."""

    @property
    def connected(self) -> bool | None:
        """True if the last read reached the broker, False if failing, None if unknown."""
        ...


@runtime_checkable
class StoppableTransport(Protocol):
    """Optional transport capability: end consume() promptly when asked to stop."""

    def bind_stop_event(self, event: threading.Event) -> None:
        """End consume() and interrupt any retry waits once ``event`` is set."""
        ...


@runtime_checkable
class ClosableTransport(Protocol):
    """Optional transport capability: release the publisher connection."""

    def close(self) -> None:
        """Close the publisher connection, if any. Never raises or blocks for long."""
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
