"""stemtrace: A lightweight Celery task flow visualizer.

Usage:
    import stemtrace

    # Worker instrumentation
    stemtrace.init_worker(app)

    # FastAPI embedding
    stemtrace.init_app(app)

    # Introspection
    stemtrace.is_initialized()  # -> bool
    stemtrace.get_config()      # -> StemtraceConfig | None
    stemtrace.get_transport()   # -> EventTransport | None
"""

import os
from typing import TYPE_CHECKING, Any

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.core.graph import TaskGraph, TaskNode
from stemtrace.core.ports import EventTransport
from stemtrace.library.bootsteps import register_bootsteps
from stemtrace.library.config import StemtraceConfig, _reset_config, set_config
from stemtrace.library.config import get_config as _get_config
from stemtrace.library.signals import connect_signals
from stemtrace.library.transports import get_transport as _get_transport
from stemtrace.server.fastapi import (
    StemtraceExtension,
    create_router,
    require_api_key,
    require_basic_auth,
)

if TYPE_CHECKING:
    from celery import Celery
    from fastapi import FastAPI

__version__ = "0.2.2"
__all__ = [
    "ConfigurationError",
    "StemtraceConfig",
    "StemtraceExtension",
    "TaskEvent",
    "TaskGraph",
    "TaskNode",
    "TaskState",
    "__version__",
    "create_router",
    "get_config",
    "get_transport",
    "init_app",
    "init_worker",
    "is_initialized",
    "require_api_key",
    "require_basic_auth",
]

_transport: EventTransport | None = None


def is_initialized() -> bool:
    """Check if stemtrace has been initialized."""
    return _transport is not None


def get_config() -> StemtraceConfig | None:
    """Get the active stemtrace configuration."""
    return _get_config()


def get_transport() -> EventTransport | None:
    """Get the active event transport."""
    return _transport


def init_worker(
    app: "Celery",
    *,
    transport_url: str | None = None,
    prefix: str = "stemtrace",
    ttl: int = 86400,
    capture_args: bool = True,
    capture_result: bool = True,
    scrub_sensitive_data: bool = True,
    additional_sensitive_keys: frozenset[str] | None = None,
    safe_keys: frozenset[str] | None = None,
) -> None:
    """Initialize stemtrace for Celery worker instrumentation.

    Args:
        app: The Celery application instance.
        transport_url: Broker URL for events. If None, uses Celery's broker_url.
        prefix: Key/queue prefix for events.
        ttl: Event TTL in seconds (default: 24 hours).
        capture_args: Whether to capture task args/kwargs (default: True).
        capture_result: Whether to capture task return values (default: True).
        scrub_sensitive_data: Whether to scrub sensitive keys (default: True).
        additional_sensitive_keys: Extra keys to treat as sensitive.
        safe_keys: Keys to never scrub (overrides sensitive).

    Raises:
        ConfigurationError: If no broker URL can be determined.
    """
    global _transport

    url = transport_url or app.conf.broker_url
    if not url:
        raise ConfigurationError(
            "No broker URL available. Either pass transport_url or configure "
            "Celery's broker_url."
        )

    config = StemtraceConfig(
        transport_url=url,
        prefix=prefix,
        ttl=ttl,
        capture_args=capture_args,
        capture_result=capture_result,
        scrub_sensitive_data=scrub_sensitive_data,
        additional_sensitive_keys=additional_sensitive_keys or frozenset(),
        safe_keys=safe_keys or frozenset(),
    )
    set_config(config)

    _transport = _get_transport(url, prefix=prefix, ttl=ttl)
    connect_signals(_transport)
    register_bootsteps(app)


def init_app(
    fastapi_app: "FastAPI",
    *,
    broker_url: str | None = None,
    prefix: str = "/stemtrace",
    ttl: int = 86400,
    max_nodes: int = 10000,
    embedded_consumer: bool = True,
    serve_ui: bool = True,
    auth_dependency: Any = None,
) -> StemtraceExtension:
    """Initialize stemtrace as a FastAPI extension.

    Args:
        fastapi_app: Your FastAPI application instance.
        broker_url: Broker URL for events. Defaults to STEMTRACE_BROKER_URL env var.
        prefix: Mount path for stemtrace routes (default: "/stemtrace").
        ttl: Event TTL in seconds (default: 24 hours).
        max_nodes: Maximum number of nodes in memory store (default: 10000).
        embedded_consumer: Run event consumer in FastAPI process (default: True).
        serve_ui: Serve React dashboard (default: True).
        auth_dependency: Optional FastAPI dependency for authentication.

    Returns:
        The initialized StemtraceExtension instance.

    Raises:
        ConfigurationError: If no broker URL can be determined.
    """
    if broker_url is None:
        broker_url = os.getenv("STEMTRACE_BROKER_URL")
        if not broker_url:
            raise ConfigurationError(
                "No broker URL available. Pass broker_url parameter or "
                "set STEMTRACE_BROKER_URL environment variable."
            )

    extension = StemtraceExtension(
        broker_url=broker_url,
        embedded_consumer=embedded_consumer,
        serve_ui=serve_ui,
        prefix=prefix,
        ttl=ttl,
        max_nodes=max_nodes,
        auth_dependency=auth_dependency,
    )
    extension.init_app(fastapi_app, prefix=prefix)

    return extension


def _reset() -> None:
    """Reset module state. For testing only."""
    global _transport
    _transport = None
    _reset_config()
