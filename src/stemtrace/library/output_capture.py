"""Output capture for task stdout/stderr.

This module provides thread-safe capture of stdout/stderr during task execution.
Uses contextvars for per-task isolation.
"""

from __future__ import annotations

import io
import logging
import sys
from contextvars import ContextVar
from typing import NamedTuple, TextIO


class CapturedOutput(NamedTuple):
    """Captured stdout and stderr from a task."""

    stdout: str
    stderr: str


class TeeWriter:
    """A writer that writes to both a buffer and the original stream."""

    def __init__(self, buffer: io.StringIO, original: TextIO) -> None:
        self._buffer = buffer
        self._original = original

    def write(self, s: str) -> int:
        self._original.write(s)
        return self._buffer.write(s)

    def flush(self) -> None:
        self._original.flush()
        self._buffer.flush()

    # Forward all other attribute access to the original stream
    def __getattr__(self, name: str) -> object:
        return getattr(self._original, name)


class OutputCapture:
    """Context manager for capturing stdout/stderr during task execution.

    Uses TeeWriter to capture output while still displaying it to the
    original streams.

    Args:
        max_size: Maximum size in bytes for captured output. Output will be
                  truncated with a marker if exceeded.
    """

    def __init__(self, max_size: int = 65536) -> None:
        self._max_size = max_size
        self._stdout_buffer: io.StringIO | None = None
        self._stderr_buffer: io.StringIO | None = None
        self._original_stdout: TextIO | None = None
        self._original_stderr: TextIO | None = None
        self._handler_original_streams: list[tuple[logging.StreamHandler, TextIO]] = []

    def start(self) -> OutputCapture:
        # Save original streams
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        self._original_stdout = original_stdout
        self._original_stderr = original_stderr

        self._stdout_buffer = io.StringIO()
        self._stderr_buffer = io.StringIO()

        stdout_tee = TeeWriter(self._stdout_buffer, original_stdout)
        stderr_tee = TeeWriter(self._stderr_buffer, original_stderr)

        sys.stdout = stdout_tee  # type: ignore[assignment]
        sys.stderr = stderr_tee  # type: ignore[assignment]

        # Redirect any logging StreamHandlers that point to stdout/stderr
        # This ensures logger.info() etc. also get captured
        self._handler_original_streams = []
        all_handlers = self._get_all_stream_handlers()

        for handler in all_handlers:
            stream = handler.stream

            if self._is_stdout_stream(stream):
                self._handler_original_streams.append((handler, stream))
                handler.stream = stdout_tee  # type: ignore[assignment]
            elif self._is_stderr_stream(stream):
                self._handler_original_streams.append((handler, stream))
                handler.stream = stderr_tee  # type: ignore[assignment]

        return self

    @staticmethod
    def _get_all_stream_handlers() -> list[logging.StreamHandler]:
        """Get all StreamHandlers from all loggers."""
        handlers: list[logging.StreamHandler] = []
        for handler in logging.root.handlers:
            if isinstance(handler, logging.StreamHandler):
                handlers.append(handler)

        for logger in logging.Logger.manager.loggerDict.values():
            if isinstance(logger, logging.Logger):
                for handler in logger.handlers:
                    if isinstance(handler, logging.StreamHandler):
                        handlers.append(handler)
        return handlers

    def _is_stdout_stream(self, stream: TextIO) -> bool:
        """Check if a stream is stdout (by identity, name, or fileno)."""
        try:
            if stream is self._original_stdout:
                return True

            name = getattr(stream, "name", None)
            if name == "<stdout>":
                return True

            if stream.fileno() == self._original_stdout.fileno():
                return True
        except Exception:
            return False

    def _is_stderr_stream(self, stream: TextIO) -> bool:
        """Check if a stream is stderr (by identity, name, or fileno)."""
        try:
            if stream is self._original_stderr:
                return True
            name = getattr(stream, "name", None)

            if name == "<stderr>":
                return True

            if stream.fileno() == self._original_stderr.fileno():
                return True
        except Exception:
            return False

    def stop(self) -> None:
        # Restore logging handlers first
        for handler, original_stream in self._handler_original_streams:
            handler.stream = original_stream
        self._handler_original_streams = []

        # Restore original streams
        if self._original_stdout is not None:
            sys.stdout = self._original_stdout
        if self._original_stderr is not None:
            sys.stderr = self._original_stderr

    def get_output(self) -> CapturedOutput:
        """Get the captured output, truncating if necessary."""
        stdout = ""
        stderr = ""

        if self._stdout_buffer is not None:
            stdout = self._stdout_buffer.getvalue()
            if len(stdout.encode("utf-8")) > self._max_size:
                # Truncate to approximate byte limit
                stdout = stdout[: self._max_size] + "\n[truncated]"

        if self._stderr_buffer is not None:
            stderr = self._stderr_buffer.getvalue()
            if len(stderr.encode("utf-8")) > self._max_size:
                stderr = stderr[: self._max_size] + "\n[truncated]"

        return CapturedOutput(stdout=stdout, stderr=stderr)


# Global capture instance for the current task
_active_capture: ContextVar[OutputCapture | None] = ContextVar(
    "_active_capture", default=None
)


def start_capture(max_size: int = 65536) -> OutputCapture:
    capture = OutputCapture(max_size=max_size)
    capture.start()
    _active_capture.set(capture)
    return capture


def stop_capture() -> CapturedOutput:
    capture = _active_capture.get()
    if capture is None:
        return CapturedOutput(stdout="", stderr="")

    output = capture.get_output()
    capture.stop(None, None, None)
    _active_capture.set(None)
    return output


def get_current_capture() -> OutputCapture | None:
    """Get the current active capture, if any."""
    return _active_capture.get()
