"""REST API endpoints for stemtrace."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import threading
import time

# FastAPI evaluates the Query annotations below at runtime, so datetime must be
# a real import, not a type-checking one.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Annotated

from celery import Celery
from fastapi import APIRouter, HTTPException, Query

from stemtrace.core.graph import NodeType
from stemtrace.core.groups import GroupConfig, ResolvedGroup, TaskAttributes
from stemtrace.server.api.schemas import (
    ErrorResponse,
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
from stemtrace.server.groups import GroupsInput, build_group_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from celery.app.control import Inspect

    from stemtrace.core.events import TaskEvent, TaskState
    from stemtrace.core.graph import TaskNode
    from stemtrace.server.consumer import AsyncEventConsumer
    from stemtrace.server.store import GraphStore, WorkerRegistry
    from stemtrace.server.websocket import WebSocketManager

logger = logging.getLogger(__name__)


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

    Returns None when the registered mapping is unavailable, so callers can
    preserve any existing task list.
    """
    if not registered or not isinstance(registered, dict):
        return None
    tasks_obj = registered.get(worker_key)
    if not isinstance(tasks_obj, list):
        return []
    tasks: list[str] = []
    for t in tasks_obj:
        if isinstance(t, str) and not t.startswith("celery."):
            tasks.append(t)
    return tasks


def _event_to_response(event: TaskEvent, include_output: bool) -> TaskEventResponse:
    """Convert a TaskEvent to its API model, optionally dropping its output."""
    response = TaskEventResponse.model_validate(event)
    if not include_output and response.output is not None:
        response = response.model_copy(update={"output": None})
    return response


def _group_lookup(groups: GroupConfig) -> Callable[[str], ResolvedGroup]:
    """Resolve task groups by name, each name once, for building one response.

    A response's nodes share few task names, and matching every node against
    every rule's globs again made large graphs with many rules slow.
    """

    @functools.cache
    def lookup(task_name: str) -> ResolvedGroup:
        """Return the group of tasks named ``task_name``."""
        return groups.resolve(TaskAttributes(name=task_name))

    return lookup


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


def _node_to_response(
    node: TaskNode,
    *,
    include_output: bool = False,
    group_of: Callable[[str], ResolvedGroup],
) -> TaskNodeResponse:
    """Convert TaskNode to API response model.

    Args:
        node: Task node from graph store.
        include_output: Keep captured task output on events. Off for list
            responses: output can be up to ``max_output_size`` per attempt and
            only the task detail page shows it.
        group_of: Task group by task name (see ``_group_lookup``).

    Returns:
        TaskNodeResponse with timestamp and duration.
    """
    first_seen = node.first_seen
    last_updated = node.last_updated

    duration_ms = None
    if first_seen and last_updated and first_seen != last_updated:
        duration_ms = int((last_updated - first_seen).total_seconds() * 1000)

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
        events=[_event_to_response(e, include_output) for e in node.events],
        first_seen=first_seen,
        last_updated=last_updated,
        duration_ms=duration_ms,
        exception_type=node.exception_type,
        hostname=node.hostname,
        queue=node.queue,
        routing_key=node.routing_key,
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
) -> GraphNodeResponse:
    """Convert TaskNode to graph response model.

    For synthetic nodes (GROUP/CHORD), compute timing from children.

    Args:
        node: Task node from graph store.
        all_nodes: All nodes dict for child lookup.
        node_alias_key: Key to resolve display name from task arguments.
        group_of: Task group by task name (see ``_group_lookup``).

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
        first_seen=first_seen,
        last_updated=last_updated,
        exception_type=node.exception_type,
        hostname=node.hostname,
        queue=node.queue,
        routing_key=node.routing_key,
    )


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
        app = Celery("stemtrace", broker=broker_url)
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


def _refresh_worker_registry_from_inspect(
    worker_registry: WorkerRegistry,
    inspector: Inspect,
) -> None:
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
    """
    # The replies describe the workers as of now, but the consumer keeps
    # applying worker events while inspect waits (up to 0.5 s per call).
    seen = worker_registry.event_generations()

    # We need PID to register a worker. `stats()` typically includes it.
    stats: dict[str, object] | None = None
    registered: dict[str, object] | None = None

    with contextlib.suppress(Exception):
        stats = inspector.stats()

    with contextlib.suppress(Exception):
        registered = inspector.registered()

    # Fallback: if stats is unavailable, at least refresh online status via ping().
    # This keeps the Workers tab responsive even for minimal Celery setups.
    if not stats:
        active: dict[str, object] | None = None
        with contextlib.suppress(Exception):
            active = inspector.ping()
        if not active:
            return
        worker_registry.apply_ping_reply(active)
        return

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


# The UI requests workers and the registry in quick succession on page load;
# one inspect round trip serves both.
_INSPECT_REFRESH_MIN_INTERVAL_SECONDS = 2.0
_INSPECT_TIMEOUT_SECONDS = 0.5


class _InspectRefresher:
    """Rate-limited, non-blocking worker registry refresh from Celery inspect.

    Runs at most one refresh at a time and at most one per
    ``_INSPECT_REFRESH_MIN_INTERVAL_SECONDS``. A request that finds a refresh
    in progress, or a recent one, returns at once instead of waiting. Failures
    are swallowed (the refresh is best-effort) and don't count as a refresh,
    so the next request retries. State is per router instance.
    """

    def __init__(
        self, worker_registry: WorkerRegistry | None, broker_url: str | None
    ) -> None:
        """Bind the registry to refresh and the broker to inspect (either None disables)."""
        self._worker_registry = worker_registry
        self._broker_url = broker_url
        self._lock = threading.Lock()
        self._last_refresh: float = 0.0

    def _refreshed_recently(self) -> bool:
        return _monotonic() - self._last_refresh < _INSPECT_REFRESH_MIN_INTERVAL_SECONDS

    async def maybe_refresh(self) -> None:
        """Refresh the worker registry unless disabled, recent, or in progress."""
        worker_registry = self._worker_registry
        broker_url = self._broker_url
        if worker_registry is None or broker_url is None:
            return
        if self._refreshed_recently():
            return
        # Don't block requests waiting for an in-progress refresh.
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._refreshed_recently():
                return
            inspector = await asyncio.to_thread(
                _get_inspector,
                broker_url,
                timeout_seconds=_INSPECT_TIMEOUT_SECONDS,
            )
            if inspector is None:
                return
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    _refresh_worker_registry_from_inspect,
                    worker_registry,
                    inspector,
                )
                self._last_refresh = _monotonic()
        finally:
            self._lock.release()


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
    store: GraphStore,
    worker_registry: WorkerRegistry | None,
    execution_count: int,
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
        last_run=store.get_last_execution_time(name),
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
    for name in sorted(_known_task_names(store, hostnames_by_task)):
        if query and query.lower() not in name.lower():
            continue
        execution_count = store.get_task_execution_count(name)
        registered_by = sorted(hostnames_by_task.get(name, ()))
        task_status = _registry_status(execution_count, registered_by)
        if status and task_status.value != status:
            continue
        tasks.append(
            _registered_task_response(
                name,
                store=store,
                worker_registry=worker_registry,
                execution_count=execution_count,
                registered_by=registered_by,
                status=task_status,
                groups=groups,
            )
        )
    return tasks


def _known_task_names(
    store: GraphStore, hostnames_by_task: Mapping[str, set[str]]
) -> set[str]:
    """Task names seen in events or registered by a worker (the registry's set)."""
    return store.get_unique_task_names() | set(hostnames_by_task)


def _add_health_routes(
    router: APIRouter,
    *,
    store: GraphStore,
    consumer: AsyncEventConsumer | None,
    ws_manager: WebSocketManager | None,
) -> None:
    """Register ``/health`` (full status) and ``/health/live`` (liveness probe)."""

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
        )

    @router.get(
        "/health/live",
        response_model=LivenessResponse,
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
        state: Annotated[str | None, Query(description="Filter by task state")] = None,
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
    ) -> TaskListResponse:
        """List tasks with optional filtering by state, name, date range and more.

        ``name`` is a case-insensitive substring match (the search box);
        ``name_exact`` is an exact match on the full task name. ``hostname``,
        ``queue`` and ``exception_type`` match exactly the value from the
        task's furthest-along event that has one (a later retry beats an
        earlier one).
        """
        from stemtrace.core.events import TaskState as TS

        task_state: TaskState | None = None
        if state is not None:
            with contextlib.suppress(ValueError):
                task_state = TS(state)

        nodes, total = store.get_nodes(
            limit=limit,
            offset=offset,
            state=task_state,
            name_contains=name,
            name_exact=name_exact,
            from_date=from_date,
            to_date=to_date,
            hostname=hostname,
            queue=queue,
            exception_type=exception_type,
        )
        group_of = _group_lookup(groups)
        return TaskListResponse(
            tasks=[_node_to_response(n, group_of=group_of) for n in nodes],
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

        tasks = _build_task_registry(
            store, worker_registry, query=query, status=status, groups=groups
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

        node, children = found
        group_of = _group_lookup(groups)
        return TaskDetailResponse(
            task=_node_to_response(node, include_output=True, group_of=group_of),
            children=[_node_to_response(c, group_of=group_of) for c in children],
        )

    @router.get(
        "/tasks/{task_id}/children",
        response_model=list[TaskNodeResponse],
        responses={404: {"model": ErrorResponse}},
    )
    async def get_task_children(task_id: str) -> list[TaskNodeResponse]:
        """Get child tasks for a specific task."""
        found = store.get_node_with_children(task_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        _, children = found
        group_of = _group_lookup(groups)
        return [_node_to_response(c, group_of=group_of) for c in children]


def _add_graph_routes(
    router: APIRouter,
    *,
    store: GraphStore,
    node_alias_key: str | None,
    groups: GroupConfig,
) -> None:
    """Register the graph list and graph detail routes."""

    @router.get("/graphs", response_model=GraphListResponse)
    async def list_graphs(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        from_date: Annotated[
            datetime | None, Query(description="Filter by start date (ISO format)")
        ] = None,
        to_date: Annotated[
            datetime | None, Query(description="Filter by end date (ISO format)")
        ] = None,
    ) -> GraphListResponse:
        """List task execution graphs (root tasks) with pagination and date filtering."""
        # Roots plus children (for synthetic node timing) from one snapshot
        roots, all_nodes, total = store.get_root_nodes_with_children(
            limit=limit,
            offset=offset,
            from_date=from_date,
            to_date=to_date,
        )
        group_of = _group_lookup(groups)
        return GraphListResponse(
            graphs=[
                _node_to_graph_response(
                    r, all_nodes, node_alias_key=node_alias_key, group_of=group_of
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
        group_of = _group_lookup(groups)
        return GraphResponse(
            root_id=root_id,
            nodes={
                tid: _node_to_graph_response(
                    n, all_nodes, node_alias_key=node_alias_key, group_of=group_of
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
        registered tasks (the task registry's names). Code-defined groups are
        listed even before any of their tasks is seen. Sorted by name, with
        ``ungrouped`` last. Doesn't query workers: see ``/tasks/registry``.
        """
        members = groups.members(
            _known_task_names(store, _hostnames_by_registered_task(worker_registry))
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


def create_api_router(  # noqa: PLR0917  public API (stemtrace.server.api): positional callers must keep working
    store: GraphStore,
    consumer: AsyncEventConsumer | None = None,
    ws_manager: WebSocketManager | None = None,
    worker_registry: WorkerRegistry | None = None,
    broker_url: str | None = None,
    node_alias_from_arguments: str | None = None,
    groups: GroupsInput = None,
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

    Returns:
        Configured API router.

    Raises:
        ConfigurationError: If ``groups`` is invalid or its file can't be loaded.
    """
    router = APIRouter(prefix="/api", tags=["stemtrace"])
    # One refresher for both /tasks/registry and /workers, so they share the
    # rate limit.
    refresher = _InspectRefresher(worker_registry, broker_url)

    _add_health_routes(router, store=store, consumer=consumer, ws_manager=ws_manager)
    group_config = build_group_config(groups)
    _add_task_routes(
        router,
        store=store,
        worker_registry=worker_registry,
        refresher=refresher,
        groups=group_config,
    )
    _add_graph_routes(
        router,
        store=store,
        node_alias_key=node_alias_from_arguments,
        groups=group_config,
    )
    _add_group_routes(
        router, store=store, worker_registry=worker_registry, groups=group_config
    )
    _add_worker_routes(router, worker_registry=worker_registry, refresher=refresher)
    return router
