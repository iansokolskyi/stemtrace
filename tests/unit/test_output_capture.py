"""Tests for per-task output capture (buffer, logging handler, tee)."""

import io
import logging
import sys
import threading
from collections.abc import Iterator

import pytest
from celery.utils.log import LoggingProxy, get_task_logger

from stemtrace.library.output_capture import (
    OutputBuffer,
    TaskOutputHandler,
    TeeStream,
    get_active_buffer,
    install_capture,
    is_installed,
    start_capture,
    stop_capture,
    uninstall_capture,
)


@pytest.fixture(autouse=True)
def clean_capture() -> Iterator[None]:
    """Remove the process-wide handler/tee and any active buffer around each test."""
    uninstall_capture()
    yield
    while (buffer := get_active_buffer()) is not None:
        stop_capture(buffer.task_id)
    uninstall_capture()


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


class TestOutputBuffer:
    """Tests for OutputBuffer accumulation and truncation."""

    def test_render_without_writes_returns_none(self) -> None:
        """An empty buffer renders as None (no output field on the event)."""
        assert OutputBuffer("t1", max_bytes=100).render() is None

    def test_render_joins_writes_in_order(self) -> None:
        """Writes are concatenated chronologically."""
        buffer = OutputBuffer("t1", max_bytes=100)
        buffer.append("first\n")
        buffer.append("second\n")
        assert buffer.render() == "first\nsecond"

    def test_empty_writes_are_ignored(self) -> None:
        """print(end="") style empty writes don't create output."""
        buffer = OutputBuffer("t1", max_bytes=100)
        buffer.append("")
        assert buffer.render() is None
        assert buffer.size_bytes == 0

    def test_render_over_cap_keeps_tail_with_marker(self) -> None:
        """When over the cap, the end of the output is kept, not the start."""
        buffer = OutputBuffer("t1", max_bytes=10)
        buffer.append("0123456789")
        buffer.append("abcdef")
        assert buffer.render() == "[… 6 bytes truncated]\n6789abcdef"

    def test_truncation_counts_utf8_bytes_not_chars(self) -> None:
        """The cap is in UTF-8 bytes; multi-byte chars count by their encoding."""
        buffer = OutputBuffer("t1", max_bytes=6)
        buffer.append("ééé")  # 6 bytes, 3 chars
        buffer.append("ab")
        # 8 bytes total, 6 kept: the leading "é" (2 bytes) is dropped whole.
        assert buffer.render() == "[… 2 bytes truncated]\nééab"

    def test_truncation_never_splits_a_character(self) -> None:
        """A cut landing inside a multi-byte char skips to the next char."""
        exact = OutputBuffer("t1", max_bytes=3)
        exact.append("é€")  # 2 + 3 bytes
        assert exact.render() == "[… 2 bytes truncated]\n€"
        inside = OutputBuffer("t1", max_bytes=4)
        inside.append("é€")
        # A 4-byte tail would start inside "é"; it is dropped entirely.
        assert inside.render() == "[… 2 bytes truncated]\n€"

    def test_single_huge_write_is_bounded(self) -> None:
        """One write larger than the cap is sliced, so memory stays bounded."""
        buffer = OutputBuffer("t1", max_bytes=1000)
        buffer.append("x" * 1_000_000 + "END")
        assert buffer.size_bytes == 1000
        rendered = buffer.render()
        assert rendered is not None
        marker, kept = rendered.split("\n", 1)
        assert marker == "[… 999003 bytes truncated]"
        assert kept.endswith("xEND")
        assert len(kept.encode()) == 1000

    def test_many_writes_keep_running_truncated_count(self) -> None:
        """The truncated count accumulates across many writes."""
        buffer = OutputBuffer("t1", max_bytes=5)
        for _ in range(100):
            buffer.append("ab")
        assert buffer.render() == "[… 195 bytes truncated]\nbabab"


class TestStartStop:
    """Tests for the ContextVar-held active buffer."""

    def test_stop_returns_captured_text_and_clears_buffer(self) -> None:
        """stop_capture returns what was written and deactivates the buffer."""
        buffer = start_capture("t1", max_bytes=100)
        buffer.append("hello")
        assert stop_capture("t1") == "hello"
        assert get_active_buffer() is None

    def test_stop_without_start_returns_none(self) -> None:
        """Stopping when nothing is active is a no-op."""
        assert stop_capture("t1") is None

    def test_stop_twice_returns_none_second_time(self) -> None:
        """Failure/retry stop first, postrun stops again: second call is a no-op."""
        start_capture("t1", max_bytes=100).append("x")
        assert stop_capture("t1") == "x"
        assert stop_capture("t1") is None

    def test_nested_capture_restores_outer_buffer(self) -> None:
        """A task applying another task eagerly keeps its own output."""
        start_capture("outer", max_bytes=100).append("outer-1\n")
        start_capture("inner", max_bytes=100).append("inner\n")
        assert stop_capture("inner") == "inner"
        active = get_active_buffer()
        assert active is not None
        assert active.task_id == "outer"
        active.append("outer-2\n")
        assert stop_capture("outer") == "outer-1\nouter-2"
        assert get_active_buffer() is None

    def test_stop_for_unknown_task_leaves_active_buffer(self) -> None:
        """A stop for a different task id doesn't steal the active buffer."""
        start_capture("t1", max_bytes=100).append("mine")
        assert stop_capture("other") is None
        assert stop_capture("t1") == "mine"

    def test_stop_outer_while_inner_active_unwinds_both(self) -> None:
        """Stopping an outer task drops any inner buffer left above it."""
        start_capture("outer", max_bytes=100).append("o")
        start_capture("inner", max_bytes=100).append("i")
        assert stop_capture("outer") == "o"
        assert get_active_buffer() is None

    def test_buffers_are_isolated_between_threads(self) -> None:
        """Concurrent tasks in a threads pool each see only their own output."""
        install_capture()
        barrier = threading.Barrier(2)
        results: dict[str, str | None] = {}
        thread_logger = logging.getLogger("tests.threaded")
        thread_logger.setLevel(logging.INFO)

        def run(task_id: str) -> None:
            start_capture(task_id, max_bytes=1000)
            barrier.wait()
            for i in range(3):
                thread_logger.info("%s line %d", task_id, i)
                barrier.wait()
            results[task_id] = stop_capture(task_id)

        try:
            threads = [threading.Thread(target=run, args=(t,)) for t in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            thread_logger.setLevel(logging.NOTSET)

        for task_id in ("a", "b"):
            assert results[task_id] == "\n".join(
                f"INFO tests.threaded: {task_id} line {i}" for i in range(3)
            )


class TestTaskOutputHandler:
    """Tests for the process-wide logging handler."""

    def _capture_warning(self, emitter: logging.Logger) -> str | None:
        start_capture("t1", max_bytes=10_000)
        emitter.warning("hello %s", "world")
        return stop_capture("t1")

    def test_module_logger_record_is_captured(self, info_logging: None) -> None:
        """logging.getLogger(__name__) records reach the buffer via root."""
        install_capture()
        out = self._capture_warning(logging.getLogger("myapp.tasks"))
        assert out == "WARNING myapp.tasks: hello world"

    def test_task_logger_record_is_captured_once(self, info_logging: None) -> None:
        """get_task_logger records are captured once, propagating or not."""
        install_capture()
        task_root = logging.getLogger("celery.task")
        saved = task_root.propagate
        try:
            for propagate in (False, True):
                task_root.propagate = propagate
                start_capture("t1", max_bytes=10_000)
                get_task_logger("myapp.jobs").info("step %d", 1)
                assert stop_capture("t1") == "INFO myapp.jobs: step 1"
        finally:
            task_root.propagate = saved

    def test_redirected_print_is_captured_without_prefix(
        self, info_logging: None
    ) -> None:
        """print() under Celery's stdout redirect reads like console output."""
        install_capture()
        out = self._capture_warning(logging.getLogger("celery.redirected"))
        assert out == "hello world"

    @pytest.mark.parametrize(
        "name",
        [
            "stemtrace",
            "stemtrace.library.signals",
            "celery",
            "celery.app.trace",
            "celery.worker.strategy",
            "kombu.connection",
            "amqp",
            "billiard.pool",
        ],
    )
    def test_internal_loggers_are_ignored(self, name: str, info_logging: None) -> None:
        """stemtrace, Celery and kombu internals don't pollute task output."""
        install_capture()
        assert self._capture_warning(logging.getLogger(name)) is None

    def test_lookalike_logger_names_are_captured(self, info_logging: None) -> None:
        """Only exact package prefixes are ignored (celery_app is user code)."""
        install_capture()
        out = self._capture_warning(logging.getLogger("celery_app"))
        assert out == "WARNING celery_app: hello world"

    def test_exception_traceback_is_included(self, info_logging: None) -> None:
        """logger.exception() includes the traceback text."""
        install_capture()
        start_capture("t1", max_bytes=10_000)
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("myapp").exception("failed")
        out = stop_capture("t1")
        assert out is not None
        assert out.startswith("ERROR myapp: failed\nTraceback (most recent call last):")
        assert out.endswith("ValueError: boom")

    def test_records_without_active_buffer_are_ignored(
        self, info_logging: None
    ) -> None:
        """Logging outside a task is untouched and buffers nothing."""
        install_capture()
        logging.getLogger("myapp").warning("outside")
        assert get_active_buffer() is None

    def test_bad_format_args_never_raise(self) -> None:
        """A broken log call inside a task doesn't break the task."""
        handler = TaskOutputHandler()
        record = logging.LogRecord(
            "myapp", logging.INFO, __file__, 1, "%d", ("x",), None
        )
        start_capture("t1", max_bytes=100)
        handler.emit(record)
        assert stop_capture("t1") is None


class TestInstall:
    """Tests for installing the handler and tee."""

    @staticmethod
    def _handlers(name: str | None) -> list[logging.Handler]:
        return [
            h
            for h in logging.getLogger(name).handlers
            if isinstance(h, TaskOutputHandler)
        ]

    def test_install_adds_handler_to_root_and_celery_task(self) -> None:
        """The handler goes on root and celery.task (which doesn't propagate)."""
        install_capture()
        assert is_installed()
        assert len(self._handlers(None)) == 1
        assert len(self._handlers("celery.task")) == 1

    def test_install_is_idempotent(self) -> None:
        """Calling install on every task never stacks handlers or tees."""
        install_capture()
        stdout = sys.stdout
        install_capture()
        install_capture()
        assert len(self._handlers(None)) == 1
        assert len(self._handlers("celery.task")) == 1
        assert sys.stdout is stdout

    def test_install_restores_handler_removed_by_logging_setup(self) -> None:
        """Celery's root-logger hijack wipes handlers; the next install re-adds it."""
        install_capture()
        root = logging.getLogger()
        for handler in self._handlers(None):
            root.removeHandler(handler)
        assert not is_installed()
        install_capture()
        assert is_installed()
        assert len(self._handlers(None)) == 1

    def test_uninstall_removes_handler_and_tee(self) -> None:
        """uninstall_capture puts logging and streams back."""
        original = sys.stdout
        install_capture()
        uninstall_capture()
        assert not is_installed()
        assert self._handlers("celery.task") == []
        assert sys.stdout is original

    def test_uninstall_leaves_streams_replaced_by_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If something else swapped sys.stdout after us, we don't clobber it."""
        install_capture()
        replacement = io.StringIO()
        monkeypatch.setattr(sys, "stdout", replacement)
        uninstall_capture()
        assert sys.stdout is replacement

    def test_not_installed_by_default(self) -> None:
        """Nothing is installed until capture is explicitly enabled."""
        assert not is_installed()
        assert not isinstance(sys.stdout, TeeStream)
        assert not isinstance(sys.stderr, TeeStream)


class TestTee:
    """Tests for the stdout/stderr tee used when Celery doesn't redirect."""

    def test_plain_stdout_is_teed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """print() is captured and still reaches the original stream."""
        original = io.StringIO()
        monkeypatch.setattr(sys, "stdout", original)
        install_capture()
        assert isinstance(sys.stdout, TeeStream)
        start_capture("t1", max_bytes=1000)
        print("hello")
        assert stop_capture("t1") == "hello"
        assert original.getvalue() == "hello\n"

    def test_stderr_is_teed_into_same_buffer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stdout and stderr interleave chronologically in one output."""
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        install_capture()
        start_capture("t1", max_bytes=1000)
        print("out")
        print("err", file=sys.stderr)
        print("out again")
        assert stop_capture("t1") == "out\nerr\nout again"

    def test_redirected_stdout_is_not_teed(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """Under Celery's redirect, print() is captured once, via logging."""
        proxy = LoggingProxy(logging.getLogger("celery.redirected"), logging.WARNING)
        monkeypatch.setattr(sys, "stdout", proxy)
        install_capture()
        assert sys.stdout is proxy
        start_capture("t1", max_bytes=1000)
        print("hello")
        assert stop_capture("t1") == "hello"

    def test_tee_writes_outside_task_are_forwarded_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without an active buffer the tee is a passthrough."""
        original = io.StringIO()
        monkeypatch.setattr(sys, "stdout", original)
        install_capture()
        print("idle")
        assert original.getvalue() == "idle\n"
        assert get_active_buffer() is None

    def test_tee_forwards_attributes_and_writelines(self) -> None:
        """The tee behaves like the stream it wraps."""
        original = io.StringIO()
        tee = TeeStream(original)
        start_capture("t1", max_bytes=1000)
        tee.writelines(["a", "b\n"])
        tee.flush()
        assert tee.wrapped is original
        assert tee.getvalue() == "ab\n"
        assert stop_capture("t1") == "ab"

    def test_tee_capture_failure_still_writes_original(self) -> None:
        """If buffering fails, the write still reaches the real stream."""
        original = io.StringIO()
        tee = TeeStream(original)
        buffer = start_capture("t1", max_bytes=1000)

        def broken_append(text: str) -> None:
            raise RuntimeError("buffer broke")

        buffer.append = broken_append  # type: ignore[method-assign]  # simulate failure
        assert tee.write("still here\n") == len("still here\n")
        assert original.getvalue() == "still here\n"
