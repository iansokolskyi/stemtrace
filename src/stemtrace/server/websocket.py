"""WebSocket connection manager for real-time event broadcasting."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

    from stemtrace.core.events import TaskEvent

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Thread-safe WebSocket manager that broadcasts events to connected clients."""

    def __init__(self) -> None:
        """Initialize WebSocket manager with empty connection set."""
        self._connections: set[WebSocket] = set()
        self._queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
        self._broadcast_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connection_count(self) -> int:
        """Number of active connections."""
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a WebSocket."""
        await websocket.accept()
        self._connections.add(websocket)
        logger.debug("WebSocket connected, total: %d", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        """Unregister a WebSocket."""
        self._connections.discard(websocket)
        logger.debug("WebSocket disconnected, total: %d", len(self._connections))

    def queue_event(self, event: TaskEvent) -> None:
        """Queue event for broadcast. Thread-safe, called from consumer thread."""
        if self._loop is None:
            return

        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(lambda: self._queue.put_nowait(event))

    async def broadcast(self, event: TaskEvent) -> None:
        """Send event to all connected clients."""
        if not self._connections:
            return

        message = event.model_dump_json()
        disconnected: list[WebSocket] = []

        for websocket in self._connections.copy():
            try:
                await websocket.send_text(message)
            except Exception:
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
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        logger.debug("WebSocket broadcast loop started")

    async def stop_broadcast_loop(self) -> None:
        """Stop background task. Call during lifespan shutdown."""
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._broadcast_task
            self._broadcast_task = None
        self._loop = None
        logger.debug("WebSocket broadcast loop stopped")

    async def _broadcast_loop(self) -> None:
        while True:
            try:
                event = await self._queue.get()
                await self.broadcast(event)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in broadcast loop")
