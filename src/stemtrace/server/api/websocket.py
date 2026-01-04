"""WebSocket endpoint for real-time event streaming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from stemtrace.server.fastapi.form_auth import (
    FormAuthConfig,
    is_authenticated_cookie,
    parse_cookie_header,
)

if TYPE_CHECKING:
    from stemtrace.server.websocket import WebSocketManager


def create_websocket_router(
    ws_manager: WebSocketManager, *, form_auth_config: FormAuthConfig | None = None
) -> APIRouter:
    """Create WebSocket router for real-time task events."""
    router = APIRouter(tags=["stemtrace-websocket"])

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Handle WebSocket connection for real-time task events."""
        if form_auth_config is not None:
            cookies = parse_cookie_header(websocket.headers.get("cookie"))
            cookie_value = cookies.get(form_auth_config.cookie_name)
            if not is_authenticated_cookie(
                cookie_value,
                secret=form_auth_config.secret,
                expected_username=form_auth_config.username,
            ):
                # Close without registering the connection.
                await websocket.accept()
                await websocket.close(code=1008)
                return

        await ws_manager.connect(websocket)
        try:
            await ws_manager.listen(websocket)
        except WebSocketDisconnect:
            pass
        finally:
            ws_manager.disconnect(websocket)

    return router
