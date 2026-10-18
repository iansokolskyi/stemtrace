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
    from stemtrace.server.groups import GroupsInput


def create_router(  # noqa: PLR0917  public API (stemtrace.create_router): positional callers must keep working
    store: GraphStore | None = None,
    consumer: AsyncEventConsumer | None = None,
    ws_manager: WebSocketManager | None = None,
    worker_registry: WorkerRegistry | None = None,
    broker_url: str | None = None,
    auth_dependency: Any = None,
    form_auth_config: FormAuthConfig | None = None,
    node_alias_from_arguments: str | None = None,
    groups: GroupsInput = None,
    api_key: str | None = None,
    auth_throttle: LoginThrottle | None = None,
) -> APIRouter:
    """Create API router. For embedded consumer, use StemtraceExtension.

    With ``form_auth_config``, a valid session cookie is accepted instead of the
    API key. With ``auth_dependency`` too, a JSON API request needs both; the
    WebSocket (not covered by ``auth_dependency``) needs the key.

    Args:
        store: Graph store to serve; a new empty one if None.
        consumer: Event consumer whose state the health endpoints report.
        ws_manager: WebSocket manager; a new one if None.
        worker_registry: Worker registry; a new one if None.
        broker_url: Celery broker URL for on-demand worker inspection.
        auth_dependency: FastAPI dependency applied to the JSON API routes.
        form_auth_config: Cookie-session configuration for the API and WebSocket.
        node_alias_from_arguments: Key to derive graph node display names from
            task arguments (digit string for args[index], else kwargs[key]).
        groups: Code-defined task groups, as for ``StemtraceExtension`` (a list
            of GroupRule or dicts, or a TOML file path); None gives automatic
            groups only.
        api_key: Requires ``Authorization: Bearer <key>`` on the JSON API
            (except the liveness probe) and the WebSocket.
        auth_throttle: Counts failed keys per client IP (a new one if None;
            pass the login's to share its counts).

    Raises:
        ConfigurationError: Here, not on the first request, if ``groups`` is
            invalid or its file can't be loaded, or ``api_key`` is shorter
            than 16 characters or has characters other than visible ASCII.
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
        groups=groups,
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
