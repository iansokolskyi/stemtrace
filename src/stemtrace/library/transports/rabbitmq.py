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
    "PUBLISH_SLOW_ATTEMPT",  # noqa: F822  deprecated, served by __getattr__ (warns)
    "RECONNECT_INITIAL_BACKOFF",
    "RECONNECT_MAX_BACKOFF",
    "RabbitMQTransport",
    "StreamEvent",
]


def __getattr__(name: str) -> Any:
    """Forward deprecated constants (e.g. PUBLISH_SLOW_ATTEMPT), which warn."""
    return getattr(_rabbitmq, name)
