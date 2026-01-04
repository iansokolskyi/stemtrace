"""Full-featured FastAPI extension with embedded consumer."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, overload

from fastapi.responses import RedirectResponse

from stemtrace.server.consumer import AsyncEventConsumer
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore, WorkerRegistry
from stemtrace.server.ui.static import get_static_router
from stemtrace.server.websocket import WebSocketManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from fastapi import APIRouter, FastAPI

    from stemtrace.server.fastapi.form_auth import FormAuthConfig

    LifespanNone = Callable[[Any], AbstractAsyncContextManager[None, bool | None]]
    LifespanState = Callable[
        [Any],
        AbstractAsyncContextManager[Mapping[str, Any], bool | None],
    ]
    Lifespan = LifespanNone | LifespanState


@asynccontextmanager
async def _null_lifespan(app: Any) -> AsyncIterator[None]:  # noqa: ARG001
    yield


class StemtraceExtension:
    """Complete FastAPI integration: store, consumer, WebSocket, and router.

    Example (recommended):
        app = FastAPI(lifespan=my_lifespan)
        stemtrace = StemtraceExtension(broker_url="redis://localhost:6379")
        stemtrace.init_app(app)  # Wraps lifespan + adds router

    Example (RabbitMQ broker + Redis event transport):
        # Celery uses RabbitMQ, but stemtrace events go to Redis for replay across restarts.
        stemtrace = StemtraceExtension(
            broker_url="amqp://guest:guest@localhost:5672//",
            transport_url="redis://localhost:6379/0",
        )
        stemtrace.init_app(app)

    Example (manual):
        stemtrace = StemtraceExtension(broker_url="redis://localhost:6379")
        app = FastAPI(lifespan=stemtrace.lifespan)
        app.include_router(stemtrace.router, prefix="/stemtrace")
    """

    def __init__(
        self,
        broker_url: str,
        *,
        transport_url: str | None = None,
        embedded_consumer: bool = True,
        serve_ui: bool = True,
        prefix: str = "/stemtrace",
        ttl: int = 86400,
        max_nodes: int = 10000,
        auth_dependency: Any = None,
        form_auth_config: FormAuthConfig | None = None,
    ) -> None:
        """Initialize extension with broker and transport configuration.

        Args:
            broker_url: Celery broker URL used for on-demand inspection (workers/registry).
            transport_url: Event transport URL used to consume stemtrace events. If None,
                defaults to broker_url.
            embedded_consumer: Whether to start the event consumer inside the FastAPI process.
            serve_ui: Whether to serve the bundled React UI.
            prefix: Mount path for stemtrace routes (also used as event prefix after normalization).
            ttl: Event retention window in seconds (transport-specific).
            max_nodes: Maximum number of nodes to keep in memory.
            auth_dependency: Optional FastAPI dependency applied to all routes for authentication.
            form_auth_config: Optional cookie-session configuration used to protect WebSocket.
        """
        self._broker_url = broker_url
        self._transport_url = transport_url or broker_url
        self._serve_ui = serve_ui
        self._prefix = self._normalize_prefix(prefix)
        self._auth_dependency = auth_dependency
        self._form_auth_config = form_auth_config

        self._store = GraphStore(max_nodes=max_nodes)
        self._worker_registry = WorkerRegistry()
        self._ws_manager = WebSocketManager()
        self._consumer: AsyncEventConsumer | None = None

        if embedded_consumer:
            self._consumer = AsyncEventConsumer(
                self._transport_url,
                self._store,
                prefix=self._prefix,
                ttl=ttl,
                worker_registry=self._worker_registry,
            )

        self._store.add_listener(self._ws_manager.queue_event)

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        """Normalize a mount/stream prefix by stripping leading/trailing slashes."""
        return prefix.strip("/")

    @property
    def store(self) -> GraphStore:
        """The in-memory graph store."""
        return self._store

    @property
    def consumer(self) -> AsyncEventConsumer | None:
        """Event consumer, or None if embedded_consumer=False."""
        return self._consumer

    @property
    def ws_manager(self) -> WebSocketManager:
        """The WebSocket connection manager."""
        return self._ws_manager

    @property
    def worker_registry(self) -> WorkerRegistry:
        """The worker registry for lifecycle tracking."""
        return self._worker_registry

    @property
    def router(self) -> APIRouter:
        """Pre-configured router. Mount with app.include_router()."""
        router = create_router(
            store=self._store,
            consumer=self._consumer,
            ws_manager=self._ws_manager,
            worker_registry=self._worker_registry,
            broker_url=self._broker_url,
            auth_dependency=self._auth_dependency,
            form_auth_config=self._form_auth_config,
        )

        if self._serve_ui:
            ui_router = get_static_router(
                show_logout=self._form_auth_config is not None
            )
            if ui_router is not None:
                router.include_router(ui_router)

        return router

    def init_app(self, app: FastAPI, *, prefix: str | None = None) -> None:
        """Initialize stemtrace on a FastAPI app. Wraps lifespan and adds router."""
        mount_prefix = f"/{self._normalize_prefix(prefix or self._prefix)}"

        original_lifespan = app.router.lifespan_context
        app.router.lifespan_context = self._wrap_lifespan(original_lifespan)

        # Add redirect for no-trailing-slash (prevents fall-through to catch-all mounts)
        @app.get(mount_prefix, include_in_schema=False)
        async def _stemtrace_redirect() -> RedirectResponse:
            return RedirectResponse(url=f"{mount_prefix}/", status_code=307)

        app.include_router(self.router, prefix=mount_prefix)

    @overload
    def _wrap_lifespan(self, original: LifespanNone) -> LifespanNone: ...

    @overload
    def _wrap_lifespan(self, original: LifespanState) -> LifespanState: ...

    def _wrap_lifespan(self, original: Any) -> Any:
        """Wrap a lifespan with stemtrace startup/shutdown."""

        @asynccontextmanager
        async def _wrapped(app: Any) -> AsyncIterator[Any]:
            await self._ws_manager.start_broadcast_loop()
            if self._consumer is not None:
                self._consumer.start()
            try:
                async with original(app) as state:
                    yield state
            finally:
                if self._consumer is not None:
                    self._consumer.stop()
                await self._ws_manager.stop_broadcast_loop()

        return _wrapped

    @property
    def lifespan(self) -> LifespanNone:
        """Standalone lifespan for manual setup (use init_app instead)."""
        return self._wrap_lifespan(_null_lifespan)

    def compose_lifespan(self, other_lifespan: Lifespan) -> Lifespan:
        """Compose stemtrace lifespan with another lifespan (for manual setup)."""
        return self._wrap_lifespan(other_lifespan)
