"""WebSocket endpoint for real-time event streaming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from stemtrace.server.fastapi.api_key import (
    WS_SUBPROTOCOL,
    ApiKeyGuard,
    KeyStatus,
    bearer_from_subprotocols,
    bearer_token,
)
from stemtrace.server.fastapi.form_auth import (
    FormAuthConfig,
    SessionStatus,
    check_session_cookie,
    parse_cookie_header,
    warn_session_secret_mismatch,
)

if TYPE_CHECKING:
    from stemtrace.server.websocket import WebSocketManager


def _session_status(
    websocket: WebSocket, form_auth_config: FormAuthConfig
) -> SessionStatus:
    cookies = parse_cookie_header(websocket.headers.get("cookie"))
    status = check_session_cookie(
        cookies.get(form_auth_config.cookie_name), form_auth_config
    )
    if status is SessionStatus.SECRET_MISMATCH:
        # Same rate-limited warning as for HTTP requests; the client only sees
        # the plain 403 (no reason). A forged, unsigned cookie can trigger it
        # too; accepted, see check_session_cookie.
        warn_session_secret_mismatch()
    return status


def _authorize(
    websocket: WebSocket,
    *,
    form_auth_config: FormAuthConfig | None,
    api_key_guard: ApiKeyGuard | None,
) -> tuple[bool, str | None]:
    """Decide whether to accept the handshake, and with which subprotocol.

    With a key configured, the key comes from the Authorization header or, for
    browsers (which can't set WebSocket headers), from a
    ``stemtrace.bearer.<base64url>`` subprotocol; the connection is then
    accepted with the ``stemtrace`` subprotocol if the client offered it, never
    echoing the key. A wrong key is refused even with a valid session cookie.
    Without a key, a valid session cookie is needed when form login is on.
    """
    if api_key_guard is not None:
        offered = list(websocket.scope.get("subprotocols") or [])
        token = bearer_token(websocket.headers.get("authorization"))
        if token is None:
            token = bearer_from_subprotocols(offered)
        client_ip = websocket.client.host if websocket.client else None
        result = api_key_guard.check(token, client_ip)
        subprotocol = WS_SUBPROTOCOL if WS_SUBPROTOCOL in offered else None
        if result.status is KeyStatus.VALID:
            return True, subprotocol
        if result.status is not KeyStatus.ABSENT or form_auth_config is None:
            return False, None
    if form_auth_config is None:
        return True, None
    return _session_status(websocket, form_auth_config) is SessionStatus.VALID, None


def create_websocket_router(
    ws_manager: WebSocketManager,
    *,
    form_auth_config: FormAuthConfig | None = None,
    api_key_guard: ApiKeyGuard | None = None,
) -> APIRouter:
    """Create WebSocket router for real-time task events."""
    router = APIRouter(tags=["stemtrace-websocket"])

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Handle WebSocket connection for real-time task events."""
        allowed, subprotocol = _authorize(
            websocket, form_auth_config=form_auth_config, api_key_guard=api_key_guard
        )
        if not allowed:
            # Deny the handshake without completing it. Accepting first and
            # then closing half-opens the connection; if the unauthenticated
            # client aborts during that dance, uvicorn's legacy websockets
            # impl runs close() on a protocol whose transfer_data_task was
            # never set and raises AttributeError. Closing before accept lets
            # Starlette reject the handshake (HTTP 403) without opening it.
            await websocket.close(code=1008)
            return

        await ws_manager.connect(websocket, subprotocol=subprotocol)
        try:
            await ws_manager.listen(websocket)
        except WebSocketDisconnect:
            pass
        finally:
            ws_manager.disconnect(websocket)

    return router
