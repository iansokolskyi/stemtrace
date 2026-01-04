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
import secrets
import urllib.parse
from collections.abc import Awaitable, Callable
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
from stemtrace.server.fastapi.form_auth import FormAuthConfig, is_authenticated_cookie
from stemtrace.server.fastapi.login_routes import create_login_router

if TYPE_CHECKING:
    from celery import Celery
    from fastapi import FastAPI

__version__ = "0.3.1"
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
    transport_url: str | None = None,
    prefix: str = "/stemtrace",
    ttl: int = 86400,
    max_nodes: int = 10000,
    embedded_consumer: bool = True,
    serve_ui: bool = True,
    auth_dependency: Any = None,
    login_username: str | None = None,
    login_password: str | None = None,
    login_secret: str | None = None,
    login_ttl_seconds: int = 86400,
) -> StemtraceExtension:
    """Initialize stemtrace as a FastAPI extension.

    Args:
        fastapi_app: Your FastAPI application instance.
        broker_url: Celery broker URL used for on-demand inspection (workers/registry).
            Defaults to STEMTRACE_BROKER_URL env var.
        transport_url: Event transport URL used to consume stemtrace events. If None,
            defaults to broker_url. Can also be provided via STEMTRACE_TRANSPORT_URL.
        prefix: Mount path for stemtrace routes (default: "/stemtrace").
        ttl: Event TTL in seconds (default: 24 hours).
        max_nodes: Maximum number of nodes in memory store (default: 10000).
        embedded_consumer: Run event consumer in FastAPI process (default: True).
        serve_ui: Serve React dashboard (default: True).
        auth_dependency: Optional FastAPI dependency for authentication.
        login_username: Enable built-in form login if set (single allowed username).
        login_password: Enable built-in form login if set (password for login_username).
        login_secret: Secret used to sign session cookies. If not provided, uses
            STEMTRACE_LOGIN_SECRET env var. If still missing, a random secret is
            generated (sessions invalidated on app restart).
        login_ttl_seconds: Session TTL in seconds (default: 24 hours).

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

    if transport_url is None:
        transport_url = os.getenv("STEMTRACE_TRANSPORT_URL") or broker_url

    mount_prefix = f"/{prefix.strip('/')}"

    # Optional built-in form login (cookie-based session).
    # This is UI-first: the cookie is automatically sent by the browser on UI/API/WS.
    form_auth_config: FormAuthConfig | None = None
    effective_login_username = login_username or os.getenv("STEMTRACE_LOGIN_USERNAME")
    effective_login_password = login_password or os.getenv("STEMTRACE_LOGIN_PASSWORD")
    if effective_login_username and effective_login_password:
        effective_secret = (
            login_secret
            or os.getenv("STEMTRACE_LOGIN_SECRET")
            or secrets.token_urlsafe(32)
        )
        form_auth_config = FormAuthConfig(
            username=effective_login_username,
            password=effective_login_password,
            secret=effective_secret,
            ttl_seconds=login_ttl_seconds,
            cookie_name="stemtrace_session",
            cookie_path=mount_prefix,
        )

        _install_stemtrace_form_auth(
            fastapi_app,
            mount_prefix=mount_prefix,
            form_auth_config=form_auth_config,
        )

    extension = StemtraceExtension(
        broker_url=broker_url,
        transport_url=transport_url,
        embedded_consumer=embedded_consumer,
        serve_ui=serve_ui,
        prefix=prefix,
        ttl=ttl,
        max_nodes=max_nodes,
        auth_dependency=auth_dependency,
        form_auth_config=form_auth_config,
    )
    extension.init_app(fastapi_app, prefix=prefix)

    return extension


def _install_stemtrace_form_auth(
    app: "FastAPI",
    *,
    mount_prefix: str,
    form_auth_config: FormAuthConfig,
) -> None:
    """Install form-login routes and HTTP middleware for stemtrace mount.

    The login router must be added before stemtrace UI's SPA catch-all route,
    otherwise `/login` would be handled by the SPA fallback.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse, RedirectResponse
    from starlette.responses import Response

    login_router = create_login_router(
        form_auth_config,
        default_next_path=f"{mount_prefix}/",
    )
    app.include_router(login_router, prefix=mount_prefix)

    login_path = f"{mount_prefix}/login"
    logout_path = f"{mount_prefix}/logout"

    @app.middleware("http")
    async def _stemtrace_form_auth_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if not path.startswith(mount_prefix):
            return await call_next(request)

        # Allow login/logout without auth.
        if path in (login_path, logout_path):
            return await call_next(request)

        cookie_value = request.cookies.get(form_auth_config.cookie_name)
        if is_authenticated_cookie(
            cookie_value,
            secret=form_auth_config.secret,
            expected_username=form_auth_config.username,
        ):
            return await call_next(request)

        # Not authenticated.
        if path.startswith(f"{mount_prefix}/api"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        if path.startswith(f"{mount_prefix}/assets"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)

        next_target = path
        if request.url.query:
            next_target = f"{path}?{request.url.query}"
        next_qs = urllib.parse.urlencode({"next": next_target})
        return RedirectResponse(url=f"{login_path}?{next_qs}", status_code=303)


def _reset() -> None:
    """Reset module state. For testing only."""
    global _transport
    _transport = None
    _reset_config()
