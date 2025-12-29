"""Full-featured FastAPI extension with embedded consumer."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from stemtrace.server.consumer import AsyncEventConsumer
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore
from stemtrace.server.ui.static import get_static_router
from stemtrace.server.websocket import WebSocketManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import APIRouter, FastAPI


class StemtraceExtension:
    """Complete FastAPI integration: store, consumer, WebSocket, and router.

    Example:
        flow = StemtraceExtension(broker_url="redis://localhost:6379")
        app = FastAPI(lifespan=flow.lifespan)
        app.include_router(flow.router, prefix="/stemtrace")
    """

    def __init__(
        self,
        broker_url: str,
        *,
        embedded_consumer: bool = True,
        serve_ui: bool = True,
        prefix: str = "stemtrace",
        ttl: int = 86400,
        max_nodes: int = 10000,
        auth_dependency: Any = None,
    ) -> None:
        """Initialize extension with broker URL and optional configuration."""
        self._broker_url = broker_url
        self._embedded_consumer = embedded_consumer
        self._serve_ui = serve_ui
        self._prefix = prefix
        self._ttl = ttl
        self._auth_dependency = auth_dependency

        self._store = GraphStore(max_nodes=max_nodes)
        self._ws_manager = WebSocketManager()
        self._consumer: AsyncEventConsumer | None = None

        if embedded_consumer:
            self._consumer = AsyncEventConsumer(
                broker_url,
                self._store,
                prefix=prefix,
                ttl=ttl,
            )

        self._store.add_listener(self._ws_manager.queue_event)

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
    def router(self) -> APIRouter:
        """Pre-configured router. Mount with app.include_router()."""
        router = create_router(
            store=self._store,
            consumer=self._consumer,
            ws_manager=self._ws_manager,
            auth_dependency=self._auth_dependency,
        )

        if self._serve_ui:
            ui_router = get_static_router()
            if ui_router is not None:
                router.include_router(ui_router)

        return router

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG002
        """FastAPI lifespan: starts consumer and WebSocket on startup."""
        await self._ws_manager.start_broadcast_loop()
        if self._consumer is not None:
            self._consumer.start()

        try:
            yield
        finally:
            if self._consumer is not None:
                self._consumer.stop()
            await self._ws_manager.stop_broadcast_loop()

    def compose_lifespan(self, other_lifespan: Any = None) -> Any:
        """Compose with another lifespan context manager."""

        @asynccontextmanager
        async def combined(app: FastAPI) -> AsyncIterator[None]:
            """Combined lifespan context manager."""
            async with self.lifespan(app):
                if other_lifespan is not None:
                    async with other_lifespan(app):
                        yield
                else:
                    yield

        return combined
