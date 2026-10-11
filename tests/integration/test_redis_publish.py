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
import threading
import time
import urllib.parse
import warnings
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
import redis

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports.redis import (
    PUBLISH_SOCKET_TIMEOUT,
    RedisTransport,
)

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
def test_publish_to_silent_listener_costs_one_bounded_attempt() -> None:
    """A Redis that accepts but never answers blocks one publish, not each one."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        transport = RedisTransport.from_url(f"redis://127.0.0.1:{port}/0")

        durations = _timed_publishes(transport, 20)

    # One attempt timed out and opened the breaker; the other 19 were dropped
    # without contacting the listener. Before the publish timeouts and the
    # breaker, each of the 20 waited redis-py's 5s default.
    assert transport._publish_breaker.is_open is True
    assert transport._publish_breaker.dropped == 19
    assert sum(durations) < 10 * PUBLISH_SOCKET_TIMEOUT, durations


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
