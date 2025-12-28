"""Celery signal handlers for task lifecycle events."""

import logging
import threading
import traceback as tb_module
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_revoked,
    task_sent,
)

from celery_flow.core.events import TaskEvent, TaskState
from celery_flow.core.ports import EventTransport
from celery_flow.library.config import get_config
from celery_flow.library.scrubbing import (
    DEFAULT_SENSITIVE_KEYS,
    safe_serialize,
    scrub_args,
    scrub_dict,
)

if TYPE_CHECKING:
    from celery import Task

logger = logging.getLogger(__name__)

_transport: EventTransport | None = None
# Track task IDs that have received PENDING to avoid duplicates from retries
_pending_emitted: set[str] = set()
_pending_emitted_lock = threading.RLock()


def _publish_event(event: TaskEvent) -> None:
    """Publish event via transport. Fire-and-forget: logs errors, never raises."""
    if _transport is None:
        logger.warning("celery-flow not initialized, event dropped: %s", event.task_id)
        return

    try:
        _transport.publish(event)
    except Exception:
        logger.warning(
            "Failed to publish event for task %s", event.task_id, exc_info=True
        )


def _get_scrub_config() -> tuple[
    frozenset[str], frozenset[str] | None, int, bool, bool, bool
]:
    """Get scrubbing configuration.

    Returns:
        Tuple of (sensitive_keys, safe_keys, max_size, scrub_enabled,
                  capture_args, capture_result)
    """
    config = get_config()
    if config is None:
        return (DEFAULT_SENSITIVE_KEYS, None, 10240, True, True, True)

    if config.scrub_sensitive_data:
        sensitive = DEFAULT_SENSITIVE_KEYS | config.additional_sensitive_keys
        safe = config.safe_keys if config.safe_keys else None
    else:
        sensitive = frozenset()
        safe = None

    return (
        sensitive,
        safe,
        config.max_data_size,
        config.scrub_sensitive_data,
        config.capture_args,
        config.capture_result,
    )


def _scrub_and_serialize_args(
    args: tuple[Any, ...],
) -> list[Any] | None:
    """Scrub and serialize positional arguments."""
    sensitive, safe, max_size, scrub_enabled, capture_args, _ = _get_scrub_config()
    if not capture_args:
        return None

    if scrub_enabled:
        scrubbed = scrub_args(args, sensitive, safe_keys=safe)
    else:
        scrubbed = list(args)

    result: Any = safe_serialize(scrubbed, max_size, sensitive, safe_keys=safe)
    # safe_serialize may return truncation message string or the list
    if isinstance(result, list):
        return result
    return [result] if result is not None else scrubbed


def _scrub_and_serialize_kwargs(
    kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """Scrub and serialize keyword arguments."""
    sensitive, safe, max_size, scrub_enabled, capture_args, _ = _get_scrub_config()
    if not capture_args:
        return None

    if scrub_enabled:
        scrubbed = scrub_dict(kwargs, sensitive, safe_keys=safe)
    else:
        scrubbed = kwargs

    result: Any = safe_serialize(scrubbed, max_size, sensitive, safe_keys=safe)
    # safe_serialize may return truncation message string or the dict
    if isinstance(result, dict):
        return result
    # If truncated, wrap message in dict
    return {"_truncated": result} if result is not None else scrubbed


def _scrub_and_serialize_result(result: Any) -> Any | None:
    """Scrub and serialize task result."""
    sensitive, safe, max_size, _, _, capture_result = _get_scrub_config()
    if not capture_result:
        return None

    return safe_serialize(result, max_size, sensitive, safe_keys=safe)


def _format_exception(exc: BaseException | None, einfo: Any = None) -> str | None:
    """Format exception to a string message."""
    if exc is not None:
        return f"{type(exc).__name__}: {exc}"
    if einfo is not None:
        return str(einfo.exception) if hasattr(einfo, "exception") else str(einfo)
    return None


def _format_traceback(einfo: Any = None) -> str | None:
    """Format traceback from exception info."""
    if einfo is None:
        return None

    # billiard ExceptionInfo has traceback attribute
    if hasattr(einfo, "traceback"):
        tb_str: str = einfo.traceback
        return tb_str
    # Standard exception info tuple
    if hasattr(einfo, "tb"):
        return "".join(tb_module.format_tb(einfo.tb))
    return None


def _on_task_prerun(
    task_id: str,
    task: "Task",
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    **_: Any,
) -> None:
    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=task.name,
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(task.request, "parent_id", None),
            root_id=getattr(task.request, "root_id", None),
            retries=task.request.retries or 0,
            args=_scrub_and_serialize_args(args),
            kwargs=_scrub_and_serialize_kwargs(kwargs),
        )
    )


def _on_task_postrun(
    task_id: str,
    task: "Task",
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    retval: Any,
    state: str,
    **_: Any,
) -> None:
    del args, kwargs
    if state != "SUCCESS":
        return

    # Clean up PENDING tracking
    _pending_emitted.discard(task_id)

    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=task.name,
            state=TaskState.SUCCESS,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(task.request, "parent_id", None),
            root_id=getattr(task.request, "root_id", None),
            retries=task.request.retries or 0,
            result=_scrub_and_serialize_result(retval),
        )
    )


def _on_task_failure(
    task_id: str,
    exception: BaseException,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    traceback: Any,
    einfo: Any,
    sender: "Task",
    **_: Any,
) -> None:
    del args, kwargs, traceback

    # Clean up PENDING tracking
    _pending_emitted.discard(task_id)

    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=sender.name,
            state=TaskState.FAILURE,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(sender.request, "parent_id", None),
            root_id=getattr(sender.request, "root_id", None),
            retries=sender.request.retries or 0,
            exception=_format_exception(exception),
            traceback=_format_traceback(einfo),
        )
    )


def _on_task_retry(
    sender: "Task",
    request: Any,
    reason: Any,
    einfo: Any,
    **_: Any,
) -> None:
    # reason can be an exception or string
    exc_message: str | None = None
    if isinstance(reason, BaseException):
        exc_message = _format_exception(reason)
    elif reason is not None:
        exc_message = str(reason)

    # Use current retry count (not +1) so RETRY groups with the STARTED that failed
    # Timeline: STARTED(0) → RETRY(0) → STARTED(1) → RETRY(1) → ...
    _publish_event(
        TaskEvent(
            task_id=request.id,
            name=sender.name,
            state=TaskState.RETRY,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(request, "parent_id", None),
            root_id=getattr(request, "root_id", None),
            retries=request.retries or 0,
            exception=exc_message,
            traceback=_format_traceback(einfo),
        )
    )


def _on_task_revoked(
    request: Any,
    terminated: bool,
    signum: int | None,
    expired: bool,
    sender: "Task",
    **_: Any,
) -> None:
    del terminated, signum, expired
    _publish_event(
        TaskEvent(
            task_id=request.id,
            name=sender.name,
            state=TaskState.REVOKED,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(request, "parent_id", None),
            root_id=getattr(request, "root_id", None),
            retries=getattr(request, "retries", 0) or 0,
        )
    )


def _on_task_sent(
    sender: str | None = None,
    task_id: str | None = None,
    task: str | None = None,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    """Handle task_sent signal - fires when .delay() or .apply_async() is called.

    Note: This fires on the SENDER side (where the task is queued), not on the worker.
    It captures the PENDING state before a worker picks up the task.

    For retries, we skip emitting PENDING since the task is already tracked
    and will get RETRY + STARTED events.
    """
    if task_id is None:
        return

    # Skip PENDING for retries - check headers first, then our tracking set
    if headers and headers.get("retries", 0) > 0:
        return

    # Skip if we've already emitted PENDING for this task (handles retry re-queues)
    # Use lock to make check-then-add atomic and prevent duplicates from concurrent threads
    with _pending_emitted_lock:
        if task_id in _pending_emitted:
            return
        _pending_emitted.add(task_id)

    task_name = task or sender or "unknown"

    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=task_name,
            state=TaskState.PENDING,
            timestamp=datetime.now(timezone.utc),
            args=_scrub_and_serialize_args(args) if args else None,
            kwargs=_scrub_and_serialize_kwargs(kwargs) if kwargs else None,
        )
    )


def connect_signals(transport: EventTransport) -> None:
    """Register signal handlers with the given transport."""
    global _transport
    _transport = transport

    task_sent.connect(_on_task_sent)
    task_prerun.connect(_on_task_prerun)
    task_postrun.connect(_on_task_postrun)
    task_failure.connect(_on_task_failure)
    task_retry.connect(_on_task_retry)
    task_revoked.connect(_on_task_revoked)

    # Set publisher for bootsteps (RECEIVED events)
    from celery_flow.library.bootsteps import _set_publisher

    _set_publisher(_publish_event)

    logger.info("celery-flow signal handlers connected")


def disconnect_signals() -> None:
    """Disconnect all signal handlers."""
    global _transport
    _transport = None

    task_sent.disconnect(_on_task_sent)
    task_prerun.disconnect(_on_task_prerun)
    task_postrun.disconnect(_on_task_postrun)
    task_failure.disconnect(_on_task_failure)
    task_retry.disconnect(_on_task_retry)
    task_revoked.disconnect(_on_task_revoked)

    # Clear tracking state
    _pending_emitted.clear()

    logger.info("celery-flow signal handlers disconnected")
