"""Thread-safe in-memory graph store."""

from __future__ import annotations

import contextlib
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel

from stemtrace.core.graph import NodeType, TaskGraph, TaskNode
from stemtrace.server.api.schemas import WorkerStatus

if TYPE_CHECKING:
    from stemtrace.core.events import RegisteredTaskDefinition, TaskEvent


class WorkerInfo(BaseModel):
    """Information about a registered worker."""

    hostname: str
    pid: int
    registered_tasks: list[str]
    registered_at: datetime
    last_seen: datetime
    status: WorkerStatus = WorkerStatus.ONLINE
    # True once a worker_shutdown event closed this process. Only a fresh
    # registration (worker_ready, or stats naming this pid) clears it.
    shutdown_reported: bool = False


def _ensure_tz_aware(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _ensure_end_of_day(dt: datetime) -> datetime:
    """Ensure to_date is end of day and timezone-aware.

    When a date-only value (YYYY-MM-DD) is parsed, it becomes midnight.
    For to_date filtering, we want end of day to include the full day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # If time is midnight (date-only input), set to end of day
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


def _snapshot(node: TaskNode) -> TaskNode:
    """Copy a node so callers can read it without holding the store lock.

    The consumer thread inserts events mid-list (they are kept in lifecycle
    order) and appends children, so handing out the live node could let an
    API response see duplicated or skipped events. Call with the lock held.
    """
    return node.snapshot()


# Fallback for nodes with no events (synthetic nodes)
_MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)


def _get_node_timestamp(node: TaskNode, graph: TaskGraph) -> datetime:
    """Get the most recent timestamp for a node (including children for synthetic nodes)."""
    if node.last_updated is not None:
        return node.last_updated

    # For synthetic nodes (GROUP/CHORD), use the latest child timestamp
    if node.node_type in (NodeType.GROUP, NodeType.CHORD) and node.children:
        child_timestamps: list[datetime] = []
        for child_id in node.children:
            child = graph.get_node(child_id)
            if child and child.last_updated is not None:
                child_timestamps.append(child.last_updated)
        if child_timestamps:
            return max(child_timestamps)

    return _MIN_DATETIME


def _get_first_timestamp(node: TaskNode, graph: TaskGraph) -> datetime:
    """Get the first timestamp for a node (including children for synthetic nodes)."""
    if node.first_seen is not None:
        return node.first_seen

    # For synthetic nodes (GROUP/CHORD), use the earliest child timestamp
    if node.node_type in (NodeType.GROUP, NodeType.CHORD) and node.children:
        child_timestamps: list[datetime] = []
        for child_id in node.children:
            child = graph.get_node(child_id)
            if child and child.first_seen is not None:
                child_timestamps.append(child.first_seen)
        if child_timestamps:
            return min(child_timestamps)

    return _MIN_DATETIME


if TYPE_CHECKING:
    from collections.abc import Callable

    from stemtrace.core.events import TaskEvent, TaskState


class WorkerRegistry:
    """Thread-safe registry for worker lifecycle tracking.

    Maintains per-worker task lists and online status from
    worker_ready and worker_shutdown events.
    """

    def __init__(self) -> None:
        """Initialize empty worker registry."""
        self._workers: dict[str, WorkerInfo] = {}
        # Nodename -> worker_id registered last. Registration order is observed
        # by this server, so unlike event timestamps (the worker's clock) and
        # last_seen (mixes both clocks) it can't be skewed, and unlike pids it
        # doesn't wrap.
        self._newest_by_node: dict[str, str] = {}
        self._task_definitions_by_name: dict[str, RegisteredTaskDefinition] = {}
        self._lock = threading.RLock()

    def register_worker(
        self,
        hostname: str,
        pid: int,
        tasks: list[str],
        task_definitions: dict[str, RegisteredTaskDefinition] | None = None,
        event_timestamp: datetime | None = None,
    ) -> None:
        """Register a worker with its task list.

        Updates existing worker if already registered (worker restart scenario).

        Args:
            hostname: Worker hostname.
            pid: Worker process ID.
            tasks: List of registered task names.
            task_definitions: Optional mapping of task name to task definition
                metadata (docstring/signature/module/bound). When provided, this
                is used to enrich `/api/tasks/registry`.
            event_timestamp: When the worker event occurred. Used for last_seen
                to properly handle historical events from Redis replay.
        """
        # Validate PID - PID 0 is invalid (reserved for kernel)
        if pid <= 0:
            return

        with self._lock:
            worker_id = f"{hostname}:{pid}"
            # Use event timestamp for last_seen (important for Redis replay)
            # Fall back to now() for direct calls (e.g., tests)
            timestamp = event_timestamp or datetime.now(timezone.utc)

            # Update global task definitions map (best-effort, last-writer-wins).
            if task_definitions:
                self._task_definitions_by_name.update(task_definitions)

            # If worker already exists, update it (restart scenario)
            if worker_id in self._workers:
                self._workers[worker_id].registered_tasks = tasks
                self._workers[worker_id].pid = pid
                self._workers[worker_id].last_seen = timestamp
                self._workers[worker_id].status = WorkerStatus.ONLINE
                self._workers[worker_id].shutdown_reported = False
            else:
                # New worker registration
                self._workers[worker_id] = WorkerInfo(
                    hostname=hostname,
                    pid=pid,
                    registered_tasks=tasks,
                    registered_at=timestamp,
                    last_seen=timestamp,
                    status=WorkerStatus.ONLINE,
                )
            self._newest_by_node[hostname] = worker_id

    def get_task_definition(self, name: str) -> RegisteredTaskDefinition | None:
        """Get task definition metadata by task name.

        Args:
            name: Fully qualified task name.

        Returns:
            RegisteredTaskDefinition if known, otherwise None.
        """
        with self._lock:
            return self._task_definitions_by_name.get(name)

    def mark_shutdown(self, hostname: str, pid: int) -> None:
        """Record a worker_shutdown event: the process is gone for good.

        Unlike :meth:`mark_offline`, a later ping reply can't bring it back;
        only registering the worker again (worker_ready, or stats naming this
        pid) does.

        Args:
            hostname: Worker nodename.
            pid: Worker process ID.
        """
        with self._lock:
            worker = self._workers.get(f"{hostname}:{pid}")
            if worker is not None:
                worker.status = WorkerStatus.OFFLINE
                worker.shutdown_reported = True

    def mark_offline(self, hostname: str, pid: int) -> None:
        """Mark a worker offline because it stopped answering, not because it said so.

        A later ping reply may bring it back (see :meth:`mark_node_alive`).

        Args:
            hostname: Worker nodename.
            pid: Worker process ID.
        """
        with self._lock:
            worker = self._workers.get(f"{hostname}:{pid}")
            if worker is not None:
                worker.status = WorkerStatus.OFFLINE

    def mark_node_alive(self, hostname: str, pid: int | None = None) -> None:
        """Apply "this nodename answered a Celery inspect call" to its processes.

        A nodename has at most one live process, so every other pid registered
        under it is superseded and goes offline. The live one is ``pid`` when
        the reply carries it (``stats()``). A ping reply carries no pid, so the
        live one is the pid registered last, and only if no worker_shutdown
        event closed it. Updates its ``last_seen`` and brings it online.

        This is used by on-demand refresh operations (e.g., Celery inspect) to
        avoid mutating WorkerInfo outside of the registry lock.

        Args:
            hostname: Worker nodename that answered.
            pid: Live process ID if the reply names it, otherwise None.
        """
        with self._lock:
            if pid is None:
                newest_id = self._newest_by_node.get(hostname)
                live = self._workers.get(newest_id) if newest_id else None
            else:
                live = self._workers.get(f"{hostname}:{pid}")

            for worker in self._workers.values():
                if worker.hostname == hostname and worker is not live:
                    worker.status = WorkerStatus.OFFLINE

            if live is None or (pid is None and live.shutdown_reported):
                return
            live.status = WorkerStatus.ONLINE
            live.shutdown_reported = False
            live.last_seen = datetime.now(timezone.utc)

    def get_registered_tasks(self, hostname: str, pid: int) -> list[str]:
        """Get registered tasks for a specific worker.

        Args:
            hostname: Worker hostname.
            pid: Worker process ID.

        Returns:
            List of registered task names, or empty list if worker not found.
        """
        with self._lock:
            worker_id = f"{hostname}:{pid}"
            worker = self._workers.get(worker_id)
            if worker:
                # Return a copy to prevent callers mutating internal state after
                # the lock is released.
                return list(worker.registered_tasks)
        return []

    def get_all_workers(self) -> list[WorkerInfo]:
        """Get all registered workers.

        Returns:
            List of WorkerInfo, sorted by last_seen (most recent first).
        """
        with self._lock:
            return sorted(
                self._workers.values(),
                key=lambda w: w.last_seen,
                reverse=True,
            )

    def get_worker(self, hostname: str, pid: int) -> WorkerInfo | None:
        """Get worker info by hostname and PID.

        Args:
            hostname: Worker hostname.
            pid: Worker process ID.

        Returns:
            WorkerInfo if found, None otherwise.
        """
        with self._lock:
            worker_id = f"{hostname}:{pid}"
            return self._workers.get(worker_id)

    def get_workers_by_hostname(self, hostname: str) -> list[WorkerInfo]:
        """Get all workers matching a hostname.

        Args:
            hostname: Worker hostname to filter by.

        Returns:
            List of WorkerInfo matching the hostname, sorted by last_seen (most recent first).
        """
        with self._lock:
            matching = [w for w in self._workers.values() if w.hostname == hostname]
            return sorted(matching, key=lambda w: w.last_seen, reverse=True)

    def remove_stale_workers(
        self,
        stale_timeout_minutes: int = 3,
        cleanup_timeout_minutes: int = 30,
    ) -> None:
        """Mark workers as offline if not seen recently, remove very old ones.

        Args:
            stale_timeout_minutes: Minutes of inactivity before marking stale.
            cleanup_timeout_minutes: Minutes since last seen before removing
                offline workers entirely (reduces clutter from old sessions).
        """
        with self._lock:
            now = datetime.now(timezone.utc)
            stale_cutoff = now - timedelta(minutes=stale_timeout_minutes)
            cleanup_cutoff = now - timedelta(minutes=cleanup_timeout_minutes)

            # Mark stale workers offline, remove very old offline workers
            to_remove: list[str] = []
            for worker_id, worker_info in self._workers.items():
                if worker_info.last_seen < stale_cutoff:
                    worker_info.status = WorkerStatus.OFFLINE
                # Remove offline workers not seen for a long time
                if (
                    worker_info.status == WorkerStatus.OFFLINE
                    and worker_info.last_seen < cleanup_cutoff
                ):
                    to_remove.append(worker_id)

            for worker_id in to_remove:
                worker = self._workers.pop(worker_id)
                if self._newest_by_node.get(worker.hostname) == worker_id:
                    del self._newest_by_node[worker.hostname]


class GraphStore:
    """Thread-safe in-memory store for TaskGraph with LRU eviction."""

    def __init__(self, max_nodes: int = 10000) -> None:
        """Initialize store with optional maximum node limit for LRU eviction."""
        self._graph = TaskGraph()
        self._lock = threading.RLock()
        self._max_nodes = max_nodes
        self._listeners: list[Callable[[TaskEvent], None]] = []

    def add_event(self, event: TaskEvent) -> None:
        """Add event to graph and notify listeners.

        Listeners receive the event as stored (naive timestamps normalized to
        UTC). Exact duplicate events (e.g. replayed from the broker) are
        ignored and not broadcast.
        """
        with self._lock:
            stored = self._graph.add_event(event)
            if stored is None:
                return
            self._maybe_evict()

        for listener in self._listeners:
            with contextlib.suppress(Exception):
                listener(stored)

    def get_node(self, task_id: str) -> TaskNode | None:
        """Get a snapshot of a node by ID, or None if not found."""
        with self._lock:
            node = self._graph.get_node(task_id)
            return _snapshot(node) if node is not None else None

    def get_nodes(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        state: TaskState | None = None,
        name_contains: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> tuple[list[TaskNode], int]:
        """Get nodes with optional filtering, most recent first.

        Excludes synthetic nodes (GROUP, CHORD) which are for graph
        visualization only.

        Returns:
            Tuple of (filtered nodes, total count matching filters).
        """
        # Filter, sort and copy in one critical section: the consumer thread
        # mutates live nodes, so a node could otherwise change state between
        # passing the filter and being copied.
        with self._lock:
            # Exclude synthetic nodes (GROUP, CHORD) - they're for graphs, not task list
            nodes = [
                n for n in self._graph.nodes.values() if n.node_type == NodeType.TASK
            ]

            if state is not None:
                nodes = [n for n in nodes if n.state == state]
            if name_contains is not None:
                name_lower = name_contains.lower()
                nodes = [n for n in nodes if name_lower in n.name.lower()]
            if from_date is not None:
                from_dt = _ensure_tz_aware(from_date)
                nodes = [
                    n
                    for n in nodes
                    if n.last_updated is not None and n.last_updated >= from_dt
                ]
            if to_date is not None:
                to_dt = _ensure_end_of_day(to_date)
                nodes = [
                    n
                    for n in nodes
                    if n.first_seen is not None and n.first_seen <= to_dt
                ]

            nodes.sort(key=lambda n: n.last_updated or _MIN_DATETIME, reverse=True)
            total = len(nodes)
            page = [_snapshot(n) for n in nodes[offset : offset + limit]]
        return page, total

    def get_root_nodes(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> tuple[list[TaskNode], int]:
        """Get root nodes (no parent), most recent first.

        Returns:
            Tuple of (filtered root nodes, total count matching filters).
        """
        with self._lock:
            live, total = self._root_nodes_locked(
                limit=limit, offset=offset, from_date=from_date, to_date=to_date
            )
            return [_snapshot(n) for n in live], total

    def get_root_nodes_with_children(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> tuple[list[TaskNode], dict[str, TaskNode], int]:
        """Get root nodes and their direct children from one consistent snapshot.

        Returns:
            Tuple of (filtered root nodes, map of task_id to node for the roots
            and their direct children, total count matching filters).
        """
        with self._lock:
            live, total = self._root_nodes_locked(
                limit=limit, offset=offset, from_date=from_date, to_date=to_date
            )
            roots = [_snapshot(n) for n in live]
            related: dict[str, TaskNode] = {}
            for root in roots:
                related[root.task_id] = root
                for child_id in root.children:
                    child = self._graph.nodes.get(child_id)
                    if child is not None and child_id not in related:
                        related[child_id] = _snapshot(child)
            return roots, related, total

    def _root_nodes_locked(
        self,
        *,
        limit: int,
        offset: int,
        from_date: datetime | None,
        to_date: datetime | None,
    ) -> tuple[list[TaskNode], int]:
        """Filter, sort and page live root nodes. Call with the lock held."""
        root_nodes = [
            self._graph.nodes[rid]
            for rid in self._graph.root_ids
            if rid in self._graph.nodes
        ]

        # Date filtering - use child timestamps for synthetic nodes
        if from_date is not None:
            from_dt = _ensure_tz_aware(from_date)
            root_nodes = [
                n for n in root_nodes if _get_node_timestamp(n, self._graph) >= from_dt
            ]
        if to_date is not None:
            to_dt = _ensure_end_of_day(to_date)
            root_nodes = [
                n for n in root_nodes if _get_first_timestamp(n, self._graph) <= to_dt
            ]

        # Sort needs the graph for synthetic nodes' children
        root_nodes.sort(
            key=lambda n: _get_node_timestamp(n, self._graph),
            reverse=True,
        )
        total = len(root_nodes)
        return root_nodes[offset : offset + limit], total

    def get_node_with_children(
        self, task_id: str
    ) -> tuple[TaskNode, list[TaskNode]] | None:
        """Get a node and its direct children from one consistent snapshot.

        Returns:
            Tuple of (node, children), or None if the node doesn't exist.
        """
        with self._lock:
            node = self._graph.get_node(task_id)
            if node is None:
                return None
            snapshot = _snapshot(node)
            children = [
                _snapshot(self._graph.nodes[cid])
                for cid in snapshot.children
                if cid in self._graph.nodes
            ]
            return snapshot, children

    def get_children(self, task_id: str) -> list[TaskNode]:
        """Get child nodes of a task."""
        with self._lock:
            node = self._graph.get_node(task_id)
            if node is None:
                return []
            return [
                _snapshot(self._graph.nodes[cid])
                for cid in node.children
                if cid in self._graph.nodes
            ]

    def get_graph_from_root(self, root_id: str) -> dict[str, TaskNode]:
        """Get all nodes in subgraph starting from root."""
        with self._lock:
            root = self._graph.get_node(root_id)
            if root is None:
                return {}

            result: dict[str, TaskNode] = {}
            to_visit = [root_id]

            while to_visit:
                current_id = to_visit.pop()
                if current_id in result:
                    continue
                node = self._graph.get_node(current_id)
                if node is None:
                    continue
                result[current_id] = _snapshot(node)
                to_visit.extend(node.children)

            return result

    def add_listener(self, callback: Callable[[TaskEvent], None]) -> None:
        """Register callback for new events (used by WebSocket manager)."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[TaskEvent], None]) -> None:
        """Unregister an event listener."""
        with contextlib.suppress(ValueError):
            self._listeners.remove(callback)

    @property
    def node_count(self) -> int:
        """Current node count."""
        with self._lock:
            return len(self._graph.nodes)

    def get_unique_task_names(self) -> set[str]:
        """Get all unique task names seen in events.

        Excludes synthetic nodes (GROUP, CHORD) which have placeholder
        names like 'group' or 'chord'.
        """
        with self._lock:
            return {
                node.name
                for node in self._graph.nodes.values()
                if node.node_type == NodeType.TASK
            }

    def get_task_execution_count(self, task_name: str) -> int:
        """Get number of executions (nodes) for a task name.

        Excludes synthetic nodes (GROUP, CHORD).

        Args:
            task_name: Fully qualified task name.

        Returns:
            Number of task executions (TaskNodes) with this name.
        """
        with self._lock:
            return sum(
                1
                for node in self._graph.nodes.values()
                if node.name == task_name and node.node_type == NodeType.TASK
            )

    def get_last_execution_time(self, task_name: str) -> datetime | None:
        """Get the most recent execution timestamp for a task name.

        Returns the latest event timestamp across all executions of this task.
        Excludes synthetic nodes (GROUP, CHORD).

        Args:
            task_name: Fully qualified task name.

        Returns:
            Most recent event timestamp, or None if task has never been executed.
        """
        with self._lock:
            last_time: datetime | None = None
            for node in self._graph.nodes.values():
                if (
                    node.name == task_name
                    and node.node_type == NodeType.TASK
                    and node.last_updated is not None
                ):
                    node_time = node.last_updated
                    if last_time is None or node_time > last_time:
                        last_time = node_time
            return last_time

    def _maybe_evict(self) -> None:
        """Evict oldest 10% when over capacity. Call with lock held."""
        if len(self._graph.nodes) <= self._max_nodes:
            return

        nodes_by_age = sorted(
            self._graph.nodes.values(),
            key=lambda n: n.first_seen or _MIN_DATETIME,
        )

        to_remove = len(nodes_by_age) - int(self._max_nodes * 0.9)
        for node in nodes_by_age[:to_remove]:
            if node.parent_id and node.parent_id in self._graph.nodes:
                parent = self._graph.nodes[node.parent_id]
                if node.task_id in parent.children:
                    parent.children.remove(node.task_id)

            if node.task_id in self._graph.root_ids:
                self._graph.root_ids.remove(node.task_id)

            del self._graph.nodes[node.task_id]
