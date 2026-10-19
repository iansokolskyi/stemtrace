"""stemtrace: A lightweight Celery task flow visualizer.

Example:
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

import logging
import os
import warnings
from typing import TYPE_CHECKING, Any

from stemtrace._version import __version__
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.core.graph import TaskGraph, TaskNode
from stemtrace.core.groups import GroupRule
from stemtrace.core.ports import EventTransport
from stemtrace.library.bootsteps import register_bootsteps
from stemtrace.library.config import StemtraceConfig, _reset_config, set_config
from stemtrace.library.config import get_config as _get_config
from stemtrace.library.signals import connect_signals
from stemtrace.server.fastapi import (
    StemtraceExtension,
    create_router,
    require_api_key,
    require_basic_auth,
)
from stemtrace.server.fastapi.api_key import configured_api_key
from stemtrace.server.fastapi.form_auth import FormAuthConfig, resolve_session_secret
from stemtrace.server.fastapi.form_auth_middleware import create_form_auth_middleware
from stemtrace.server.fastapi.login_routes import create_login_router
from stemtrace.server.groups import GroupsInput, configured_groups
from stemtrace.transports import get_transport as _get_transport

if TYPE_CHECKING:
    from celery import Celery
    from fastapi import FastAPI

__all__ = [
    "ConfigurationError",
    "GroupRule",
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
_node_alias_deprecation_logged = False

logger = logging.getLogger(__name__)

_NODE_ALIAS_WORKER_DEPRECATION = (
    "init_worker(node_alias_from_arguments=...) has no effect on workers: "
    "node aliases are resolved by the stemtrace server. Pass "
    "node_alias_from_arguments to stemtrace.init_app(), or use "
    "`stemtrace server --node-alias-from-arguments` / the "
    "STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS env var. This parameter will be "
    "removed in a future major release."
)


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
    max_data_size: int = 10240,
    node_alias_from_arguments: str | None = None,
    capture_output: bool = False,
    max_output_size: int = 16384,
    publish_received: bool = False,
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
        max_data_size: Maximum size in bytes for serialized data (default: 10KB).
        node_alias_from_arguments: Deprecated, has no effect on workers. Node
            aliases are resolved by the server: pass ``node_alias_from_arguments``
            to ``init_app()``, or use ``stemtrace server
            --node-alias-from-arguments`` / ``STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS``.
            A non-None value emits a ``DeprecationWarning`` (and a one-time
            log warning, since Celery workers hide deprecation warnings); the
            parameter will be removed in a future major release.
        capture_output: Capture what each task attempt prints and logs and show
            it in the UI (default: False). Output is not scrubbed.
        max_output_size: Maximum UTF-8 bytes of output kept per attempt; the
            end is kept (default: 16KB).
        publish_received: Publish a RECEIVED event when the worker takes each
            task message, so the UI shows when a task reached a worker before
            it started (default: False). It costs one synchronous publish per
            message in the worker's main process (about 0.7 ms on RabbitMQ,
            about 22% of a no-op task's prefork throughput there), which is
            why it is opt-in.

    Raises:
        ConfigurationError: If no broker URL can be determined, or
            max_output_size isn't positive.
    """
    global _transport, _node_alias_deprecation_logged

    if node_alias_from_arguments is not None:
        warnings.warn(_NODE_ALIAS_WORKER_DEPRECATION, DeprecationWarning, stacklevel=2)
        # Python's default filters hide DeprecationWarning unless it's raised from
        # __main__, so under `celery worker` nobody would see it. Log it once too.
        if not _node_alias_deprecation_logged:
            _node_alias_deprecation_logged = True
            logger.warning("%s", _NODE_ALIAS_WORKER_DEPRECATION)

    url = transport_url or app.conf.broker_url
    if not url:
        raise ConfigurationError(
            "No broker URL available. Either pass transport_url or configure "
            "Celery's broker_url."
        )
    if max_output_size <= 0:
        raise ConfigurationError(
            f"max_output_size must be a positive number of bytes, got {max_output_size}."
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
        max_data_size=max_data_size,
        node_alias_from_arguments=node_alias_from_arguments,
        capture_output=capture_output,
        max_output_size=max_output_size,
        publish_received=publish_received,
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
    node_alias_from_arguments: str | None = None,
    groups: GroupsInput = None,
    api_key: str | None = None,
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
            generated per process and a warning is logged: sessions then only
            work within one process (and are invalidated on restart), so set the
            same long random value on every worker/replica of a multi-process
            deployment.
        login_ttl_seconds: Session TTL in seconds (default: 24 hours).
        node_alias_from_arguments: Derive graph node display name from task arguments.
            Digit string for args[index], string for kwargs[key], or None (default)
            to show task names. Surrounding whitespace is stripped. If None or
            blank, uses the STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS env var (blank
            means unset).
        groups: Code-defined task groups: a list of ``GroupRule`` (or dicts
            with the same fields: name, match, tags, color) or a path to a TOML
            file. The first rule whose ``match`` globs match a task name wins;
            other tasks get their automatic group (from the task name). If
            None or blank, uses the STEMTRACE_GROUPS env var (a TOML file
            path; blank means unset).
        api_key: Require ``Authorization: Bearer <key>`` on the JSON API (not
            the liveness probe) and the WebSocket, for scripts and coding
            agents. With the form login, a session cookie also passes; with
            ``auth_dependency``, the key or the dependency lets a JSON API
            request in (the WebSocket, which the dependency doesn't cover,
            needs the key). At least 16 visible ASCII
            characters. If None or blank, uses the STEMTRACE_API_KEY env var
            (blank means unset).

    Returns:
        The initialized StemtraceExtension instance.

    Raises:
        ConfigurationError: If no broker URL can be determined, the groups
            are invalid or their file can't be loaded, or the API key is too
            short or has invalid characters.
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

    # Blank values count as unset (so the env var applies); padding is trimmed.
    node_alias = (node_alias_from_arguments or "").strip() or None
    if node_alias is None:
        node_alias = (
            os.getenv("STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS") or ""
        ).strip() or None

    # Validated before anything is installed on the app.
    group_config = configured_groups(groups)
    effective_api_key = configured_api_key(api_key)

    mount_prefix = f"/{prefix.strip('/')}"

    # Optional built-in form login (cookie-based session).
    # This is UI-first: the cookie is automatically sent by the browser on UI/API/WS.
    form_auth_config: FormAuthConfig | None = None
    effective_login_username = login_username or os.getenv("STEMTRACE_LOGIN_USERNAME")
    effective_login_password = login_password or os.getenv("STEMTRACE_LOGIN_PASSWORD")
    if effective_login_username and effective_login_password:
        effective_secret, secret_is_generated = resolve_session_secret(
            login_secret or os.getenv("STEMTRACE_LOGIN_SECRET")
        )
        form_auth_config = FormAuthConfig(
            username=effective_login_username,
            password=effective_login_password,
            secret=effective_secret,
            ttl_seconds=login_ttl_seconds,
            cookie_name="stemtrace_session",
            cookie_path=mount_prefix,
            secret_is_generated=secret_is_generated,
        )

    # Built before the login routes so they can share its failed-attempt
    # throttle; constructing it doesn't touch the app.
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
        node_alias_from_arguments=node_alias,
        groups=group_config,
        api_key=effective_api_key,
    )
    if form_auth_config is not None:
        _install_stemtrace_form_auth(
            fastapi_app,
            mount_prefix=mount_prefix,
            form_auth_config=form_auth_config,
            extension=extension,
        )
    extension.init_app(fastapi_app, prefix=prefix)

    return extension


def _install_stemtrace_form_auth(
    app: "FastAPI",
    *,
    mount_prefix: str,
    form_auth_config: FormAuthConfig,
    extension: StemtraceExtension,
) -> None:
    """Install form-login routes and HTTP middleware for stemtrace mount.

    The login router must be added before stemtrace UI's SPA catch-all route,
    otherwise `/login` would be handled by the SPA fallback.
    """
    login_router = create_login_router(
        form_auth_config,
        default_next_path=f"{mount_prefix}/",
        throttle=extension.auth_throttle,
    )
    app.include_router(login_router, prefix=mount_prefix)

    app.middleware("http")(
        create_form_auth_middleware(
            form_auth_config,
            login_path=f"{mount_prefix}/login",
            logout_path=f"{mount_prefix}/logout",
            api_prefix=f"{mount_prefix}/api",
            assets_prefix=f"{mount_prefix}/assets",
            protected_prefix=mount_prefix,
            public_paths=(f"{mount_prefix}/api/health/live",),
            api_key_configured=extension.auth_throttle is not None,
        )
    )


def _reset() -> None:
    """Reset module state. For testing only."""
    global _transport, _node_alias_deprecation_logged
    _transport = None
    _node_alias_deprecation_logged = False
    _reset_config()
