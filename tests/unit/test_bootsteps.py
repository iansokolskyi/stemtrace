"""Tests for bootsteps module."""

from __future__ import annotations

from unittest.mock import MagicMock

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.bootsteps import (
    ReceivedEventStep,
    _set_publisher,
    register_bootsteps,
)


class TestReceivedEventStep:
    """Tests for ReceivedEventStep bootstep."""

    def test_emits_received_event_dict_format(self) -> None:
        """Test RECEIVED event is emitted for dict body format."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        # Create mock consumer with strategies
        mock_consumer = MagicMock()
        original_strategy = MagicMock(return_value="result")
        mock_consumer.strategies = {"test_task": original_strategy}

        # Create and start bootstep
        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        # Get wrapped strategy and call it
        wrapped = mock_consumer.strategies["test_task"]
        body = {"id": "task-123", "parent_id": "parent-456", "root_id": "root-789"}
        message = MagicMock()
        message.headers = {}

        result = wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

        # Check RECEIVED event was emitted
        assert len(events) == 1
        assert events[0].task_id == "task-123"
        assert events[0].state == TaskState.RECEIVED
        assert events[0].name == "test_task"
        assert events[0].parent_id == "parent-456"
        assert events[0].root_id == "root-789"

        # Check original strategy was called
        assert result == "result"

    def test_emits_received_event_tuple_format(self) -> None:
        """Test RECEIVED event is emitted for tuple body format."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        mock_consumer = MagicMock()
        original_strategy = MagicMock()
        mock_consumer.strategies = {"test_task": original_strategy}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        # Old tuple format: (args, kwargs, embed)
        body = ([], {}, {"id": "task-tuple", "parent_id": None, "root_id": None})
        message = MagicMock()
        message.headers = {}

        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

        assert len(events) == 1
        assert events[0].task_id == "task-tuple"
        assert events[0].state == TaskState.RECEIVED

    def test_fallback_to_message_headers(self) -> None:
        """Test fallback to message headers when body doesn't have task_id."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        mock_consumer = MagicMock()
        mock_consumer.strategies = {"test_task": MagicMock()}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {}  # Empty body
        message = MagicMock()
        message.headers = {"id": "task-from-header"}

        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

        assert len(events) == 1
        assert events[0].task_id == "task-from-header"

    def test_no_event_when_no_task_id(self) -> None:
        """Test no event is emitted when task_id cannot be extracted."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        mock_consumer = MagicMock()
        mock_consumer.strategies = {"test_task": MagicMock()}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {}
        message = MagicMock()
        message.headers = {}

        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

        # No event should be emitted
        assert len(events) == 0

    def test_stop_restores_original_strategies(self) -> None:
        """Test stop() restores original strategies."""
        mock_consumer = MagicMock()
        original_strategy = MagicMock()
        mock_consumer.strategies = {"test_task": original_strategy}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        # Strategy should be wrapped
        assert mock_consumer.strategies["test_task"] != original_strategy

        step.stop(mock_consumer)

        # Strategy should be restored
        assert mock_consumer.strategies["test_task"] == original_strategy

    def test_no_crash_when_publisher_not_set(self) -> None:
        """Test no crash when publisher is None."""
        _set_publisher(None)

        mock_consumer = MagicMock()
        mock_consumer.strategies = {"test_task": MagicMock()}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {"id": "task-123"}
        message = MagicMock()
        message.headers = {}

        # Should not raise
        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

    def test_skips_received_for_retries(self) -> None:
        """Retries should not emit RECEIVED (they already have RETRY + STARTED events)."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        mock_consumer = MagicMock()
        mock_consumer.strategies = {"test_task": MagicMock()}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {"id": "task-123", "retries": 1}
        message = MagicMock()
        message.headers = {}

        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())
        assert events == []

    def test_wrap_strategy_suppresses_emit_errors(self) -> None:
        """Errors during RECEIVED emission should be suppressed, and strategy still runs."""

        def broken_publish(event: TaskEvent) -> None:
            raise RuntimeError("publish failed")

        _set_publisher(broken_publish)

        mock_consumer = MagicMock()
        original_strategy = MagicMock(return_value="ok")
        mock_consumer.strategies = {"test_task": original_strategy}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {"id": "task-123"}
        message = MagicMock()
        message.headers = {}

        result = wrapped(message, body, MagicMock(), MagicMock(), MagicMock())
        assert result == "ok"
        original_strategy.assert_called_once()


class TestRegisterBootsteps:
    """Tests for register_bootsteps function."""

    def test_registers_bootstep(self) -> None:
        """Test bootstep is registered with the app."""
        mock_app = MagicMock()
        mock_app.steps = {"consumer": set()}

        register_bootsteps(mock_app)

        assert ReceivedEventStep in mock_app.steps["consumer"]
