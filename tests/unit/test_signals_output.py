"""Tests for attaching captured task output to terminal events.

The first group drives the signal handlers directly with a fake task, in the
order Celery's tracer sends them (``celery/app/trace.py``: ``task_failure`` and
``task_retry`` fire inside the error handler, *before* ``task_postrun``). The
second group runs a real Celery app eagerly (``task.apply()``), which goes
through the same tracer and signals as a worker.
"""

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery
from celery.utils.log import get_task_logger

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library import signals
from stemtrace.library.config import StemtraceConfig, set_config
from stemtrace.library.output_capture import (
    get_active_buffer,
    is_installed,
    stop_capture,
    uninstall_capture,
)
from stemtrace.library.signals import (
    _on_task_failure,
    _on_task_postrun,
    _on_task_prerun,
    _on_task_retry,
    connect_signals,
    disconnect_signals,
)
from stemtrace.library.transports.memory import MemoryTransport

module_logger = logging.getLogger("tests.unit.output_tasks")
task_logger = get_task_logger("tests.unit.output_tasks.celery")


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    """Reset transport, signals, capture install and config around each test."""
    MemoryTransport.clear()
    disconnect_signals()
    uninstall_capture()
    yield
    while (buffer := get_active_buffer()) is not None:
        stop_capture(buffer.task_id)
    uninstall_capture()
    disconnect_signals()
    MemoryTransport.clear()
    set_config(StemtraceConfig(transport_url="memory://"))


@pytest.fixture
def info_logging() -> Iterator[None]:
    """Let INFO records through the root and celery.task loggers."""
    root = logging.getLogger()
    task_root = logging.getLogger("celery.task")
    saved = (root.level, task_root.level)
    root.setLevel(logging.INFO)
    task_root.setLevel(logging.INFO)
    yield
    root.setLevel(saved[0])
    task_root.setLevel(saved[1])


def _enable(capture_output: bool = True, max_output_size: int = 16384) -> None:
    set_config(
        StemtraceConfig(
            transport_url="memory://",
            capture_output=capture_output,
            max_output_size=max_output_size,
        )
    )
    connect_signals(MemoryTransport())


def _fake_task(task_id: str = "task-1", retries: int = 0) -> Any:
    request = SimpleNamespace(
        id=task_id,
        parent_id=None,
        root_id=task_id,
        group=None,
        chord=None,
        retries=retries,
    )
    return SimpleNamespace(name="tests.output_task", request=request)


def _prerun(task: Any) -> None:
    _on_task_prerun(task_id=task.request.id, task=task, args=(), kwargs={})


def _postrun(task: Any, state: str, retval: Any = None) -> None:
    _on_task_postrun(
        task_id=task.request.id,
        task=task,
        args=(),
        kwargs={},
        retval=retval,
        state=state,
    )


def _events(state: TaskState) -> list[TaskEvent]:
    return [
        e
        for e in MemoryTransport.events
        if isinstance(e, TaskEvent) and e.state == state
    ]


class TestSignalHandlers:
    """Signal handlers attach output to SUCCESS/FAILURE/RETRY events."""

    def test_disabled_capture_installs_nothing_and_attaches_nothing(
        self, info_logging: None
    ) -> None:
        """With capture_output off (default) no handler is installed."""
        _enable(capture_output=False)
        task = _fake_task()
        _prerun(task)
        module_logger.warning("not captured")
        _postrun(task, "SUCCESS", retval=1)

        assert not is_installed()
        assert get_active_buffer() is None
        assert _events(TaskState.SUCCESS)[0].output is None

    def test_success_event_carries_output(self, info_logging: None) -> None:
        """Output logged during the task lands on its SUCCESS event."""
        _enable()
        task = _fake_task()
        _prerun(task)
        module_logger.info("working")
        _postrun(task, "SUCCESS", retval=1)

        assert is_installed()
        assert _events(TaskState.STARTED)[0].output is None
        assert _events(TaskState.SUCCESS)[0].output == (
            "INFO tests.unit.output_tasks: working"
        )
        assert get_active_buffer() is None

    def test_failure_event_carries_output_before_postrun(
        self, info_logging: None
    ) -> None:
        """task_failure fires before task_postrun and gets the output."""
        _enable()
        task = _fake_task()
        _prerun(task)
        module_logger.error("about to fail")
        _on_task_failure(
            task_id=task.request.id,
            exception=ValueError("boom"),
            args=(),
            kwargs={},
            traceback=None,
            einfo=None,
            sender=task,
        )
        _postrun(task, "FAILURE")

        failures = _events(TaskState.FAILURE)
        assert len(failures) == 1
        assert failures[0].output == "ERROR tests.unit.output_tasks: about to fail"
        assert _events(TaskState.SUCCESS) == []
        assert get_active_buffer() is None

    def test_each_retry_attempt_has_its_own_output(self, info_logging: None) -> None:
        """RETRY carries attempt 0's output; the final SUCCESS only attempt 1's."""
        _enable()
        first = _fake_task(retries=0)
        _prerun(first)
        module_logger.warning("attempt 0")
        _on_task_retry(
            sender=first,
            request=first.request,
            reason=RuntimeError("flaky"),
            einfo=None,
        )
        _postrun(first, "RETRY")

        second = _fake_task(retries=1)
        _prerun(second)
        module_logger.warning("attempt 1")
        _postrun(second, "SUCCESS", retval="ok")

        assert _events(TaskState.RETRY)[0].output == (
            "WARNING tests.unit.output_tasks: attempt 0"
        )
        assert _events(TaskState.SUCCESS)[0].output == (
            "WARNING tests.unit.output_tasks: attempt 1"
        )

    def test_task_without_output_has_none(self, info_logging: None) -> None:
        """A silent task doesn't get an empty string."""
        _enable()
        task = _fake_task()
        _prerun(task)
        _postrun(task, "SUCCESS", retval=1)
        assert _events(TaskState.SUCCESS)[0].output is None

    @pytest.mark.parametrize("state", ["REJECTED", "IGNORED"])
    def test_postrun_for_other_states_discards_buffer(
        self, state: str, info_logging: None
    ) -> None:
        """Reject/Ignore have no terminal event, but the buffer is still freed."""
        _enable()
        task = _fake_task()
        _prerun(task)
        module_logger.warning("dropped")
        _postrun(task, state)
        assert get_active_buffer() is None
        assert _events(TaskState.SUCCESS) == []

    def test_output_is_capped_by_max_output_size(self, info_logging: None) -> None:
        """max_output_size bounds the attached output, keeping the tail."""
        _enable(max_output_size=100)
        task = _fake_task()
        _prerun(task)
        for i in range(10):
            module_logger.warning("line %d", i)
        _postrun(task, "SUCCESS")
        output = _events(TaskState.SUCCESS)[0].output
        assert output is not None
        marker, kept = output.split("\n", 1)
        assert marker.startswith("[… ")
        assert marker.endswith(" bytes truncated]")
        assert kept.endswith("WARNING tests.unit.output_tasks: line 9")
        assert len(kept.encode()) <= 100
        assert "line 0" not in kept

    def test_capture_start_failure_never_breaks_task(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """A capture setup error is swallowed; events still flow."""
        _enable()

        def broken_install() -> None:
            raise RuntimeError("cannot install")

        monkeypatch.setattr(signals, "install_capture", broken_install)
        task = _fake_task()
        _prerun(task)
        _postrun(task, "SUCCESS", retval=1)

        assert get_active_buffer() is None
        assert _events(TaskState.STARTED)[0].task_id == "task-1"
        assert _events(TaskState.SUCCESS)[0].output is None

    def test_capture_stop_failure_publishes_without_output(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """A capture teardown error still publishes the terminal event."""
        _enable()

        def broken_stop(task_id: str) -> str | None:
            raise RuntimeError("cannot stop")

        monkeypatch.setattr(signals, "stop_capture", broken_stop)
        task = _fake_task()
        _prerun(task)
        module_logger.warning("lost")
        _postrun(task, "SUCCESS", retval=1)

        assert _events(TaskState.SUCCESS)[0].output is None


@pytest.fixture
def celery_app() -> Celery:
    """A real Celery app with stemtrace capture enabled, run eagerly."""
    app = Celery("output_tests", broker="memory://", backend="cache+memory://")
    app.conf.task_store_eager_result = False
    _enable()
    return app


class TestEagerCelery:
    """Real Celery tracer + signals, via task.apply()."""

    def test_print_and_both_logger_styles_are_captured(
        self, celery_app: Celery, info_logging: None
    ) -> None:
        """print(), get_task_logger and logging.getLogger are all captured."""

        @celery_app.task(name="tests.mixed_output")
        def mixed_output() -> str:
            print("from print")
            task_logger.info("from task logger")
            module_logger.warning("from module logger")
            return "done"

        result = mixed_output.apply()

        assert result.get() == "done"
        success = _events(TaskState.SUCCESS)
        assert len(success) == 1
        assert success[0].output == (
            "from print\n"
            "INFO tests.unit.output_tasks.celery: from task logger\n"
            "WARNING tests.unit.output_tasks: from module logger"
        )

    def test_failure_output_includes_lines_before_the_exception(
        self, celery_app: Celery, info_logging: None
    ) -> None:
        """The FAILURE event carries what the task printed before raising."""

        @celery_app.task(name="tests.failing_output")
        def failing_output() -> None:
            print("step 1 ok")
            raise ValueError("step 2 broke")

        failing_output.apply()

        failure = _events(TaskState.FAILURE)
        assert len(failure) == 1
        assert failure[0].output == "step 1 ok"
        assert failure[0].exception == "ValueError: step 2 broke"

    def test_retry_attempts_get_separate_output(
        self, celery_app: Celery, info_logging: None
    ) -> None:
        """Eager retries emit RETRY then SUCCESS, each with its attempt's output."""

        @celery_app.task(name="tests.retrying_output", bind=True, max_retries=2)
        def retrying_output(self: Any) -> str:
            print(f"attempt {self.request.retries}")
            if self.request.retries < 2:
                raise self.retry(countdown=0)
            return "ok"

        retrying_output.apply()

        retries = _events(TaskState.RETRY)
        assert [e.output for e in retries] == ["attempt 0", "attempt 1"]
        assert [e.output for e in _events(TaskState.SUCCESS)] == ["attempt 2"]

    def test_nested_apply_keeps_outer_output(
        self, celery_app: Celery, info_logging: None
    ) -> None:
        """A task applying another task eagerly keeps its own output separate."""

        @celery_app.task(name="tests.inner_output")
        def inner_output() -> None:
            print("inner")

        @celery_app.task(name="tests.outer_output")
        def outer_output() -> None:
            print("outer before")
            inner_output.apply()
            print("outer after")

        outer_output.apply()

        by_name = {e.name: e.output for e in _events(TaskState.SUCCESS)}
        assert by_name == {
            "tests.inner_output": "inner",
            "tests.outer_output": "outer before\nouter after",
        }
