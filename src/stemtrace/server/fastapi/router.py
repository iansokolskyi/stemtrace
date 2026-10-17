"""FastAPI router factory for stemtrace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from stemtrace.server.api.routes import create_api_router
from stemtrace.server.api.websocket import create_websocket_router
from stemtrace.server.fastapi.api_key import (
    ApiKeyGuard,
    api_key_dependency,
    normalize_api_key,
)
from stemtrace.server.fastapi.login_throttle import LoginThrottle
from stemtrace.server.store import GraphStore, WorkerRegistry
from stemtrace.server.websocket import WebSocketManager

if TYPE_CHECKING:
    from stemtrace.server.consumer import AsyncEventConsumer
    from stemtrace.server.fastapi.form_auth import FormAuthConfig


def create_router(  # noqa: PLR0917  public API (stemtrace.create_router): positional callers must keep working
    store: GraphStore | None = None,
    consumer: AsyncEventConsumer | None = None,
    ws_manager: WebSocketManager | None = None,
    worker_registry: WorkerRegistry | None = None,
    broker_url: str | None = None,
    auth_dependency: Any = None,
    form_auth_config: FormAuthConfig | None = None,
    node_alias_from_arguments: str | None = None,
    api_key: str | None = None,
    auth_throttle: LoginThrottle | None = None,
) -> APIRouter:
    """Create API router. For embedded consumer, use StemtraceExtension.

    ``api_key`` requires ``Authorization: Bearer <key>`` on the JSON API
    (except the liveness probe) and the WebSocket; with ``form_auth_config``,
    a valid session cookie is accepted instead. With ``auth_dependency`` too,
    a JSON API request needs both; the WebSocket (not covered by
    ``auth_dependency``) needs the key. ``auth_throttle`` counts failed keys per client IP
    (a new one if None; pass the login's to share its counts).

    Raises:
        ConfigurationError: If ``api_key`` is shorter than 16 characters or
            has characters other than visible ASCII.
    """
    if store is None:
        store = GraphStore()
    if ws_manager is None:
        ws_manager = WebSocketManager()
    if worker_registry is None:
        worker_registry = WorkerRegistry()

    key = normalize_api_key(api_key)
    guard = ApiKeyGuard(key, auth_throttle or LoginThrottle()) if key else None

    router = APIRouter()
    api_router = create_api_router(
        store,
        consumer=consumer,
        ws_manager=ws_manager,
        worker_registry=worker_registry,
        broker_url=broker_url,
        node_alias_from_arguments=node_alias_from_arguments,
    )
    ws_router = create_websocket_router(
        ws_manager, form_auth_config=form_auth_config, api_key_guard=guard
    )

    dependencies: list[Any] = []
    if guard is not None:
        dependencies.append(api_key_dependency(guard, form_auth_config))
    if auth_dependency is not None:
        dependencies.append(auth_dependency)

    router.include_router(api_router, dependencies=dependencies)
    router.include_router(ws_router)

    return router
