"""Server component - Web server for visualization."""

from stemtrace.server.consumer import AsyncEventConsumer, EventConsumer
from stemtrace.server.fastapi import (
    StemtraceExtension,
    create_router,
    no_auth,
    require_api_key,
    require_basic_auth,
)
from stemtrace.server.store import GraphStore
from stemtrace.server.websocket import WebSocketManager

__all__ = [
    "AsyncEventConsumer",
    "EventConsumer",
    "GraphStore",
    "StemtraceExtension",
    "WebSocketManager",
    "create_router",
    "no_auth",
    "require_api_key",
    "require_basic_auth",
]
