"""Per-task output capture: what a task printed and logged, per attempt.

How it works:

- A ``ContextVar`` holds the active :class:`OutputBuffer`. ``task_prerun`` starts
  one, the terminal signal handlers stop it and attach the text to the terminal
  event. A ContextVar is per thread / greenlet, so concurrent tasks in a threads,
  gevent or eventlet pool never see each other's output. Nothing swaps global
  streams per task.
- One process-wide :class:`TaskOutputHandler` is attached to the root logger
  **and** to ``celery.task`` (Celery sets ``propagate=False`` on it, so
  ``get_task_logger`` records never reach root). It appends records to the
  active buffer, if there is one. Under Celery's default
  ``worker_redirect_stdouts=True``, ``print()`` goes through the
  ``celery.redirected`` logger, so the handler sees it too.
- When ``sys.stdout``/``sys.stderr`` are *not* Celery's ``LoggingProxy``
  (redirect disabled, eager runs), a :class:`TeeStream` is installed once that
  forwards to the original stream and also appends to the active buffer.

Output is one combined, chronological string: with the redirect on, stdout and
stderr both arrive as ``celery.redirected`` records and can't be told apart.

Limitations: output from threads the task starts itself isn't captured (new
threads start with an empty context), and output is free text, so it isn't
scrubbed for secrets.
"""

from __future__ import annotations

import logging
import sys
import threading
from contextvars import ContextVar
from typing import Any, TextIO, cast

from celery.utils.log import LoggingProxy

logger = logging.getLogger(__name__)

# Records from these loggers are always captured, even though they sit under
# "celery": print() under Celery's redirect, and task loggers.
_INCLUDED_LOGGERS = ("celery.redirected", "celery.task")
# stemtrace's own logs and Celery/kombu internals aren't task output.
_EXCLUDED_LOGGERS = ("stemtrace", "celery", "kombu", "amqp", "billiard")
_REDIRECT_LOGGER = "celery.redirected"
# Set on a record once appended, so a record reaching the handler twice (via
# celery.task and, if it propagates, root) is captured once.
_CAPTURED_ATTR = "_stemtrace_output_captured"

_active_buffer: ContextVar[OutputBuffer | None] = ContextVar(
    "stemtrace_output_buffer", default=None
)


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _should_capture(logger_name: str) -> bool:
    if _matches(logger_name, _INCLUDED_LOGGERS):
        return True
    return not _matches(logger_name, _EXCLUDED_LOGGERS)


class OutputBuffer:
    """Output of one task attempt, capped to its last ``max_bytes`` UTF-8 bytes.

    The end of the output is kept (that's where a failure shows up); the start
    is dropped as writes come in, so memory stays bounded even for one huge
    write.
    """

    def __init__(
        self, task_id: str, max_bytes: int, previous: OutputBuffer | None = None
    ) -> None:
        """Create an empty buffer for ``task_id``.

        Args:
            task_id: Celery task id this buffer belongs to.
            max_bytes: Maximum UTF-8 bytes of output to keep.
            previous: Buffer that was active before this one (eager nesting).
        """
        self.task_id = task_id
        self.previous = previous
        self._max_bytes = max_bytes
        self._chunks: list[bytes] = []
        self._size = 0
        self._truncated = 0
        # Free-threaded builds can inherit the context into threads a task
        # starts, so two threads may append to one buffer.
        self._lock = threading.Lock()

    @property
    def size_bytes(self) -> int:
        """Bytes currently kept (never more than ``max_bytes``)."""
        return self._size

    def append(self, text: str) -> None:
        """Add text, dropping the oldest bytes beyond the cap."""
        data = text.encode("utf-8", errors="replace")
        if not data:
            return
        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            if self._size > self._max_bytes:
                self._drop_oldest(self._size - self._max_bytes)

    def _drop_oldest(self, excess: int) -> None:
        while excess > 0 and self._chunks:
            head = self._chunks[0]
            if len(head) <= excess:
                self._chunks.pop(0)
                dropped = len(head)
            else:
                cut = excess
                # Don't start the kept text in the middle of a UTF-8 sequence.
                while cut < len(head) and (head[cut] & 0xC0) == 0x80:
                    cut += 1
                self._chunks[0] = head[cut:]
                dropped = cut
            self._size -= dropped
            self._truncated += dropped
            excess -= dropped

    def render(self) -> str | None:
        """Return the captured text, or None if the task wrote nothing.

        If output was dropped, the text starts with a
        ``[… N bytes truncated]`` line.
        """
        with self._lock:
            text = b"".join(self._chunks).decode("utf-8", errors="replace")
            truncated = self._truncated
        text = text.rstrip("\n")
        if truncated:
            return f"[… {truncated} bytes truncated]\n{text}"
        return text or None


def get_active_buffer() -> OutputBuffer | None:
    """Return the buffer capturing output in the current context, if any."""
    return _active_buffer.get()


def start_capture(task_id: str, max_bytes: int) -> OutputBuffer:
    """Start capturing output for a task attempt in the current context.

    A buffer that is already active (a task applying another task eagerly) is
    kept and restored when this one stops.

    Args:
        task_id: Celery task id.
        max_bytes: Maximum UTF-8 bytes of output to keep.

    Returns:
        The new active buffer.
    """
    buffer = OutputBuffer(task_id, max_bytes, previous=_active_buffer.get())
    _active_buffer.set(buffer)
    return buffer


def stop_capture(task_id: str) -> str | None:
    """Stop capturing for a task and return its output.

    Safe to call more than once: Celery sends ``task_failure``/``task_retry``
    and then ``task_postrun`` for the same attempt.

    Args:
        task_id: Celery task id whose capture to stop.

    Returns:
        The captured text, or None if nothing was captured or no capture for
        ``task_id`` is active.
    """
    buffer = _active_buffer.get()
    while buffer is not None and buffer.task_id != task_id:
        buffer = buffer.previous
    if buffer is None:
        return None
    # Also drops any inner buffer left above it.
    _active_buffer.set(buffer.previous)
    return buffer.render()


class TaskOutputHandler(logging.Handler):
    """Logging handler that appends records to the active task's buffer.

    Formats records as ``LEVEL logger: message`` (with tracebacks). Records
    from Celery's stdout redirect are appended as the bare message, so
    ``print()`` output reads as it would on a console.
    """

    def __init__(self) -> None:
        """Create the handler at NOTSET; logger levels decide what gets here."""
        super().__init__()
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        """Append the record to the active buffer. Never raises."""
        try:
            buffer = _active_buffer.get()
            if buffer is None or getattr(record, _CAPTURED_ATTR, False):
                return
            if not _should_capture(record.name):
                return
            if record.name == _REDIRECT_LOGGER and not record.exc_info:
                text = record.getMessage()
            else:
                text = self.format(record)
            setattr(record, _CAPTURED_ATTR, True)
            buffer.append(text + "\n")
        except Exception:
            # Logged under "stemtrace", which this handler ignores: no recursion.
            logger.debug("Failed to capture log record", exc_info=True)


class TeeStream:
    """Stream wrapper that forwards writes and copies them to the active buffer.

    Only installed when Celery isn't redirecting the stream to logging, so
    output is never captured twice.
    """

    def __init__(self, wrapped: TextIO) -> None:
        """Wrap ``wrapped`` (the original ``sys.stdout`` or ``sys.stderr``)."""
        self._wrapped = wrapped

    @property
    def wrapped(self) -> TextIO:
        """The original stream."""
        return self._wrapped

    def write(self, text: str) -> int:
        """Write to the original stream, then to the active buffer, if any."""
        written = self._wrapped.write(text)
        buffer = _active_buffer.get()
        if buffer is not None:
            try:
                buffer.append(text)
            except Exception:
                logger.debug("Failed to capture stream write", exc_info=True)
        return written

    def writelines(self, lines: Any) -> None:
        """Write each string in ``lines``."""
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        """Flush the original stream."""
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        """Forward everything else (encoding, fileno, isatty, ...) to the original."""
        return getattr(self._wrapped, name)


_handler = TaskOutputHandler()
_install_lock = threading.Lock()


def _capture_loggers() -> tuple[logging.Logger, logging.Logger]:
    return logging.getLogger(), logging.getLogger("celery.task")


def _tee(stream: TextIO) -> TextIO:
    # Under Celery's redirect, print() already reaches the logging handler.
    already_captured = isinstance(stream, LoggingProxy | TeeStream)
    if already_captured:
        return stream
    # TeeStream implements the parts of TextIO that print()/logging use and
    # forwards the rest; it isn't a TextIO subclass.
    return cast("TextIO", TeeStream(stream))


def install_capture() -> None:
    """Install the logging handler and, if needed, the stdout/stderr tee.

    Idempotent and cheap, so it's called at every ``task_prerun``: Celery's
    worker logging setup (``worker_hijack_root_logger``) clears root and
    ``celery.task`` handlers *after* ``init_worker`` has run, and user logging
    config may do the same. Re-adding at prerun also means it runs in the
    process that executes tasks (the prefork child).
    """
    with _install_lock:
        for target in _capture_loggers():
            if _handler not in target.handlers:
                target.addHandler(_handler)
        sys.stdout = _tee(sys.stdout)
        sys.stderr = _tee(sys.stderr)


def is_installed() -> bool:
    """Return True if the capture handler is on the root logger."""
    return _handler in logging.getLogger().handlers


def uninstall_capture() -> None:
    """Remove the handler and tee (tests, or turning capture off at runtime).

    Streams are only restored if they're still our tee; a stream someone else
    swapped in since is left alone.
    """
    with _install_lock:
        for target in _capture_loggers():
            target.removeHandler(_handler)
        if isinstance(sys.stdout, TeeStream):
            sys.stdout = sys.stdout.wrapped
        if isinstance(sys.stderr, TeeStream):
            sys.stderr = sys.stderr.wrapped
