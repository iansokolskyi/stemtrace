"""Shared pytest fixtures."""

import urllib.parse
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState

RawGet = Callable[[Any, str], Awaitable[int]]


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
