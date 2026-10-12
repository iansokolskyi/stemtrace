"""Tests for per-task output capture (buffer, logging handler, tee)."""

import contextvars
import copy
import io
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO, cast

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.log import LoggingProxy, get_task_logger

from stemtrace.library import output_capture
from stemtrace.library.output_capture import (
    OutputBuffer,
    TaskOutputHandler,
    TeeStream,
    _get_active_buffer,
    _is_installed,
    _uninstall_capture,
    install_capture,
    start_capture,
    stop_capture,
)


class ListHandler(logging.Handler):
    """Logging handler that records formatted messages (a test fake)."""

    def __init__(self, level: int = logging.NOTSET) -> None:
        """Start with no messages."""
        super().__init__(level)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Remember the record's message."""
        self.messages.append(record.getMessage())


@pytest.fixture(autouse=True)
def clean_capture() -> Iterator[None]:
    """Remove the process-wide handler/tee and any active buffer around each test."""
    _uninstall_capture()
    yield
    while (buffer := _get_active_buffer()) is not None:
        stop_capture(buffer.task_id)
    _uninstall_capture()


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


class TestOutputBufferScaling:
    """Many tiny writes (print() is two writes) must stay cheap at any cap."""

    def test_tiny_writes_at_large_cap_are_fast_and_bounded(self) -> None:
        """400k two-byte writes at a 400 KB cap: linear time, bounded memory.

        Dropping the oldest output one tiny chunk at a time made every write
        cost O(cap) once the buffer was full (about a minute for this loop).
        """
        cap = 400_000
        buffer = OutputBuffer("t1", max_bytes=cap)
        started = time.perf_counter()
        for _ in range(400_000):
            buffer.append("ab")
            assert buffer.size_bytes <= 2 * cap
        elapsed = time.perf_counter() - started
        rendered = buffer.render()
        assert elapsed < 5
        assert rendered is not None
        marker, kept = rendered.split("\n", 1)
        assert marker == "[… 400000 bytes truncated]"
        assert len(kept.encode()) == cap
        assert buffer.size_bytes == cap

    def test_slack_trim_keeps_character_boundaries(self) -> None:
        """Trimming after the slack is used up never splits a character."""
        buffer = OutputBuffer("t1", max_bytes=5)
        for _ in range(10):
            buffer.append("é")  # 2 bytes each: 20 bytes total
        # 5-byte tail would start inside an "é"; keep the 4 bytes after it.
        assert buffer.render() == "[… 16 bytes truncated]\néé"


class TestStartStop:
    """Tests for the ContextVar-held active buffer."""

    def test_stop_returns_captured_text_and_clears_buffer(self) -> None:
        """stop_capture returns what was written and deactivates the buffer."""
        buffer = start_capture("t1", max_bytes=100)
        buffer.append("hello")
        assert stop_capture("t1") == "hello"
        assert _get_active_buffer() is None

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
        active = _get_active_buffer()
        assert active is not None
        assert active.task_id == "outer"
        active.append("outer-2\n")
        assert stop_capture("outer") == "outer-1\nouter-2"
        assert _get_active_buffer() is None

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
        assert _get_active_buffer() is None

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

    def test_redirected_records_are_left_to_the_tee(self, info_logging: None) -> None:
        """celery.redirected records (one per write chunk) aren't captured here.

        The tee captures print() text as written; capturing the per-chunk
        records too would garble multi-argument prints and duplicate them.
        """
        install_capture()
        assert self._capture_warning(logging.getLogger("celery.redirected")) is None

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

    def test_celery_task_logger_itself_is_captured(self, info_logging: None) -> None:
        """Records on the celery.task logger are task output, not internals."""
        install_capture()
        out = self._capture_warning(logging.getLogger("celery.task"))
        assert out == "WARNING celery.task: hello world"

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
        assert _get_active_buffer() is None

    def test_bad_format_args_never_raise(self) -> None:
        """A broken log call inside a task doesn't break the task."""
        handler = TaskOutputHandler()
        record = logging.LogRecord(
            "myapp", logging.INFO, __file__, 1, "%d", ("x",), None
        )
        start_capture("t1", max_bytes=100)
        handler.emit(record)
        assert stop_capture("t1") is None

    def test_soft_time_limit_while_formatting_a_record_propagates(self) -> None:
        """Celery's control flow raised into the task is not a capture failure."""

        class InterruptedFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                raise SoftTimeLimitExceeded

        handler = TaskOutputHandler()
        handler.setFormatter(InterruptedFormatter())
        record = logging.LogRecord("myapp", logging.INFO, __file__, 1, "x", (), None)
        start_capture("t1", max_bytes=100)
        try:
            with pytest.raises(SoftTimeLimitExceeded):
                handler.emit(record)
        finally:
            stop_capture("t1")

    def test_soft_time_limit_while_forwarding_to_last_resort_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupted(record: logging.LogRecord) -> None:
            raise SoftTimeLimitExceeded

        monkeypatch.setattr(
            TaskOutputHandler, "_forward_to_last_resort", staticmethod(interrupted)
        )
        record = logging.LogRecord("myapp", logging.INFO, __file__, 1, "x", (), None)

        with pytest.raises(SoftTimeLimitExceeded):
            TaskOutputHandler().emit(record)


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
        assert _is_installed()
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
        assert not _is_installed()
        install_capture()
        assert _is_installed()
        assert len(self._handlers(None)) == 1

    def test_uninstall_removes_handler_and_tee(self) -> None:
        """_uninstall_capture puts logging and streams back."""
        original = sys.stdout
        install_capture()
        _uninstall_capture()
        assert not _is_installed()
        assert self._handlers("celery.task") == []
        assert sys.stdout is original

    def test_uninstall_leaves_streams_replaced_by_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If something else swapped sys.stdout after us, we don't clobber it."""
        install_capture()
        replacement = io.StringIO()
        monkeypatch.setattr(sys, "stdout", replacement)
        _uninstall_capture()
        assert sys.stdout is replacement

    def test_not_installed_by_default(self) -> None:
        """Nothing is installed until capture is explicitly enabled."""
        assert not _is_installed()
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

    def test_soft_time_limit_during_tee_lookup_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Celery's control flow is not swallowed by the tee's fail-open lookup."""

        def interrupted() -> logging.LogRecord:
            raise SoftTimeLimitExceeded

        monkeypatch.setattr(output_capture, "_record_being_emitted", interrupted)
        tee = TeeStream(io.StringIO())
        start_capture("t1", max_bytes=100)
        try:
            with pytest.raises(SoftTimeLimitExceeded):
                tee.write("hello")
        finally:
            stop_capture("t1")

    def test_soft_time_limit_while_appending_to_the_buffer_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupted(self: OutputBuffer, text: str) -> None:
            raise SoftTimeLimitExceeded

        monkeypatch.setattr(OutputBuffer, "append", interrupted)
        tee = TeeStream(io.StringIO())
        start_capture("t1", max_bytes=100)
        try:
            with pytest.raises(SoftTimeLimitExceeded):
                tee.write("hello")
        finally:
            stop_capture("t1")

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

    def test_redirected_stdout_is_teed_over_the_proxy(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """Under Celery's redirect, print() text is captured as written, once.

        LoggingProxy turns each write() chunk into a record, so
        ``print("value:", 42)`` is four records; the tee sees the text itself.
        The proxy still gets every write, so the worker log is unchanged.
        """
        redirected = logging.getLogger("celery.redirected")
        proxy_records = ListHandler()
        redirected.addHandler(proxy_records)
        monkeypatch.setattr(sys, "stdout", LoggingProxy(redirected, logging.WARNING))
        try:
            install_capture()
            assert isinstance(sys.stdout, TeeStream)
            start_capture("t1", max_bytes=1000)
            print("value:", 42)
            print("no newline", end="")
            print(" then the rest")
            assert stop_capture("t1") == "value: 42\nno newline then the rest"
        finally:
            redirected.removeHandler(proxy_records)
        # One record per write chunk, as Celery logs them to the worker log.
        assert proxy_records.messages == [
            "value:",
            " ",
            "42",
            "no newline",
            " then the rest",
        ]

    def test_redirect_recursion_is_not_captured_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A handler writing back to the redirected stream doesn't duplicate output.

        Without Celery's handlers, a redirected record reaches logging.lastResort,
        which writes to sys.stderr: the tee again. That inner write is the same
        text and must not be captured a second time.
        """
        redirected = logging.getLogger("celery.redirected")
        proxy = LoggingProxy(redirected, logging.WARNING)
        monkeypatch.setattr(sys, "stdout", proxy)
        monkeypatch.setattr(sys, "stderr", proxy)
        monkeypatch.setattr(redirected, "propagate", False)
        monkeypatch.setattr(
            logging, "lastResort", logging._StderrHandler(logging.WARNING)
        )
        install_capture()
        redirected.addHandler(TaskOutputHandler())
        try:
            start_capture("t1", max_bytes=1000)
            print("once")
            assert stop_capture("t1") == "once"
        finally:
            for handler in list(redirected.handlers):
                redirected.removeHandler(handler)

    def test_tee_writes_outside_task_are_forwarded_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without an active buffer the tee is a passthrough."""
        original = io.StringIO()
        monkeypatch.setattr(sys, "stdout", original)
        install_capture()
        print("idle")
        assert original.getvalue() == "idle\n"
        assert _get_active_buffer() is None

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

    def test_tee_behaves_like_the_wrapped_text_stream(self, tmp_path: Path) -> None:
        """Common stream checks and the context manager reach the real stream."""
        with (tmp_path / "out.txt").open("w", encoding="utf-8") as original:
            tee = TeeStream(original)
            assert tee.isatty() is False
            assert tee.fileno() == original.fileno()
            assert tee.encoding == "utf-8"
            assert tee.writable() is True
            assert tee.closed is False
            with tee as entered:
                assert entered is tee
                print("inside", file=entered)
            assert original.closed is True
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "inside\n"

    def test_tee_over_logging_proxy_supports_with(self) -> None:
        """A wrapped stream without __exit__ (LoggingProxy) still works in `with`."""
        proxy = LoggingProxy(logging.getLogger("celery.redirected"), logging.WARNING)
        with TeeStream(proxy) as tee:
            assert tee.isatty() is False
        assert proxy.closed is False

    def test_stream_handler_on_the_tee_is_captured_once(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """A StreamHandler created after the tee writes through it: one copy only.

        Its record would otherwise be captured by the handler and again by the
        tee; our handler's copy wins, so every record is formatted the same way.
        """
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        install_capture()
        lib = logging.getLogger("somelib")
        lib_handler = logging.StreamHandler()  # binds the current sys.stderr: the tee
        lib.addHandler(lib_handler)
        try:
            start_capture("t1", max_bytes=1000)
            lib.warning("hello from lib")
            assert stop_capture("t1") == "WARNING somelib: hello from lib"
        finally:
            lib.removeHandler(lib_handler)

    def test_stream_handler_on_the_tee_filtering_by_level_leaves_record_to_us(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """If that StreamHandler drops the record by level, we still capture it."""
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        install_capture()
        lib = logging.getLogger("somelib")
        lib_handler = logging.StreamHandler()
        lib_handler.setLevel(logging.ERROR)
        lib.addHandler(lib_handler)
        try:
            start_capture("t1", max_bytes=1000)
            lib.warning("only via the handler")
            assert stop_capture("t1") == "WARNING somelib: only via the handler"
        finally:
            lib.removeHandler(lib_handler)

    def test_stream_handler_on_the_tee_rejecting_by_filter_leaves_record_to_us(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """If that StreamHandler's filter drops the record, we still capture it."""
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        install_capture()
        lib = logging.getLogger("somelib")
        lib_handler = logging.StreamHandler()
        lib_handler.addFilter(lambda record: "secret" not in record.getMessage())
        lib.addHandler(lib_handler)
        try:
            start_capture("t1", max_bytes=1000)
            lib.warning("not secret enough")
            lib.warning("a secret line")
            # That handler's filter only affects its own (console) output.
            assert stop_capture("t1") == (
                "WARNING somelib: not secret enough\nWARNING somelib: a secret line"
            )
        finally:
            lib.removeHandler(lib_handler)

    @pytest.mark.parametrize("placement", ["on-the-logger", "on-root"])
    def test_stream_handler_on_the_tee_doesnt_leak_internal_loggers(
        self, placement: str, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """Internal records written through the tee by another handler are dropped.

        Covers a handler that runs before ours (on the record's own logger)
        and one that runs after ours (added to root later).
        """
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        install_capture()
        target = logging.getLogger("kombu" if placement == "on-the-logger" else None)
        late = logging.StreamHandler()
        target.addHandler(late)
        try:
            start_capture("t1", max_bytes=1000)
            logging.getLogger("kombu.connection").warning("broker hiccup")
            logging.getLogger("stemtrace.library.signals").warning("publish failed")
            logging.getLogger("celery.app.trace").warning("internal")
            print("user print")
            assert stop_capture("t1") == "user print"
        finally:
            target.removeHandler(late)
        assert "broker hiccup" in sys.stderr.getvalue()

    def test_stream_handler_on_the_tee_outside_our_chain_is_captured_by_tee(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """A non-propagating logger our handler never sees: the tee keeps its copy."""
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        install_capture()
        lib = logging.getLogger("isolatedlib")
        lib.propagate = False
        lib_handler = logging.StreamHandler()
        lib.addHandler(lib_handler)
        try:
            start_capture("t1", max_bytes=1000)
            lib.warning("only the tee sees this")
            assert stop_capture("t1") == "only the tee sees this"
        finally:
            lib.removeHandler(lib_handler)
            lib.propagate = True

    def test_tee_write_from_a_shallow_stack_is_captured(self) -> None:
        """A write near the top of a thread's stack (few frames) still works."""
        original = io.StringIO()
        tee = TeeStream(original)
        start_capture("t1", max_bytes=1000)
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(tee.write, "shallow"))
        thread.start()
        thread.join()
        assert stop_capture("t1") == "shallow"
        assert original.getvalue() == "shallow"

    def test_missing_streams_are_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sys.stdout can be None (closed fd, pythonw); print() must keep working."""
        monkeypatch.setattr(sys, "stdout", None)
        install_capture()
        assert sys.stdout is None
        start_capture("t1", max_bytes=1000)
        print("goes nowhere")  # print() ignores a None sys.stdout
        assert stop_capture("t1") is None

    def test_non_writable_streams_are_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stream that can't be written isn't wrapped."""
        path = tmp_path / "in.txt"
        path.write_text("x", encoding="utf-8")
        with path.open(encoding="utf-8") as read_only:
            monkeypatch.setattr(sys, "stdout", read_only)
            install_capture()
            assert sys.stdout is read_only

    @pytest.mark.parametrize("kind", ["no-write", "closed"])
    def test_unusable_streams_are_left_alone(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Objects without write() and closed streams aren't wrapped."""
        stream: object
        if kind == "no-write":
            stream = object()
        else:
            closed = io.StringIO()
            closed.close()
            stream = closed
        monkeypatch.setattr(sys, "stdout", stream)
        install_capture()
        assert sys.stdout is stream

    def test_tee_can_be_copied(self) -> None:
        """copy.copy() works (no recursion through __getattr__)."""
        original = io.StringIO()
        clone = copy.copy(TeeStream(original))
        assert clone.wrapped is original
        clone.write("via clone")
        assert original.getvalue() == "via clone"

    def test_tee_does_not_forward_dunder_lookups(self) -> None:
        """Missing dunders raise AttributeError instead of hitting the stream."""
        tee = TeeStream(io.StringIO())
        with pytest.raises(AttributeError):
            tee.__getattr__("__reduce_ex_missing__")
        bare = TeeStream.__new__(TeeStream)
        with pytest.raises(AttributeError):
            bare.__getattr__("encoding")

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


def _quiet_logger(name: str, *handlers: logging.Handler) -> logging.Logger:
    """A non-propagating logger with exactly ``handlers`` (call in the test body).

    pytest attaches its capture handler to non-propagating loggers when a test
    phase starts, so this must run inside the test, not in a fixture.
    """
    target = logging.getLogger(name)
    for handler in list(target.handlers):
        target.removeHandler(handler)
    target.propagate = False
    target.setLevel(logging.DEBUG)
    for handler in handlers:
        target.addHandler(handler)
    return target


def _reset_logger(target: logging.Logger) -> None:
    for handler in list(target.handlers):
        target.removeHandler(handler)
    target.propagate = True
    target.setLevel(logging.NOTSET)


class TestTeeUserVisibleWrites:
    """Text a user would see on the console during logging is still captured."""

    def test_logging_error_diagnostic_is_captured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken log call's "--- Logging error ---" report lands in the output."""
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        monkeypatch.setattr(logging, "raiseExceptions", True)
        install_capture()
        console = logging.StreamHandler(io.StringIO())
        broken = _quiet_logger("tests.broken", console, TaskOutputHandler())
        try:
            start_capture("t1", max_bytes=10_000)
            broken.info("%d", "abc")
            out = stop_capture("t1")
        finally:
            _reset_logger(broken)
        assert out is not None
        assert out.startswith(
            "--- Logging error ---\nTraceback (most recent call last):"
        )
        assert "TypeError" in out
        assert "--- Logging error ---" in sys.stderr.getvalue()

    def test_prints_during_message_formatting_are_captured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A __str__ that prints while a record is formatted is user output too."""

        class Noisy:
            def __str__(self) -> str:
                print("side effect")
                return "noisy"

        monkeypatch.setattr(sys, "stdout", io.StringIO())
        install_capture()
        lib = _quiet_logger("tests.noisy", TaskOutputHandler())
        try:
            start_capture("t1", max_bytes=10_000)
            lib.warning("value %s", Noisy())
            out = stop_capture("t1")
        finally:
            _reset_logger(lib)
        assert out == "side effect\nWARNING tests.noisy: value noisy"


class TestTeeFailsOpen:
    """If working out who wrote to the tee fails, the text is captured."""

    def test_record_without_a_name_is_captured(self) -> None:
        """A malformed record (name None) doesn't break or drop the write."""
        original = io.StringIO()
        tee = TeeStream(original)
        handler = logging.StreamHandler(tee)
        record = logging.LogRecord("x", logging.WARNING, __file__, 1, "odd", None, None)
        record.name = None  # type: ignore[assignment]  # malformed on purpose
        start_capture("t1", max_bytes=1000)
        handler.handle(record)
        assert stop_capture("t1") == "odd"
        assert original.getvalue() == "odd\n"

    def test_frame_lookup_failure_is_captured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sys._getframe raising (a shallow stack) still captures the write."""

        def no_frames(depth: int = 0) -> object:
            raise ValueError("call stack is not deep enough")

        original = io.StringIO()
        tee = TeeStream(original)
        start_capture("t1", max_bytes=1000)
        monkeypatch.setattr(output_capture.sys, "_getframe", no_frames)
        tee.write("still captured")
        monkeypatch.undo()
        assert stop_capture("t1") == "still captured"


class TestRepointStreamHandlers:
    """StreamHandlers created after the tee are pointed at the real stream."""

    def test_install_repoints_handlers_bound_to_the_tee(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The next install (each task's prerun) re-points a late StreamHandler."""
        original = io.StringIO()
        monkeypatch.setattr(sys, "stderr", original)
        install_capture()
        late = logging.StreamHandler()  # binds sys.stderr: the tee
        assert isinstance(late.stream, TeeStream)
        lib = logging.getLogger("tests.repoint")
        lib.addHandler(late)
        try:
            install_capture()
            assert late.stream is original
        finally:
            lib.removeHandler(late)

    def test_non_propagating_logger_handler_stays_on_the_tee(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A library logger with propagate=False keeps being captured, every task.

        Its StreamHandler (created during task 1, bound to the tee) is the only
        way its records reach the output: our handler isn't in that chain, so
        re-pointing it at the real stream would lose tasks 2+.
        """
        console = io.StringIO()
        monkeypatch.setattr(sys, "stderr", console)
        outputs: list[str | None] = []
        lib: logging.Logger | None = None
        try:
            for n in range(1, 4):
                install_capture()  # this task's prerun
                start_capture(f"t{n}", max_bytes=1000)
                if lib is None:
                    lib = _quiet_logger("tests.nonprop_lib", logging.StreamHandler())
                lib.warning("task %d says hi", n)
                outputs.append(stop_capture(f"t{n}"))
        finally:
            if lib is not None:
                _reset_logger(lib)
        assert outputs == [f"task {n} says hi" for n in range(1, 4)]
        assert console.getvalue() == "".join(f"task {n} says hi\n" for n in range(1, 4))

    def test_handler_shared_with_a_non_propagating_logger_stays_on_the_tee(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One handler on a propagating and a non-propagating logger isn't moved."""
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        install_capture()
        shared = logging.StreamHandler()
        quiet = _quiet_logger("tests.shared_quiet", shared)
        loud = logging.getLogger("tests.shared_loud")
        loud.addHandler(shared)
        try:
            install_capture()
            assert isinstance(shared.stream, TeeStream)
        finally:
            loud.removeHandler(shared)
            _reset_logger(quiet)

    def test_handler_overriding_handle_is_captured_once_after_repoint(
        self, monkeypatch: pytest.MonkeyPatch, info_logging: None
    ) -> None:
        """A handler the frame walk can't see through no longer duplicates or leaks."""

        class DirectHandler(logging.StreamHandler):  # type: ignore[type-arg]
            def handle(self, record: logging.LogRecord) -> bool:
                self.emit(record)  # no Handler.handle frame on the stack
                return True

        original = io.StringIO()
        monkeypatch.setattr(sys, "stderr", original)
        install_capture()
        direct = DirectHandler()
        logging.getLogger().addHandler(direct)
        try:
            install_capture()  # next task's prerun
            start_capture("t1", max_bytes=1000)
            logging.getLogger("somelib").warning("from lib")
            logging.getLogger("kombu.connection").warning("broker hiccup")
            out = stop_capture("t1")
        finally:
            logging.getLogger().removeHandler(direct)
        assert out == "WARNING somelib: from lib"
        assert "broker hiccup" in original.getvalue()

    def test_handler_with_read_only_stream_is_left_alone(self) -> None:
        """A handler whose stream can't be reassigned doesn't break install."""

        class PropertyStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
            @property  # type: ignore[override]
            def stream(self) -> TextIO:
                return cast("TextIO", sys.stdout)

            @stream.setter
            def stream(self, value: TextIO) -> None:
                raise AttributeError("read-only")

        install_capture()
        odd = PropertyStreamHandler.__new__(PropertyStreamHandler)
        logging.Handler.__init__(odd)
        lib = logging.getLogger("tests.readonly")
        lib.addHandler(odd)
        try:
            install_capture()
            assert isinstance(odd.stream, TeeStream)
        finally:
            lib.removeHandler(odd)


class TestLastResort:
    """The handler must not hide logs that Python would print via lastResort.

    A logger chain with no handlers falls back to ``logging.lastResort``
    (WARNING+ to stderr). Our handler on root would count as a handler and
    silence that, so it forwards such records itself, exactly once.
    """

    @pytest.fixture
    def last_resort(self, monkeypatch: pytest.MonkeyPatch) -> ListHandler:
        """Replace logging.lastResort with a recording handler."""
        fake = ListHandler(level=logging.WARNING)
        monkeypatch.setattr(logging, "lastResort", fake)
        return fake

    @pytest.fixture
    def isolate(self) -> Iterator[Callable[..., logging.Logger]]:
        """Make a non-propagating logger (standing in for root) with only our handler.

        Call it in the test body: pytest attaches its capture handler to
        non-propagating loggers when each test phase starts, and that handler
        would count as "another handler".
        """
        made: list[logging.Logger] = []

        def make(name: str = "tests.lastresort") -> logging.Logger:
            target = logging.getLogger(name)
            for handler in list(target.handlers):
                target.removeHandler(handler)
            target.propagate = False
            target.setLevel(logging.DEBUG)
            target.addHandler(TaskOutputHandler())
            made.append(target)
            return target

        yield make
        for target in made:
            for handler in list(target.handlers):
                target.removeHandler(handler)
            target.propagate = True
            target.setLevel(logging.NOTSET)

    def test_warning_without_other_handlers_reaches_last_resort(
        self, last_resort: ListHandler, isolate: Callable[..., logging.Logger]
    ) -> None:
        """Outside a task, a WARNING still reaches lastResort as before."""
        isolate().warning("visible %s", "still")
        assert last_resort.messages == ["visible still"]

    def test_info_below_last_resort_level_is_not_forwarded(
        self, last_resort: ListHandler, isolate: Callable[..., logging.Logger]
    ) -> None:
        """lastResort only prints WARNING and above; so do we."""
        isolate().info("quiet")
        assert last_resort.messages == []

    def test_other_handler_in_chain_means_no_forwarding(
        self, last_resort: ListHandler, isolate: Callable[..., logging.Logger]
    ) -> None:
        """When a real handler exists, Python wouldn't use lastResort either."""
        isolated = isolate()
        isolated.addHandler(ListHandler(level=logging.CRITICAL))  # counts anyway
        isolated.warning("handled elsewhere")
        assert last_resort.messages == []

    def test_forwarded_once_with_our_handler_twice_in_chain(
        self, last_resort: ListHandler, isolate: Callable[..., logging.Logger]
    ) -> None:
        """Our handler on both celery.task and root still forwards once."""
        isolate()
        child = logging.getLogger("tests.lastresort.child")
        child.addHandler(TaskOutputHandler())
        try:
            child.error("once")
        finally:
            for handler in list(child.handlers):
                child.removeHandler(handler)
        assert last_resort.messages == ["once"]

    def test_no_last_resort_configured_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch, isolate: Callable[..., logging.Logger]
    ) -> None:
        """Apps that set logging.lastResort = None keep their behavior."""
        monkeypatch.setattr(logging, "lastResort", None)
        isolated = isolate()
        start_capture("t1", max_bytes=1000)
        isolated.warning("captured, not printed")
        assert stop_capture("t1") == "WARNING tests.lastresort: captured, not printed"

    def test_internal_logger_records_are_forwarded_too(
        self, last_resort: ListHandler, isolate: Callable[..., logging.Logger]
    ) -> None:
        """Forwarding doesn't depend on the capture filter (stemtrace's logs too)."""
        internal = isolate("stemtrace.tests_lastresort")
        start_capture("t1", max_bytes=1000)
        internal.warning("publish failed")
        assert stop_capture("t1") is None
        assert last_resort.messages == ["publish failed"]

    def test_inside_task_output_is_captured_once_and_still_printed(
        self, monkeypatch: pytest.MonkeyPatch, isolate: Callable[..., logging.Logger]
    ) -> None:
        """lastResort writes to sys.stderr (our tee) without duplicating output."""
        stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setattr(
            logging, "lastResort", logging._StderrHandler(logging.WARNING)
        )
        install_capture()
        isolated = isolate()
        start_capture("t1", max_bytes=1000)
        isolated.warning("careful")
        assert stop_capture("t1") == "WARNING tests.lastresort: careful"
        assert stderr.getvalue() == "careful\n"

    def test_broken_last_resort_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, isolate: Callable[..., logging.Logger]
    ) -> None:
        """A failing lastResort can't break the task that logged."""

        class BrokenHandler(logging.Handler):
            def handle(self, record: logging.LogRecord) -> bool:
                raise RuntimeError("stderr gone")

        monkeypatch.setattr(logging, "lastResort", BrokenHandler(logging.WARNING))
        isolated = isolate()
        start_capture("t1", max_bytes=1000)
        isolated.warning("still captured")
        assert stop_capture("t1") == "WARNING tests.lastresort: still captured"

    def test_records_from_unknown_loggers_are_not_forwarded(
        self, last_resort: ListHandler
    ) -> None:
        """A record handed to the handler directly has no chain to check."""
        record = logging.LogRecord(
            "tests.never.created.logger", logging.ERROR, __file__, 1, "m", None, None
        )
        TaskOutputHandler().handle(record)
        assert last_resort.messages == []

    def test_root_logger_record_is_checked_from_root(
        self, last_resort: ListHandler
    ) -> None:
        """Records logged on root itself look at root's handlers (pytest's here)."""
        record = logging.LogRecord("root", logging.ERROR, __file__, 1, "m", None, None)
        TaskOutputHandler().handle(record)
        assert last_resort.messages == []
