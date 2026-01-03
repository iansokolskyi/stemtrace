"""Celery signal handlers for task lifecycle events."""

import inspect
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
    worker_ready,
    worker_shutdown,
)

from stemtrace.core.events import RegisteredTaskDefinition, TaskEvent, TaskState
from stemtrace.core.ports import EventTransport
from stemtrace.library.config import get_config
from stemtrace.library.scrubbing import (
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

# Guard against overly-large docstrings bloating broker events.
_MAX_DOCSTRING_CHARS = 4000


def _extract_chord_info(chord_attr: Any) -> tuple[str | None, str | None]:
    """Extract chord group_id and callback task_id from chord attribute.

    Celery's task.request.chord on HEADER tasks contains:
    - The chord callback signature with options.group_id and options.task_id

    Returns:
        Tuple of (chord_group_id, callback_task_id)
    """
    if chord_attr is None:
        return None, None

    group_id: str | None = None
    callback_id: str | None = None

    # Celery Signature object
    if hasattr(chord_attr, "options"):
        opts = chord_attr.options
        if isinstance(opts, dict):
            group_id = opts.get("group_id") or opts.get("group")
            callback_id = opts.get("task_id")

    # Dict-like (Celery chord dict: {'task': ..., 'options': {'group_id': ..., 'task_id': ...}})
    elif isinstance(chord_attr, dict):
        opts = chord_attr.get("options", {})
        if isinstance(opts, dict):
            group_id = opts.get("group_id") or opts.get("group")
            callback_id = opts.get("task_id")

    return group_id, callback_id


def _publish_event(event: TaskEvent) -> None:
    """Publish event via transport. Fire-and-forget: logs errors, never raises."""
    if _transport is None:
        logger.warning("stemtrace not initialized, event dropped: %s", event.task_id)
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
    chord_id, chord_callback_id = _extract_chord_info(
        getattr(task.request, "chord", None)
    )
    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=task.name,
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(task.request, "parent_id", None),
            root_id=getattr(task.request, "root_id", None),
            group_id=getattr(task.request, "group", None),
            chord_id=chord_id,
            chord_callback_id=chord_callback_id,
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

    chord_id, chord_callback_id = _extract_chord_info(
        getattr(task.request, "chord", None)
    )
    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=task.name,
            state=TaskState.SUCCESS,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(task.request, "parent_id", None),
            root_id=getattr(task.request, "root_id", None),
            group_id=getattr(task.request, "group", None),
            chord_id=chord_id,
            chord_callback_id=chord_callback_id,
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

    chord_id, chord_callback_id = _extract_chord_info(
        getattr(sender.request, "chord", None)
    )
    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=sender.name,
            state=TaskState.FAILURE,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(sender.request, "parent_id", None),
            root_id=getattr(sender.request, "root_id", None),
            group_id=getattr(sender.request, "group", None),
            chord_id=chord_id,
            chord_callback_id=chord_callback_id,
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

    chord_id, chord_callback_id = _extract_chord_info(getattr(request, "chord", None))
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
            group_id=getattr(request, "group", None),
            chord_id=chord_id,
            chord_callback_id=chord_callback_id,
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
    chord_id, chord_callback_id = _extract_chord_info(getattr(request, "chord", None))
    _publish_event(
        TaskEvent(
            task_id=request.id,
            name=sender.name,
            state=TaskState.REVOKED,
            timestamp=datetime.now(timezone.utc),
            parent_id=getattr(request, "parent_id", None),
            root_id=getattr(request, "root_id", None),
            group_id=getattr(request, "group", None),
            chord_id=chord_id,
            chord_callback_id=chord_callback_id,
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

    # Extract group_id from headers if available
    group_id = headers.get("group") if headers else None

    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=task_name,
            state=TaskState.PENDING,
            timestamp=datetime.now(timezone.utc),
            group_id=group_id,
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

    # Worker lifecycle signals
    worker_ready.connect(on_worker_ready)
    worker_shutdown.connect(on_worker_shutdown)

    logger.info("stemtrace signal handlers connected")


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

    # Worker lifecycle signals
    worker_ready.disconnect(on_worker_ready)
    worker_shutdown.disconnect(on_worker_shutdown)

    # Clear tracking state
    _pending_emitted.clear()

    logger.info("stemtrace signal handlers disconnected")


def _get_hostname_and_pid(sender: Any) -> tuple[str, int]:
    """Extract hostname and PID from Celery worker instance.

    Args:
        sender: Worker instance (WorkController) from worker signals.

    Returns:
        Tuple of (hostname, pid).
    """
    import os

    # Celery exposes hostname through .hostname attribute on WorkController
    hostname = getattr(sender, "hostname", None)
    if hostname is None:
        hostname = "unknown-host"
        logger.warning("Could not determine hostname from sender")

    # We're running in the worker process, so os.getpid() gives us the PID
    pid = os.getpid()

    return hostname, pid


def _extract_registered_tasks(sender: Any) -> list[str]:
    """Extract registered task names from Celery app.

    Args:
        sender: Worker instance (WorkController) from worker_ready signal.

    Returns:
        List of registered task names (fully qualified).
    """
    task_names: list[str] = []

    # Get the Celery app - it's at sender.app for worker signals
    app = getattr(sender, "app", None)
    if app is None:
        logger.warning("Could not find Celery app on sender")
        return task_names

    # Get the tasks registry from app.tasks
    tasks_registry = getattr(app, "tasks", None)
    if tasks_registry is None:
        logger.warning("Could not find tasks registry on app")
        return task_names

    # app.tasks is a dict-like registry: {name: Task}
    # Filter out internal Celery tasks (celery.*)
    for name in tasks_registry:
        if isinstance(name, str) and not name.startswith("celery."):
            task_names.append(name)

    logger.info("Extracted %d registered tasks for worker", len(task_names))
    return task_names


def _extract_task_definitions(sender: Any) -> dict[str, RegisteredTaskDefinition]:
    """Extract task metadata (docstring/signature/module/bound) from Celery app.

    This runs on the worker in response to `worker_ready`, so we have access to
    the actual Task objects. The server generally does not import user task code,
    so this is the authoritative source for registry metadata.

    Args:
        sender: Worker instance (WorkController) from worker_ready signal.

    Returns:
        Mapping of task name to RegisteredTaskDefinition.
    """
    definitions: dict[str, RegisteredTaskDefinition] = {}

    app = getattr(sender, "app", None)
    if app is None:
        return definitions

    tasks_registry = getattr(app, "tasks", None)
    if tasks_registry is None:
        return definitions

    # tasks_registry is dict-like: {name: Task}
    for name, task_obj in getattr(tasks_registry, "items", lambda: [])():
        if not isinstance(name, str) or name.startswith("celery."):
            continue
        if task_obj is None:
            continue

        run_callable = getattr(task_obj, "run", None)
        module: str | None = None
        docstring: str | None = None
        signature: str | None = None

        # Best-effort: prefer module/docstring from the task implementation (run).
        if run_callable is not None:
            module = getattr(run_callable, "__module__", None)
            docstring = inspect.getdoc(run_callable)
            try:
                signature = str(inspect.signature(run_callable))
            except (TypeError, ValueError):
                signature = None

        # Fall back to task object/module if needed.
        if module is None:
            module = getattr(task_obj, "__module__", None)

        # Bound tasks are declared via @app.task(bind=True). Celery exposes this
        # on the Task instance as `bind`. Only treat an actual bool as authoritative
        # to avoid MagicMock truthiness in tests.
        bind_attr = getattr(task_obj, "bind", None)
        bound = bind_attr if isinstance(bind_attr, bool) else False

        if docstring is not None and len(docstring) > _MAX_DOCSTRING_CHARS:
            docstring = docstring[:_MAX_DOCSTRING_CHARS] + "\n\n[truncated]"

        definitions[name] = RegisteredTaskDefinition(
            name=name,
            module=module,
            signature=signature,
            docstring=docstring,
            bound=bound,
        )

    return definitions


def on_worker_ready(sender: Any, **_: Any) -> None:
    """Handle worker_ready signal - publish worker registration event.

    Celery sends this when worker starts and loads all registered tasks.
    We capture hostname, PID, and the full task registry.

    Args:
        sender: Worker instance (WorkController).
    """
    global _transport

    try:
        hostname, pid = _get_hostname_and_pid(sender)
        registered_tasks = _extract_registered_tasks(sender)
        task_definitions = _extract_task_definitions(sender)

        # Publish worker lifecycle event using the global transport
        # This goes to the same stream as task events for unified consumption
        from stemtrace.core.events import WorkerEvent, WorkerEventType

        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname=hostname,
            pid=pid,
            timestamp=datetime.now(timezone.utc),
            registered_tasks=registered_tasks,
            task_definitions=task_definitions,
        )

        if _transport is not None:
            _transport.publish(event)
            logger.info(
                "Worker ready event published: %s:%d (%d tasks)",
                hostname,
                pid,
                len(registered_tasks),
            )
        else:
            logger.warning("No transport configured, worker_ready event not published")

    except Exception as e:
        logger.warning("Failed to publish worker_ready event: %s", e, exc_info=True)


def on_worker_shutdown(sender: Any = None, sig: int | None = None, **_: Any) -> None:
    """Handle worker_shutdown signal - publish worker shutdown event.

    Celery sends this on graceful worker shutdown.

    Args:
        sender: Worker instance (WorkController).
        sig: Signal number causing shutdown (keyword arg from Celery).
    """
    global _transport
    del sig  # Unused but provided by Celery signal

    try:
        hostname, pid = _get_hostname_and_pid(sender)

        from stemtrace.core.events import WorkerEvent, WorkerEventType

        event = WorkerEvent(
            event_type=WorkerEventType.WORKER_SHUTDOWN,
            hostname=hostname,
            pid=pid,
            timestamp=datetime.now(timezone.utc),
            shutdown_time=datetime.now(timezone.utc),
        )

        if _transport is not None:
            _transport.publish(event)
            logger.info("Worker shutdown event published: %s:%d", hostname, pid)
        else:
            logger.warning(
                "No transport configured, worker_shutdown event not published"
            )

    except Exception as e:
        logger.warning("Failed to publish worker_shutdown event: %s", e, exc_info=True)
