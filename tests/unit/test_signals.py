"""Tests for Celery signal handlers."""

import logging
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from celery import Celery, group
from celery.signals import (
    after_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_revoked,
    task_sent,
)

from stemtrace.core.events import TaskState, WorkerEvent, WorkerEventType
from stemtrace.library.config import StemtraceConfig, set_config
from stemtrace.library.signals import (
    _MAX_DOCSTRING_CHARS,
    _extract_chord_info,
    _extract_registered_tasks,
    _extract_task_definitions,
    _format_exception,
    _format_traceback,
    _get_hostname_and_pid,
    _on_after_task_publish,
    _on_task_failure,
    _on_task_postrun,
    _on_task_prerun,
    _on_task_retry,
    _on_task_revoked,
    connect_signals,
    disconnect_signals,
    on_worker_ready,
    on_worker_shutdown,
)
from stemtrace.transports.memory import MemoryTransport


@pytest.fixture(autouse=True)
def clean_transport() -> None:
    """Clean up transport state before each test."""
    MemoryTransport.clear()
    disconnect_signals()


@pytest.fixture
def config() -> StemtraceConfig:
    """Set up default config for tests."""
    cfg = StemtraceConfig(transport_url="memory://")
    set_config(cfg)
    return cfg


@pytest.fixture
def transport(config: StemtraceConfig) -> MemoryTransport:
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


class TestWorkerLifecycleSignals:
    """Tests for worker_ready and worker_shutdown signal handlers."""

    def test_get_hostname_and_pid_from_sender(self) -> None:
        """Extract hostname from sender and pid from OS."""
        sender = MagicMock()
        sender.hostname = "worker-1.example.com"

        hostname, pid = _get_hostname_and_pid(sender)
        assert hostname == "worker-1.example.com"
        assert pid == os.getpid()

    def test_extract_registered_tasks_filters_celery_internal(self) -> None:
        """Exclude celery.* tasks from registered task list."""
        sender = MagicMock()
        sender.app.tasks = {
            "tasks.add": MagicMock(),
            "celery.backend_cleanup": MagicMock(),
            "tasks.multiply": MagicMock(),
        }

        tasks = _extract_registered_tasks(sender)
        assert "tasks.add" in tasks
        assert "tasks.multiply" in tasks
        assert all(not t.startswith("celery.") for t in tasks)

    def test_on_worker_ready_publishes_worker_event(
        self, transport: MemoryTransport
    ) -> None:
        """worker_ready publishes WorkerEvent with registered tasks."""
        sender = MagicMock()
        sender.hostname = "worker-1"
        sender.app.tasks = {
            "tasks.add": MagicMock(),
            "celery.backend_cleanup": MagicMock(),
            "tasks.multiply": MagicMock(),
        }

        on_worker_ready(sender=sender)

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert isinstance(event, WorkerEvent)
        assert event.event_type == WorkerEventType.WORKER_READY
        assert event.hostname == "worker-1"
        assert event.pid == os.getpid()
        assert event.shutdown_time is None
        assert sorted(event.registered_tasks) == ["tasks.add", "tasks.multiply"]

    def test_on_worker_shutdown_publishes_worker_event(
        self, transport: MemoryTransport
    ) -> None:
        """worker_shutdown publishes WorkerEvent with shutdown_time."""
        sender = MagicMock()
        sender.hostname = "worker-1"
        sender.app.tasks = {}

        on_worker_shutdown(sender=sender, sig=15)

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert isinstance(event, WorkerEvent)
        assert event.event_type == WorkerEventType.WORKER_SHUTDOWN
        assert event.hostname == "worker-1"
        assert event.pid == os.getpid()
        assert event.shutdown_time is not None

    def test_worker_lifecycle_logs_handoff_not_delivery(
        self, transport: MemoryTransport, caplog: Any
    ) -> None:
        """Lifecycle logs say the event went to the transport, which may drop it."""
        caplog.set_level(logging.INFO, logger="stemtrace.library.signals")
        sender = MagicMock()
        sender.hostname = "worker-1"
        sender.app.tasks = {"tasks.add": MagicMock()}

        on_worker_ready(sender=sender)
        on_worker_shutdown(sender=sender, sig=15)

        pid = os.getpid()
        assert f"Worker ready event sent to transport: worker-1:{pid} (1 tasks)" in (
            caplog.text
        )
        assert f"Worker shutdown event sent to transport: worker-1:{pid}" in (
            caplog.text
        )
        assert "event published" not in caplog.text

    def test_worker_lifecycle_does_not_raise_without_transport(self) -> None:
        """Handlers should never raise if stemtrace not initialized."""
        disconnect_signals()
        sender = MagicMock()
        sender.hostname = "worker-1"
        sender.app.tasks = {"tasks.add": MagicMock()}

        on_worker_ready(sender=sender)
        on_worker_shutdown(sender=sender, sig=15)

        assert len(MemoryTransport.events) == 0

    def test_extract_task_definitions_empty_without_app(self) -> None:
        """No sender.app returns empty task definitions mapping."""
        sender = MagicMock()
        sender.app = None
        assert _extract_task_definitions(sender) == {}

    def test_extract_task_definitions_empty_without_tasks_registry(self) -> None:
        """No app.tasks returns empty task definitions mapping."""
        sender = SimpleNamespace(app=SimpleNamespace(tasks=None))
        assert _extract_task_definitions(sender) == {}

    def test_extract_task_definitions_truncates_docstring(self) -> None:
        """Docstrings longer than the limit are truncated with a marker."""

        def run_impl() -> None:
            pass

        run_impl.__doc__ = "x" * (_MAX_DOCSTRING_CHARS + 50)

        class TaskObj:
            run = staticmethod(run_impl)
            bind = True

        sender = SimpleNamespace(app=SimpleNamespace(tasks={"my.task": TaskObj()}))
        defs = _extract_task_definitions(sender)
        assert "my.task" in defs
        assert defs["my.task"].bound is True
        assert defs["my.task"].docstring is not None
        assert defs["my.task"].docstring.endswith("[truncated]")
        assert len(defs["my.task"].docstring) > _MAX_DOCSTRING_CHARS

    def test_extract_task_definitions_signature_best_effort(self) -> None:
        """Non-callable run attribute yields signature=None without raising."""

        class TaskObj:
            run = 123  # inspect.signature will raise TypeError
            bind = False

        sender = SimpleNamespace(app=SimpleNamespace(tasks={"my.bad": TaskObj()}))
        defs = _extract_task_definitions(sender)
        assert defs["my.bad"].signature is None

    def test_extract_task_definitions_module_fallback_and_bind_guard(self) -> None:
        """Fallback module uses task_obj.__module__ and bind guard ignores non-bool."""

        class TaskObj:
            __module__ = "my.module"
            run = None
            bind = MagicMock()

        sender = SimpleNamespace(app=SimpleNamespace(tasks={"my.task": TaskObj()}))
        defs = _extract_task_definitions(sender)
        assert defs["my.task"].module == "my.module"
        assert defs["my.task"].bound is False


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


def _publish_headers(**overrides: Any) -> dict[str, Any]:
    """Message headers as Celery 5's protocol 2 builds them."""
    headers: dict[str, Any] = {
        "lang": "py",
        "task": "tests.sample_task",
        "id": "task-123",
        "eta": None,
        "retries": 0,
        "group": None,
        "parent_id": None,
        "root_id": "task-123",
    }
    headers.update(overrides)
    return headers


class TestAfterTaskPublish:
    """after_task_publish handler (the sender's PENDING event)."""

    def test_emits_pending_with_parent_root_and_group(
        self, transport: MemoryTransport
    ) -> None:
        _on_after_task_publish(
            sender="tests.sample_task",
            body=((1, 2), {"key": "value"}, {}),
            headers=_publish_headers(
                group="group-1", parent_id="parent-1", root_id="root-1"
            ),
            exchange="",
            routing_key="celery",
        )

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.task_id == "task-123"
        assert event.name == "tests.sample_task"
        assert event.state == TaskState.PENDING
        assert event.parent_id == "parent-1"
        assert event.root_id == "root-1"
        assert event.group_id == "group-1"
        assert event.args == [1, 2]
        assert event.kwargs == {"key": "value"}

    def test_protocol_1_body_gives_id_name_args_and_group(
        self, transport: MemoryTransport
    ) -> None:
        _on_after_task_publish(
            sender="tests.sample_task",
            body={
                "id": "task-9",
                "task": "tests.other_task",
                "args": [3],
                "kwargs": {},
                "taskset": "group-9",
                "retries": 0,
            },
            headers={},
        )

        event = MemoryTransport.events[0]
        assert (event.task_id, event.name, event.group_id) == (
            "task-9",
            "tests.other_task",
            "group-9",
        )
        assert event.args == [3]
        assert event.parent_id is None

    def test_scrubs_sensitive_kwargs(self, transport: MemoryTransport) -> None:
        _on_after_task_publish(
            sender="tests.sample_task",
            body=((), {"password": "secret123", "username": "alice"}, {}),
            headers=_publish_headers(),
        )

        event = MemoryTransport.events[0]
        assert event.kwargs is not None
        assert event.kwargs["password"] == "[Filtered]"
        assert event.kwargs["username"] == "alice"

    def test_ignores_message_without_task_id(self, transport: MemoryTransport) -> None:
        _on_after_task_publish(
            sender="tests.sample_task", body=((), {}, {}), headers={}
        )

        assert MemoryTransport.events == []

    def test_uses_sender_when_headers_have_no_task_name(
        self, transport: MemoryTransport
    ) -> None:
        _on_after_task_publish(
            sender="tests.fallback_task",
            body=((), {}, {}),
            headers=_publish_headers(task=None),
        )

        assert MemoryTransport.events[0].name == "tests.fallback_task"

    def test_skips_retry_and_later_emits_first_send(
        self, transport: MemoryTransport
    ) -> None:
        _on_after_task_publish(
            sender="tests.sample_task",
            body=((), {}, {}),
            headers=_publish_headers(retries=1),
        )
        assert MemoryTransport.events == []

        # The retry skip must not mark the task as already announced.
        _on_after_task_publish(
            sender="tests.sample_task", body=((), {}, {}), headers=_publish_headers()
        )
        assert len(MemoryTransport.events) == 1

    def test_deduplicates_pending_for_same_task_id(
        self, transport: MemoryTransport
    ) -> None:
        for _ in range(2):
            _on_after_task_publish(
                sender="tests.sample_task",
                body=((), {}, {}),
                headers=_publish_headers(),
            )

        assert len(MemoryTransport.events) == 1

    def test_never_raises_on_malformed_message(
        self, transport: MemoryTransport
    ) -> None:
        _on_after_task_publish(sender="tests.sample_task", body=object(), headers=None)

        assert MemoryTransport.events == []


class TestRealPublish:
    """PENDING events from real publishes through a memory broker."""

    @pytest.fixture
    def app(self, transport: MemoryTransport) -> Celery:
        app = Celery("publish-tests", broker="memory://", backend="cache+memory://")

        @app.task(name="tests.add")
        def add(x: int, y: int) -> int:
            return x + y

        return app

    def test_send_task_pending_carries_parent_and_root(self, app: Celery) -> None:
        app.send_task("tests.add", args=(1, 2), parent_id="parent-1", root_id="root-1")

        [event] = MemoryTransport.events
        assert event.state == TaskState.PENDING
        assert (event.parent_id, event.root_id) == ("parent-1", "root-1")
        assert event.args == [1, 2]

    def test_group_apply_async_pending_carries_group_id(self, app: Celery) -> None:
        add = app.tasks["tests.add"]
        result = group(add.s(1, 1), add.s(2, 2)).apply_async()

        assert {e.group_id for e in MemoryTransport.events} == {result.id}
        assert len(MemoryTransport.events) == 2

    def test_eager_apply_publishes_no_pending(self, app: Celery) -> None:
        app.tasks["tests.add"].apply(args=(1, 2))

        assert [e for e in MemoryTransport.events if e.state == TaskState.PENDING] == []


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


class TestSignalsScrubbingAndCaptureBranches:
    """Extra tests to cover rarely-hit configuration branches."""

    def test_task_prerun_does_not_scrub_when_scrubbing_disabled(self) -> None:
        """When scrub_sensitive_data=False, sensitive values should not be filtered."""
        set_config(
            StemtraceConfig(transport_url="memory://", scrub_sensitive_data=False)
        )
        connect_signals(MemoryTransport())

        task = MagicMock()
        task.name = "tests.sample_task"
        task.request.parent_id = None
        task.request.root_id = None
        task.request.group = None
        task.request.chord = None
        task.request.retries = 0

        _on_task_prerun(
            task_id="task-123",
            task=task,
            args=(),
            kwargs={"password": "secret123"},
        )

        event = MemoryTransport.events[0]
        assert event.kwargs is not None
        assert event.kwargs["password"] == "secret123"

    def test_task_prerun_does_not_capture_args_when_capture_args_disabled(self) -> None:
        """When capture_args=False, args/kwargs should be omitted."""
        set_config(StemtraceConfig(transport_url="memory://", capture_args=False))
        connect_signals(MemoryTransport())

        task = MagicMock()
        task.name = "tests.sample_task"
        task.request.parent_id = None
        task.request.root_id = None
        task.request.group = None
        task.request.chord = None
        task.request.retries = 0

        _on_task_prerun(
            task_id="task-123",
            task=task,
            args=("hello", 1),
            kwargs={"key": "value"},
        )

        event = MemoryTransport.events[0]
        assert event.args is None
        assert event.kwargs is None

    def test_task_prerun_wraps_truncation_message_for_kwargs(self) -> None:
        """When kwargs exceed max size, kwargs should be wrapped in _truncated dict."""
        set_config(
            StemtraceConfig(
                transport_url="memory://",
                max_data_size=10,
                scrub_sensitive_data=True,
            )
        )
        connect_signals(MemoryTransport())

        task = MagicMock()
        task.name = "tests.sample_task"
        task.request.parent_id = None
        task.request.root_id = None
        task.request.group = None
        task.request.chord = None
        task.request.retries = 0

        _on_task_prerun(
            task_id="task-123",
            task=task,
            args=(),
            kwargs={"big": "x" * 1000},
        )

        event = MemoryTransport.events[0]
        assert event.kwargs is not None
        assert "_truncated" in event.kwargs

    def test_task_postrun_omits_result_when_capture_result_disabled(self) -> None:
        """When capture_result=False, SUCCESS events should have result=None."""
        set_config(StemtraceConfig(transport_url="memory://", capture_result=False))
        connect_signals(MemoryTransport())

        task = MagicMock()
        task.name = "tests.sample_task"
        task.request.parent_id = None
        task.request.root_id = None
        task.request.group = None
        task.request.chord = None
        task.request.retries = 0

        _on_task_postrun(
            task_id="task-123",
            task=task,
            args=(),
            kwargs={},
            retval={"hello": "world"},
            state="SUCCESS",
        )

        event = MemoryTransport.events[0]
        assert event.state == TaskState.SUCCESS
        assert event.result is None


class TestTaskRetryBranches:
    def test_task_retry_with_none_reason_uses_cancelled_message(self) -> None:
        """If task_retry has no reason and no einfo, use Celery's cancel reason."""
        set_config(StemtraceConfig(transport_url="memory://"))
        connect_signals(MemoryTransport())

        sender = MagicMock()
        sender.name = "tests.sample_task"

        request = MagicMock()
        request.id = "task-123"
        request.parent_id = None
        request.root_id = None
        request.group = None
        request.chord = None
        request.retries = 0

        _on_task_retry(sender=sender, request=request, reason=None, einfo=None)

        event = MemoryTransport.events[0]
        assert event.state == TaskState.RETRY
        assert event.exception == "cancelled by Celery"
        assert event.traceback is None

    def test_task_retry_signal_without_reason_publishes_retry(
        self, transport: MemoryTransport, mock_task: MagicMock
    ) -> None:
        """Celery's cancel path sends task_retry without `reason`."""
        request = SimpleNamespace(
            id="task-123",
            parent_id=None,
            root_id=None,
            group=None,
            chord=None,
            retries=2,
        )

        # Mirrors celery.worker.request.Request._announce_cancelled.
        task_retry.send(mock_task, request=request, einfo=None)

        assert len(MemoryTransport.events) == 1
        event = MemoryTransport.events[0]
        assert event.state == TaskState.RETRY
        assert event.task_id == "task-123"
        assert event.retries == 2
        assert event.exception == "cancelled by Celery"


class TestFailureTracebackFormatting:
    def test_task_failure_uses_einfo_traceback_string(self) -> None:
        """If einfo.traceback exists, it should be used verbatim."""
        set_config(StemtraceConfig(transport_url="memory://"))
        connect_signals(MemoryTransport())

        sender = MagicMock()
        sender.name = "tests.sample_task"
        sender.request.parent_id = None
        sender.request.root_id = None
        sender.request.group = None
        sender.request.chord = None
        sender.request.retries = 0

        einfo = SimpleNamespace(traceback="traceback-string")
        _on_task_failure(
            task_id="task-123",
            exception=ValueError("boom"),
            args=(),
            kwargs={},
            traceback=None,
            einfo=einfo,
            sender=sender,
        )

        event = MemoryTransport.events[0]
        assert event.traceback == "traceback-string"

    def test_task_failure_formats_tb_object(self) -> None:
        """If einfo.tb exists, it should be formatted into a string."""
        set_config(StemtraceConfig(transport_url="memory://"))
        connect_signals(MemoryTransport())

        sender = MagicMock()
        sender.name = "tests.sample_task"
        sender.request.parent_id = None
        sender.request.root_id = None
        sender.request.group = None
        sender.request.chord = None
        sender.request.retries = 0

        try:
            raise ValueError("boom")
        except ValueError as exc:
            tb = exc.__traceback__

        einfo = SimpleNamespace(tb=tb)
        _on_task_failure(
            task_id="task-123",
            exception=ValueError("boom"),
            args=(),
            kwargs={},
            traceback=None,
            einfo=einfo,
            sender=sender,
        )

        event = MemoryTransport.events[0]
        assert event.traceback is not None
        assert len(event.traceback) > 0


class TestFormattingHelpers:
    """Cover formatting helper edge cases."""

    def test_format_exception_uses_einfo_exception(self) -> None:
        """When exc is None and einfo.exception exists, it should be stringified."""
        einfo = SimpleNamespace(exception=ValueError("boom"))
        assert _format_exception(None, einfo=einfo) == "boom"

    def test_format_traceback_returns_none_when_no_traceback_info(self) -> None:
        """If einfo has no known traceback attributes, _format_traceback returns None."""
        assert _format_traceback(SimpleNamespace()) is None


class TestWorkerLifecycleEdgeCases:
    """Cover worker lifecycle error-handling branches."""

    def test_get_hostname_and_pid_falls_back_when_missing_hostname(self) -> None:
        """Missing hostname should return 'unknown-host' and never raise."""
        sender = SimpleNamespace()  # No hostname attribute
        hostname, pid = _get_hostname_and_pid(sender)
        assert hostname == "unknown-host"
        assert pid == os.getpid()

    def test_extract_registered_tasks_returns_empty_when_sender_has_no_app(
        self,
    ) -> None:
        """If sender.app is missing, registered tasks should be empty."""
        sender = SimpleNamespace()  # No app attribute
        assert _extract_registered_tasks(sender) == []

    def test_extract_registered_tasks_returns_empty_when_app_has_no_tasks(self) -> None:
        """If app.tasks is missing/None, registered tasks should be empty."""
        sender = SimpleNamespace(app=SimpleNamespace(tasks=None))
        assert _extract_registered_tasks(sender) == []

    def test_on_worker_ready_logs_and_suppresses_publish_errors(
        self, caplog: Any
    ) -> None:
        """Errors publishing worker_ready should be caught and logged."""
        broken_transport = MagicMock()
        broken_transport.publish.side_effect = RuntimeError("publish failed")
        connect_signals(broken_transport)

        sender = SimpleNamespace(
            hostname="worker-1",
            app=SimpleNamespace(tasks={"tasks.add": MagicMock()}),
        )

        on_worker_ready(sender=sender)
        assert "Failed to publish worker_ready event" in caplog.text

    def test_on_worker_shutdown_logs_and_suppresses_publish_errors(
        self, caplog: Any
    ) -> None:
        """Errors publishing worker_shutdown should be caught and logged."""
        broken_transport = MagicMock()
        broken_transport.publish.side_effect = RuntimeError("publish failed")
        connect_signals(broken_transport)

        sender = SimpleNamespace(hostname="worker-1", app=SimpleNamespace(tasks={}))

        on_worker_shutdown(sender=sender, sig=15)
        assert "Failed to publish worker_shutdown event" in caplog.text


def _receiver_errors(responses: list[tuple[Any, Any]]) -> list[Any]:
    """Exceptions Celery's Signal.send caught from receivers (it logs, not raises)."""
    return [response for _, response in responses if isinstance(response, Exception)]


class TestCelerySignalDispatch:
    """Receivers called the way Celery 5.6 sends each signal (keywords only)."""

    def test_after_task_publish_send_publishes_pending(
        self, transport: MemoryTransport
    ) -> None:
        # Mirrors celery.app.amqp.AMQP.send_task_message (protocol 2).
        responses = after_task_publish.send(
            sender="tests.sample_task",
            body=((1,), {}, {}),
            headers=_publish_headers(),
            exchange="",
            routing_key="celery",
        )

        assert _receiver_errors(responses) == []
        assert [e.state for e in MemoryTransport.events] == [TaskState.PENDING]

    def test_task_sent_send_publishes_nothing(self, transport: MemoryTransport) -> None:
        # Deprecated and sent after after_task_publish for the same message:
        # handling it too would announce the task twice.
        responses = task_sent.send(
            sender="tests.sample_task",
            task_id="task-123",
            task="tests.sample_task",
            args=(1,),
            kwargs={},
            eta=None,
            taskset=None,
        )

        assert _receiver_errors(responses) == []
        assert MemoryTransport.events == []

    def test_task_lifecycle_send_publishes_started_and_success(
        self, transport: MemoryTransport, mock_task: MagicMock
    ) -> None:
        # Mirrors celery.app.trace.build_tracer.
        responses = task_prerun.send(
            sender=mock_task, task_id="task-123", task=mock_task, args=(), kwargs={}
        )
        responses += task_postrun.send(
            sender=mock_task,
            task_id="task-123",
            task=mock_task,
            args=(),
            kwargs={},
            retval=7,
            state="SUCCESS",
        )

        assert _receiver_errors(responses) == []
        assert [e.state for e in MemoryTransport.events] == [
            TaskState.STARTED,
            TaskState.SUCCESS,
        ]

    def test_task_failure_send_publishes_failure(
        self, transport: MemoryTransport, mock_task: MagicMock
    ) -> None:
        exc = ValueError("boom")
        # Mirrors celery.app.trace.TraceInfo.handle_failure.
        responses = task_failure.send(
            sender=mock_task,
            task_id="task-123",
            exception=exc,
            args=(),
            kwargs={},
            traceback=None,
            einfo=None,
        )

        assert _receiver_errors(responses) == []
        assert [e.state for e in MemoryTransport.events] == [TaskState.FAILURE]
        assert MemoryTransport.events[0].exception == "ValueError: boom"

    def test_task_revoked_send_publishes_revoked(
        self, transport: MemoryTransport, mock_task: MagicMock
    ) -> None:
        # Mirrors celery.worker.request.Request.task_revoked (via send_revoked).
        responses = task_revoked.send(
            mock_task,
            request=mock_task.request,
            terminated=False,
            signum=None,
            expired=False,
        )

        assert _receiver_errors(responses) == []
        assert [e.state for e in MemoryTransport.events] == [TaskState.REVOKED]
