"""Shared pytest fixtures."""

import os
import socket
import urllib.parse
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

import pytest
from kombu.transport.memory import Channel as MemoryChannel

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.config import _reset_config
from tests.redis_isolation import (
    DEFAULT_REDIS_URL,
    check_worker_count,
    count_tx_workers,
    redis_url_for_worker,
)

RawGet = Callable[[Any, str], Awaitable[int]]


_LOST_WORKERS = pytest.StashKey[list[str]]()


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast, on the xdist controller, when workers can't each get a Redis db."""
    tx = getattr(config.option, "tx", None)  # absent under -p no:xdist
    if "PYTEST_XDIST_WORKER" in os.environ or not tx or config.option.dist == "no":
        return
    # ``-n`` has already been turned into ``tx``; ``--tx N*popen`` arrives as ``tx`` only.
    # A crashed worker's replacement takes the next unused id, so an unset restart
    # limit becomes 0: a lost worker stops the run (see pytest_sessionfinish).
    if config.option.maxworkerrestart is None:
        config.option.maxworkerrestart = "0"
    try:
        restarts = int(config.option.maxworkerrestart)
    except ValueError as err:
        raise pytest.UsageError("--max-worker-restart must be a whole number") from err
    try:
        check_worker_count(
            os.environ.get("REDIS_URL", DEFAULT_REDIS_URL),
            count_tx_workers(tx) + restarts,
        )
    except ValueError as err:
        raise pytest.UsageError(str(err)) from err


@pytest.hookimpl(optionalhook=True)  # hookspec exists only when xdist is loaded
def pytest_testnodedown(node: Any, error: object) -> None:
    """Record a worker that died, on the controller."""
    # xdist 3.5.0 and 3.8.0 DSession.worker_errordown call this with the error first.
    if error is not None and error != "keyboard-interrupt":
        node.config.stash.setdefault(_LOST_WORKERS, []).append(node.gateway.id)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run when more workers died than --max-worker-restart replaced.

    A worker that dies before it has a test item (SIGKILL, OOM, a crash at import)
    leaves xdist nothing to report, and it ends the run with a green or "no tests
    ran" status.
    """
    lost = session.config.stash.get(_LOST_WORKERS, [])
    # None: pytest_configure skipped this controller (it inherited
    # PYTEST_XDIST_WORKER) and never set the limit, so no restart is allowed.
    limit = int(session.config.option.maxworkerrestart or 0)
    if (
        lost
        and len(lost) > limit
        and (
            session.exitstatus
            in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED)
        )
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


# Integration modules read REDIS_URL at import, so set it before they are collected.
# pytest_configure above has already checked that every worker gets a valid db.
if "PYTEST_XDIST_WORKER" in os.environ:
    os.environ["REDIS_URL"] = redis_url_for_worker(
        os.environ.get("REDIS_URL", DEFAULT_REDIS_URL),
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


@pytest.fixture(autouse=True)
def _empty_memory_broker() -> Iterator[None]:
    """Drop messages a test left in kombu's memory broker.

    kombu 5.6 keeps its queues on ``memory.Channel`` (class level), so a message
    a test published would otherwise reach the next test's real worker.
    """
    yield
    MemoryChannel.queues.clear()


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
