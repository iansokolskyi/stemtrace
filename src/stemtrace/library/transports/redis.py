"""Deprecated alias of :mod:`stemtrace.transports.redis` (see the package docstring)."""

from stemtrace.transports.redis import (
    READ_TIMEOUT_WARN_AFTER,
    READ_TIMEOUT_WARN_INTERVAL,
    RECONNECT_INITIAL_BACKOFF,
    RECONNECT_MAX_BACKOFF,
    XREAD_BLOCK_MS,
    RedisTransport,
    StreamEvent,
    effective_socket_timeout,
)

__all__ = [
    "READ_TIMEOUT_WARN_AFTER",
    "READ_TIMEOUT_WARN_INTERVAL",
    "RECONNECT_INITIAL_BACKOFF",
    "RECONNECT_MAX_BACKOFF",
    "XREAD_BLOCK_MS",
    "RedisTransport",
    "StreamEvent",
    "effective_socket_timeout",
]
