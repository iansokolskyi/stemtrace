"""Celery signal handlers for task lifecycle events."""

import inspect
import logging
import threading
import traceback as tb_module
from collections import OrderedDict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypedDict

from celery.exceptions import Retry
from celery.signals import (
    after_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_revoked,
    worker_process_shutdown,
    worker_ready,
    worker_shutdown,
)
from celery.utils.serialization import UnpickleableExceptionWrapper

from stemtrace.core.events import RegisteredTaskDefinition, TaskEvent, TaskState
from stemtrace.core.exceptions import is_task_interrupt
from stemtrace.core.ports import ClosableTransport, EventTransport
from stemtrace.library.config import get_config
from stemtrace.library.output_capture import (
    install_capture,
    start_capture,
    stop_capture,
)
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
# Task IDs this process already sent a PENDING for (a task republished
# with the same ID, e.g. a retry re-queue, is announced once). A process that
# only sends tasks (a web app) never runs them, so nothing removes their IDs:
# the set keeps the most recent _PENDING_DEDUP_SIZE IDs only.
_PENDING_DEDUP_SIZE = 10_000


class _RecentIds:
    """Insertion-ordered set of IDs that forgets the oldest past a limit."""

    def __init__(self) -> None:
        """Start empty."""
        self._ids: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, task_id: object) -> bool:
        """Whether the ID is remembered."""
        return task_id in self._ids

    def __len__(self) -> int:
        """How many IDs are remembered."""
        return len(self._ids)

    def add(self, task_id: str) -> None:
        """Remember an ID, forgetting the oldest beyond the limit."""
        self._ids[task_id] = None
        while len(self._ids) > _PENDING_DEDUP_SIZE:
            self._ids.popitem(last=False)

    def discard(self, task_id: str) -> None:
        """Forget an ID if remembered."""
        self._ids.pop(task_id, None)

    def clear(self) -> None:
        """Forget every ID."""
        self._ids.clear()


_pending_emitted = _RecentIds()
_pending_emitted_lock = threading.RLock()

# Guard against overly-large docstrings bloating broker events.
_MAX_DOCSTRING_CHARS = 4000

# Celery's cancel path (lost broker connection, cold shutdown) sends task_retry
# with no reason and no einfo; mirror the reason Celery itself records.
_CANCELLED_RETRY_REASON = "cancelled by Celery"


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
    """Publish event via transport. Fire-and-forget: logs errors, never raises.

    The exception is Celery's own control flow (see ``is_task_interrupt``),
    which propagates.
    """
    if _transport is None:
        logger.warning("stemtrace not initialized, event dropped: %s", event.task_id)
        return

    try:
        _transport.publish(event)
    except Exception as exc:
        if is_task_interrupt(exc):
            # Celery's control flow (a soft time limit raised into the task
            # while it published) is not a publish failure. Celery's own signal
            # dispatcher, which calls this handler, logs and swallows what a
            # receiver raises, so this keeps stemtrace from swallowing it, not
            # Celery.
            raise
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


def _start_output_capture(task_id: str) -> None:
    """Start capturing the task's output if ``capture_output`` is enabled.

    Fire-and-forget: a capture problem never affects the task.
    """
    config = get_config()
    if config is None or not config.capture_output:
        return
    try:
        install_capture()
        start_capture(task_id, max_bytes=config.max_output_size)
    except Exception:
        logger.debug("Failed to start output capture for %s", task_id, exc_info=True)


def _finish_output_capture(task_id: str) -> str | None:
    """Stop capturing the task's output and return it (None if none).

    Called by every terminal handler. Celery sends ``task_failure`` /
    ``task_retry`` *before* ``task_postrun``, so the first call for an attempt
    gets the output and the postrun call is a no-op.
    """
    try:
        return stop_capture(task_id)
    except Exception:
        logger.debug("Failed to stop output capture for %s", task_id, exc_info=True)
        return None


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


def _exception_type(exc: BaseException | None) -> str | None:
    """The exception's class as ``module.QualName``, just ``QualName`` for builtins.

    Only the class: unlike the message, it can't contain task arguments.
    Celery replaces an exception it can't pickle with an
    ``UnpickleableExceptionWrapper`` that records the original class (celery
    5.6.3 utils/serialization.py ``get_pickleable_exception``, used for
    ``Retry.exc``); that class is reported.
    """
    if exc is None:
        return None
    if isinstance(exc, UnpickleableExceptionWrapper):
        module, name = exc.exc_module, exc.exc_cls_name
    else:
        module, name = type(exc).__module__, type(exc).__qualname__
    return name if module in (None, "builtins") else f"{module}.{name}"


def _retry_cause(reason: Any) -> BaseException | None:
    """The exception a retry was for.

    Celery sends task_retry with ``reason=`` the ``Retry`` that ``task.retry()``
    raised (celery 5.6.3 app/trace.py ``TraceInfo.handle_retry``). With
    ``retry(exc=E)``, ``Retry.exc`` is E. The usual ``except E: raise
    self.retry()`` passes no ``exc``: ``Task.retry`` raises its ``Retry`` while
    E is being handled (app/task.py, ``raise ret``), so Python chains E as the
    Retry's context; an explicit cause (``raise Retry() from E``) comes first,
    as in a traceback. A retry with neither (a polling retry) is the ``Retry``.
    """
    if not isinstance(reason, BaseException):
        return None
    # Kept typed: narrowing to celery's untyped Retry makes ``reason`` Any.
    cause: BaseException = reason
    if isinstance(reason, Retry):
        if isinstance(reason.exc, BaseException):
            return reason.exc
        chained = reason.__cause__ or (
            None if reason.__suppress_context__ else reason.__context__
        )
        if isinstance(chained, BaseException):
            return chained
    return cause


def _destination(exchange: Any, routing_key: Any) -> tuple[str | None, str | None]:
    """The queue and routing key of a task message, from how it was published.

    Unless a route names both an exchange and a routing key, or a non-direct
    exchange, Celery publishes to the default exchange ``''`` with the
    queue's name as the routing key (celery 5.6.3 and 5.2.2 app/amqp.py
    ``send_task_message``, "convert to anon-exchange"), and the broker
    delivers it to the queue of that name (kombu 5.6.1
    transport/virtual/base.py ``Channel.basic_publish``; AMQP's default
    exchange). Through a named exchange the queue isn't known: the exchange
    may route the message to any number of queues.

    Args:
        exchange: Exchange name, or a ``kombu.Exchange`` (Celery falls back
            to its default ``Exchange`` object for non-direct exchanges).
        routing_key: Routing key the message was published with.

    Returns:
        Tuple of (queue, routing_key), each None when not known.
    """
    key = routing_key if isinstance(routing_key, str) and routing_key else None
    exchange_name = getattr(exchange, "name", exchange)
    return (key if exchange_name == "" else None), key


def _delivered_destination(delivery_info: Any) -> tuple[str | None, str | None]:
    """Queue and routing key from a delivered message's ``delivery_info``.

    The broker reports the exchange and routing key the message was published
    with: kombu 5.6.1 transport/virtual/base.py
    ``Channel._inplace_augment_message`` (Redis) and amqp 5.3.1 channel.py
    ``Channel._on_basic_deliver`` (RabbitMQ). See ``_destination``.
    """
    if not isinstance(delivery_info, dict):
        return None, None
    return _destination(delivery_info.get("exchange"), delivery_info.get("routing_key"))


class _WorkerFields(TypedDict):
    """Where a worker-side event's task ran and came from."""

    hostname: str | None
    queue: str | None
    routing_key: str | None


def _worker_fields(request: Any) -> _WorkerFields:
    """Hostname, queue and routing key of a worker's task request.

    ``request`` is ``task.request`` or the worker's ``Request``: celery 5.6.3
    worker/request.py ``Request.__init__`` gives both the consumer's
    ``hostname`` and the broker's ``exchange`` and ``routing_key`` in
    ``delivery_info``. An eager ``apply()`` ran in the caller's process, not
    on a worker (``Task.apply`` sets ``hostname`` to the bare
    ``gethostname()``), so it gets no hostname.
    """
    queue, routing_key = _delivered_destination(getattr(request, "delivery_info", None))
    is_eager = getattr(request, "is_eager", False) is True
    return _WorkerFields(
        hostname=None if is_eager else _nodename(request),
        queue=queue,
        routing_key=routing_key,
    )


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


# The task receivers below take keyword-only arguments: Celery 5.6.3's
# Signal.send() (celery/utils/dispatch/signal.py) calls every receiver as
# receiver(signal=..., sender=..., **named), never with positional ones.
def _on_task_prerun(
    *,
    task_id: str,
    task: "Task",
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    **_: Any,
) -> None:
    """Handle task_prerun: publish STARTED and start capturing output."""
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
            **_worker_fields(task.request),
        )
    )
    # Started after publishing so the publish itself is never part of the output.
    _start_output_capture(task_id)


def _on_task_postrun(
    *,
    task_id: str,
    task: "Task",
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    retval: Any,
    state: str,
    **_: Any,
) -> None:
    """Handle task_postrun: publish SUCCESS with the attempt's output.

    Other states were already published by the failure/retry handlers (which
    run first); for those, and for REJECTED/IGNORED, this only frees the
    output buffer.
    """
    del args, kwargs

    output = _finish_output_capture(task_id)
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
            output=output,
            **_worker_fields(task.request),
        )
    )


def _on_task_failure(
    *,
    task_id: str,
    exception: BaseException,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    traceback: Any,
    einfo: Any,
    sender: "Task",
    **_: Any,
) -> None:
    """Handle task_failure: publish FAILURE with the attempt's output."""
    del args, kwargs, traceback

    output = _finish_output_capture(task_id)

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
            exception_type=_exception_type(exception),
            traceback=_format_traceback(einfo),
            output=output,
            **_worker_fields(sender.request),
        )
    )


def _on_task_retry(
    *,
    sender: "Task",
    request: Any,
    reason: Any = None,
    einfo: Any = None,
    **_: Any,
) -> None:
    """Handle task_retry: publish RETRY with the failed attempt's output."""
    output = _finish_output_capture(request.id)

    # reason can be an exception or string. Celery's cancel path calls
    # send_retry(task, request=..., einfo=None) without a reason.
    exc_message: str | None
    if isinstance(reason, BaseException):
        exc_message = _format_exception(reason)
    elif reason is not None:
        exc_message = str(reason)
    else:
        exc_message = _format_exception(None, einfo) or _CANCELLED_RETRY_REASON

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
            exception_type=_exception_type(_retry_cause(reason)),
            traceback=_format_traceback(einfo),
            output=output,
            **_worker_fields(request),
        )
    )


def _on_task_revoked(
    *,
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
            **_worker_fields(request),
        )
    )


def _published_message(
    body: Any, headers: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The fields of a published task message, from either protocol.

    Protocol 2 (Celery's default) keeps the metadata in the headers and the
    arguments in a ``(args, kwargs, embed)`` body; protocol 1 keeps it all in
    a dict body (and has no parent or root IDs).
    """
    if headers and headers.get("id"):
        args, kwargs = (body[0], body[1]) if isinstance(body, tuple) else ((), {})
        return {
            "id": headers["id"],
            "task": headers.get("task"),
            "retries": headers.get("retries") or 0,
            "group": headers.get("group"),
            "parent_id": headers.get("parent_id"),
            "root_id": headers.get("root_id"),
            "args": args,
            "kwargs": kwargs,
        }
    if isinstance(body, dict) and body.get("id"):
        return {
            "id": body["id"],
            "task": body.get("task"),
            "retries": body.get("retries") or 0,
            "group": body.get("taskset"),
            "parent_id": None,
            "root_id": None,
            "args": body.get("args") or (),
            "kwargs": body.get("kwargs") or {},
        }
    return None


def _retries(value: Any) -> int:
    """A message's retry count; 0 if it isn't a number (non-Celery producers)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _on_after_task_publish(
    *,
    sender: str | None = None,
    body: Any = None,
    headers: dict[str, Any] | None = None,
    exchange: Any = None,
    routing_key: Any = None,
    **_: Any,
) -> None:
    """Emit the sender's PENDING event once a task message is published.

    Fires on the sending side (``.delay()``, ``.apply_async()``, canvas
    dispatch), from the message headers, which carry the parent, root and
    group IDs. (The deprecated ``task_sent`` signal gets none of these and
    isn't used.) Eager ``apply()`` publishes nothing, so it emits no PENDING.

    A retry is republished with ``retries > 0``: no PENDING, the task is
    already tracked and gets RETRY + STARTED events. Fire-and-forget: a
    malformed message is ignored.
    """
    try:
        message = _published_message(body, headers)
    except (IndexError, KeyError, TypeError):  # not a task message we know
        return
    if message is None or _retries(message["retries"]) > 0:
        return
    task_id = str(message["id"])

    # Check-then-add under the lock: concurrent publishes of one task (or a
    # retry re-queue) announce it once.
    with _pending_emitted_lock:
        if task_id in _pending_emitted:
            return
        _pending_emitted.add(task_id)

    args, kwargs = message["args"], message["kwargs"]
    # after_task_publish gets the exchange and routing key Celery published
    # with, not the queue name (celery 5.6.3 and 5.2.2 app/amqp.py
    # send_task_message).
    queue, routing_key = _destination(exchange, routing_key)
    _publish_event(
        TaskEvent(
            task_id=task_id,
            name=message["task"] or sender or "unknown",
            state=TaskState.PENDING,
            timestamp=datetime.now(timezone.utc),
            parent_id=message["parent_id"],
            root_id=message["root_id"],
            group_id=message["group"],
            args=_scrub_and_serialize_args(tuple(args)) if args else None,
            kwargs=_scrub_and_serialize_kwargs(dict(kwargs)) if kwargs else None,
            queue=queue,
            routing_key=routing_key,
        )
    )


def connect_signals(transport: EventTransport) -> None:
    """Register signal handlers with the given transport."""
    global _transport
    _transport = transport

    after_task_publish.connect(_on_after_task_publish)
    task_prerun.connect(_on_task_prerun)
    task_postrun.connect(_on_task_postrun)
    task_failure.connect(_on_task_failure)
    task_retry.connect(_on_task_retry)
    task_revoked.connect(_on_task_revoked)

    # Worker lifecycle signals
    worker_ready.connect(on_worker_ready)
    worker_shutdown.connect(on_worker_shutdown)
    worker_process_shutdown.connect(on_worker_process_shutdown)

    logger.info("stemtrace signal handlers connected")


def disconnect_signals() -> None:
    """Disconnect all signal handlers."""
    global _transport
    _transport = None

    after_task_publish.disconnect(_on_after_task_publish)
    task_prerun.disconnect(_on_task_prerun)
    task_postrun.disconnect(_on_task_postrun)
    task_failure.disconnect(_on_task_failure)
    task_retry.disconnect(_on_task_retry)
    task_revoked.disconnect(_on_task_revoked)

    # Worker lifecycle signals
    worker_ready.disconnect(on_worker_ready)
    worker_shutdown.disconnect(on_worker_shutdown)
    worker_process_shutdown.disconnect(on_worker_process_shutdown)

    # Clear tracking state
    _pending_emitted.clear()

    logger.info("stemtrace signal handlers disconnected")


def _nodename(obj: Any) -> str | None:
    """The Celery nodename (``name@host``) of a worker, its consumer or a task request.

    All three carry the same value: a task's ``request.hostname`` is its
    consumer's ``hostname`` (celery 5.6.3 worker/strategy.py ``default``),
    which is the worker's (worker/components.py ``Consumer.create``), the
    nodename ``celery worker -n`` and ``celery multi`` set.
    """
    hostname = getattr(obj, "hostname", None)
    return hostname if isinstance(hostname, str) and hostname else None


def _get_hostname_and_pid(sender: Any) -> tuple[str, int]:
    """Extract hostname and PID from Celery worker instance.

    Args:
        sender: Worker instance (WorkController) from worker signals.

    Returns:
        Tuple of (hostname, pid).
    """
    import os

    hostname = _nodename(sender)
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
                "Worker ready event sent to transport: %s:%d (%d tasks)",
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
            logger.info("Worker shutdown event sent to transport: %s:%d", hostname, pid)
        else:
            logger.warning(
                "No transport configured, worker_shutdown event not published"
            )

    except Exception as e:
        logger.warning("Failed to publish worker_shutdown event: %s", e, exc_info=True)


def on_worker_process_shutdown(**_: Any) -> None:
    """Handle worker_process_shutdown: close this pool process's publisher connection.

    Celery sends it in a prefork child just before it exits; the child skips
    Python's exit hooks, so without this its broker connection would be
    dropped abruptly instead of closed.
    """
    transport: object = _transport  # EventTransport has no close(); it is optional
    if isinstance(transport, ClosableTransport):
        try:
            transport.close()
        except Exception:
            logger.debug("Failed to close the transport on shutdown", exc_info=True)
