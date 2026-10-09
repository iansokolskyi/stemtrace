"""WebSocket endpoint for real-time event streaming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from stemtrace.server.fastapi.form_auth import (
    FormAuthConfig,
    SessionStatus,
    check_session_cookie,
    parse_cookie_header,
    warn_session_secret_mismatch,
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
            status = check_session_cookie(cookie_value, form_auth_config)
            if status is not SessionStatus.VALID:
                if status is SessionStatus.SECRET_MISMATCH:
                    # Same rate-limited warning as for HTTP requests; the
                    # client only sees the plain 403 (no reason). A forged,
                    # unsigned cookie can trigger it too; accepted, see
                    # check_session_cookie.
                    warn_session_secret_mismatch()
                # Deny the handshake without completing it. Accepting first and
                # then closing half-opens the connection; if the unauthenticated
                # client aborts during that dance, uvicorn's legacy websockets
                # impl runs close() on a protocol whose transfer_data_task was
                # never set and raises AttributeError. Closing before accept lets
                # Starlette reject the handshake (HTTP 403) without opening it.
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
