"""FastAPI router factory for stemtrace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from stemtrace.server.api.routes import create_api_router
from stemtrace.server.api.websocket import create_websocket_router
from stemtrace.server.store import GraphStore, WorkerRegistry
from stemtrace.server.websocket import WebSocketManager

if TYPE_CHECKING:
    from stemtrace.server.consumer import AsyncEventConsumer
    from stemtrace.server.fastapi.form_auth import FormAuthConfig


def create_router(
    store: GraphStore | None = None,
    consumer: AsyncEventConsumer | None = None,
    ws_manager: WebSocketManager | None = None,
    worker_registry: WorkerRegistry | None = None,
    broker_url: str | None = None,
    auth_dependency: Any = None,
    form_auth_config: FormAuthConfig | None = None,
) -> APIRouter:
    """Create API router. For embedded consumer, use StemtraceExtension."""
    if store is None:
        store = GraphStore()
    if ws_manager is None:
        ws_manager = WebSocketManager()
    if worker_registry is None:
        worker_registry = WorkerRegistry()

    router = APIRouter()
    api_router = create_api_router(
        store,
        consumer,
        ws_manager,
        worker_registry,
        broker_url=broker_url,
    )
    ws_router = create_websocket_router(ws_manager, form_auth_config=form_auth_config)

    dependencies: list[Any] = []
    if auth_dependency is not None:
        dependencies.append(auth_dependency)

    router.include_router(api_router, dependencies=dependencies)
    router.include_router(ws_router)

    return router
