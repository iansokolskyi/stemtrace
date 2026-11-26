"""Tests for WebSocketManager."""

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, WebSocketDisconnect

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api.websocket import create_websocket_router
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.websocket import (
    LOOP_ERROR_BACKOFF_INITIAL_S,
    LOOP_ERROR_BACKOFF_MAX_S,
    LOOP_ERROR_LOG_INTERVAL_S,
    WebSocketManager,
)


@pytest.fixture
def ws_manager() -> WebSocketManager:
    """Create a fresh WebSocketManager for each test."""
    return WebSocketManager()


@pytest.fixture
def sample_event() -> TaskEvent:
    """Create a sample event for testing."""
    return TaskEvent(
        task_id="test-123",
        name="tests.sample",
        state=TaskState.STARTED,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def mock_websocket() -> MagicMock:
    """Create a mock WebSocket."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=asyncio.CancelledError)
    return ws


class TestWebSocketManagerBasics:
    def test_initial_state(self, ws_manager: WebSocketManager) -> None:
        assert ws_manager.connection_count == 0

    @pytest.mark.asyncio
    async def test_connect(
        self, ws_manager: WebSocketManager, mock_websocket: MagicMock
    ) -> None:
        await ws_manager.connect(mock_websocket)

        mock_websocket.accept.assert_awaited_once()
        assert ws_manager.connection_count == 1

    @pytest.mark.asyncio
    async def test_connect_multiple(self, ws_manager: WebSocketManager) -> None:
        for _ in range(3):
            ws = MagicMock()
            ws.accept = AsyncMock()
            await ws_manager.connect(ws)

        assert ws_manager.connection_count == 3

    @pytest.mark.asyncio
    async def test_disconnect(
        self, ws_manager: WebSocketManager, mock_websocket: MagicMock
    ) -> None:
        await ws_manager.connect(mock_websocket)
        assert ws_manager.connection_count == 1

        ws_manager.disconnect(mock_websocket)
        assert ws_manager.connection_count == 0

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent(
        self, ws_manager: WebSocketManager, mock_websocket: MagicMock
    ) -> None:
        # Should not raise when disconnecting a non-connected websocket
        ws_manager.disconnect(mock_websocket)
        assert ws_manager.connection_count == 0


class TestWebSocketManagerBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_to_connections(
        self,
        ws_manager: WebSocketManager,
        sample_event: TaskEvent,
    ) -> None:
        ws1 = MagicMock()
        ws1.accept = AsyncMock()
        ws1.send_text = AsyncMock()
        ws2 = MagicMock()
        ws2.accept = AsyncMock()
        ws2.send_text = AsyncMock()

        await ws_manager.connect(ws1)
        await ws_manager.connect(ws2)
        await ws_manager.broadcast(sample_event)

        ws1.send_text.assert_awaited_once()
        ws2.send_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broadcast_omits_task_output(
        self, ws_manager: WebSocketManager
    ) -> None:
        """Captured output stays out of broadcasts; the detail endpoint serves it."""
        ws = MagicMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()
        await ws_manager.connect(ws)
        event = TaskEvent(
            task_id="test-123",
            name="tests.sample",
            state=TaskState.SUCCESS,
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            output="x" * 1000,
        )

        await ws_manager.broadcast(event)

        sent = json.loads(ws.send_text.await_args.args[0])
        assert sent["task_id"] == "test-123"
        assert sent["state"] == "SUCCESS"
        assert "output" not in sent

    @pytest.mark.asyncio
    async def test_broadcast_empty_connections(
        self, ws_manager: WebSocketManager, sample_event: TaskEvent
    ) -> None:
        # Should not raise with no connections
        await ws_manager.broadcast(sample_event)

    @pytest.mark.asyncio
    async def test_broadcast_disconnects_on_error(
        self,
        ws_manager: WebSocketManager,
        sample_event: TaskEvent,
    ) -> None:
        good_ws = MagicMock()
        good_ws.accept = AsyncMock()
        good_ws.send_text = AsyncMock()

        bad_ws = MagicMock()
        bad_ws.accept = AsyncMock()
        bad_ws.send_text = AsyncMock(side_effect=Exception("Connection closed"))

        await ws_manager.connect(good_ws)
        await ws_manager.connect(bad_ws)
        assert ws_manager.connection_count == 2

        await ws_manager.broadcast(sample_event)

        # Good ws should still be connected, bad one disconnected
        assert ws_manager.connection_count == 1


class TestWebSocketManagerBroadcastLoop:
    @pytest.mark.asyncio
    async def test_start_stop_broadcast_loop(
        self, ws_manager: WebSocketManager
    ) -> None:
        await ws_manager.start_broadcast_loop()

        # Loop should be running, has internal task
        assert ws_manager._broadcast_task is not None
        assert ws_manager._loop is not None

        await ws_manager.stop_broadcast_loop()

        assert ws_manager._broadcast_task is None
        assert ws_manager._loop is None

    @pytest.mark.asyncio
    async def test_queue_event_no_loop(
        self, ws_manager: WebSocketManager, sample_event: TaskEvent
    ) -> None:
        # Should not raise when no loop is running
        ws_manager.queue_event(sample_event)

    @pytest.mark.asyncio
    async def test_queue_event_broadcasts(
        self, ws_manager: WebSocketManager, sample_event: TaskEvent
    ) -> None:
        ws = MagicMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()

        await ws_manager.connect(ws)
        await ws_manager.start_broadcast_loop()

        try:
            ws_manager.queue_event(sample_event)
            # Give the broadcast loop time to process
            await asyncio.sleep(0.05)

            ws.send_text.assert_awaited()
        finally:
            await ws_manager.stop_broadcast_loop()


_RECEIVE_TIMEOUT_S = 5.0


def _fake_ws() -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    return ws


async def _wait_for_send(ws: MagicMock) -> None:
    """Poll without sleeping a fixed time: yield until send_text was awaited."""

    async def _until() -> None:
        while not ws.send_text.await_count:
            await asyncio.sleep(0)

    await asyncio.wait_for(_until(), _RECEIVE_TIMEOUT_S)


def _run_in_new_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run ``coro`` on a fresh loop without asyncio.run().

    asyncio.run() resets the thread's current loop to None, which leaks unclosed
    loops into later tests under the lowest pinned pytest-asyncio.
    """
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def _broadcast_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "stemtrace.server.websocket"]


class _Flooded(Exception):
    """Raised from the log handler to end a runaway loop instead of hanging."""


class _BoundedLog(logging.Handler):
    """Collects records and aborts the logging caller once the flood limit is hit.

    A spinning broadcast loop never yields, so no timeout can stop it; raising from
    the handler escapes its ``except`` block and ends the task.
    """

    LIMIT = 50

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        if len(self.records) > self.LIMIT:
            raise _Flooded


class TestSecondLifespan:
    """A queue binds to the first loop that awaits it; each lifespan needs its own."""

    def test_second_event_loop_delivers_broadcast_with_bounded_logs(
        self, sample_event: TaskEvent
    ) -> None:
        manager = WebSocketManager()

        async def lifespan(ws: MagicMock) -> None:
            await manager.connect(ws)
            await manager.start_broadcast_loop()
            try:
                manager.queue_event(sample_event)
                await _wait_for_send(ws)
            finally:
                await manager.stop_broadcast_loop()
                manager.disconnect(ws)

        first, second = _fake_ws(), _fake_ws()
        _run_in_new_loop(lifespan(first))
        log = _BoundedLog()
        ws_logger = logging.getLogger("stemtrace.server.websocket")
        ws_logger.addHandler(log)
        try:
            _run_in_new_loop(lifespan(second))
        except asyncio.TimeoutError:
            # Before the fix the broadcast never arrives; the asserts below report it.
            pass
        finally:
            ws_logger.removeHandler(log)

        assert first.send_text.await_count == 1
        assert second.send_text.await_count == 1
        assert [r.getMessage() for r in log.records if r.levelno >= logging.ERROR] == []

    def test_extension_second_lifespan_delivers_store_event(
        self, sample_event: TaskEvent
    ) -> None:
        ext = StemtraceExtension(
            broker_url="memory://", embedded_consumer=False, serve_ui=False
        )
        app = FastAPI()

        async def lifespan(ws: MagicMock, task_id: str) -> None:
            async with ext.lifespan(app):
                await ext.ws_manager.connect(ws)
                ext.store.add_event(
                    sample_event.model_copy(update={"task_id": task_id})
                )
                await _wait_for_send(ws)
                ext.ws_manager.disconnect(ws)

        first, second = _fake_ws(), _fake_ws()
        _run_in_new_loop(lifespan(first, "t1"))
        log = _BoundedLog()
        ws_logger = logging.getLogger("stemtrace.server.websocket")
        ws_logger.addHandler(log)
        try:
            _run_in_new_loop(lifespan(second, "t2"))
        except asyncio.TimeoutError:
            # Before the fix the broadcast never arrives; the asserts below report it.
            pass
        finally:
            ws_logger.removeHandler(log)

        assert second.send_text.await_count == 1
        assert [r.getMessage() for r in log.records if r.levelno >= logging.ERROR] == []

    @pytest.mark.asyncio
    async def test_queue_event_after_stop_is_not_delivered_on_next_start(
        self, sample_event: TaskEvent
    ) -> None:
        manager = WebSocketManager()
        await manager.start_broadcast_loop()
        await manager.stop_broadcast_loop()

        manager.queue_event(sample_event)

        ws = _fake_ws()
        await manager.connect(ws)
        await manager.start_broadcast_loop()
        try:
            for _ in range(20):  # let anything stale reach the new loop
                await asyncio.sleep(0)
        finally:
            await manager.stop_broadcast_loop()
        assert ws.send_text.await_count == 0


class _FlakyManager(WebSocketManager):
    """Raises from broadcast() for the first ``failures`` events."""

    def __init__(self, failures: int, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # forwards sleep/clock
        self.failures = failures

    async def broadcast(self, event: TaskEvent) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("boom")
        await super().broadcast(event)


class TestBroadcastLoopErrors:
    @staticmethod
    def _manager(failures: int, sleeps: list[float], now: list[float]) -> _FlakyManager:
        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            await asyncio.sleep(0)

        return _FlakyManager(failures, sleep=fake_sleep, clock=lambda: now[0])

    @pytest.mark.asyncio
    async def test_loop_error_backs_off_logs_once_and_recovers(
        self, sample_event: TaskEvent, caplog: pytest.LogCaptureFixture
    ) -> None:
        sleeps: list[float] = []
        manager = self._manager(3, sleeps, [0.0])
        ws = _fake_ws()
        await manager.connect(ws)
        await manager.start_broadcast_loop()
        try:
            with caplog.at_level(logging.DEBUG, logger="stemtrace.server.websocket"):
                for _ in range(4):
                    manager.queue_event(sample_event)
                await _wait_for_send(ws)
        finally:
            await manager.stop_broadcast_loop()

        assert sleeps == [
            LOOP_ERROR_BACKOFF_INITIAL_S,
            LOOP_ERROR_BACKOFF_INITIAL_S * 2,
            LOOP_ERROR_BACKOFF_INITIAL_S * 4,
        ]
        errors = [r for r in _broadcast_logs(caplog) if r.levelno >= logging.ERROR]
        assert [r.getMessage() for r in errors] == [
            "Error in broadcast loop",
            "Broadcast loop recovered after 3 errors",
        ]
        assert errors[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_loop_error_state_resets_after_each_recovery(
        self, sample_event: TaskEvent, caplog: pytest.LogCaptureFixture
    ) -> None:
        sleeps: list[float] = []
        manager = self._manager(1, sleeps, [0.0])
        ws = _fake_ws()
        await manager.connect(ws)
        await manager.start_broadcast_loop()

        async def until(done: Callable[[], bool]) -> None:
            for _ in range(1000):  # bounded: a missing step fails, not hangs
                if done():
                    return
                await asyncio.sleep(0)

        async def push(*, delivered: int, slept: int) -> None:
            manager.queue_event(sample_event)
            await until(
                lambda: ws.send_text.await_count >= delivered and len(sleeps) >= slept
            )

        def errors() -> list[str]:
            return [
                r.getMessage()
                for r in _broadcast_logs(caplog)
                if r.levelno >= logging.ERROR
            ]

        try:
            with caplog.at_level(logging.DEBUG, logger="stemtrace.server.websocket"):
                # fail, succeed, fail, succeed, succeed
                await push(delivered=0, slept=1)
                await push(delivered=1, slept=1)
                manager.failures = 1
                await push(delivered=1, slept=2)
                await push(delivered=2, slept=2)
                before_last = errors()
                await push(delivered=3, slept=2)
                after_last = errors()
        finally:
            await manager.stop_broadcast_loop()

        assert sleeps == [LOOP_ERROR_BACKOFF_INITIAL_S, LOOP_ERROR_BACKOFF_INITIAL_S]
        assert before_last == [
            "Error in broadcast loop",
            "Broadcast loop recovered after 1 errors",
            "Error in broadcast loop",
            "Broadcast loop recovered after 1 errors",
        ]
        assert after_last == before_last

    @pytest.mark.asyncio
    async def test_loop_error_summaries_are_rate_limited_and_backoff_is_capped(
        self, sample_event: TaskEvent, caplog: pytest.LogCaptureFixture
    ) -> None:
        sleeps: list[float] = []
        now = [0.0]
        failures = 12
        manager = self._manager(failures, sleeps, now)
        await manager.start_broadcast_loop()
        try:
            with caplog.at_level(logging.DEBUG, logger="stemtrace.server.websocket"):
                for i in range(failures):
                    manager.queue_event(sample_event)
                    for _ in range(1000):  # bounded: a missing backoff fails, not hangs
                        if len(sleeps) > i:
                            break
                        await asyncio.sleep(0)
                    if i == 5:
                        now[0] += LOOP_ERROR_LOG_INTERVAL_S
        finally:
            await manager.stop_broadcast_loop()

        assert sleeps[-1] == LOOP_ERROR_BACKOFF_MAX_S
        messages = [
            r.getMessage()
            for r in _broadcast_logs(caplog)
            if r.levelno >= logging.ERROR
        ]
        assert messages[0] == "Error in broadcast loop"
        assert len(messages) == 2
        assert messages[1].startswith("Broadcast loop still failing (")


class TestWebSocketManagerListen:
    @pytest.mark.asyncio
    async def test_listen_until_disconnect(self, ws_manager: WebSocketManager) -> None:
        ws = MagicMock()
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=Exception("Disconnected"))

        await ws_manager.connect(ws)

        # Listen should return when connection closes (exception suppressed)
        await ws_manager.listen(ws)


class TestApiWebSocketRouter:
    @pytest.mark.asyncio
    async def test_websocket_router_disconnect_is_handled_and_disconnect_called(
        self,
    ) -> None:
        """WebSocketDisconnect should be swallowed and disconnect() must run in finally."""
        ws_manager = MagicMock()
        ws_manager.connect = AsyncMock()
        ws_manager.listen = AsyncMock(side_effect=WebSocketDisconnect())
        ws_manager.disconnect = MagicMock()

        router = create_websocket_router(ws_manager)
        route = next(r for r in router.routes if getattr(r, "path", None) == "/ws")

        websocket = MagicMock()
        await route.endpoint(websocket)

        ws_manager.connect.assert_awaited_once_with(websocket, subprotocol=None)
        ws_manager.listen.assert_awaited_once_with(websocket)
        ws_manager.disconnect.assert_called_once_with(websocket)
