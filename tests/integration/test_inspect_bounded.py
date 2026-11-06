"""Rows I1 and I2 of D7a against real connection failures (no broker is running).

Real kombu, real sockets: a closed local port refuses at once, a non-routable
address (``10.255.255.1``) never answers, so only our own bounds end the attempt.
Bounds are generous on purpose; the spec's are 3.5 s per attempt, 4 s per request.
"""

import contextlib
import os
import socket
import struct
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import kombu.messaging as kombu_messaging
import kombu.utils.functional as kombu_functional
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.server.api import routes
from stemtrace.server.api.routes import _InspectRefresher, create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry

pytestmark = pytest.mark.integration

ATTEMPT_BOUND_S = 3.5
REQUEST_BOUND_S = 4.0
BACKOFF_WINDOW_BOUND_S = 0.2


def _refused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _urls() -> dict[str, str]:
    port = _refused_port()
    return {
        "redis-refused": f"redis://127.0.0.1:{port}/0",
        "amqp-refused": f"amqp://guest:guest@127.0.0.1:{port}//",
        "redis-blackholed": "redis://10.255.255.1:6379/0",
        "amqp-blackholed": "amqp://guest:guest@10.255.255.1:5672//",
    }


URLS = _urls()


@pytest.fixture(params=sorted(URLS))
def broker_url(request: pytest.FixtureRequest) -> str:
    return URLS[request.param]


def test_i1_i2_one_inspect_attempt_ends_within_the_bound(broker_url: str) -> None:
    refresher = _InspectRefresher(WorkerRegistry(), broker_url)

    started = time.monotonic()
    refresher.refresh_blocking()
    elapsed = time.monotonic() - started

    assert elapsed < ATTEMPT_BOUND_S
    assert refresher.ready_in() > 0.0


def test_i1_i2_requests_return_within_the_bound_and_fast_inside_the_backoff(
    broker_url: str,
) -> None:
    app = FastAPI()
    app.include_router(
        create_api_router(
            GraphStore(), worker_registry=WorkerRegistry(), broker_url=broker_url
        )
    )
    client = TestClient(app)

    started = time.monotonic()
    first = client.get("/api/anomalies")
    first_elapsed = time.monotonic() - started
    durations = []
    for path in ("/api/anomalies", "/api/workers", "/api/tasks/registry"):
        started = time.monotonic()
        response = client.get(path)
        durations.append(time.monotonic() - started)
        assert response.status_code == 200

    assert first.status_code == 200
    assert first_elapsed < REQUEST_BOUND_S
    assert max(durations) < BACKOFF_WINDOW_BOUND_S


# --- I6 and I7: a broker that accepts the connection and then says nothing ---------------

# Worst case measured: 4.2 s here, 5.3 s in the round 2 review (RabbitMQ); plus margin.
IO_ATTEMPT_BOUND_S = 8.0
REQUIRE_BROKERS_ENV = "STEMTRACE_REQUIRE_BROKERS"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")


class FaultProxy:
    """A TCP proxy to ``upstream`` that can turn silent: it then drops both directions."""

    def __init__(self, upstream: tuple[str, int] | None) -> None:
        self._upstream = upstream
        self._silent = threading.Event()
        self._stop = threading.Event()
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._server.settimeout(0.2)
        self.port = int(self._server.getsockname()[1])
        self._socks: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def go_silent(self) -> None:
        self._silent.set()

    def go_dead(self) -> None:
        """Reset every connection and stop listening, as a broker that went down does."""
        self._silent.set()
        linger_zero = struct.pack("ii", 1, 0)  # close() then sends RST, not FIN
        for sock in [self._server, *self._socks]:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger_zero)
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)  # a recv in a pump thread holds the fd
            with contextlib.suppress(OSError):
                sock.close()

    def close(self) -> None:
        self._stop.set()
        for sock in [self._server, *self._socks]:
            with contextlib.suppress(OSError):
                sock.close()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except OSError:
                continue
            self._socks.append(client)
            if self._upstream is None or self._silent.is_set():
                continue  # never answers
            try:
                up = socket.create_connection(self._upstream, timeout=2)
            except OSError:
                continue
            self._socks.append(up)
            for src, dst in ((client, up), (up, client)):
                threading.Thread(
                    target=self._pump, args=(src, dst), daemon=True
                ).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data = src.recv(65536)
                if not data:
                    return
                if not self._silent.is_set():
                    dst.sendall(data)
            except OSError:
                return


def _reachable(url: str) -> bool:
    parts = urlsplit(url)
    try:
        socket.create_connection(
            (parts.hostname or "localhost", parts.port or 0), timeout=1
        ).close()
    except OSError:
        return False
    return True


@pytest.fixture(params=["redis", "amqp"])
def silent_broker_url(request: pytest.FixtureRequest) -> Iterator[str]:
    """A broker URL whose server accepts TCP and never answers (row I6)."""
    proxy = FaultProxy(None)
    try:
        if request.param == "redis":
            yield f"redis://127.0.0.1:{proxy.port}/0"
        else:
            yield f"amqp://guest:guest@127.0.0.1:{proxy.port}//"
    finally:
        proxy.close()


def test_i6_a_broker_that_accepts_and_never_answers_ends_the_attempt(
    silent_broker_url: str,
) -> None:
    refresher = _InspectRefresher(WorkerRegistry(), silent_broker_url)

    started = time.monotonic()
    first = refresher.refresh_blocking()
    elapsed = time.monotonic() - started

    assert elapsed < IO_ATTEMPT_BOUND_S
    assert first == frozenset()
    assert refresher.ready_in() > 0.0
    assert refresher._lock.acquire(blocking=False)  # not held by a hung attempt
    refresher._lock.release()


class SilentFrom:
    """Inspect wrapper: the connection goes silent just before ``onset`` is called (row I7)."""

    def __init__(self, inner: Any, proxy: FaultProxy, onset: str) -> None:
        self._inner = inner
        self._proxy = proxy
        self._onset = onset

    def _call(self, name: str) -> object:
        if name == self._onset:
            self._proxy.go_silent()
        return getattr(self._inner, name)()

    def stats(self) -> object:
        return self._call("stats")

    def registered(self) -> object:
        return self._call("registered")

    def ping(self) -> object:
        return self._call("ping")


@pytest.mark.parametrize("onset", ["stats", "registered", "ping"])
@pytest.mark.parametrize("kind", ["redis", "amqp"])
def test_i7_a_connection_that_goes_silent_mid_attempt_ends_the_attempt(
    kind: str, onset: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The URL stays out of the test id: xdist workers get different Redis databases.
    url = REDIS_URL if kind == "redis" else RABBITMQ_URL
    if not _reachable(url):
        message = f"{kind} broker not available at {url}"
        if os.environ.get(REQUIRE_BROKERS_ENV) == "1":
            pytest.fail(message)  # the job that sets it provides both brokers
        pytest.skip(message)
    parts = urlsplit(url)
    proxy = FaultProxy((parts.hostname or "localhost", parts.port or 0))
    proxied = url.replace(f":{parts.port}", f":{proxy.port}", 1)
    real_get = routes._get_inspector
    monkeypatch.setattr(
        routes,
        "_get_inspector",
        lambda u, **kw: SilentFrom(real_get(proxied, **kw), proxy, onset),
    )
    refresher = _InspectRefresher(WorkerRegistry(), url)

    try:
        started = time.monotonic()
        refresher.refresh_blocking()
        elapsed = time.monotonic() - started
    finally:
        proxy.close()

    assert elapsed < IO_ATTEMPT_BOUND_S
    assert refresher._failures == 1
    assert refresher._lock.acquire(blocking=False)
    refresher._lock.release()


# --- I8: the connection breaks between a call's last read and its publish write ----------

PUBLISH_SLEEP_BOUND = 2
BROKEN_PUBLISH_BOUND_S = 6.0
RETRY_RUNAWAY_SLEEPS = 60


class _StopRetrying(Exception):
    """Raised from the patched sleep so a regression ends the thread instead of leaking it."""


@pytest.mark.parametrize("kind", ["redis", "amqp"])
def test_i8_a_broker_that_goes_down_before_the_publish_write_ends_the_attempt(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Celery's ``Mailbox._publish`` publishes with ``retry=True``: kombu's ``ensure``
    then reconnects, by default for ever, while the broker stays down."""
    url = REDIS_URL if kind == "redis" else RABBITMQ_URL
    if not _reachable(url):
        message = f"{kind} broker not available at {url}"
        if os.environ.get(REQUIRE_BROKERS_ENV) == "1":
            pytest.fail(message)
        pytest.skip(message)
    parts = urlsplit(url)
    proxy = FaultProxy((parts.hostname or "localhost", parts.port or 0))
    proxied = url.replace(f":{parts.port}", f":{proxy.port}", 1)
    real_get = routes._get_inspector
    monkeypatch.setattr(
        routes, "_get_inspector", lambda u, **kw: real_get(proxied, **kw)
    )
    real_declare = kombu_messaging.Producer.maybe_declare
    broke = threading.Event()

    def declare_then_the_broker_dies(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_declare(self, *args, **kwargs)
        if not broke.is_set():
            broke.set()
            proxy.go_dead()  # RST on the live socket, and nothing listens any more
            time.sleep(0.2)  # let the RST reach the client socket before the write
        return result

    monkeypatch.setattr(
        kombu_messaging.Producer, "maybe_declare", declare_then_the_broker_dies
    )
    sleeps: list[float] = []
    stop = threading.Event()

    def counting_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        time.sleep(0.01)  # the count is what matters, not the wait
        if stop.is_set() or len(sleeps) > RETRY_RUNAWAY_SLEEPS:
            raise _StopRetrying

    monkeypatch.setattr(kombu_functional, "sleep", counting_sleep)
    refresher = _InspectRefresher(WorkerRegistry(), url)
    worker = threading.Thread(target=refresher.refresh_blocking, daemon=True)

    try:
        worker.start()
        worker.join(BROKEN_PUBLISH_BOUND_S)
        finished = not worker.is_alive()
    finally:
        stop.set()
        worker.join(5)
        proxy.close()

    assert finished, f"attempt still retrying after {len(sleeps)} sleeps"
    assert len(sleeps) <= PUBLISH_SLEEP_BOUND
    assert refresher._failures == 1
