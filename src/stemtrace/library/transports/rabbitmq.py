"""Deprecated alias of :mod:`stemtrace.transports.rabbitmq` (see the package docstring)."""

from typing import Any

from stemtrace.transports import rabbitmq as _rabbitmq
from stemtrace.transports.rabbitmq import (
    CONNECT_TIMEOUT,
    DRAIN_TIMEOUT,
    PUBLISH_FAILURE_LOG_INTERVAL,
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_IO_TIMEOUT,
    PUBLISH_PROBE_TIMEOUT,
    RECONNECT_INITIAL_BACKOFF,
    RECONNECT_MAX_BACKOFF,
    RabbitMQTransport,
    StreamEvent,
)

__all__ = [
    "CONNECT_TIMEOUT",
    "DRAIN_TIMEOUT",
    "PUBLISH_FAILURES_TO_OPEN",
    "PUBLISH_FAILURE_LOG_INTERVAL",
    "PUBLISH_IO_TIMEOUT",
    "PUBLISH_PROBE_TIMEOUT",
    # Deprecated: served by __getattr__ (which warns), so not a module global.
    "PUBLISH_SLOW_ATTEMPT",  # noqa: F822
    "RECONNECT_INITIAL_BACKOFF",
    "RECONNECT_MAX_BACKOFF",
    "RabbitMQTransport",
    "StreamEvent",
]


def __getattr__(name: str) -> Any:
    """Serve deprecated constants (e.g. PUBLISH_SLOW_ATTEMPT) with a warning.

    The warning is issued here, not by forwarding to the new module, so that
    it points at the code that used the constant.
    """
    if name in _rabbitmq._DEPRECATED_CONSTANTS:
        return _rabbitmq._deprecated_constant(name, stacklevel=3)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
