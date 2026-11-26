"""WebSocket connection manager for real-time event broadcasting."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

    from stemtrace.core.events import TaskEvent

logger = logging.getLogger(__name__)

# Backoff after an unexpected broadcast-loop error, and the gap between
# summary log lines while the failure continues.
LOOP_ERROR_BACKOFF_INITIAL_S = 0.5
LOOP_ERROR_BACKOFF_MAX_S = 30.0
LOOP_ERROR_LOG_INTERVAL_S = 60.0


class WebSocketManager:
    """Thread-safe WebSocket manager that broadcasts events to connected clients."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize WebSocket manager with empty connection set.

        Args:
            sleep: Awaitable sleep used to back off after a loop error.
            clock: Monotonic clock used to rate-limit error logs.
        """
        self._connections: set[WebSocket] = set()
        # Created per lifespan: an asyncio.Queue binds to the first loop that awaits it.
        self._queue: asyncio.Queue[TaskEvent] | None = None
        self._sleep = sleep
        self._clock = clock
        self._broadcast_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connection_count(self) -> int:
        """Number of active connections."""
        return len(self._connections)

    async def connect(
        self, websocket: WebSocket, *, subprotocol: str | None = None
    ) -> None:
        """Accept and register a WebSocket, selecting ``subprotocol`` if given."""
        await websocket.accept(subprotocol=subprotocol)
        self._connections.add(websocket)
        logger.debug("WebSocket connected, total: %d", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        """Unregister a WebSocket."""
        self._connections.discard(websocket)
        logger.debug("WebSocket disconnected, total: %d", len(self._connections))

    def queue_event(self, event: TaskEvent) -> None:
        """Queue event for broadcast. Thread-safe, called from consumer thread."""
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None:
            return

        def _put_event() -> None:
            queue.put_nowait(event)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_put_event)

    async def broadcast(self, event: TaskEvent) -> None:
        """Send event to all connected clients."""
        if not self._connections:
            return

        # Captured task output can be large and the UI only needs to know that
        # something changed; GET /api/tasks/{id} serves the output.
        message = event.model_dump_json(exclude={"output"})
        disconnected: list[WebSocket] = []

        for websocket in self._connections.copy():
            try:
                await websocket.send_text(message)
            except Exception:  # noqa: BLE001  a failing client is dropped; the broadcast goes on
                disconnected.append(websocket)

        for websocket in disconnected:
            self.disconnect(websocket)

    async def listen(self, websocket: WebSocket) -> None:
        """Block until client disconnects."""
        with contextlib.suppress(Exception):
            while True:
                await websocket.receive_text()

    async def start_broadcast_loop(self) -> None:
        """Start background task. Call during lifespan startup."""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._broadcast_task = asyncio.create_task(self._broadcast_loop(self._queue))
        logger.debug("WebSocket broadcast loop started")

    async def stop_broadcast_loop(self) -> None:
        """Stop background task. Call during lifespan shutdown."""
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._broadcast_task
            self._broadcast_task = None
        self._loop = None
        self._queue = None
        logger.debug("WebSocket broadcast loop stopped")

    async def _broadcast_loop(self, queue: asyncio.Queue[TaskEvent]) -> None:
        failures = 0
        last_log: float | None = None
        delay = LOOP_ERROR_BACKOFF_INITIAL_S
        while True:
            try:
                event = await queue.get()
                await self.broadcast(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001  the loop must outlive any one failure
                failures += 1
                now = self._clock()
                if last_log is None:
                    logger.exception("Error in broadcast loop")
                    last_log = now
                elif now - last_log >= LOOP_ERROR_LOG_INTERVAL_S:
                    logger.error(
                        "Broadcast loop still failing (%d errors, last: %s)",
                        failures,
                        exc,
                    )
                    last_log = now
                # Back off so a persistent error cannot spin the event loop.
                await self._sleep(delay)
                delay = min(delay * 2, LOOP_ERROR_BACKOFF_MAX_S)
            else:
                if last_log is not None:
                    logger.error("Broadcast loop recovered after %d errors", failures)
                failures = 0
                last_log = None
                delay = LOOP_ERROR_BACKOFF_INITIAL_S
