"""WebSocket endpoint for real-time event streaming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from stemtrace.server.websocket import WebSocketManager


def create_websocket_router(ws_manager: WebSocketManager) -> APIRouter:
    """Create WebSocket router for real-time task events."""
    router = APIRouter(tags=["stemtrace-websocket"])

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Handle WebSocket connection for real-time task events."""
        await ws_manager.connect(websocket)
        try:
            await ws_manager.listen(websocket)
        except WebSocketDisconnect:
            pass
        finally:
            ws_manager.disconnect(websocket)

    return router
