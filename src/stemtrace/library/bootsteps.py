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
from stemtrace.library.config import get_config

# Sibling-module imports within stemtrace.library: the chord-id, nodename and
# destination parsing and the transport publisher are shared with the signal
# handlers, so RECEIVED can't drift from STARTED. They stay underscore-private
# because they aren't part of the public API, which this package boundary
# doesn't change.
from stemtrace.library.signals import (
    _delivered_destination,
    _extract_chord_info,
    _nodename,
    on_worker_terminating,
)
from stemtrace.library.signals import _publish_event as _publish_via_transport

if TYPE_CHECKING:
    from collections.abc import Callable

    from celery.worker.consumer import Consumer

logger = logging.getLogger(__name__)

# Set by register_bootsteps(). With the prefork pool the consumer, and so this
# step, runs in the worker's main process, not in the pool children.
_publish_event: Callable[[TaskEvent], None] | None = None


def _set_publisher(publisher: Callable[[TaskEvent], None] | None) -> None:
    """Set the function RECEIVED events are published with (None: don't publish)."""
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
        self._hostname: str | None = None

    def start(self, consumer: Consumer) -> None:
        """Wrap task strategies to emit RECEIVED events."""
        logger.debug("ReceivedEventStep: wrapping task strategies")
        self._hostname = _nodename(consumer)

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
        chord: Any = None
        retries: int = 0

        if isinstance(body, list | tuple) and len(body) >= 3:
            # Protocol 2: (args, kwargs, embed). Celery 5.6's consumer passes no
            # body for it (decoding is left to the pool), so headers carry the ids.
            embed = body[2] if len(body) > 2 else {}
            if isinstance(embed, dict):
                task_id = embed.get("id")
                parent_id = embed.get("parent_id")
                root_id = embed.get("root_id")
                group_id = embed.get("group")
                chord = embed.get("chord")
                retries = embed.get("retries", 0) or 0
        elif isinstance(body, dict):
            # Protocol 1: {'task': ..., 'id': ..., ...}
            task_id = body.get("id")
            parent_id = body.get("parent_id")
            root_id = body.get("root_id")
            group_id = body.get("group")
            chord = body.get("chord")
            retries = body.get("retries", 0) or 0

        # Fallback to message headers/properties
        if hasattr(message, "headers"):
            headers = message.headers or {}
            if task_id is None:
                task_id = headers.get("id")
            parent_id = parent_id or headers.get("parent_id")
            root_id = root_id or headers.get("root_id")
            group_id = group_id or headers.get("group")
            chord = chord or headers.get("chord")
            if retries == 0:
                retries = headers.get("retries", 0) or 0

        if task_id is None:
            logger.debug("Could not extract task_id for RECEIVED event")
            return

        # Skip RECEIVED for retries - they already have RETRY events
        if retries > 0:
            logger.debug("Skipping RECEIVED for retry %d of task %s", retries, task_id)
            return

        # A chord header's message carries the chord callback signature (a dict
        # once deserialized); read its ids the way STARTED reads request.chord.
        chord_id, chord_callback_id = _extract_chord_info(chord)
        queue, routing_key = _delivered_destination(
            getattr(message, "delivery_info", None)
        )
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
                chord_callback_id=chord_callback_id,
                hostname=self._hostname,
                queue=queue,
                routing_key=routing_key,
            )
        )
        logger.debug("Emitted RECEIVED for task %s", task_id)


class WorkerTerminatingStep(bootsteps.StartStopStep):  # type: ignore[misc]
    """Publish WORKER_TERMINATING when a worker stops without sending worker_shutdown.

    Celery's cold-stop path (``WorkController.terminate``) skips
    ``worker_shutdown``, so without this the server would keep the worker online.
    """

    # After the Pool at start, so before it on terminate.
    requires: ClassVar[set[str]] = {"celery.worker.components:Pool"}

    def terminate(self, worker: Any) -> None:
        """Report that this worker process is ending without worker_shutdown."""
        # celery 5.6.3 Blueprint.stop calls terminate() on a cold stop and
        # stop() (inherited, a no-op) on a warm one.
        on_worker_terminating(worker)


def register_bootsteps(
    app: Any, *, publish: Callable[[TaskEvent], None] | None = None
) -> None:
    """Register stemtrace's bootsteps with the Celery app.

    Once ``init_worker`` ran, ``WorkerTerminatingStep`` is always registered:
    it does nothing until a cold shutdown. The RECEIVED step adds a
    synchronous publish to the worker's consumer loop for every task
    message, so it is registered only when asked for.

    Args:
        app: The Celery application instance.
        publish: Publishes the RECEIVED events and registers the step. It runs
            in the worker's consumer loop for every task message, so it must
            not raise. When omitted, the step is registered only if
            ``init_worker(publish_received=True)`` ran, and publishes through
            the transport it set up, like the task signal handlers do. Without
            that, this does nothing (as in 0.3.10, where the step never
            published).
    """
    config = get_config()
    if config is not None:
        app.steps["worker"].add(WorkerTerminatingStep)
        logger.debug("Registered WorkerTerminatingStep bootstep")
    if publish is None:
        if config is None or not config.publish_received:
            return
        publish = _publish_via_transport
    _set_publisher(publish)
    app.steps["consumer"].add(ReceivedEventStep)
    logger.debug("Registered ReceivedEventStep bootstep")
