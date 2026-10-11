"""RabbitMQ publish connections resolve broker hosts through PUBLISH_RESOLVER.

Uses the real kombu and py-amqp classes; nothing connects (the fake resolver
fails every lookup, and socket.getaddrinfo must never be called directly).
"""

import socket
from typing import Any

import pytest
from amqp.transport import SSLTransport
from kombu import Connection

from stemtrace.transports import rabbitmq
from stemtrace.transports.rabbitmq import CONNECT_TIMEOUT


class FakeResolver:
    """PUBLISH_RESOLVER stand-in that records lookups and fails them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []

    def resolve(
        self, host: str, port: int, *args: Any, timeout: float
    ) -> list[tuple[Any, ...]]:
        self.calls.append((host, port, timeout))
        raise socket.gaierror(socket.EAI_AGAIN, f"Timed out resolving {host!r}")


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> FakeResolver:
    fake = FakeResolver()
    monkeypatch.setattr(rabbitmq, "PUBLISH_RESOLVER", fake)

    def no_dns(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("socket.getaddrinfo called directly")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    return fake


def _connection(url: str) -> Any:
    cls = rabbitmq._resolving_connection_class(Connection)
    return cls(url, connect_timeout=CONNECT_TIMEOUT)


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("amqp://guest:guest@rabbit.test:5672//", 5672),
        ("amqps://guest:guest@rabbit.test:5671//", 5671),
    ],
)
def test_publish_connection_resolves_host_with_bound(
    resolver: FakeResolver, url: str, port: int
) -> None:
    conn = _connection(url)

    with pytest.raises(socket.gaierror, match="Timed out resolving"):
        conn.transport.establish_connection()

    assert resolver.calls == [("rabbit.test", port, CONNECT_TIMEOUT)]


def test_publish_connection_failover_host_resolves_with_bound(
    resolver: FakeResolver,
) -> None:
    conn = _connection("amqp://guest:guest@h1.test//;amqp://guest:guest@h2.test//")
    conn.maybe_switch_next()

    with pytest.raises(socket.gaierror):
        conn.transport.establish_connection()

    assert resolver.calls == [("h2.test", 5672, CONNECT_TIMEOUT)]


def test_publish_tls_transport_keeps_host_name_for_certificate_checks() -> None:
    """py-amqp takes SNI and hostname checks from the ssl options, which the
    resolving transport passes through untouched; only the socket connect
    changes."""
    conn = _connection("amqps://guest:guest@rabbit.test:5671//")
    ssl_options = {"server_hostname": "rabbit.test", "cert_reqs": 2}
    amqp_conn = conn.transport.Connection(
        host="rabbit.test:5671", ssl=ssl_options, connect_timeout=CONNECT_TIMEOUT
    )

    transport = amqp_conn.Transport(
        "rabbit.test:5671", CONNECT_TIMEOUT, ssl=ssl_options
    )

    assert isinstance(transport, SSLTransport)
    assert transport.sslopts == ssl_options
    assert (transport.host, transport.port) == ("rabbit.test", 5671)
