"""Broker-agnostic transport factory."""

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


def get_transport(
    url: str, prefix: str = "stemtrace", ttl: int = 86400
) -> "EventTransport":
    """Create a transport from a broker URL."""
    scheme = urlparse(url).scheme.lower()
    scheme = _SCHEME_ALIASES.get(scheme, scheme)

    if scheme == "redis":
        from stemtrace.library.transports.redis import RedisTransport

        return RedisTransport.from_url(url, prefix=prefix, ttl=ttl)
    elif scheme == "amqp":
        from stemtrace.library.transports.rabbitmq import RabbitMQTransport

        return RabbitMQTransport.from_url(url, prefix=prefix, ttl=ttl)
    elif scheme == "memory":
        from stemtrace.library.transports.memory import MemoryTransport

        return MemoryTransport()
    else:
        raise UnsupportedBrokerError(scheme)


__all__ = ["get_transport"]
