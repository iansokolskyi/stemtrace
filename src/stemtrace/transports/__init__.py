"""Broker transports and the factory that picks one from a broker URL.

Workers publish through a transport (``stemtrace.library``) and the server
consumes through one (``stemtrace.server``), so this package sits beside both
and imports only ``stemtrace.core``, never either of them.
"""

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from stemtrace.core.exceptions import UnsupportedBrokerError

if TYPE_CHECKING:
    from stemtrace.core.ports import EventTransport

_SCHEME_ALIASES: dict[str, str] = {
    "rediss": "redis",
    "amqps": "amqp",
    "pyamqp": "amqp",
}

_BROKER_KINDS: dict[str, str] = {
    "redis": "redis",
    "amqp": "rabbitmq",
    "memory": "memory",
}


def broker_kind(url: str | None) -> str | None:
    """Name the kind of broker a URL points to: ``redis``, ``rabbitmq`` or ``memory``.

    Only the scheme is read, so nothing from the URL (host, credentials)
    leaks into the answer.

    Returns:
        The kind, or None without a URL or for an unsupported scheme.
    """
    if not url:
        return None
    scheme = urlparse(url).scheme.lower()
    return _BROKER_KINDS.get(_SCHEME_ALIASES.get(scheme, scheme))


def get_transport(
    url: str, prefix: str = "stemtrace", ttl: int = 86400
) -> "EventTransport":
    """Create a transport from a broker URL."""
    scheme = urlparse(url).scheme.lower()
    scheme = _SCHEME_ALIASES.get(scheme, scheme)

    if scheme == "redis":
        from stemtrace.transports.redis import RedisTransport

        return RedisTransport.from_url(url, prefix=prefix, ttl=ttl)
    if scheme == "amqp":
        from stemtrace.transports.rabbitmq import RabbitMQTransport

        return RabbitMQTransport.from_url(url, prefix=prefix, ttl=ttl)
    if scheme == "memory":
        from stemtrace.transports.memory import MemoryTransport

        return MemoryTransport()
    raise UnsupportedBrokerError(scheme)


__all__ = ["broker_kind", "get_transport"]
