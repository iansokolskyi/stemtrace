"""Tests for WebSocketManager."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.websocket import WebSocketManager


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
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
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


class TestWebSocketManagerListen:
    @pytest.mark.asyncio
    async def test_listen_until_disconnect(self, ws_manager: WebSocketManager) -> None:
        ws = MagicMock()
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=Exception("Disconnected"))

        await ws_manager.connect(ws)

        # Listen should return when connection closes (exception suppressed)
        await ws_manager.listen(ws)
