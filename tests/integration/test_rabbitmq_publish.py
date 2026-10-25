"""Integration tests for RabbitMQTransport.publish() with real kombu.

The fail-fast tests need no broker: they point the transport at a refused
port or a local TCP listener. The failover tests need a running RabbitMQ and
are skipped when it isn't available.

Run with: pytest tests/integration/test_rabbitmq_publish.py -v
"""

import base64
import contextlib
import http.client
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from amqp.exceptions import AMQPError
from kombu import Connection
from kombu.exceptions import KombuError
from kombu.utils import functional as kombu_functional

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports import rabbitmq
from stemtrace.transports.rabbitmq import RabbitMQTransport

_WORKER = os.environ.get(
    "PYTEST_XDIST_WORKER", "gw"
)  # keeps names unique across xdist workers
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")
RABBITMQ_MANAGEMENT_URL = os.environ.get(
    "RABBITMQ_MANAGEMENT_URL", "http://guest:guest@localhost:15672"
)
LOGGER_NAME = "stemtrace.transports.rabbitmq"


def rabbitmq_available() -> bool:
    """Check if RabbitMQ is available."""
    try:
        with Connection(RABBITMQ_URL, connect_timeout=1) as conn:
            conn.connect()
        return True
    except (OSError, AMQPError, KombuError):
        return False


def _management_request(method: str, path: str, body: Any = None) -> Any:
    """Call the RabbitMQ management HTTP API and return the decoded JSON body."""
    parsed = urllib.parse.urlsplit(RABBITMQ_MANAGEMENT_URL)
    base = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 15672}"
    credentials = f"{parsed.username or 'guest'}:{parsed.password or 'guest'}"
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{base}/api/{path}", data=data, method=method)
    request.add_header(
        "Authorization", "Basic " + base64.b64encode(credentials.encode()).decode()
    )
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def management_api_available() -> bool:
    """Check if the RabbitMQ management API is reachable."""
    try:
        _management_request("GET", "overview")
        return True
    except (OSError, ValueError, http.client.HTTPException):
        # URLError/HTTPError/timeouts, bad JSON, or a non-HTTP service on the port
        return False


def _refused_url() -> str:
    """AMQP URL of a local port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"amqp://guest:guest@127.0.0.1:{port}//"


class _Listener:
    """Local TCP listener that counts connections and closes or holds them."""

    def __init__(self, *, hold: bool) -> None:
        self.accepted = 0
        self._hold = hold
        self._held: list[socket.socket] = []
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._server.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        port = self._server.getsockname()[1]
        return f"amqp://guest:guest@127.0.0.1:{port}//"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            self.accepted += 1
            if self._hold:
                self._held.append(conn)
            else:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        for conn in self._held:
            conn.close()
        self._server.close()


@pytest.fixture
def closing_listener() -> Iterator[_Listener]:
    listener = _Listener(hold=False)
    yield listener
    listener.close()


@pytest.fixture
def silent_listener() -> Iterator[_Listener]:
    listener = _Listener(hold=True)
    yield listener
    listener.close()


def _declare_queue(prefix: str) -> None:
    """Declare the consumer queue for ``prefix`` straight on the real broker."""
    RabbitMQTransport(RABBITMQ_URL, prefix=prefix, ttl=60)._declare_exchange_and_queue()


def _event(task_id: str) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.rabbitmq_publish",
        state=TaskState.STARTED,
        timestamp=datetime.now(timezone.utc),
    )


def _timed_publish(transport: RabbitMQTransport, task_id: str) -> float:
    start = time.perf_counter()
    transport.publish(_event(task_id))
    return time.perf_counter() - start


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == LOGGER_NAME and r.levelno == logging.WARNING
    ]


class TestRabbitMQPublishFailFastNoBroker:
    """publish() returns quickly and stops connecting while the broker is down."""

    def test_publish_refused_port_fails_fast_then_skips(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = RabbitMQTransport(_refused_url(), prefix="test", ttl=60)
        publisher = transport._publisher
        real_publish = publisher.publish
        reached_connection: list[str] = []

        def counting_publish(*args: Any, **kwargs: Any) -> None:
            reached_connection.append("attempt")
            real_publish(*args, **kwargs)

        monkeypatch.setattr(publisher, "publish", counting_publish)
        # kombu 5.6.2 (uv.lock) retry_over_time calls its module-level ``sleep`` between
        # connect attempts; record those calls instead of timing the publish.
        sleeps: list[float] = []
        monkeypatch.setattr(kombu_functional, "sleep", sleeps.append)

        # Fast failures drop only their own event until three in a row open
        # the breaker.
        for i in range(3):
            transport.publish(_event(f"t-{i}"))
        for i in range(3, 103):
            transport.publish(_event(f"t-{i}"))

        # A refused connect returns at once; kombu's own retry would sleep 2s.
        # Wall time can't tell that from a preempted process (the first publish
        # also imports kombu lazily: 635 ms under load), a sleep call can.
        assert sleeps == []
        # Skipped events never reach the connection. Counting the attempts
        # replaces a per-publish time bound, which any scheduler pause beat
        # (19-65ms against 10ms under CPU load, ~7us otherwise).
        assert len(reached_connection) == rabbitmq.PUBLISH_FAILURES_TO_OPEN
        assert transport._publish_breaker.is_open is True
        # One line for the first failure, one when the breaker opens.
        assert len(_warnings(caplog)) == 2

    def test_publish_silent_listener_bounded_by_connect_timeout(
        self,
        silent_listener: _Listener,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(rabbitmq, "CONNECT_TIMEOUT", 0.3)
        transport = RabbitMQTransport(silent_listener.url, prefix="test", ttl=60)

        # Timed-out attempts count like any failure: three in a row open the
        # breaker, after which nothing connects.
        attempts = [_timed_publish(transport, f"t-{i}") for i in range(3)]
        for i in range(3, 20):
            _timed_publish(transport, f"t-{i}")

        # One bounded wait each: a hidden retry would double it.
        assert all(0.25 <= d < 2 * 0.3 for d in attempts), attempts
        assert silent_listener.accepted == rabbitmq.PUBLISH_FAILURES_TO_OPEN
        assert transport._publish_breaker.is_open is True
        assert len(_warnings(caplog)) == 2

    def test_publish_failover_url_tries_second_host_without_sleep(
        self, closing_listener: _Listener, caplog: pytest.LogCaptureFixture
    ) -> None:
        url = f"{_refused_url()};{closing_listener.url}"
        transport = RabbitMQTransport(url, prefix="test", ttl=60)

        elapsed = _timed_publish(transport, "t-1")

        # kombu's own retry would sleep 2s between rounds and, with
        # max_retries=0, never reach the second host at all.
        assert closing_listener.accepted == 1
        assert elapsed < 0.5
        assert len(_warnings(caplog)) == 1


@pytest.mark.skipif(not rabbitmq_available(), reason="RabbitMQ not available")
class TestRabbitMQPublishFailoverBroker:
    """Failover URLs against a real broker."""

    @pytest.fixture
    def prefix(self) -> Iterator[str]:
        prefix = f"test_publish_{_WORKER}_{int(time.time() * 1000)}"
        yield prefix
        with (
            contextlib.suppress(Exception),
            Connection(RABBITMQ_URL, connect_timeout=1) as conn,
        ):
            channel = conn.channel()
            probe = RabbitMQTransport(RABBITMQ_URL, prefix=prefix, ttl=60)
            channel.queue_delete(probe.queue_name)
            channel.exchange_delete(probe.exchange_name)

    @staticmethod
    def _received(queue_name: str, count: int, timeout: float = 5.0) -> list[Any]:
        """Fetch up to ``count`` message bodies from the queue."""
        bodies: list[Any] = []
        deadline = time.monotonic() + timeout
        with Connection(RABBITMQ_URL, connect_timeout=1) as conn:
            channel = conn.channel()
            while len(bodies) < count and time.monotonic() < deadline:
                message = channel.basic_get(queue_name, no_ack=True)
                if message is None:
                    time.sleep(0.05)
                    continue
                bodies.append(json.loads(message.body))
        return bodies

    def test_publish_closed_port_then_real_broker_delivers(
        self, prefix: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        url = f"{_refused_url()};{RABBITMQ_URL}"
        transport = RabbitMQTransport(url, prefix=prefix, ttl=60)
        _declare_queue(prefix)

        first = _timed_publish(transport, "failover-1")
        second = _timed_publish(transport, "failover-2")

        bodies = self._received(transport.queue_name, 2)
        assert [body["task_id"] for body in bodies] == ["failover-1", "failover-2"]
        assert first < 1.0
        assert second < 1.0
        assert _warnings(caplog) == []

    def test_publish_silent_host_then_real_broker_pays_timeout_once(
        self,
        prefix: str,
        silent_listener: _Listener,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(rabbitmq, "CONNECT_TIMEOUT", 0.3)
        url = f"{silent_listener.url};{RABBITMQ_URL}"
        transport = RabbitMQTransport(url, prefix=prefix, ttl=60)
        _declare_queue(prefix)

        first = _timed_publish(transport, "sticky-1")
        rest = [_timed_publish(transport, f"sticky-{i}") for i in range(2, 6)]

        bodies = self._received(transport.queue_name, 5)
        assert [body["task_id"] for body in bodies] == [
            f"sticky-{i}" for i in range(1, 6)
        ]
        assert 0.25 <= first < 1.5
        # Later publishes start with the broker that worked.
        assert max(rest) < 0.25
        assert silent_listener.accepted == 1


@pytest.mark.skipif(
    not rabbitmq_available() or not management_api_available(),
    reason="RabbitMQ with management API not available",
)
class TestRabbitMQPublishFailoverCredentials:
    """Bare-host failover entries keep the first entry's credentials and vhost."""

    @pytest.fixture
    def app_account(self) -> Iterator[tuple[str, str, str]]:
        """A vhost and a non-guest user that may only use that vhost."""
        suffix = f"{_WORKER}_{int(time.time() * 1000)}"
        vhost, user, password = f"stemtrace_vh_{suffix}", f"app_{suffix}", "s3cret"
        _management_request("PUT", f"vhosts/{vhost}", {})
        _management_request("PUT", f"users/{user}", {"password": password, "tags": ""})
        _management_request(
            "PUT",
            f"permissions/{vhost}/{user}",
            {"configure": ".*", "write": ".*", "read": ".*"},
        )
        # RabbitMQ grants the creating user (guest) access to a new vhost;
        # revoke it, so a publish that fell back to guest can't succeed.
        _management_request("DELETE", f"permissions/{vhost}/guest")
        yield vhost, user, password
        with contextlib.suppress(Exception):
            _management_request("DELETE", f"vhosts/{vhost}")
        with contextlib.suppress(Exception):
            _management_request("DELETE", f"users/{user}")

    def test_publish_after_failover_keeps_first_entry_credentials_and_vhost(
        self, app_account: tuple[str, str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        vhost, user, password = app_account
        broker = Connection(RABBITMQ_URL)
        # The first host doesn't resolve; the second entry is a bare host,
        # which kombu completes with the first entry's user, password, port
        # and vhost.
        url = (
            f"amqp://{user}:{password}@stemtrace-unresolvable.invalid:"
            f"{broker.port}/{vhost};{broker.hostname}"
        )
        direct = f"amqp://{user}:{password}@{broker.hostname}:{broker.port}/{vhost}"
        consumer = RabbitMQTransport(direct, prefix="cred", ttl=60)
        consumer._declare_exchange_and_queue()
        transport = RabbitMQTransport(url, prefix="cred", ttl=60)

        transport.publish(_event("cred-1"))  # h1 doesn't resolve; via h2
        transport.publish(_event("cred-2"))  # starts with h2
        transport.publish(_event("cred-3"))

        bodies: list[Any] = []
        deadline = time.monotonic() + 5.0
        with Connection(direct, connect_timeout=1) as conn:
            channel = conn.channel()
            while len(bodies) < 3 and time.monotonic() < deadline:
                message = channel.basic_get(consumer.queue_name, no_ack=True)
                if message is None:
                    time.sleep(0.05)
                    continue
                bodies.append(json.loads(message.body))
        assert [body["task_id"] for body in bodies] == ["cred-1", "cred-2", "cred-3"]
        assert _warnings(caplog) == []
        assert password not in caplog.text


class _RecordingResolver:
    """PUBLISH_RESOLVER stand-in: answers ``address`` and records invalidations."""

    def __init__(self, address: tuple[str, int]) -> None:
        self.address = address
        self.invalidated: list[tuple[Any, ...]] = []

    def resolve(self, host: str, port: int, *args: Any, timeout: float) -> list[Any]:
        del host, port, args, timeout
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", self.address)]

    def invalidate(self, *key: Any) -> None:
        self.invalidated.append(key)


@pytest.mark.integration
def test_publish_connect_failure_refreshes_cached_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host whose cached address refuses connections gets re-resolved."""
    with socket.socket() as gone:
        gone.bind(("127.0.0.1", 0))
        refused = ("127.0.0.1", gone.getsockname()[1])
    resolver = _RecordingResolver(refused)
    monkeypatch.setattr(rabbitmq, "PUBLISH_RESOLVER", resolver)
    conn = rabbitmq._resolving_connection_class(Connection)(
        "amqp://guest:guest@rabbit.failover.test:5672//", connect_timeout=1
    )

    with pytest.raises(ConnectionRefusedError):
        conn.transport.establish_connection()

    assert resolver.invalidated == [
        (
            "rabbit.failover.test",
            5672,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
    ]
