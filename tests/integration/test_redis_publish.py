"""Integration tests for RedisTransport.publish() with real redis-py sockets.

The silent-listener test needs no Redis: it points the transport at a local
TCP listener that accepts connections but never answers. The other tests need
a running Redis (REDIS_URL) and are skipped when it isn't available.

Run with: pytest tests/integration/test_redis_publish.py -v
"""

import contextlib
import os
import signal
import socket
import sys
import threading
import time
import urllib.parse
import warnings
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import redis

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports import redis as redis_transport
from stemtrace.transports.redis import (
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_SOCKET_TIMEOUT,
    RECONNECT_INITIAL_BACKOFF,
    RedisTransport,
)
from stemtrace.transports.resolver import BoundedResolver

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")

# Delay the proxy adds to every chunk it forwards from Redis: slow enough to
# count as a slow publish, well below the publish socket timeout.
PROXY_DELAY = 0.6


def redis_available() -> bool:
    """Check if Redis is available."""
    try:
        redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
    except (redis.RedisError, OSError):
        return False
    return True


requires_redis = pytest.mark.skipif(not redis_available(), reason="Redis not available")


def _event(i: int) -> TaskEvent:
    return TaskEvent(
        task_id=f"publish-{i}",
        name="tests.task",
        state=TaskState.STARTED,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _timed_publishes(transport: RedisTransport, count: int) -> list[float]:
    durations = []
    for i in range(count):
        started = time.monotonic()
        transport.publish(_event(i))
        durations.append(time.monotonic() - started)
    return durations


class _DelayingProxy:
    """TCP proxy to Redis that sleeps before forwarding each chunk Redis sends."""

    def __init__(self, target: tuple[str, int], delay: float) -> None:
        self._target = target
        self._delay = delay
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._server.settimeout(0.1)
        self._stop = threading.Event()
        self._sockets: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self._server.getsockname()[1])

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            upstream = socket.create_connection(self._target, timeout=5)
            self._sockets += [client, upstream]
            for src, dst, delay in (
                (client, upstream, 0.0),
                (upstream, client, self._delay),
            ):
                threading.Thread(
                    target=self._pipe, args=(src, dst, delay), daemon=True
                ).start()

    def _pipe(self, src: socket.socket, dst: socket.socket, delay: float) -> None:
        src.settimeout(None)
        with contextlib.suppress(OSError):
            while chunk := src.recv(65536):
                time.sleep(delay)
                dst.sendall(chunk)

    def close(self) -> None:
        self._stop.set()
        for sock in [self._server, *self._sockets]:
            with contextlib.suppress(OSError):
                sock.close()


@pytest.fixture
def stream_prefix() -> Iterator[str]:
    """Unique stream prefix, deleted after the test."""
    prefix = f"test_publish_{time.time_ns()}"
    yield prefix
    with contextlib.suppress(redis.RedisError, OSError):
        redis.from_url(REDIS_URL).delete(f"{prefix}:events")


@pytest.mark.integration
def test_publish_to_silent_listener_opens_after_three_attempts() -> None:
    """A Redis that accepts but never answers costs three attempts, then none."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        transport = RedisTransport.from_url(f"redis://127.0.0.1:{port}/0")

        durations = _timed_publishes(transport, 20)

    breaker = transport._publish_breaker
    attempts = durations[:PUBLISH_FAILURES_TO_OPEN]
    # Each attempt is one bounded read timeout: hidden retries would double it.
    assert all(d < 2 * PUBLISH_SOCKET_TIMEOUT for d in attempts), durations
    assert breaker.is_open is True
    # The events after the third failure were dropped without an attempt.
    # Before the publish timeouts and the breaker, each of the 20 waited
    # redis-py's 5s default.
    assert breaker.dropped == 20 - PUBLISH_FAILURES_TO_OPEN
    assert sum(durations) < 10 * PUBLISH_SOCKET_TIMEOUT, durations


class _ReplyOnlyToHandshake:
    """Fake Redis: answers the handshake, then ignores XADD or replies ``xadd_reply``."""

    def __init__(self, xadd_reply: bytes | None = None) -> None:
        self._xadd_reply = xadd_reply
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._server.settimeout(0.1)
        self._stop = threading.Event()
        self._sockets: list[socket.socket] = []
        self.xadds = 0
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self._server.getsockname()[1]}/0"

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            self._sockets.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        reader = conn.makefile("rb")
        with contextlib.suppress(OSError, ValueError):
            while header := reader.readline():
                # RESP array of bulk strings: *<n>, then $<len> and data each.
                args = []
                for _ in range(int(header[1:])):
                    length = int(reader.readline()[1:])
                    args.append(reader.read(length + 2)[:-2])
                command = args[0].upper()
                if command == b"XADD":
                    self.xadds += 1
                    if self._xadd_reply is not None:
                        conn.sendall(self._xadd_reply)
                elif command == b"HELLO":
                    # Sent by redis-py versions that negotiate a protocol
                    # (RESP3 by default in 8.x); the client checks "proto".
                    proto = int(args[1]) if len(args) > 1 else 2
                    if proto == 3:
                        conn.sendall(b"%1\r\n+proto\r\n:3\r\n")
                    else:
                        conn.sendall(b"*2\r\n$5\r\nproto\r\n:2\r\n")
                else:
                    conn.sendall(b"+OK\r\n")

    def close(self) -> None:
        self._stop.set()
        for sock in [self._server, *self._sockets]:
            with contextlib.suppress(OSError):
                sock.close()


@pytest.mark.integration
def test_publish_reply_timeout_after_xadd_counts_as_unconfirmed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An XADD whose reply times out may still be applied: not "dropped"."""
    server = _ReplyOnlyToHandshake()
    try:
        transport = RedisTransport.from_url(server.url)

        transport.publish(_event(0))
    finally:
        server.close()

    assert server.xadds == 1
    assert transport._publish_breaker.is_open is False
    assert (
        "(0 event(s) dropped, 1 sent but unconfirmed since the last report)"
        in caplog.text
    )


@pytest.mark.integration
def test_publish_loading_error_reply_counts_as_dropped_not_unconfirmed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Redis still loading its dataset rejects the XADD with -LOADING.

    redis-py raises BusyLoadingError, a ConnectionError subclass, but Redis
    answered: the event was rejected, not lost in transit, so it counts as
    dropped and a failed retry doubles the cooldown instead of jumping to the
    unconfirmed-retry maximum.
    """
    server = _ReplyOnlyToHandshake(b"-LOADING Redis is loading the dataset\r\n")
    try:
        transport = RedisTransport.from_url(server.url)
        breaker = transport._publish_breaker
        for i in range(PUBLISH_FAILURES_TO_OPEN):
            transport.publish(_event(i))
        assert breaker.is_open is True
        # Let the cooldown pass on the breaker's clock, then probe once more.
        breaker._open_until = 0.0
        transport.publish(_event(99))
    finally:
        server.close()

    assert server.xadds == PUBLISH_FAILURES_TO_OPEN + 1
    assert breaker.cooldown == 2.0
    assert "unconfirmed" not in caplog.text


@pytest.mark.integration
@requires_redis
def test_publish_to_real_redis_appends_every_event(stream_prefix: str) -> None:
    transport = RedisTransport.from_url(REDIS_URL, prefix=stream_prefix)

    _timed_publishes(transport, 20)

    assert redis.from_url(REDIS_URL).xlen(f"{stream_prefix}:events") == 20
    assert transport._publish_breaker.is_open is False


@pytest.mark.integration
@requires_redis
def test_publish_through_slow_proxy_delivers_every_event(
    stream_prefix: str,
) -> None:
    """A Redis that answers slowly, within the timeouts, gets every event.

    Slow successes don't open the breaker: each publish waits for Redis, up
    to the socket timeouts.
    """
    parsed = urllib.parse.urlsplit(REDIS_URL)
    proxy = _DelayingProxy(
        (parsed.hostname or "localhost", parsed.port or 6379), PROXY_DELAY
    )
    try:
        userinfo, _, _ = parsed.netloc.rpartition("@")
        netloc = (
            f"{userinfo}@127.0.0.1:{proxy.port}"
            if userinfo
            else f"127.0.0.1:{proxy.port}"
        )
        proxied = parsed._replace(netloc=netloc).geturl()
        transport = RedisTransport.from_url(proxied, prefix=stream_prefix)

        durations = _timed_publishes(transport, 5)
    finally:
        proxy.close()

    assert redis.from_url(REDIS_URL).xlen(f"{stream_prefix}:events") == 5
    assert transport._publish_breaker.is_open is False
    assert min(durations) >= PROXY_DELAY, durations


@pytest.mark.integration
@requires_redis
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_publish_in_forked_child_while_parent_thread_holds_pool_lock(
    stream_prefix: str,
) -> None:
    """A prefork child publishes even if the parent's pool lock was held at fork.

    redis-py 8.1 resets an inherited pool on first use in the child and takes
    the pool's lock to do it, without a timeout; a lock held by another parent
    thread at fork time would hang the child forever.
    """
    transport = RedisTransport.from_url(REDIS_URL, prefix=stream_prefix)
    transport.publish(_event(0))  # the parent's pool now has a connection
    pool_lock = transport.publish_client.connection_pool._lock
    held, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with pool_lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        with warnings.catch_warnings():
            # Python warns that forking a multi-threaded process may deadlock:
            # that is the situation under test.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child process
            signal.alarm(5)  # a hang ends the child with SIGALRM, not the run
            transport.publish(_event(1))
            os._exit(0)
        _, status = os.waitpid(pid, 0)
    finally:
        release.set()
        holder.join(5)

    assert os.waitstatus_to_exitcode(status) == 0
    assert redis.from_url(REDIS_URL).xlen(f"{stream_prefix}:events") == 2


@pytest.mark.integration
@requires_redis
def test_publish_connects_to_address_from_publish_resolver(
    stream_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish client takes Redis's address from the bounded resolver."""
    parsed = urllib.parse.urlsplit(REDIS_URL)
    real_host, real_port = parsed.hostname or "localhost", parsed.port or 6379
    looked_up: list[str] = []

    def lookup(host: str, port: int, *args: Any) -> list[tuple[Any, ...]]:
        looked_up.append(host)
        return socket.getaddrinfo(real_host, real_port, *args)

    monkeypatch.setattr(
        redis_transport, "PUBLISH_RESOLVER", BoundedResolver(getaddrinfo=lookup)
    )
    userinfo, _, _ = parsed.netloc.rpartition("@")
    alias = "redis-alias.stemtrace.test:6379"
    netloc = f"{userinfo}@{alias}" if userinfo else alias
    transport = RedisTransport.from_url(
        parsed._replace(netloc=netloc).geturl(), prefix=stream_prefix
    )

    _timed_publishes(transport, 3)

    assert redis.from_url(REDIS_URL).xlen(f"{stream_prefix}:events") == 3
    assert looked_up == ["redis-alias.stemtrace.test"]


class _ScriptedDns:
    """getaddrinfo stand-in returning ``answers`` in turn (the last one repeats)."""

    def __init__(self, *answers: tuple[str, int]) -> None:
        self._answers = list(answers)
        self.calls = 0

    def __call__(self, host: str, port: int, *args: Any) -> list[tuple[Any, ...]]:
        self.calls += 1
        ip, real_port = (
            self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        )
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, real_port))]


class _ManualClock:
    """Monotonic clock for the publish breaker, advanced by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.integration
def test_publish_after_dns_failover_recovers_within_one_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host moves to a new address while the old one refuses connections.

    The failed connect refreshes the cached DNS answer at once, so the next
    attempt reaches the new address (at the latest the first retry after the
    breaker's 1s cooldown), instead of waiting for the 30s cache TTL and then
    the next retry after it.
    """
    with socket.socket() as gone:
        gone.bind(("127.0.0.1", 0))
        old_port = gone.getsockname()[1]  # closed: connections are refused
    server = _ReplyOnlyToHandshake(b"$3\r\n1-0\r\n")
    new_port = int(server.url.rsplit(":", 1)[1].split("/")[0])
    dns = _ScriptedDns(("127.0.0.1", old_port), ("127.0.0.1", new_port))
    monkeypatch.setattr(
        redis_transport, "PUBLISH_RESOLVER", BoundedResolver(getaddrinfo=dns)
    )
    clock = _ManualClock()
    started = clock.now
    try:
        transport = RedisTransport(
            client=redis.Redis(),  # consume client, unused here
            prefix="failover",
            ttl=60,
            publish_client_factory=lambda: redis_transport._publish_client_from_url(
                "redis://redis.failover.test:6379/0"
            ),
            monotonic=clock,
        )
        breaker = transport._publish_breaker
        for i in range(10):
            transport.publish(_event(i))
            if server.xadds:
                break
            # Let the background refresh finish, as it would between events.
            for thread in threading.enumerate():
                if thread.name.startswith("stemtrace-dns-"):
                    thread.join(5)
            if breaker.is_open:
                clock.now += breaker.cooldown
    finally:
        server.close()

    assert server.xadds == 1
    assert i == 1  # the second attempt already used the new address
    assert clock.now - started <= RECONNECT_INITIAL_BACKOFF
    assert breaker.is_open is False


@pytest.mark.integration
@requires_redis
def test_publish_from_forking_uwsgi_like_process_falls_back_in_process(
    stream_prefix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Under uWSGI sys.executable is the uwsgi binary: the helper can't run
    this Python, so lookups stay in-process and events still get through."""
    uwsgi = tmp_path / "uwsgi"
    uwsgi.write_text("#!/bin/sh\nexit 1\n")
    uwsgi.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(uwsgi))
    resolver = BoundedResolver()
    monkeypatch.setattr(redis_transport, "PUBLISH_RESOLVER", resolver)
    resolver.prepare_fork()  # this process forks, like a uWSGI master
    resolver.parent_after_fork()
    for thread in threading.enumerate():
        if thread.name == "stemtrace-dns-helper-check":
            thread.join(10)
    parsed = urllib.parse.urlsplit(REDIS_URL)
    userinfo, _, _ = parsed.netloc.rpartition("@")
    host = f"localhost:{parsed.port or 6379}"
    netloc = f"{userinfo}@{host}" if userinfo else host
    transport = RedisTransport.from_url(
        parsed._replace(netloc=netloc).geturl(), prefix=stream_prefix
    )

    _timed_publishes(transport, 5)

    assert redis.from_url(REDIS_URL).xlen(f"{stream_prefix}:events") == 5
    assert "resolving in-process" in caplog.text
