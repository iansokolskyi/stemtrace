"""Shared pytest fixtures."""

from datetime import UTC, datetime

import pytest

from stemtrace.core.events import TaskEvent, TaskState


@pytest.fixture
def sample_event() -> TaskEvent:
    """Create a sample task event for testing."""
    return TaskEvent(
        task_id="test-123",
        name="tests.sample_task",
        state=TaskState.PENDING,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
