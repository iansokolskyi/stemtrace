"""Celery bootsteps for capturing task lifecycle events.

Bootsteps hook into Celery's worker consumer pipeline to capture
events that don't have dedicated signals (like RECEIVED).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from celery import bootsteps

from stemtrace.core.events import TaskEvent, TaskState

if TYPE_CHECKING:
    from celery.worker.consumer import Consumer

logger = logging.getLogger(__name__)

# Import here to avoid circular imports
_publish_event: Any = None


def _set_publisher(publisher: Any) -> None:
    """Set the event publisher function (called from signals module)."""
    global _publish_event
    _publish_event = publisher


# Celery's bootsteps module is not fully typed
class ReceivedEventStep(bootsteps.ConsumerStep):  # type: ignore[misc]
    """Bootstep that emits RECEIVED events when tasks are received by worker.

    This hooks into the consumer's task message handling to capture the moment
    a task message is received from the broker, before it's executed.
    """

    requires: ClassVar[set[str]] = {"celery.worker.consumer.tasks:Tasks"}

    def __init__(
        self,
        consumer: Consumer,
        **kwargs: Any,
    ) -> None:
        """Initialize the bootstep."""
        super().__init__(consumer, **kwargs)
        self._original_strategy: dict[str, Any] = {}

    def start(self, consumer: Consumer) -> None:
        """Wrap task strategies to emit RECEIVED events."""
        logger.debug("ReceivedEventStep: wrapping task strategies")

        # Wrap each task's strategy to emit RECEIVED before execution
        for name, strategy in list(consumer.strategies.items()):
            if name not in self._original_strategy:
                self._original_strategy[name] = strategy
                consumer.strategies[name] = self._wrap_strategy(name, strategy)

    def stop(self, consumer: Consumer) -> None:
        """Restore original strategies."""
        for name, strategy in self._original_strategy.items():
            if name in consumer.strategies:
                consumer.strategies[name] = strategy
        self._original_strategy.clear()

    def _wrap_strategy(self, task_name: str, strategy: Any) -> Any:
        """Wrap a task strategy to emit RECEIVED before execution."""

        def wrapped_strategy(
            message: Any,
            body: Any,
            ack: Any,
            reject: Any,
            callbacks: Any,
            **kwargs: Any,
        ) -> Any:
            """Emit RECEIVED event before delegating to original strategy."""
            # Emit RECEIVED event
            try:
                self._emit_received(task_name, body, message)
            except Exception:
                logger.debug("Failed to emit RECEIVED event", exc_info=True)

            # Call original strategy
            return strategy(message, body, ack, reject, callbacks, **kwargs)

        return wrapped_strategy

    def _emit_received(self, task_name: str, body: Any, message: Any) -> None:
        """Emit a RECEIVED event for the task.

        Skips retries since they already have RETRY + STARTED events.
        """
        if _publish_event is None:
            return

        # Extract task ID and retries from body (can be tuple or dict format)
        task_id: str | None = None
        parent_id: str | None = None
        root_id: str | None = None
        group_id: str | None = None
        chord_id: str | None = None
        retries: int = 0

        if isinstance(body, list | tuple) and len(body) >= 3:
            # Old format: (args, kwargs, embed)
            embed = body[2] if len(body) > 2 else {}
            if isinstance(embed, dict):
                task_id = embed.get("id")
                parent_id = embed.get("parent_id")
                root_id = embed.get("root_id")
                group_id = embed.get("group")
                chord_id = embed.get("chord")
                retries = embed.get("retries", 0) or 0
        elif isinstance(body, dict):
            # New format: {'task': ..., 'id': ..., ...}
            task_id = body.get("id")
            parent_id = body.get("parent_id")
            root_id = body.get("root_id")
            group_id = body.get("group")
            chord_id = body.get("chord")
            retries = body.get("retries", 0) or 0

        # Fallback to message headers/properties
        if hasattr(message, "headers"):
            headers = message.headers or {}
            if task_id is None:
                task_id = headers.get("id")
            parent_id = parent_id or headers.get("parent_id")
            root_id = root_id or headers.get("root_id")
            group_id = group_id or headers.get("group")
            chord_id = chord_id or headers.get("chord")
            if retries == 0:
                retries = headers.get("retries", 0) or 0

        if task_id is None:
            logger.debug("Could not extract task_id for RECEIVED event")
            return

        # Skip RECEIVED for retries - they already have RETRY events
        if retries > 0:
            logger.debug("Skipping RECEIVED for retry %d of task %s", retries, task_id)
            return

        _publish_event(
            TaskEvent(
                task_id=task_id,
                name=task_name,
                state=TaskState.RECEIVED,
                timestamp=datetime.now(timezone.utc),
                parent_id=parent_id,
                root_id=root_id,
                group_id=group_id,
                chord_id=chord_id,
            )
        )
        logger.debug("Emitted RECEIVED for task %s", task_id)


def register_bootsteps(app: Any) -> None:
    """Register stemtrace bootsteps with the Celery app.

    Args:
        app: The Celery application instance.
    """
    app.steps["consumer"].add(ReceivedEventStep)
    logger.debug("Registered ReceivedEventStep bootstep")
