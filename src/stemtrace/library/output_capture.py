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
  active buffer, if there is one.
- A :class:`TeeStream` wraps ``sys.stdout`` and ``sys.stderr`` (installed once):
  it forwards every write to the stream it wraps and appends the text to the
  active buffer. Under Celery's default ``worker_redirect_stdouts=True`` that
  stream is Celery's ``LoggingProxy``, which still logs each write chunk to
  ``celery.redirected`` for the worker log; the handler ignores those records,
  so ``print()`` is captured once, as written (``print("a", 1)`` would be four
  records but is one line of text).

Output is one combined, chronological string of stdout, stderr and log records.

The handler also stands in for ``logging.lastResort``: Python prints WARNING+
records to stderr when a logger chain has no handlers, and our handler on root
would otherwise count as one and silence them.

Limitations: output from threads the task starts itself isn't captured (new
threads start with an empty context), and output is free text, so it isn't
scrubbed for secrets.
"""

from __future__ import annotations

import logging
import sys
import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TextIO, cast

if TYPE_CHECKING:
    from types import FrameType

logger = logging.getLogger(__name__)

# Task loggers sit under "celery" but are user output.
_INCLUDED_LOGGERS = ("celery.task",)
# stemtrace's own logs and Celery/kombu internals aren't task output. This also
# covers celery.redirected: print() text is captured by the tee instead.
_EXCLUDED_LOGGERS = ("stemtrace", "celery", "kombu", "amqp", "billiard")
# Set on a record once appended / forwarded, so a record reaching the handler
# twice (via celery.task and, if it propagates, root) is handled once.
_CAPTURED_ATTR = "_stemtrace_output_captured"
_LAST_RESORT_ATTR = "_stemtrace_last_resort_done"

_active_buffer: ContextVar[OutputBuffer | None] = ContextVar(
    "stemtrace_output_buffer", default=None
)
# True while a tee is writing to the stream it wraps, or while the handler is
# printing through lastResort: writes that come back into a tee from there
# (LoggingProxy -> logging -> lastResort -> sys.stderr) are the same text, so
# they're forwarded but not captured again.
_tee_suppressed: ContextVar[bool] = ContextVar(
    "stemtrace_tee_suppressed", default=False
)


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _should_capture(logger_name: str) -> bool:
    if _matches(logger_name, _INCLUDED_LOGGERS):
        return True
    return not _matches(logger_name, _EXCLUDED_LOGGERS)


class OutputBuffer:
    """Output of one task attempt, capped to its last ``max_bytes`` UTF-8 bytes.

    The end of the output is kept (that's where a failure shows up). Bytes
    live in one ``bytearray``; the start is dropped once it holds twice the
    cap, so trimming is amortized O(1) per written byte even for many tiny
    writes, and memory stays under ``2 * max_bytes`` plus the latest write.
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
        self._data = bytearray()
        self._truncated = 0
        # Free-threaded builds can inherit the context into threads a task
        # starts, so two threads may append to one buffer.
        self._lock = threading.Lock()

    @property
    def size_bytes(self) -> int:
        """Bytes currently held (at most ``2 * max_bytes`` until rendered)."""
        return len(self._data)

    def append(self, text: str) -> None:
        """Add text, dropping the oldest bytes once well beyond the cap."""
        data = text.encode("utf-8", errors="replace")
        if not data:
            return
        with self._lock:
            self._data += data
            if len(self._data) > 2 * self._max_bytes:
                self._trim_to_cap()

    def _trim_to_cap(self) -> None:
        cut = len(self._data) - self._max_bytes
        if cut <= 0:
            return
        # Don't start the kept text in the middle of a UTF-8 sequence.
        while cut < len(self._data) and (self._data[cut] & 0xC0) == 0x80:
            cut += 1
        del self._data[:cut]
        self._truncated += cut

    def render(self) -> str | None:
        """Return the captured text, or None if the task wrote nothing.

        If output was dropped, the text starts with a
        ``[… N bytes truncated]`` line.
        """
        with self._lock:
            self._trim_to_cap()
            text = self._data.decode("utf-8", errors="replace")
            truncated = self._truncated
        text = text.rstrip("\n")
        if truncated:
            return f"[… {truncated} bytes truncated]\n{text}"
        return text or None


def _get_active_buffer() -> OutputBuffer | None:
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


def _handler_chain(record: logging.LogRecord) -> list[logging.Handler] | None:
    """Handlers a record reaches, as ``Logger.callHandlers`` walks them.

    Walks up from the record's logger while ``propagate`` is set. Returns None
    when the record's logger doesn't exist (it didn't come through a logger).
    """
    if record.name == logging.root.name:
        current: logging.Logger | None = logging.root
    else:
        found = logging.Logger.manager.loggerDict.get(record.name)
        if not isinstance(found, logging.Logger):
            return None
        current = found
    handlers: list[logging.Handler] = []
    while current is not None:
        handlers.extend(current.handlers)
        current = current.parent if current.propagate else None
    return handlers


def _has_other_handlers(record: logging.LogRecord) -> bool | None:
    """Whether the record's logger chain has a handler other than ours.

    Counts handlers regardless of their level, like ``Logger.callHandlers``
    does when deciding whether to fall back to ``logging.lastResort``.
    """
    chain = _handler_chain(record)
    if chain is None:
        return None
    return any(not isinstance(h, TaskOutputHandler) for h in chain)


_HANDLE_CODE = logging.Handler.handle.__code__
# How far up from a tee write to look for logging.Handler.handle: a handler's
# emit (possibly a subclass calling super().emit) writes to the stream.
_HANDLER_FRAME_DEPTH = 8


def _record_being_emitted() -> logging.LogRecord | None:
    """The log record whose handler is writing to the tee right now, if any.

    Called from :meth:`TeeStream.write`; looks a few frames up for
    ``logging.Handler.handle`` and returns its ``record``.
    """
    # The caller of TeeStream.write (which always has one).
    frame: FrameType | None = sys._getframe(2)
    for _ in range(_HANDLER_FRAME_DEPTH):
        if frame is None:
            return None
        if frame.f_code is _HANDLE_CODE:
            record = frame.f_locals.get("record")
            return record if isinstance(record, logging.LogRecord) else None
        frame = frame.f_back
    return None


def _tee_should_skip(record: logging.LogRecord) -> bool:
    """Whether a handler's write of ``record`` to the tee must not be captured.

    A handler whose stream is the tee (a StreamHandler created after the tee
    was installed) would otherwise copy records into the output: internal ones
    (Celery, kombu, stemtrace) that must never be output, and records our
    handler captures itself, which would appear twice. Only records our handler
    can't see (a chain without it) are left to the tee.
    """
    if not _should_capture(record.name):
        return True
    chain = _handler_chain(record) or []
    return any(isinstance(h, TaskOutputHandler) for h in chain)


class TaskOutputHandler(logging.Handler):
    """Logging handler that appends records to the active task's buffer.

    Formats records as ``LEVEL logger: message`` (with tracebacks). When it is
    the only handler a record reaches, it also passes WARNING+ records to
    ``logging.lastResort``, as Python would if the handler weren't installed.
    """

    def __init__(self) -> None:
        """Create the handler at NOTSET; logger levels decide what gets here."""
        super().__init__()
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        """Append the record to the active buffer. Never raises."""
        try:
            self._capture(record)
        except Exception:
            # Logged under "stemtrace", which this handler ignores: no recursion.
            logger.debug("Failed to capture log record", exc_info=True)
        try:
            self._forward_to_last_resort(record)
        except Exception:
            logger.debug("Failed to forward log record to lastResort", exc_info=True)

    def _capture(self, record: logging.LogRecord) -> None:
        buffer = _active_buffer.get()
        if buffer is None or getattr(record, _CAPTURED_ATTR, False):
            return
        if not _should_capture(record.name):
            return
        text = self.format(record)
        setattr(record, _CAPTURED_ATTR, True)
        buffer.append(text + "\n")

    @staticmethod
    def _forward_to_last_resort(record: logging.LogRecord) -> None:
        last_resort = logging.lastResort
        if last_resort is None or record.levelno < last_resort.level:
            return
        if getattr(record, _LAST_RESORT_ATTR, False):
            return
        if _has_other_handlers(record) is not False:
            return
        setattr(record, _LAST_RESORT_ATTR, True)
        token = _tee_suppressed.set(True)
        try:
            last_resort.handle(record)
        finally:
            _tee_suppressed.reset(token)


class TeeStream:
    """Stream wrapper that forwards writes and copies them to the active buffer.

    Wraps the real stream, or Celery's ``LoggingProxy`` when Celery redirects
    stdout/stderr to logging. Anything not defined here (``encoding``,
    ``fileno``, ``isatty``, ...) is forwarded to the wrapped stream.
    """

    def __init__(self, wrapped: TextIO) -> None:
        """Wrap ``wrapped`` (the original ``sys.stdout`` or ``sys.stderr``)."""
        self._wrapped = wrapped

    @property
    def wrapped(self) -> TextIO:
        """The original stream."""
        return self._wrapped

    def write(self, text: str) -> int:
        """Write to the wrapped stream, then to the active buffer, if any."""
        if _tee_suppressed.get():
            return self._wrapped.write(text)
        token = _tee_suppressed.set(True)
        try:
            written = self._wrapped.write(text)
        finally:
            _tee_suppressed.reset(token)
        buffer = _active_buffer.get()
        if buffer is not None:
            try:
                record = _record_being_emitted()
                if record is None or not _tee_should_skip(record):
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

    def __enter__(self) -> TeeStream:
        """Support ``with stream:`` like the wrapped stream."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Leave the context the way the wrapped stream would (files close)."""
        exit_ = getattr(self._wrapped, "__exit__", None)
        if exit_ is not None:
            exit_(*exc_info)

    def __getattr__(self, name: str) -> Any:
        """Forward everything else (encoding, fileno, isatty, ...) to the original.

        ``_wrapped`` itself and dunder names are never forwarded: ``copy`` and
        ``pickle`` probe them on a half-built instance, where forwarding would
        recurse.
        """
        if name == "_wrapped" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._wrapped, name)


_handler = TaskOutputHandler()
_install_lock = threading.Lock()


def _capture_loggers() -> tuple[logging.Logger, logging.Logger]:
    return logging.getLogger(), logging.getLogger("celery.task")


def _can_tee(stream: object) -> bool:
    """Whether ``stream`` is a writable stream we haven't wrapped yet."""
    if stream is None or isinstance(stream, TeeStream):
        return False
    if not callable(getattr(stream, "write", None)):
        return False
    writable = getattr(stream, "writable", None)
    if not callable(writable):
        return True
    try:
        return bool(writable())
    except (OSError, ValueError):
        return False


def _tee(stream: TextIO) -> TextIO:
    # None (closed fd, pythonw) and read-only streams are left as they are, so
    # print() behaves exactly as without stemtrace.
    if not _can_tee(stream):
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


def _is_installed() -> bool:
    """Return True if the capture handler is on the root logger."""
    return _handler in logging.getLogger().handlers


def _uninstall_capture() -> None:
    """Remove the handler and tee (for tests; not a supported runtime switch).

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
