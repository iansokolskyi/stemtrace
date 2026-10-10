"""In-memory transport for testing."""

from collections.abc import Iterator
from typing import ClassVar

from typing_extensions import Self

from stemtrace.core.events import TaskEvent, WorkerEvent

StreamEvent = TaskEvent | WorkerEvent


class MemoryTransport:
    """In-memory event transport. Events stored in class-level list for test inspection."""

    events: ClassVar[list[StreamEvent]] = []

    def publish(self, event: StreamEvent) -> None:
        """Store event in memory."""
        MemoryTransport.events.append(event)

    def consume(self) -> Iterator[StreamEvent]:
        """Yield all stored events."""
        yield from MemoryTransport.events

    @classmethod
    def from_url(cls, url: str) -> Self:
        """Create transport (ignores URL)."""
        del url
        return cls()

    @classmethod
    def clear(cls) -> None:
        """Clear all stored events."""
        cls.events.clear()
