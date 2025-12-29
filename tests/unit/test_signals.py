"""Tests for Celery signal handlers."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from celery_flow.core.events import TaskState
from celery_flow.library.config import CeleryFlowConfig, set_config
from celery_flow.library.signals import (
    _extract_chord_info,
    _on_task_failure,
    _on_task_postrun,
    _on_task_prerun,
    _on_task_retry,
    _on_task_revoked,
    _on_task_sent,
    connect_signals,
    disconnect_signals,
)
from celery_flow.library.transports.memory import MemoryTransport


@pytest.fixture(autouse=True)
def clean_transport() -> None:
    """Clean up transport state before each test."""
    MemoryTransport.clear()
    disconnect_signals()


@pytest.fixture
def config() -> CeleryFlowConfig:
    """Set up default config for tests."""
    cfg = CeleryFlowConfig(transport_url="memory://")
    set_config(cfg)
    return cfg


@pytest.fixture
def transport(config: CeleryFlowConfig) -> MemoryTransport:
    """Create and connect a MemoryTransport."""
    transport = MemoryTransport()
    connect_signals(transport)
    return transport


@pytest.fixture
def mock_task() -> MagicMock:
    """Create a mock Celery task."""
    task = MagicMock()
    task.name = "tests.sample_task"
    task.request.id = "task-123"
    task.request.parent_id = None
    task.request.root_id = None
    task.request.group = None
    task.request.chord = None
    task.request.retries = 0
    return task


@pytest.fixture
def mock_task_with_parent() -> MagicMock:
    """Create a mock Celery task with parent."""
    task = MagicMock()
    task.name = "tests.child_task"
    task.request.id = "task-456"
    task.request.parent_id = "task-123"
    task.request.root_id = "task-001"
    task.request.group = None
    task.request.chord = None
    task.request.retries = 0
    return task


class TestConnectDisconnect:
    """Tests for connect/disconnect functions."""

    def test_connect_signals_stores_transport(self) -> None:
        """connect_signals() enables event publishing."""
        transport = MemoryTransport()
        connect_signals(transport)

        # Simulate a signal - should publish event
        task = MagicMock()
        task.name = "tests.task"
        task.request.parent_id = None
        task.request.root_id = None
        task.request.group = None
        task.request.chord = None
        task.request.retries = 0

        _on_task_prerun(
            task_id="test-id",
            task=task,
            args=(),
            kwargs={},
        )

        assert len(MemoryTransport.events) == 1

    def test_disconnect_clears_transport(self) -> None:
        """disconnect_signals() stops event publishing."""
        transport = MemoryTransport()
        connect_signals(transport)
        disconnect_signals()

        # Events after disconnect should be dropped
        task = MagicMock()
        task.name = "tests.task"
        task.request.parent_id = None
        task.request.root_id = None
        task.request.group = None
        task.request.chord = None
        task.request.retries = 0

        _on_task_prerun(
            task_id="test-id",
            task=task,
            args=(),
            kwargs={},
        )

        # Event not published (logged warning instead)
        assert len(MemoryTransport.events) == 0


class TestTaskPrerun:
    """Tests for task_prerun signal handler."""

    def test_emits_started_event(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_prerun emits STARTED event."""
        _on_task_prerun(
            task_id="task-123",
            task=mock_task,
            args=("arg1",),
            kwargs={"key": "value"},
        )

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.task_id == "task-123"
        assert event.name == "tests.sample_task"
        assert event.state == TaskState.STARTED
        assert event.parent_id is None
        assert event.root_id is None

    def test_captures_args_and_kwargs(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_prerun captures args and kwargs."""
        _on_task_prerun(
            task_id="task-123",
            task=mock_task,
            args=("arg1", 42),
            kwargs={"key": "value", "count": 10},
        )

        event = MemoryTransport.events[0]
        assert event.args == ["arg1", 42]
        assert event.kwargs == {"key": "value", "count": 10}

    def test_scrubs_sensitive_kwargs(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_prerun scrubs sensitive data in kwargs."""
        _on_task_prerun(
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={"username": "alice", "password": "secret123"},
        )

        event = MemoryTransport.events[0]
        assert event.kwargs is not None
        assert event.kwargs["username"] == "alice"
        assert event.kwargs["password"] == "[Filtered]"

    def test_captures_parent_and_root(
        self,
        transport: MemoryTransport,
        mock_task_with_parent: MagicMock,
    ) -> None:
        """task_prerun captures parent_id and root_id."""
        _on_task_prerun(
            task_id="task-456",
            task=mock_task_with_parent,
            args=(),
            kwargs={},
        )

        event = MemoryTransport.events[0]
        assert event.parent_id == "task-123"
        assert event.root_id == "task-001"

    def test_captures_retry_count(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_prerun captures current retry count."""
        mock_task.request.retries = 2

        _on_task_prerun(
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={},
        )

        event = MemoryTransport.events[0]
        assert event.retries == 2


class TestTaskPostrun:
    """Tests for task_postrun signal handler."""

    def test_emits_success_event_on_success(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_postrun emits SUCCESS for successful tasks."""
        _on_task_postrun(
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={},
            retval={"result": "data"},
            state="SUCCESS",
        )

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.state == TaskState.SUCCESS

    def test_captures_result(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_postrun captures the return value."""
        _on_task_postrun(
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={},
            retval={"sum": 42, "count": 3},
            state="SUCCESS",
        )

        event = MemoryTransport.events[0]
        assert event.result == {"sum": 42, "count": 3}

    def test_ignores_failure_state(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_postrun doesn't emit for FAILURE (handled by task_failure)."""
        _on_task_postrun(
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={},
            retval=None,
            state="FAILURE",
        )

        assert len(MemoryTransport.events) == 0

    def test_ignores_retry_state(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_postrun doesn't emit for RETRY (handled by task_retry)."""
        _on_task_postrun(
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={},
            retval=None,
            state="RETRY",
        )

        assert len(MemoryTransport.events) == 0


class TestTaskFailure:
    """Tests for task_failure signal handler."""

    def test_emits_failure_event(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_failure emits FAILURE event."""
        exception = ValueError("Something went wrong")

        _on_task_failure(
            task_id="task-123",
            exception=exception,
            args=(),
            kwargs={},
            traceback=None,
            einfo=None,
            sender=mock_task,
        )

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.task_id == "task-123"
        assert event.state == TaskState.FAILURE

    def test_captures_exception_message(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_failure captures the exception message."""
        exception = ConnectionError("Connection refused")

        _on_task_failure(
            task_id="task-123",
            exception=exception,
            args=(),
            kwargs={},
            traceback=None,
            einfo=None,
            sender=mock_task,
        )

        event = MemoryTransport.events[0]
        assert event.exception == "ConnectionError: Connection refused"


class TestTaskRetry:
    """Tests for task_retry signal handler."""

    def test_emits_retry_event(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_retry emits RETRY event."""
        request = MagicMock()
        request.id = "task-123"
        request.parent_id = None
        request.root_id = None
        request.group = None
        request.chord = None
        request.retries = 1

        _on_task_retry(
            sender=mock_task,
            request=request,
            reason="Connection timeout",
            einfo=None,
        )

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.state == TaskState.RETRY
        assert event.retries == 1  # Same as the attempt that failed

    def test_captures_exception_reason(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_retry captures exception when reason is an exception."""
        request = MagicMock()
        request.id = "task-123"
        request.parent_id = None
        request.root_id = None
        request.group = None
        request.chord = None
        request.retries = 0

        reason = TimeoutError("Request timed out")

        _on_task_retry(
            sender=mock_task,
            request=request,
            reason=reason,
            einfo=None,
        )

        event = MemoryTransport.events[0]
        assert event.exception == "TimeoutError: Request timed out"

    def test_captures_string_reason(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_retry captures string reason."""
        request = MagicMock()
        request.id = "task-123"
        request.parent_id = None
        request.root_id = None
        request.group = None
        request.chord = None
        request.retries = 0

        _on_task_retry(
            sender=mock_task,
            request=request,
            reason="Max retries exceeded",
            einfo=None,
        )

        event = MemoryTransport.events[0]
        assert event.exception == "Max retries exceeded"


class TestTaskRevoked:
    """Tests for task_revoked signal handler."""

    def test_emits_revoked_event(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_revoked emits REVOKED event."""
        request = MagicMock()
        request.id = "task-123"
        request.parent_id = None
        request.root_id = None
        request.group = None
        request.chord = None
        request.retries = 0

        _on_task_revoked(
            request=request,
            terminated=True,
            signum=15,
            expired=False,
            sender=mock_task,
        )

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.state == TaskState.REVOKED


class TestTaskSent:
    """Tests for task_sent signal handler (PENDING state)."""

    def test_emits_pending_event(
        self,
        transport: MemoryTransport,
    ) -> None:
        """task_sent emits PENDING event."""
        _on_task_sent(
            sender="tests.sample_task",
            task_id="task-123",
            task="tests.sample_task",
            args=(),
            kwargs={},
        )

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.task_id == "task-123"
        assert event.name == "tests.sample_task"
        assert event.state == TaskState.PENDING

    def test_captures_args_and_kwargs(
        self,
        transport: MemoryTransport,
    ) -> None:
        """task_sent captures args and kwargs."""
        _on_task_sent(
            sender="tests.sample_task",
            task_id="task-123",
            task="tests.sample_task",
            args=("hello", 42),
            kwargs={"key": "value"},
        )

        event = MemoryTransport.events[0]
        assert event.args == ["hello", 42]
        assert event.kwargs == {"key": "value"}

    def test_scrubs_sensitive_kwargs(
        self,
        transport: MemoryTransport,
    ) -> None:
        """task_sent scrubs sensitive data in kwargs."""
        _on_task_sent(
            sender="tests.sample_task",
            task_id="task-123",
            task="tests.sample_task",
            args=(),
            kwargs={"password": "secret123", "username": "alice"},
        )

        event = MemoryTransport.events[0]
        assert event.kwargs is not None
        assert event.kwargs["password"] == "[Filtered]"
        assert event.kwargs["username"] == "alice"

    def test_handles_missing_task_id(
        self,
        transport: MemoryTransport,
    ) -> None:
        """task_sent ignores calls without task_id."""
        _on_task_sent(
            sender="tests.sample_task",
            task_id=None,
            task="tests.sample_task",
            args=(),
            kwargs={},
        )

        assert len(MemoryTransport.events) == 0

    def test_uses_sender_as_fallback_name(
        self,
        transport: MemoryTransport,
    ) -> None:
        """task_sent uses sender if task is None."""
        _on_task_sent(
            sender="tests.fallback_task",
            task_id="task-123",
            task=None,
            args=(),
            kwargs={},
        )

        event = MemoryTransport.events[0]
        assert event.name == "tests.fallback_task"


class TestFireAndForget:
    """Tests for fire-and-forget behavior."""

    def test_publish_error_is_logged_not_raised(
        self,
        mock_task: MagicMock,
        caplog: Any,
    ) -> None:
        """Transport errors are logged, not raised."""
        # Create a transport that raises on publish
        broken_transport = MagicMock()
        broken_transport.publish.side_effect = RuntimeError("Connection failed")
        connect_signals(broken_transport)

        # This should not raise
        _on_task_prerun(
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={},
        )

        # Error should be logged
        assert "Failed to publish event" in caplog.text


class TestExtractChordInfo:
    """Tests for _extract_chord_info helper function.

    This function parses Celery's chord attribute to extract:
    - group_id: The ID shared by header tasks
    - callback_id: The task ID of the callback task
    """

    def test_none_input_returns_none(self) -> None:
        """None chord attribute returns (None, None)."""
        group_id, callback_id = _extract_chord_info(None)
        assert group_id is None
        assert callback_id is None

    def test_dict_with_options(self) -> None:
        """Dict chord attribute with options dict is parsed correctly."""
        chord_dict = {
            "options": {
                "group_id": "group-abc-123",
                "task_id": "callback-task-456",
            }
        }
        group_id, callback_id = _extract_chord_info(chord_dict)
        assert group_id == "group-abc-123"
        assert callback_id == "callback-task-456"

    def test_dict_with_group_key_fallback(self) -> None:
        """Dict with 'group' key in options (alternative key name)."""
        chord_dict = {
            "options": {
                "group": "group-abc-123",
                "task_id": "callback-task-456",
            }
        }
        group_id, callback_id = _extract_chord_info(chord_dict)
        assert group_id == "group-abc-123"
        assert callback_id == "callback-task-456"

    def test_signature_object_with_options(self) -> None:
        """Celery Signature object with options attribute is parsed correctly."""
        mock_signature = MagicMock()
        mock_signature.options = {
            "group_id": "group-xyz-789",
            "task_id": "callback-task-abc",
        }

        group_id, callback_id = _extract_chord_info(mock_signature)
        assert group_id == "group-xyz-789"
        assert callback_id == "callback-task-abc"

    def test_empty_options_dict(self) -> None:
        """Empty options dict returns (None, None)."""
        chord_dict: dict[str, dict[str, str]] = {"options": {}}
        group_id, callback_id = _extract_chord_info(chord_dict)
        assert group_id is None
        assert callback_id is None

    def test_missing_options_key(self) -> None:
        """Dict without options key returns (None, None)."""
        chord_dict: dict[str, str] = {"task": "some.task"}
        group_id, callback_id = _extract_chord_info(chord_dict)
        assert group_id is None
        assert callback_id is None

    def test_options_not_dict(self) -> None:
        """Non-dict options value returns (None, None)."""
        chord_dict = {"options": "not-a-dict"}
        group_id, callback_id = _extract_chord_info(chord_dict)
        assert group_id is None
        assert callback_id is None


class TestChordIdCapture:
    """Tests for chord_id and chord_callback_id capture in signal handlers."""

    def test_prerun_captures_chord_info(
        self,
        transport: MemoryTransport,
    ) -> None:
        """task_prerun captures chord_id and chord_callback_id from task.request.chord."""
        task = MagicMock()
        task.name = "tests.header_task"
        task.request.id = "header-task-1"
        task.request.parent_id = None
        task.request.root_id = None
        task.request.group = "chord-group-id"
        task.request.chord = {
            "options": {
                "group_id": "chord-group-id",
                "task_id": "callback-task-id",
            }
        }
        task.request.retries = 0

        _on_task_prerun(
            task_id="header-task-1",
            task=task,
            args=(),
            kwargs={},
        )

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.group_id == "chord-group-id"
        assert event.chord_id == "chord-group-id"
        assert event.chord_callback_id == "callback-task-id"

    def test_prerun_without_chord(
        self,
        transport: MemoryTransport,
        mock_task: MagicMock,
    ) -> None:
        """task_prerun without chord info has None for chord fields."""
        _on_task_prerun(
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={},
        )

        event = MemoryTransport.events[0]
        assert event.chord_id is None
        assert event.chord_callback_id is None
