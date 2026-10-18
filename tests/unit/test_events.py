"""Tests for core event models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType


class TestTaskState:
    def test_has_expected_values(self) -> None:
        assert TaskState.PENDING.value == "PENDING"
        assert TaskState.SUCCESS.value == "SUCCESS"
        assert TaskState.FAILURE.value == "FAILURE"

    def test_all_celery_states_defined(self) -> None:
        expected_states = {
            "PENDING",
            "RECEIVED",
            "STARTED",
            "SUCCESS",
            "FAILURE",
            "REVOKED",
            "REJECTED",
            "RETRY",
            "IGNORED",
        }
        actual_states = {s.value for s in TaskState}
        assert actual_states == expected_states

    def test_string_comparison(self) -> None:
        assert TaskState.SUCCESS == "SUCCESS"
        assert TaskState.FAILURE == "FAILURE"
        assert TaskState.PENDING != "STARTED"

    def test_string_in_collections(self) -> None:
        terminal_states = {"SUCCESS", "FAILURE", "REVOKED", "REJECTED"}
        assert TaskState.SUCCESS in terminal_states
        assert TaskState.STARTED not in terminal_states


class TestTaskEventCreation:
    def test_with_required_fields(self) -> None:
        event = TaskEvent(
            task_id="abc-123",
            name="myapp.tasks.send_email",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert event.task_id == "abc-123"
        assert event.name == "myapp.tasks.send_email"
        assert event.state == TaskState.STARTED
        assert event.parent_id is None
        assert event.root_id is None
        assert event.trace_id is None
        assert event.retries == 0

    def test_with_all_fields(self) -> None:
        event = TaskEvent(
            task_id="child-456",
            name="myapp.tasks.subtask",
            state=TaskState.RETRY,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            parent_id="parent-123",
            root_id="root-001",
            trace_id="trace-abc-xyz",
            retries=3,
        )
        assert event.task_id == "child-456"
        assert event.parent_id == "parent-123"
        assert event.root_id == "root-001"
        assert event.trace_id == "trace-abc-xyz"
        assert event.retries == 3

    def test_validates_field_types(self) -> None:
        with pytest.raises(ValidationError):
            TaskEvent(
                task_id=123,  # type: ignore[arg-type]
                name="test",
                state=TaskState.PENDING,
                timestamp=datetime.now(timezone.utc),
            )

    def test_validates_retries_type(self) -> None:
        with pytest.raises(ValidationError):
            TaskEvent(
                task_id="abc-123",
                name="test",
                state=TaskState.PENDING,
                timestamp=datetime.now(timezone.utc),
                retries="five",  # type: ignore[arg-type]
            )

    def test_state_from_string(self) -> None:
        event = TaskEvent(
            task_id="abc-123",
            name="test",
            state="SUCCESS",  # type: ignore[arg-type]
            timestamp=datetime.now(timezone.utc),
        )
        assert event.state == TaskState.SUCCESS

    def test_invalid_state_raises(self) -> None:
        with pytest.raises(ValidationError):
            TaskEvent(
                task_id="abc-123",
                name="test",
                state="INVALID_STATE",  # type: ignore[arg-type]
                timestamp=datetime.now(timezone.utc),
            )


class TestTaskEventImmutability:
    def test_cannot_modify_task_id(self) -> None:
        event = TaskEvent(
            task_id="abc-123",
            name="myapp.tasks.send_email",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )
        with pytest.raises(ValidationError):
            event.task_id = "changed"  # type: ignore[misc]

    def test_cannot_modify_state(self) -> None:
        event = TaskEvent(
            task_id="abc-123",
            name="test",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )
        with pytest.raises(ValidationError):
            event.state = TaskState.SUCCESS  # type: ignore[misc]


class TestTaskEventEquality:
    def test_equal_events(self) -> None:
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        event1 = TaskEvent(
            task_id="abc-123",
            name="test",
            state=TaskState.STARTED,
            timestamp=ts,
        )
        event2 = TaskEvent(
            task_id="abc-123",
            name="test",
            state=TaskState.STARTED,
            timestamp=ts,
        )
        assert event1 == event2

    def test_different_task_id_not_equal(self) -> None:
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        event1 = TaskEvent(
            task_id="abc-123",
            name="test",
            state=TaskState.STARTED,
            timestamp=ts,
        )
        event2 = TaskEvent(
            task_id="xyz-789",
            name="test",
            state=TaskState.STARTED,
            timestamp=ts,
        )
        assert event1 != event2

    def test_hashable_for_sets(self) -> None:
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        event1 = TaskEvent(
            task_id="abc-123",
            name="test",
            state=TaskState.STARTED,
            timestamp=ts,
        )
        event2 = TaskEvent(
            task_id="abc-123",
            name="test",
            state=TaskState.STARTED,
            timestamp=ts,
        )
        event3 = TaskEvent(
            task_id="xyz-789",
            name="test",
            state=TaskState.STARTED,
            timestamp=ts,
        )
        event_set = {event1, event2, event3}
        assert len(event_set) == 2


class TestTaskEventSerialization:
    def test_output_defaults_to_none(self) -> None:
        """Events from workers without output capture validate unchanged."""
        event = TaskEvent.model_validate(
            {
                "task_id": "abc-123",
                "name": "myapp.tasks.send_email",
                "state": "SUCCESS",
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        assert event.output is None

    def test_output_roundtrips_through_json(self) -> None:
        """Captured output survives the wire format."""
        event = TaskEvent(
            task_id="abc-123",
            name="myapp.tasks.send_email",
            state=TaskState.FAILURE,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            output="hello\nWARNING myapp: careful",
        )
        restored = TaskEvent.model_validate_json(event.model_dump_json())
        assert restored.output == "hello\nWARNING myapp: careful"

    def test_unknown_fields_are_ignored(self) -> None:
        """Older servers ignore fields added by newer workers (like output)."""
        event = TaskEvent.model_validate(
            {
                "task_id": "abc-123",
                "name": "myapp.tasks.send_email",
                "state": "SUCCESS",
                "timestamp": "2024-01-01T12:00:00Z",
                "field_from_the_future": "x",
            }
        )
        assert event.task_id == "abc-123"
        assert not hasattr(event, "field_from_the_future")

    def test_to_dict(self) -> None:
        event = TaskEvent(
            task_id="abc-123",
            name="myapp.tasks.send_email",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        data = event.model_dump()
        assert data["task_id"] == "abc-123"
        assert data["state"] == TaskState.STARTED
        assert data["parent_id"] is None

    def test_to_json(self) -> None:
        event = TaskEvent(
            task_id="abc-123",
            name="myapp.tasks.send_email",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        json_str = event.model_dump_json()
        assert "abc-123" in json_str
        assert "STARTED" in json_str

    def test_from_dict(self) -> None:
        data = {
            "task_id": "abc-123",
            "name": "myapp.tasks.send_email",
            "state": "SUCCESS",
            "timestamp": "2024-01-01T12:00:00Z",
            "parent_id": "parent-001",
            "retries": 2,
        }
        event = TaskEvent.model_validate(data)
        assert event.task_id == "abc-123"
        assert event.state == TaskState.SUCCESS
        assert event.parent_id == "parent-001"
        assert event.retries == 2

    def test_roundtrip(self) -> None:
        original = TaskEvent(
            task_id="abc-123",
            name="myapp.tasks.send_email",
            state=TaskState.STARTED,
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            parent_id="parent-001",
            root_id="root-001",
            trace_id="trace-xyz",
            retries=1,
        )
        data = original.model_dump(mode="json")
        restored = TaskEvent.model_validate(data)
        assert restored == original


class TestWorkerEventType:
    """Tests for WorkerEventType enum values."""

    def test_has_expected_values(self) -> None:
        assert WorkerEventType.WORKER_READY.value == "worker_ready"
        assert WorkerEventType.WORKER_SHUTDOWN.value == "worker_shutdown"

    def test_string_comparison(self) -> None:
        assert WorkerEventType.WORKER_READY == "worker_ready"
        assert WorkerEventType.WORKER_SHUTDOWN == "worker_shutdown"


class TestWorkerEventCreation:
    """Tests for WorkerEvent model creation and validation."""

    def test_worker_ready_event(self) -> None:
        """Create worker_ready event with registered tasks."""
        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1.example.com",
            pid=12345,
            timestamp=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            registered_tasks=["myapp.tasks.add", "myapp.tasks.process"],
        )
        assert event.event_type == WorkerEventType.WORKER_READY
        assert event.hostname == "worker-1.example.com"
        assert event.pid == 12345
        assert len(event.registered_tasks) == 2
        assert event.shutdown_time is None

    def test_worker_shutdown_event(self) -> None:
        """Create worker_shutdown event with shutdown time."""
        shutdown_time = datetime(2024, 1, 1, 13, 30, 0, tzinfo=timezone.utc)
        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_SHUTDOWN,
            hostname="worker-1.example.com",
            pid=12345,
            timestamp=shutdown_time,
            shutdown_time=shutdown_time,
        )
        assert event.event_type == WorkerEventType.WORKER_SHUTDOWN
        assert event.shutdown_time == shutdown_time
        assert event.registered_tasks == []

    def test_required_fields(self) -> None:
        """All required fields must be present."""
        with pytest.raises(ValidationError):
            WorkerEvent(
                event_type=WorkerEventType.WORKER_READY,
                hostname="worker-1.example.com",
                # Missing required: pid, timestamp
            )

    def test_defaults_to_empty_list(self) -> None:
        """registered_tasks defaults to empty list."""
        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1.example.com",
            pid=12345,
            timestamp=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        assert event.registered_tasks == []

    def test_serialization(self) -> None:
        """WorkerEvent serializes to dict correctly."""
        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1.example.com",
            pid=12345,
            timestamp=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            registered_tasks=["myapp.tasks.send_email"],
        )
        data = event.model_dump()
        assert data["event_type"] == "worker_ready"
        assert data["hostname"] == "worker-1.example.com"
        assert data["pid"] == 12345
        assert data["registered_tasks"] == ["myapp.tasks.send_email"]
        assert "shutdown_time" not in data or data["shutdown_time"] is None

    def test_json_serialization(self) -> None:
        """WorkerEvent serializes to JSON correctly."""
        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1.example.com",
            pid=12345,
            timestamp=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            registered_tasks=["myapp.tasks.process"],
        )
        json_str = event.model_dump_json()
        assert "worker_ready" in json_str
        assert "worker-1.example.com" in json_str
        assert str(12345) in json_str

    def test_roundtrip(self) -> None:
        """WorkerEvent survives serialization roundtrip."""
        original = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-2.example.com",
            pid=54321,
            timestamp=datetime(2024, 2, 1, 12, 0, tzinfo=timezone.utc),
            registered_tasks=["myapp.tasks.analyze"],
        )
        data = original.model_dump(mode="json")
        restored = WorkerEvent.model_validate(data)
        assert restored == original
