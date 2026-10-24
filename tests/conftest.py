"""Shared pytest fixtures."""

import os
import socket
import urllib.parse
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.config import _reset_config

RawGet = Callable[[Any, str], Awaitable[int]]

_DEFAULT_REDIS_URL = "redis://localhost:6379/15"


def _redis_url_for_worker(url: str, worker_id: str | None) -> str:
    """Return ``url`` pointing at a Redis db that only ``worker_id`` uses.

    xdist worker ``gwN`` gets the db N positions below the configured one, so
    workers never share streams or keys. Without xdist the URL is unchanged.
    """
    if not worker_id or not worker_id.startswith("gw"):
        return url
    parts = urllib.parse.urlsplit(url)
    base = int(parts.path.lstrip("/") or 0)
    db = base - int(worker_id[2:])
    if db < 0:
        raise RuntimeError(
            f"xdist worker {worker_id} has no Redis db below {base}; use fewer workers"
        )
    return urllib.parse.urlunsplit(parts._replace(path=f"/{db}"))


# Integration modules read REDIS_URL at import time and their worker subprocesses
# inherit it, so set it before they are collected.
if "PYTEST_XDIST_WORKER" in os.environ:
    os.environ["REDIS_URL"] = _redis_url_for_worker(
        os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL),
        os.environ["PYTEST_XDIST_WORKER"],
    )


class InterleavingLock:
    """Lock wrapper that runs a callback right before every acquisition.

    Simulates another thread winning the race for the lock at each
    acquisition point, which makes interleavings deterministic in tests.
    The callback may use the lock itself (re-entry doesn't re-trigger it).
    """

    def __init__(self, inner: Any, before_acquire: Callable[[], None]) -> None:
        self._inner = inner
        self._before_acquire = before_acquire
        self._in_callback = False
        self.interleavings = 0

    def __enter__(self) -> "InterleavingLock":
        if not self._in_callback:
            self._in_callback = True
            try:
                self._before_acquire()
                self.interleavings += 1
            finally:
                self._in_callback = False
        self._inner.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._inner.release()


@pytest.fixture(autouse=True)
def _reset_stemtrace_config() -> Iterator[None]:
    """Clear the worker config a test set, so it can't leak into the next test."""
    yield
    _reset_config()


@pytest.fixture
def sample_event() -> TaskEvent:
    """Create a sample task event for testing."""
    return TaskEvent(
        task_id="test-123",
        name="tests.sample_task",
        state=TaskState.PENDING,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def raw_get() -> RawGet:
    """GET a raw, un-normalized path straight through an ASGI app.

    HTTP clients normalize ``..`` segments before sending, so this builds the
    ASGI scope by hand (as a server would from the request line) and returns
    the response status code.
    """

    async def _get(app: Any, raw_path: str) -> int:
        messages: list[MutableMapping[str, Any]] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("latin-1"),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        async def receive() -> MutableMapping[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: MutableMapping[str, Any]) -> None:
            messages.append(message)

        await app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        return int(start["status"])

    return _get


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    """A loopback server socket that accepts connections."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    yield server
    server.close()


@pytest.fixture
def refused_address() -> Iterator[tuple[str, int]]:
    """A loopback address that refuses connections for the whole test.

    The port stays bound without listen(), so connecting gets ECONNREFUSED and no
    other process can take the port meanwhile (releasing it first would race).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        host, port = holder.getsockname()
        yield host, port
