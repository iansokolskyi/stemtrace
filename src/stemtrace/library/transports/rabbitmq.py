"""Deprecated alias of :mod:`stemtrace.transports.rabbitmq` (see the package docstring)."""

from stemtrace.transports.rabbitmq import (
    CONNECT_TIMEOUT,
    DRAIN_TIMEOUT,
    PUBLISH_FAILURE_LOG_INTERVAL,
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_IO_TIMEOUT,
    PUBLISH_PROBE_TIMEOUT,
    PUBLISH_SLOW_ATTEMPT,
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
    "PUBLISH_SLOW_ATTEMPT",
    "RECONNECT_INITIAL_BACKOFF",
    "RECONNECT_MAX_BACKOFF",
    "RabbitMQTransport",
    "StreamEvent",
]
