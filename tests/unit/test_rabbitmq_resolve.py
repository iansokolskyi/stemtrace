"""RabbitMQ publish connections resolve broker hosts through PUBLISH_RESOLVER.

Uses the real kombu and py-amqp classes; nothing connects (the fake resolver
fails every lookup, and socket.getaddrinfo must never be called directly).
"""

import socket
from types import SimpleNamespace
from typing import Any

import pytest
from amqp.transport import SSLTransport
from kombu import Connection
from kombu.transport.memory import Transport as MemoryTransport

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


def test_publish_connection_leaves_non_amqp_transport_unchanged() -> None:
    assert rabbitmq._resolving_transport_class(MemoryTransport) is MemoryTransport
    assert rabbitmq._resolving_transport_class(None) is None


class AddressResolver:
    """PUBLISH_RESOLVER stand-in that answers with fixed addresses."""

    def __init__(self, addresses: list[tuple[str, int]]) -> None:
        self.addresses = addresses
        self.invalidated: list[tuple[str, int]] = []

    def resolve(
        self, host: str, port: int, *args: Any, timeout: float
    ) -> list[tuple[Any, ...]]:
        del host, port, args, timeout
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)
            for address in self.addresses
        ]

    def invalidate(self, host: str, port: int, *args: Any) -> None:
        del args
        self.invalidated.append((host, port))


def test_connect_resolved_falls_back_to_next_address_after_refusal(
    monkeypatch: pytest.MonkeyPatch,
    listener: socket.socket,
    refused_address: tuple[str, int],
) -> None:
    fake = AddressResolver([refused_address, listener.getsockname()])
    monkeypatch.setattr(rabbitmq, "PUBLISH_RESOLVER", fake)
    transport = SimpleNamespace(sock=None)

    rabbitmq._connect_resolved(transport, "rabbit.test", 5672, 1.0)

    try:
        assert transport.sock.getpeername() == listener.getsockname()
    finally:
        transport.sock.close()
    assert fake.invalidated == []


def test_connect_resolved_when_every_address_refuses_raises_and_refreshes(
    monkeypatch: pytest.MonkeyPatch, refused_address: tuple[str, int]
) -> None:
    fake = AddressResolver([refused_address, refused_address])
    monkeypatch.setattr(rabbitmq, "PUBLISH_RESOLVER", fake)
    transport = SimpleNamespace(sock=None)

    with pytest.raises(ConnectionRefusedError):
        rabbitmq._connect_resolved(transport, "rabbit.test", 5672, 1.0)

    assert transport.sock is None
    assert fake.invalidated == [("rabbit.test", 5672)]


def test_connect_resolved_when_socket_cannot_be_created_raises_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AddressResolver([("127.0.0.1", 5672)])
    monkeypatch.setattr(rabbitmq, "PUBLISH_RESOLVER", fake)

    def no_descriptors(*args: Any) -> Any:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(socket, "socket", no_descriptors)
    transport = SimpleNamespace(sock=None)

    with pytest.raises(OSError, match="Too many open files"):
        rabbitmq._connect_resolved(transport, "rabbit.test", 5672, 1.0)

    assert transport.sock is None
    assert fake.invalidated == [("rabbit.test", 5672)]


def test_connect_resolved_with_no_addresses_raises_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AddressResolver([])
    monkeypatch.setattr(rabbitmq, "PUBLISH_RESOLVER", fake)
    transport = SimpleNamespace(sock=None)

    with pytest.raises(socket.gaierror, match=r"No address for 'rabbit\.test'"):
        rabbitmq._connect_resolved(transport, "rabbit.test", 5672, 1.0)

    assert transport.sock is None
    assert fake.invalidated == [("rabbit.test", 5672)]
