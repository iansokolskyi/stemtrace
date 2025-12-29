"""stemtrace: A lightweight Celery task flow visualizer.

Usage:
    import stemtrace

    stemtrace.init(app)

    # Introspection
    stemtrace.is_initialized()  # -> bool
    stemtrace.get_config()      # -> StemtraceConfig | None
    stemtrace.get_transport()   # -> EventTransport | None
"""

from typing import TYPE_CHECKING

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.core.graph import TaskGraph, TaskNode
from stemtrace.core.ports import EventTransport
from stemtrace.library.bootsteps import register_bootsteps
from stemtrace.library.config import StemtraceConfig, _reset_config, set_config
from stemtrace.library.config import get_config as _get_config
from stemtrace.library.signals import connect_signals
from stemtrace.library.transports import get_transport as _get_transport

if TYPE_CHECKING:
    from celery import Celery

__version__ = "0.1.0"
__all__ = [
    "ConfigurationError",
    "StemtraceConfig",
    "TaskEvent",
    "TaskGraph",
    "TaskNode",
    "TaskState",
    "__version__",
    "get_config",
    "get_transport",
    "init",
    "is_initialized",
]

# Module-level state
_transport: EventTransport | None = None


def is_initialized() -> bool:
    """Check if stemtrace has been initialized.

    Returns:
        True if init() has been called, False otherwise.

    Example:
        >>> import stemtrace
        >>> stemtrace.is_initialized()
        False
        >>> stemtrace.init(app)
        >>> stemtrace.is_initialized()
        True
    """
    return _transport is not None


def get_config() -> StemtraceConfig | None:
    """Get the active stemtrace configuration.

    Returns:
        The configuration if initialized, None otherwise.

    Example:
        >>> import stemtrace
        >>> stemtrace.init(app, prefix="my_prefix")
        >>> config = stemtrace.get_config()
        >>> config.prefix
        'my_prefix'
    """
    return _get_config()


def get_transport() -> EventTransport | None:
    """Get the active event transport.

    Useful for testing to verify events are published correctly.

    Returns:
        The transport if initialized, None otherwise.

    Example:
        >>> import stemtrace
        >>> stemtrace.init(app, transport_url="memory://")
        >>> transport = stemtrace.get_transport()
        >>> # For MemoryTransport, you can access published events
    """
    return _transport


def init(
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
    """Initialize stemtrace event tracking.

    Connects to Celery's task signals and publishes lifecycle events
    to the configured transport (Redis, RabbitMQ, etc.).

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

    Example:
        >>> from celery import Celery
        >>> import stemtrace
        >>> app = Celery("myapp", broker="redis://localhost:6379/0")
        >>> stemtrace.init(app)

        # With explicit transport URL:
        >>> stemtrace.init(app, transport_url="redis://events-redis:6379/1")

        # Disable arg capture:
        >>> stemtrace.init(app, capture_args=False)
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

    # Register bootsteps for RECEIVED events (worker-side)
    register_bootsteps(app)


def _reset() -> None:
    """Reset module state. For testing only."""
    global _transport
    _transport = None
    _reset_config()
