"""REST API endpoints for stemtrace."""

from __future__ import annotations

import contextlib
import functools
import logging
import re
import threading
import time

# FastAPI evaluates the Query annotations below at runtime, so datetime must be
# a real import, not a type-checking one.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Annotated, NamedTuple

from celery import Celery
from fastapi import APIRouter, HTTPException, Query
from fastapi.routing import APIRoute

# Not asyncio.to_thread: under uvloop (which uvicorn picks when installed) that
# goes through loop.run_in_executor, which on Python 3.14 calls the deprecated
# asyncio.iscoroutinefunction (uvloop 0.22.1). anyio's worker threads don't.
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from stemtrace.core.anomalies import DEFAULT_SETTINGS, AnomalySettings
from stemtrace.core.graph import (
    ChordNotRunReason,
    NodeType,
    chord_not_run_reasons,
)
from stemtrace.core.groups import GroupConfig, ResolvedGroup, TaskAttributes
from stemtrace.server.api import params
from stemtrace.server.api.anomaly_routes import (
    AnomalyEvaluator,
    add_anomaly_routes,
    anomaly_response,
    kind_name,
)
from stemtrace.server.api.schemas import (
    AnomalyKind,
    ErrorResponse,
    FlowSummaryResponse,
    GraphListResponse,
    GraphNodeResponse,
    GraphResponse,
    HealthResponse,
    LivenessResponse,
    RegisteredTaskResponse,
    TaskDetailResponse,
    TaskEventResponse,
    TaskGroupListResponse,
    TaskGroupResponse,
    TaskListResponse,
    TaskNodeResponse,
    TaskRegistryResponse,
    TaskStatus,
    WorkerListResponse,
    WorkerResponse,
)
from stemtrace.server.api.summary_routes import add_summary_routes
from stemtrace.server.groups import GroupsInput, build_group_config, group_lookup
from stemtrace.server.inspect_probe import (
    INSPECT_REFRESH_MIN_INTERVAL_SECONDS,
    InspectProbe,
)
from stemtrace.transports import broker_kind

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from celery.app.control import Inspect
    from starlette.types import Receive, Scope, Send

    from stemtrace.core.anomalies import Anomaly
    from stemtrace.core.events import TaskEvent
    from stemtrace.core.graph import TaskNode
    from stemtrace.server.consumer import AsyncEventConsumer
    from stemtrace.server.store import GraphStore, WorkerRegistry, WorkerTimeline
    from stemtrace.server.websocket import WebSocketManager

logger = logging.getLogger(__name__)

# Workers never register these (library/signals.py filters the same prefix).
_CELERY_BUILTIN_PREFIX = "celery."

# Celery 5.6.3 worker/control.py registered() appends "name [rate_limit=10/m ...]"
# for the DEFAULT_TASK_INFO_ITEMS a task sets.
_TASK_INFO_SUFFIX = re.compile(r" \[(?:exchange|routing_key|rate_limit)=.*\]\Z")


def _monotonic() -> float:
    """Return a monotonic clock reading.

    Wrapped to make rate-limiting logic testable without monkeypatching the
    global stdlib `time.monotonic()` used by Starlette/AnyIO internals.
    """
    return time.monotonic()


def _pid_from_stats_payload(payload: object) -> int | None:
    """Extract PID from Celery inspect stats payload."""
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if isinstance(pid, int) and pid > 0:
        return pid
    return None


def _tasks_for_worker_key(
    registered: dict[str, object] | None,
    worker_key: str,
) -> list[str] | None:
    """Return registered task names for a worker key.

    Returns None when this node gave no list (no reply at all, or it missed
    the registered() broadcast), so callers can preserve its existing task list.
    """
    if not registered or not isinstance(registered, dict):
        return None
    tasks_obj = registered.get(worker_key)
    if not isinstance(tasks_obj, list):
        return None
    tasks: list[str] = []
    for t in tasks_obj:
        if isinstance(t, str) and not t.startswith(_CELERY_BUILTIN_PREFIX):
            tasks.append(_TASK_INFO_SUFFIX.sub("", t))
    return tasks


def _event_to_response(event: TaskEvent, include_output: bool) -> TaskEventResponse:
    """Convert a TaskEvent to its API model, optionally dropping its output."""
    response = TaskEventResponse.model_validate(event)
    if not include_output and response.output is not None:
        response = response.model_copy(update={"output": None})
    return response


def _group_of(
    node: TaskNode, group_of: Callable[[str], ResolvedGroup]
) -> tuple[str | None, list[str]]:
    """Return a node's task group name and tags; synthetic nodes have none.

    Resolved from the task name, never the alias-resolved display name.
    """
    if node.node_type != NodeType.TASK:
        return None, []
    group = group_of(node.name)
    return group.name, list(group.tags)


async def _filter_task_names(
    store: GraphStore,
    group_of: Callable[[str], ResolvedGroup],
    task_group: str | None,
    tag: str | None,
) -> frozenset[str] | None:
    """Return the stored task names that pass ``task_group`` and ``tag``; None without either.

    Only a filtered request resolves names, in a worker thread (see
    ``_group_task_names``); an unfiltered list resolves none, so it doesn't use one.
    """
    if task_group is None and tag is None:
        return None
    return await run_in_threadpool(_group_task_names, store, group_of, task_group, tag)


def _group_task_names(
    store: GraphStore,
    group_of: Callable[[str], ResolvedGroup],
    task_group: str | None,
    tag: str | None,
) -> frozenset[str]:
    """Return the stored task names that pass the ``task_group`` and ``tag`` filters.

    ``task_group`` matches the group's name exactly; ``tag`` matches when it
    is one of the group's tags; given both, a name must match both (an empty
    value matches nothing, like the other exact filters). Names are resolved
    here, outside the store lock, once each; a task whose name is first seen
    after this snapshot is matched from the next request on.

    Runs in a worker thread (``run_in_threadpool``): with thousands of
    distinct names it takes long enough (about 80 ms at 7,000) to hold up
    every other request on the event loop. ``group_of``'s cache is
    thread-safe.
    """

    def accepts(task_name: str) -> bool:
        """Whether tasks named ``task_name`` pass both filters."""
        group = group_of(task_name)
        if task_group is not None and group.name != task_group:
            return False
        return tag is None or tag in group.tags

    return frozenset(filter(accepts, store.get_unique_task_names()))


_NO_ANOMALIES: Mapping[str, Anomaly] = {}


def _anomaly_of(node: TaskNode, anomalies: Mapping[str, Anomaly]) -> Anomaly | None:
    """The node's anomaly; GROUP/CHORD nodes never have one."""
    return anomalies.get(node.task_id) if node.node_type == NodeType.TASK else None


def _anomaly_kind(
    node: TaskNode, anomalies: Mapping[str, Anomaly]
) -> AnomalyKind | None:
    """The kind of the node's anomaly, for the ``anomaly`` field."""
    anomaly = _anomaly_of(node, anomalies)
    return kind_name(anomaly) if anomaly is not None else None


def _node_to_response(
    node: TaskNode,
    *,
    include_output: bool = False,
    include_events: bool = True,
    group_of: Callable[[str], ResolvedGroup],
    anomalies: Mapping[str, Anomaly] = _NO_ANOMALIES,
) -> TaskNodeResponse:
    """Convert TaskNode to API response model.

    Args:
        node: Task node from graph store.
        include_output: Keep captured task output on events. Off for list
            responses: output can be up to ``max_output_size`` per attempt and
            only the task detail page shows it.
        include_events: Embed the events; off for ``compact=1`` lists, which
            then carry ``events: null``.
        group_of: Task group by task name (see ``group_lookup``).
        anomalies: The flagged tasks by task ID (see ``AnomalyEvaluator``).

    Returns:
        TaskNodeResponse with timestamp and duration.
    """
    first_seen = node.first_seen
    last_updated = node.last_updated

    duration_ms = None
    if first_seen and last_updated and first_seen != last_updated:
        duration_ms = int((last_updated - first_seen).total_seconds() * 1000)
    runtime_ms, total_runtime_ms = node.run_times()

    task_group, tags = _group_of(node, group_of)
    return TaskNodeResponse(
        task_id=node.task_id,
        name=node.name,
        state=node.state,
        node_type=node.node_type,
        group_id=node.group_id,
        chord_id=node.chord_id,
        parent_id=node.parent_id,
        reported_parent_id=node.reported_parent_id,
        children=node.children,
        task_group=task_group,
        tags=tags,
        events=[_event_to_response(e, include_output) for e in node.events]
        if include_events
        else None,
        first_seen=first_seen,
        last_updated=last_updated,
        duration_ms=duration_ms,
        queue_wait_ms=node.queue_wait_ms,
        eta=node.eta,
        runtime_ms=runtime_ms,
        total_runtime_ms=total_runtime_ms,
        exception_type=node.exception_type,
        hostname=node.hostname,
        queue=node.queue,
        routing_key=node.routing_key,
        anomaly=_anomaly_kind(node, anomalies),
    )


def _resolve_node_alias(node: TaskNode, key: str | None) -> str:
    """Resolve display name for a graph node from task arguments.

    When a key is configured, extracts a value from the args (if key is a
    digit string) or kwargs (if key is a regular string) of the first event
    that carries them. Not every event does (e.g. RECEIVED has no arguments),
    so the leading event can't be relied on. Falls back to the node's
    registered task name.

    Args:
        node: Task node containing events with args/kwargs.
        key: Digit string for args[index], string for kwargs[key],
            or None to use the task name.

    Returns:
        Resolved display name for the node.
    """
    if key is None:
        return node.name

    if key.isdigit():
        index = int(key)
        args = next((e.args for e in node.events if isinstance(e.args, list)), None)
        if args is not None and index < len(args):
            return str(args[index])
    else:
        kwargs = next(
            (e.kwargs for e in node.events if isinstance(e.kwargs, dict)), None
        )
        if kwargs is not None and key in kwargs:
            return str(kwargs[key])

    return node.name


def _node_to_graph_response(
    node: TaskNode,
    all_nodes: dict[str, TaskNode] | None = None,
    node_alias_key: str | None = None,
    *,
    group_of: Callable[[str], ResolvedGroup],
    not_run_reason: ChordNotRunReason | None = None,
    flow: FlowSummaryResponse | None = None,
    anomalies: Mapping[str, Anomaly] = _NO_ANOMALIES,
) -> GraphNodeResponse:
    """Convert TaskNode to graph response model.

    For synthetic nodes (GROUP/CHORD), compute timing from children.

    Args:
        node: Task node from graph store.
        all_nodes: All nodes dict for child lookup.
        node_alias_key: Key to resolve display name from task arguments.
        group_of: Task group by task name (see ``group_lookup``).
        not_run_reason: Why a CHORD's callback hasn't run; only known
            when the caller has the chord's whole graph (see
            ``chord_not_run_reasons``).
        flow: The flow summary of a root in the graph list.
        anomalies: The flagged tasks by task ID (see ``AnomalyEvaluator``).

    Returns:
        GraphNodeResponse with timing from children.
    """
    first_seen = node.first_seen
    last_updated = node.last_updated

    # For synthetic nodes (GROUP/CHORD), compute timing from children
    if not node.events and node.children and all_nodes:
        child_first_seen: list[datetime] = []
        child_last_updated: list[datetime] = []
        for child_id in node.children:
            child = all_nodes.get(child_id)
            if child and child.first_seen and child.last_updated:
                child_first_seen.append(child.first_seen)
                child_last_updated.append(child.last_updated)
        if child_first_seen:
            first_seen = min(child_first_seen)
        if child_last_updated:
            last_updated = max(child_last_updated)

    duration_ms = None
    if first_seen and last_updated and first_seen != last_updated:
        duration_ms = int((last_updated - first_seen).total_seconds() * 1000)
    runtime_ms, total_runtime_ms = node.run_times()

    # UI traversal is order-sensitive when callbacks can appear as both direct
    # children and CHORD-linked nodes. Prefer TASK children first so callback
    # branches render before synthetic containers.
    children = node.children
    if all_nodes is not None:
        children = sorted(
            node.children,
            key=lambda c: getattr(all_nodes.get(c), "node_type", None) != "TASK",
        )

    task_group, tags = _group_of(node, group_of)
    return GraphNodeResponse(
        task_id=node.task_id,
        name=_resolve_node_alias(node, node_alias_key),
        state=node.state,
        node_type=node.node_type,
        group_id=node.group_id,
        chord_id=node.chord_id,
        parent_id=node.parent_id,
        reported_parent_id=node.reported_parent_id,
        children=children,
        task_group=task_group,
        tags=tags,
        duration_ms=duration_ms,
        queue_wait_ms=node.queue_wait_ms,
        eta=node.eta,
        runtime_ms=runtime_ms,
        total_runtime_ms=total_runtime_ms,
        first_seen=first_seen,
        last_updated=last_updated,
        exception_type=node.exception_type,
        hostname=node.hostname,
        queue=node.queue,
        routing_key=node.routing_key,
        not_run_reason=not_run_reason,
        flow=flow,
        anomaly=_anomaly_kind(node, anomalies),
    )


# kombu 5.6.2, celery 5.6.3, amqp 5.3.1, redis 8.1.0. Inspect reads
# ``Connection.default_channel``, whose ``_ensure_connection`` runs ``retry_over_time``
# (kombu/utils/functional.py) with ``timeout=connect_timeout`` and ``max_retries=None``: the
# deadline is checked only after an attempt fails, with a 2 s sleep between attempts, so a
# refused port costs 3 attempts (about 6 s) per call. ``transport_options['max_retries']``
# (read by ``_extract_failover_opts``) set to 0 makes the first failure raise. One attempt is
# bounded by the socket connect timeout: amqp ``Transport._connect`` calls
# ``sock.settimeout(connect_timeout)``; for Redis, kombu's ``socket_connect_timeout`` (default
# None: the OS TCP timeout, about 2 minutes) goes to redis-py. Name resolution is unbounded.
# After the connect, nothing bounds a read by default: kombu's Redis ``Channel.__init__`` pings
# and ``_size`` reads with ``socket_timeout`` None, and amqp ``Transport._read`` blocks on a
# socket set back to blocking mode (``_init_socket``), so a broker that goes quiet hangs the
# attempt. ``socket_timeout`` (Redis) and ``read_timeout``/``write_timeout`` (amqp, passed by
# kombu's ``establish_connection``) bound each read, above the 0.5 s reply wait. A call
# that times out still costs about 2.5 s on Redis and 3 s on RabbitMQ (its connect, then
# the read timeout, measured), so a whole attempt ends within about 5 s.
_INSPECT_CONNECT_TIMEOUT_SECONDS = 1.5
_INSPECT_IO_TIMEOUT_SECONDS = 1.0
_INSPECT_CONNECT_BOUNDS: dict[str, object] = {
    "broker_connection_timeout": _INSPECT_CONNECT_TIMEOUT_SECONDS,
    "broker_transport_options": {
        "max_retries": 0,
        "socket_connect_timeout": _INSPECT_CONNECT_TIMEOUT_SECONDS,
        "socket_timeout": _INSPECT_IO_TIMEOUT_SECONDS,
        "read_timeout": _INSPECT_IO_TIMEOUT_SECONDS,
        "write_timeout": _INSPECT_IO_TIMEOUT_SECONDS,
    },
}


def _get_inspector(
    broker_url: str | None,
    *,
    timeout_seconds: float = 0.5,
) -> Inspect | None:
    """Get Celery inspector for on-demand worker status.

    Args:
        broker_url: Broker URL to create minimal Celery app.
        timeout_seconds: Timeout for Celery inspect broadcasts.

    Returns:
        Inspect instance, or None if broker_url not provided.
    """
    if broker_url is None:
        return None
    # IMPORTANT: Celery's constructor signature is positional-heavy and has
    # changed across versions. Always pass `broker` by keyword to avoid
    # accidentally treating this as `loader=...` (which can import stdlib
    # modules like `inspect` and crash during app finalization).
    try:
        app = Celery("stemtrace", broker=broker_url, changes=_INSPECT_CONNECT_BOUNDS)
        # Celery's Inspect signature differs by version. Newer versions accept
        # `timeout=...`, older versions raise TypeError.
        with contextlib.suppress(TypeError):
            return app.control.inspect(timeout=timeout_seconds)
        return app.control.inspect()
    except Exception:
        logger.warning(
            "Failed to create Celery inspector for broker %s",
            broker_url,
            exc_info=True,
        )
        return None


class _InspectOutcome(NamedTuple):
    """What one refresh learned: who answered, and whether every call finished."""

    answered: frozenset[str]
    complete: bool


def _refresh_worker_registry_from_inspect(
    worker_registry: WorkerRegistry,
    inspector: Inspect,
) -> _InspectOutcome | None:
    """Refresh worker registry from Celery inspect (best-effort).

    This supports "late join" scenarios where the server missed worker_ready
    events (e.g., RabbitMQ fanout with no bound queue at worker startup).

    The refresh is intentionally best-effort and must never raise.

    Inspect replies are keyed by Celery nodename (``name@host``), the same
    name worker events carry, so the keys are used as-is. Shortening them to
    the host listed every worker twice and couldn't tell ``celery multi``
    nodes on one host apart.

    Args:
        worker_registry: Worker registry to populate/update.
        inspector: Celery inspector to query.

    Returns:
        The nodenames that answered (stats, else ping) and whether every call
        completed, or None when ``stats()`` failed (the caller backs off).
    """
    # The replies describe the workers as of now, but the consumer keeps
    # applying worker events while inspect waits (up to 0.5 s per call).
    seen = worker_registry.event_generations()

    # We need PID to register a worker. `stats()` typically includes it.
    stats: dict[str, object] | None = None
    registered: dict[str, object] | None = None

    try:
        stats = inspector.stats()
        # Celery 5.6.3 Inspect._prepare returns None for an empty reply; kombu
        # 5.6.2 Mailbox._broadcast -> Connection.default_channel raises when the
        # broker is unreachable. So returning at all means the broadcast ran.
    except Exception:
        # Whatever the class: registered() and ping() would each run the whole
        # connect (or wait out a read timeout) again, so stop at the first failure.
        logger.debug("inspect stats() failed", exc_info=True)
        return None

    complete = True
    try:
        registered = inspector.registered()
    except Exception:  # noqa: BLE001  best-effort: a failed call applies what stats() gave
        complete = False
        if not stats:
            # A broken connection: ping() would only wait out another timeout. The empty
            # stats() still stamps the broadcast, as when no one answers a ping.
            worker_registry.apply_ping_reply((), stats_completed=True)
            return _InspectOutcome(frozenset(), complete)

    # Fallback: if stats is unavailable, at least refresh online status via ping().
    # This keeps the Workers tab responsive even for minimal Celery setups.
    if not stats:
        active: dict[str, object] | None = None
        try:
            active = inspector.ping()
        except Exception:  # noqa: BLE001  best-effort: same as registered()
            complete = False
        # An empty stats() that completed still stamps the broadcast, after the
        # pings: nobody answered, which is evidence for silent workers.
        worker_registry.apply_ping_reply(active or (), stats_completed=True)
        return _InspectOutcome(frozenset(active or ()), complete)

    # If we got a non-empty stats response, workers that didn't answer go
    # offline (an unanswered inspect doesn't prove the process closed, so a
    # later ping may revive them), and each node's one live process is the pid
    # stats names: its older pids retire (a worker killed without
    # worker_shutdown, then restarted). A node that answers without a pid is
    # treated like a ping reply.
    replies: dict[str, tuple[int | None, list[str] | None]] = {}
    for nodename, payload in stats.items():
        pid = _pid_from_stats_payload(payload)
        tasks = _tasks_for_worker_key(registered, nodename) if pid else None
        replies[nodename] = (pid, tasks)
    worker_registry.apply_stats_reply(seen, replies)
    return _InspectOutcome(frozenset(replies), complete)


def _is_consumer_degraded(consumer: AsyncEventConsumer | None) -> bool:
    """Whether a configured event consumer has failed, for both health endpoints.

    Degraded when the consumer stopped unexpectedly (not running, yet neither
    stopped on purpose nor finished normally) or can't read from the broker.
    A disabled consumer (None) is never degraded.
    """
    if consumer is None:
        return False
    stopped_unexpectedly = not (
        consumer.is_running or consumer.is_finished or consumer.is_stopped
    )
    return stopped_unexpectedly or consumer.is_connected is False


_INSPECT_REFRESH_MIN_INTERVAL_SECONDS = INSPECT_REFRESH_MIN_INTERVAL_SECONDS
_INSPECT_TIMEOUT_SECONDS = 0.5
_INSPECT_BACKOFF_START_SECONDS = 2.0
_INSPECT_BACKOFF_MAX_SECONDS = 60.0
_INSPECT_FAILURES_MAX = 64


class _InspectRefresher:
    """Rate-limited, non-blocking worker registry refresh from Celery inspect.

    Runs at most one refresh at a time and at most one per
    ``_INSPECT_REFRESH_MIN_INTERVAL_SECONDS``. A request that finds a refresh
    in progress, or one still paced out, returns at once instead of waiting.
    A failed attempt counts too: the next waits 2, 4, 8 ... up to 60 s, and a
    success resets that. The probe and every route read the same clock.
    """

    def __init__(
        self, worker_registry: WorkerRegistry | None, broker_url: str | None
    ) -> None:
        """Bind the registry to refresh and the broker to inspect (either None disables)."""
        self._worker_registry = worker_registry
        self._broker_url = broker_url
        self._lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._last_attempt: float = 0.0
        self._wait_s = _INSPECT_REFRESH_MIN_INTERVAL_SECONDS
        self._failures = 0

    def _paced_out(self) -> bool:
        with self._pace_lock:
            return _monotonic() - self._last_attempt < self._wait_s

    def ready_in(self) -> float:
        """Seconds until the next attempt is allowed (interval or failure backoff)."""
        with self._pace_lock:
            if self._last_attempt <= 0.0:
                return 0.0
            return max(0.0, self._last_attempt + self._wait_s - _monotonic())

    def note_new_node(self, _nodename: str) -> None:
        """Drop the failure backoff: a nodename we just learned proves the broker delivers."""
        with self._pace_lock:
            self._failures = 0
            self._wait_s = _INSPECT_REFRESH_MIN_INTERVAL_SECONDS

    def _record(self, *, succeeded: bool) -> None:
        """Count an attempt toward pacing: an interval after a success, else a backoff."""
        with self._pace_lock:
            if succeeded:
                self._failures = 0
                self._wait_s = _INSPECT_REFRESH_MIN_INTERVAL_SECONDS
            else:
                # Doubling the wait (not 2**failures) can't overflow; the count is capped too.
                self._wait_s = (
                    _INSPECT_BACKOFF_START_SECONDS
                    if self._failures == 0
                    else min(self._wait_s * 2, _INSPECT_BACKOFF_MAX_SECONDS)
                )
                self._failures = min(self._failures + 1, _INSPECT_FAILURES_MAX)
            self._last_attempt = _monotonic()

    def _inspect_and_apply(
        self, worker_registry: WorkerRegistry, broker_url: str
    ) -> frozenset[str]:
        """Ask Celery and apply the reply; a failure backs the next attempt off.

        Returns:
            The nodenames that answered (empty on a failure).
        """
        inspector = _get_inspector(broker_url, timeout_seconds=_INSPECT_TIMEOUT_SECONDS)
        if inspector is None:
            return frozenset()
        outcome: _InspectOutcome | None = None
        with contextlib.suppress(Exception):
            outcome = _refresh_worker_registry_from_inspect(worker_registry, inspector)
        # A half-finished refresh (stats() answered, registered() or ping() didn't)
        # applied what it got but still backs off: the broker is struggling.
        self._record(succeeded=outcome is not None and outcome.complete)
        return outcome.answered if outcome else frozenset()

    def refresh_blocking(self) -> frozenset[str] | None:
        """Refresh on the calling thread; the probe paces it with ``ready_in``.

        Returns:
            The nodenames that answered, or None if another refresh was in
            flight (nothing ran).
        """
        worker_registry = self._worker_registry
        broker_url = self._broker_url
        if worker_registry is None or broker_url is None:
            return frozenset()
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._inspect_and_apply(worker_registry, broker_url)
        finally:
            self._lock.release()

    async def maybe_refresh(self) -> None:
        """Refresh the worker registry unless disabled, paced out, or in progress."""
        worker_registry = self._worker_registry
        broker_url = self._broker_url
        if worker_registry is None or broker_url is None:
            return
        if self._paced_out():
            return
        # Don't block requests waiting for an in-progress refresh.
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._paced_out():
                return
            await run_in_threadpool(
                self._inspect_and_apply, worker_registry, broker_url
            )
        finally:
            self._lock.release()


def _wire_inspect_probe(
    router: APIRouter,
    probe: InspectProbe | None,
    refresher: _InspectRefresher,
    worker_registry: WorkerRegistry | None,
    broker_url: str | None,
) -> None:
    """Point a probe at the refresher and the registry's new-node events.

    Nothing to inspect (no registry or no broker URL) means no probe. A probe
    the router makes itself has nobody else to start it, so it rides the app's
    startup and shutdown events (not run when the app has its own ``lifespan``;
    a worker joining still starts it).
    """
    if worker_registry is None or broker_url is None:
        return
    own = probe is None
    probe = probe or InspectProbe()
    probe.bind(refresher)
    # Before the probe's trigger: a join clears the backoff, then asks for its inspect.
    worker_registry.add_new_node_listener(refresher.note_new_node)
    worker_registry.add_new_node_listener(probe.trigger)
    # A first unanswered broadcast owes one confirming attempt (D4d).
    worker_registry.add_first_miss_listener(probe.confirm)
    if own:
        router.add_event_handler("startup", probe.start)
        router.add_event_handler("shutdown", probe.stop)


def _hostnames_by_registered_task(
    worker_registry: WorkerRegistry | None,
) -> dict[str, set[str]]:
    """Map each task name registered by a worker to the hostnames registering it.

    Sets, because one hostname can appear as several workers (restarts).
    """
    hostnames: dict[str, set[str]] = {}
    if worker_registry is None:
        return hostnames
    for worker in worker_registry.get_all_workers():
        for task_name in worker.registered_tasks:
            hostnames.setdefault(task_name, set()).add(worker.hostname)
    return hostnames


def _registry_status(execution_count: int, registered_by: list[str]) -> TaskStatus:
    """Classify a task by whether it has run and whether any worker registers it."""
    if not registered_by:
        return TaskStatus.NOT_REGISTERED
    return TaskStatus.ACTIVE if execution_count > 0 else TaskStatus.NEVER_RUN


def _registered_task_response(
    name: str,
    *,
    worker_registry: WorkerRegistry | None,
    execution_count: int,
    last_run: datetime | None,
    registered_by: list[str],
    status: TaskStatus,
    groups: GroupConfig,
) -> RegisteredTaskResponse:
    """Describe one task, preferring worker-reported metadata.

    The module comes from the worker's task definition when it has one, else
    from the dotted task name (``app.tasks.add`` -> ``app.tasks``).
    """
    definition = worker_registry.get_task_definition(name) if worker_registry else None
    parts = name.rsplit(".", 1)
    fallback_module = parts[0] if len(parts) > 1 else None
    group = groups.resolve(TaskAttributes(name=name))
    return RegisteredTaskResponse(
        name=name,
        module=definition.module
        if definition and definition.module
        else fallback_module,
        signature=definition.signature if definition else None,
        docstring=definition.docstring if definition else None,
        bound=definition.bound if definition else False,
        execution_count=execution_count,
        registered_by=registered_by,
        last_run=last_run,
        status=status,
        task_group=group.name,
        tags=list(group.tags),
    )


def _build_task_registry(
    store: GraphStore,
    worker_registry: WorkerRegistry | None,
    *,
    query: str | None,
    status: str | None,
    groups: GroupConfig,
) -> list[RegisteredTaskResponse]:
    """List every known task: executed ones and ones registered by workers.

    Sorted by name. ``query`` filters by case-insensitive name substring and
    ``status`` by :class:`TaskStatus` value; an unknown status matches nothing.
    """
    hostnames_by_task = _hostnames_by_registered_task(worker_registry)

    tasks: list[RegisteredTaskResponse] = []
    runs = store.get_task_run_summaries()
    for name in sorted(runs.keys() | hostnames_by_task.keys()):
        # Workers never register built-ins, so they would always read as not_registered.
        if name.startswith(_CELERY_BUILTIN_PREFIX):
            continue
        if query and query.lower() not in name.lower():
            continue
        execution_count, last_run = runs.get(name, (0, None))
        registered_by = sorted(hostnames_by_task.get(name, ()))
        task_status = _registry_status(execution_count, registered_by)
        if status and task_status.value != status:
            continue
        tasks.append(
            _registered_task_response(
                name,
                worker_registry=worker_registry,
                execution_count=execution_count,
                last_run=last_run,
                registered_by=registered_by,
                status=task_status,
                groups=groups,
            )
        )
    return tasks


def _known_task_names(
    store: GraphStore, hostnames_by_task: Mapping[str, set[str]]
) -> set[str]:
    """Task names seen in events or registered by a worker."""
    return store.get_unique_task_names() | set(hostnames_by_task)


def _add_health_routes(
    router: APIRouter,
    *,
    store: GraphStore,
    consumer: AsyncEventConsumer | None,
    ws_manager: WebSocketManager | None,
    broker_url: str | None,
) -> None:
    """Register ``/health`` (full status) and ``/health/live`` (liveness probe)."""
    broker = broker_kind(broker_url)

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Return server health status and connection counts.

        ``status`` is "degraded" when a configured consumer has stopped
        unexpectedly or can't read from the broker, and "ok" otherwise (a
        consumer whose event source finished normally, or that was stopped on
        purpose, is not degraded). The response is always HTTP 200 so existing
        health checks keep working.
        """
        from stemtrace._version import __version__

        running = consumer.is_running if consumer else False
        connected = consumer.is_connected if consumer else None

        return HealthResponse(
            status="degraded" if _is_consumer_degraded(consumer) else "ok",
            version=__version__,
            consumer_running=running,
            consumer_connected=connected,
            websocket_connections=ws_manager.connection_count if ws_manager else 0,
            node_count=store.node_count,
            broker=broker,
        )

    @router.get(
        "/health/live",
        response_model=LivenessResponse,
        # The API key gate exempts this route by name (api_key.PUBLIC_ROUTE_NAMES).
        name="stemtrace_health_live",
    )
    async def health_live() -> LivenessResponse:
        """Process liveness probe for container health checks.

        Always 200 while the app is serving, so a broker blip that stops the
        consumer doesn't mark the container unhealthy for good. Served without
        form-login authentication, so it exposes only an informational status,
        decided by the same rule as ``/health``: "degraded" when the event
        consumer stopped unexpectedly or can't read from the broker, "ok"
        otherwise.
        """
        status = "degraded" if _is_consumer_degraded(consumer) else "ok"
        return LivenessResponse(status=status)


def _add_task_routes(
    router: APIRouter,
    *,
    store: GraphStore,
    worker_registry: WorkerRegistry | None,
    refresher: _InspectRefresher,
    groups: GroupConfig,
    evaluator: AnomalyEvaluator,
) -> None:
    """Register the task list, task registry, task detail and children routes.

    ``/tasks/registry`` is registered before ``/tasks/{task_id}``: routes match
    in registration order, so the other way round "registry" would be taken
    for a task id.
    """

    @router.get(
        "/tasks",
        response_model=TaskListResponse,
        responses={400: {"model": ErrorResponse}},
    )
    async def list_tasks(
        *,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        state: Annotated[
            str | None,
            Query(
                description=(
                    "Filter by state; a comma-separated list matches any "
                    "(FAILURE,RETRY). Any case; an unknown state is a 422."
                )
            ),
        ] = None,
        name: Annotated[
            str | None, Query(description="Filter by name substring")
        ] = None,
        name_exact: Annotated[
            str | None,
            Query(description="Filter by exact task name (case-sensitive)"),
        ] = None,
        from_date: Annotated[
            datetime | None, Query(description="Filter by start date (ISO format)")
        ] = None,
        to_date: Annotated[
            datetime | None, Query(description="Filter by end date (ISO format)")
        ] = None,
        updated_before: Annotated[
            datetime | None,
            Query(
                description=(
                    "Only tasks last updated before this time (ISO format, exclusive)"
                )
            ),
        ] = None,
        hostname: Annotated[
            str | None,
            Query(description="Filter by exact worker nodename (name@host)"),
        ] = None,
        queue: Annotated[
            str | None, Query(description="Filter by exact queue name")
        ] = None,
        exception_type: Annotated[
            str | None,
            Query(
                description=(
                    "Filter by exact exception class (module.Class; just Class "
                    "for builtins)"
                )
            ),
        ] = None,
        task_group: Annotated[
            str | None,
            Query(description="Filter by exact task group name"),
        ] = None,
        tag: Annotated[
            str | None,
            Query(description="Filter by a tag of the task's group"),
        ] = None,
        within: Annotated[
            str | None,
            Query(description="Updated within this long (30s, 15m, 6h, 7d)"),
        ] = None,
        compact: Annotated[
            bool, Query(description="Leave out each task's events (events: null)")
        ] = False,
    ) -> TaskListResponse:
        """List tasks with optional filtering by state, name, date range and more.

        ``name`` is a case-insensitive substring match (the search box);
        ``name_exact`` is an exact match on the full task name. ``hostname``,
        ``queue`` and ``exception_type`` match exactly the value from the
        task's furthest-along event that has one (a later retry beats an
        earlier one). ``task_group`` matches the task's group name exactly and
        ``tag`` one of its group's tags. ``within`` is a relative ``from_date``
        (now minus it); pass one or the other. ``from_date`` with
        ``updated_before`` lists the runs one bucket of ``/stats/timeseries``
        counts: both compare the time of the run's last event.
        """
        states = params.parse_states(state)
        start = params.start_from(within, from_date, params.utcnow())
        group_of = group_lookup(groups)
        task_names = await _filter_task_names(store, group_of, task_group, tag)
        anomalies = (await evaluator.evaluate()).anomalies
        nodes, total = store.get_nodes(
            limit=limit,
            offset=offset,
            state=states,
            name_contains=name,
            name_exact=name_exact,
            from_date=start,
            to_date=to_date,
            updated_before=updated_before,
            hostname=hostname,
            queue=queue,
            exception_type=exception_type,
            task_names=task_names,
        )
        return TaskListResponse(
            tasks=[
                _node_to_response(
                    n,
                    include_events=not compact,
                    group_of=group_of,
                    anomalies=anomalies,
                )
                for n in nodes
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/tasks/registry",
        response_model=TaskRegistryResponse,
    )
    async def get_task_registry(
        query: Annotated[
            str | None, Query(description="Filter by task name substring")
        ] = None,
        status: Annotated[
            str | None,
            Query(
                description="Filter by status: 'active', 'never_run', 'not_registered'"
            ),
        ] = None,
        refresh: Annotated[
            bool,
            Query(
                description="Refresh worker registry from Celery inspect (best-effort)"
            ),
        ] = True,
    ) -> TaskRegistryResponse:
        """List all discovered task definitions with optional filtering.

        Status values:
        - active: Has been executed AND registered by workers
        - never_run: Registered by workers but never executed
        - not_registered: Executed but no worker has it registered
        """
        if refresh and worker_registry is not None:
            await refresher.maybe_refresh()

        # Resolves the group of every known name: off the event loop.
        tasks = await run_in_threadpool(
            functools.partial(
                _build_task_registry,
                store,
                worker_registry,
                query=query,
                status=status,
                groups=groups,
            )
        )
        return TaskRegistryResponse(tasks=tasks, total=len(tasks))

    @router.get(
        "/tasks/{task_id}",
        response_model=TaskDetailResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_task(task_id: str) -> TaskDetailResponse:
        """Get detailed information for a specific task including children."""
        found = store.get_node_with_children(task_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        node, children, flow_root_id = found
        group_of = group_lookup(groups)
        anomalies = (await evaluator.evaluate()).anomalies
        anomaly = _anomaly_of(node, anomalies)
        return TaskDetailResponse(
            task=_node_to_response(
                node, include_output=True, group_of=group_of, anomalies=anomalies
            ),
            children=[
                _node_to_response(c, group_of=group_of, anomalies=anomalies)
                for c in children
            ],
            flow_root_id=flow_root_id,
            anomaly_detail=anomaly_response(anomaly) if anomaly is not None else None,
        )

    @router.get(
        "/tasks/{task_id}/children",
        response_model=list[TaskNodeResponse],
        responses={404: {"model": ErrorResponse}},
    )
    async def get_task_children(
        task_id: str,
        compact: Annotated[
            bool, Query(description="Leave out each task's events (events: null)")
        ] = False,
    ) -> list[TaskNodeResponse]:
        """Get child tasks for a specific task."""
        found = store.get_node_with_children(task_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        children = found.children
        group_of = group_lookup(groups)
        anomalies = (await evaluator.evaluate()).anomalies
        return [
            _node_to_response(
                c, include_events=not compact, group_of=group_of, anomalies=anomalies
            )
            for c in children
        ]


def _add_graph_routes(
    router: APIRouter,
    *,
    store: GraphStore,
    node_alias_key: str | None,
    groups: GroupConfig,
    evaluator: AnomalyEvaluator,
) -> None:
    """Register the graph list and graph detail routes."""

    @router.get("/graphs", response_model=GraphListResponse)
    async def list_graphs(
        *,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        from_date: Annotated[
            datetime | None, Query(description="Filter by start date (ISO format)")
        ] = None,
        to_date: Annotated[
            datetime | None, Query(description="Filter by end date (ISO format)")
        ] = None,
        task_group: Annotated[
            str | None,
            Query(description="Filter by exact task group name"),
        ] = None,
        tag: Annotated[
            str | None,
            Query(description="Filter by a tag of the task's group"),
        ] = None,
        within: Annotated[
            str | None,
            Query(description="Updated within this long (30s, 15m, 6h, 7d)"),
        ] = None,
    ) -> GraphListResponse:
        """List task execution graphs (root tasks) with pagination and filtering.

        ``task_group`` and ``tag`` list the graphs with at least one task that
        matches (anywhere in the graph, not only its root). ``within`` is a
        relative ``from_date`` (now minus it); pass one or the other.
        """
        start = params.start_from(within, from_date, params.utcnow())
        group_of = group_lookup(groups)
        task_names = await _filter_task_names(store, group_of, task_group, tag)
        anomalies = (await evaluator.evaluate()).anomalies
        # Roots, their children (for synthetic node timing) and each flow's
        # summary from one snapshot
        roots, all_nodes, summaries, total = store.get_root_flows(
            limit=limit,
            offset=offset,
            from_date=start,
            to_date=to_date,
            task_names=task_names,
        )
        return GraphListResponse(
            graphs=[
                _node_to_graph_response(
                    r,
                    all_nodes,
                    node_alias_key=node_alias_key,
                    group_of=group_of,
                    flow=FlowSummaryResponse.model_validate(
                        summaries[r.task_id], from_attributes=True
                    ),
                    anomalies=anomalies,
                )
                for r in roots
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/graphs/{root_id}",
        response_model=GraphResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_graph(root_id: str) -> GraphResponse:
        """Get complete task graph starting from a root task."""
        graph = store.get_graph_from_root(root_id)
        if not graph:
            raise HTTPException(status_code=404, detail=f"Graph {root_id} not found")

        all_nodes: dict[str, TaskNode] = dict(graph)
        group_of = group_lookup(groups)
        # The whole graph is here, so every header that has run is known. A
        # pass over every node and event: off the event loop.
        not_run = await run_in_threadpool(chord_not_run_reasons, all_nodes)
        anomalies = (await evaluator.evaluate()).anomalies
        return GraphResponse(
            root_id=root_id,
            nodes={
                tid: _node_to_graph_response(
                    n,
                    all_nodes,
                    node_alias_key=node_alias_key,
                    group_of=group_of,
                    not_run_reason=not_run.get(tid),
                    anomalies=anomalies,
                )
                for tid, n in graph.items()
            },
        )


def _add_group_routes(
    router: APIRouter,
    *,
    store: GraphStore,
    worker_registry: WorkerRegistry | None,
    groups: GroupConfig,
) -> None:
    """Register the task group list route."""

    @router.get("/groups", response_model=TaskGroupListResponse)
    async def list_groups() -> TaskGroupListResponse:
        """List task groups with their source, tags, color and task names.

        Task names are the ones the server has seen in events or in workers'
        registered tasks, including ``celery.*`` built-ins that the task registry
        hides. Code-defined groups are
        listed even before any of their tasks is seen. Sorted by name, with
        ``ungrouped`` last. Doesn't query workers: see ``/tasks/registry``.
        """
        # Resolves the group of every known name: off the event loop.
        members = await run_in_threadpool(
            lambda: groups.members(
                _known_task_names(store, _hostnames_by_registered_task(worker_registry))
            )
        )
        return TaskGroupListResponse(
            groups=[
                TaskGroupResponse(
                    name=m.group.name,
                    source=m.group.source,
                    tags=list(m.group.tags),
                    color=m.group.color,
                    task_names=list(m.task_names),
                )
                for m in members
            ],
            total=len(members),
        )


def _add_worker_routes(
    router: APIRouter,
    *,
    worker_registry: WorkerRegistry | None,
    refresher: _InspectRefresher,
) -> None:
    """Register the worker list and per-hostname worker routes.

    Without a worker registry both answer with an empty list.
    """

    @router.get(
        "/workers",
        response_model=WorkerListResponse,
    )
    async def list_workers(
        refresh: Annotated[
            bool, Query(description="Refresh worker status from Celery")
        ] = True,
    ) -> WorkerListResponse:
        """List all registered workers, optionally refreshing from Celery.

        Args:
            refresh: If True, query Celery for current worker status and update registry.
        """
        if worker_registry is None:
            return WorkerListResponse(workers=[], total=0)

        # On-demand worker status refresh using Celery inspect
        if refresh:
            await refresher.maybe_refresh()

        workers = worker_registry.get_all_workers()
        return WorkerListResponse(
            workers=[WorkerResponse.model_validate(w) for w in workers],
            total=len(workers),
        )

    @router.get(
        "/workers/{hostname}",
        response_model=WorkerListResponse,
    )
    async def get_workers_by_hostname(hostname: str) -> WorkerListResponse:
        """Get all workers matching a hostname.

        Args:
            hostname: Worker hostname to filter by.
        """
        if worker_registry is None:
            return WorkerListResponse(workers=[], total=0)

        workers = worker_registry.get_workers_by_hostname(hostname)
        return WorkerListResponse(
            workers=[WorkerResponse.model_validate(w) for w in workers],
            total=len(workers),
        )


class _ApiRoute(APIRoute):
    """An API route whose 405 ``Allow`` header also names HEAD on a GET route.

    The HEAD route next to it (``_serve_head_on_get_routes``) answers HEAD,
    but the router builds a 405 from the one route that matched the path.
    """

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle the request; another method on a GET route gets a 405 naming HEAD."""
        # starlette 0.38-1.7 Route.handle raises this 405 inside an app, with
        # only this route's methods in Allow; raising it first adds HEAD.
        methods = self.methods or set()  # APIRoute always sets them
        if "GET" in methods and scope["method"] not in methods:
            allow = ", ".join(sorted(methods | {"HEAD"}))
            raise StarletteHTTPException(status_code=405, headers={"Allow": allow})
        await super().handle(scope, receive, send)


def _serve_head_on_get_routes(router: APIRouter) -> None:
    """Add a HEAD route next to every GET route: same endpoint, name and checks.

    A HEAD request then passes authentication like GET instead of getting a
    405 before any dependency runs. The HEAD routes stay out of the OpenAPI
    schema, where they would repeat each GET operation and its ID.
    """
    # Separate routes, not "HEAD" added to the GET routes' methods: FastAPI
    # builds one operation ID per route, so every second method duplicated it
    # (a warning, an error under strict warning filters). uvicorn (0.40.0, h11
    # and httptools) sends a HEAD response's headers without the body.
    get_routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute) and route.methods and "GET" in route.methods
    ]
    for route in get_routes:
        router.add_api_route(
            route.path.removeprefix(router.prefix),
            route.endpoint,
            methods=["HEAD"],
            name=route.name,
            response_model=route.response_model,
            response_class=route.response_class,
            status_code=route.status_code,
            dependencies=route.dependencies,
            include_in_schema=False,
        )


def create_api_router(  # noqa: PLR0917  public API (stemtrace.server.api): positional callers must keep working
    store: GraphStore,
    consumer: AsyncEventConsumer | None = None,
    ws_manager: WebSocketManager | None = None,
    worker_registry: WorkerRegistry | None = None,
    broker_url: str | None = None,
    node_alias_from_arguments: str | None = None,
    groups: GroupsInput = None,
    worker_timeline: WorkerTimeline | None = None,
    anomaly_settings: AnomalySettings = DEFAULT_SETTINGS,
    inspect_probe: InspectProbe | None = None,
) -> APIRouter:
    """Create REST API router with task and graph endpoints.

    Args:
        store: Graph store for task/graph data.
        consumer: Optional async event consumer.
        ws_manager: Optional WebSocket manager.
        worker_registry: Optional worker registry for lifecycle tracking.
        broker_url: Optional Celery broker URL for on-demand inspection.
        node_alias_from_arguments: Key to derive node display name from task
            arguments. Digit string for args[index], string for kwargs[key].
        groups: Code-defined task groups, as for ``StemtraceExtension``: a
            GroupConfig, a list of GroupRule or dicts, or a TOML file path.
            None gives automatic groups only.
        worker_timeline: Worker event history that tells an interrupted task from
            a running one (see ``StemtraceExtension``); None means no task is
            ever reported interrupted.
        anomaly_settings: Thresholds for stuck tasks; see ``anomaly_settings()``.
        inspect_probe: Probe that refreshes the registry without a request;
            ``StemtraceExtension`` passes its own and starts it. None gives the
            router its own, started by the app's startup event.

    Returns:
        Configured API router.

    Raises:
        ConfigurationError: If ``groups`` is invalid or its file can't be loaded.
    """
    router = APIRouter(prefix="/api", tags=["stemtrace"], route_class=_ApiRoute)
    # One refresher for both /tasks/registry and /workers, so they share the
    # rate limit.
    # A probe that is already bound keeps its refresher, so routers built twice
    # from one extension share one rate limit.
    shared = inspect_probe.target if inspect_probe is not None else None
    refresher = (
        shared
        if isinstance(shared, _InspectRefresher)
        else _InspectRefresher(worker_registry, broker_url)
    )
    _wire_inspect_probe(router, inspect_probe, refresher, worker_registry, broker_url)
    evaluator = AnomalyEvaluator(
        store, worker_timeline, anomaly_settings, worker_registry, broker_url
    )

    _add_health_routes(
        router,
        store=store,
        consumer=consumer,
        ws_manager=ws_manager,
        broker_url=broker_url,
    )
    group_config = build_group_config(groups)
    _add_task_routes(
        router,
        store=store,
        worker_registry=worker_registry,
        refresher=refresher,
        groups=group_config,
        evaluator=evaluator,
    )
    _add_graph_routes(
        router,
        store=store,
        node_alias_key=node_alias_from_arguments,
        groups=group_config,
        evaluator=evaluator,
    )
    _add_group_routes(
        router, store=store, worker_registry=worker_registry, groups=group_config
    )
    add_summary_routes(
        router, store=store, worker_registry=worker_registry, groups=group_config
    )
    _add_worker_routes(router, worker_registry=worker_registry, refresher=refresher)
    add_anomaly_routes(router, evaluator=evaluator, refresher=refresher)
    _serve_head_on_get_routes(router)
    return router
