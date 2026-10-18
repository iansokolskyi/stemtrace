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
    from collections.abc import Collection

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


def _eviction_age(node: TaskNode, graph: TaskGraph) -> tuple[datetime, int]:
    """Sort key for eviction: a task's first event; a group's newest member's.

    Synthetic nodes sort after tasks of the same age.
    """
    if node.first_seen is not None:
        return node.first_seen, 0
    member_times = [
        child.first_seen
        for child_id in node.children
        if (child := graph.get_node(child_id)) is not None
        and child.first_seen is not None
    ]
    return max(member_times, default=_MIN_DATETIME), 1


def _eviction_units(graph: TaskGraph) -> list[list[TaskNode]]:
    """The graph's nodes, each GROUP/CHORD together with all it contains.

    A container's ``children`` are its members (a chord's headers and its
    callback); a member that is itself a container brings its own members.
    """
    owner = {
        child_id: node.task_id
        for node in graph.nodes.values()
        if node.node_type != NodeType.TASK
        for child_id in node.children
    }

    def top(node_id: str) -> str:
        """The outermost container holding ``node_id``, or ``node_id`` itself."""
        seen: set[str] = set()
        while node_id in owner and node_id not in seen:
            seen.add(node_id)
            node_id = owner[node_id]
        return node_id

    units: dict[str, list[TaskNode]] = {}
    for node in graph.nodes.values():
        units.setdefault(top(node.task_id), []).append(node)
    return list(units.values())


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
    from collections.abc import Callable, Iterable, Mapping

    from stemtrace.core.events import TaskEvent, TaskState


# A worker not seen for this long is shown offline (see remove_stale_workers).
# Seen means a worker_ready, a task event from its nodename, or an inspect reply.
WORKER_STALE_TIMEOUT_MINUTES = 3


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
        # Nodename -> number of worker events (ready/shutdown) applied. A
        # Celery inspect reply is a snapshot of the node from when it was
        # asked; an event applied since is newer than it (see apply_stats_reply).
        self._event_generation: dict[str, int] = {}
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
        """Record a worker_ready event: the worker is online.

        A nodename has at most one live process, so registering a pid retires
        the node's other pids (a worker killed without worker_shutdown, then
        restarted). Updates the worker if already registered (pid reuse).

        Args:
            hostname: Worker nodename.
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
            self._register(hostname, pid, tasks, task_definitions, event_timestamp)
            self._event_generation[hostname] = (
                self._event_generation.get(hostname, 0) + 1
            )
            self._retire_other_pids(hostname, keep=f"{hostname}:{pid}")

    def _register(
        self,
        hostname: str,
        pid: int,
        tasks: list[str],
        task_definitions: dict[str, RegisteredTaskDefinition] | None,
        event_timestamp: datetime | None,
    ) -> None:
        """Add or refresh one worker entry and make it the node's newest."""
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

    def _retire_other_pids(self, hostname: str, keep: str) -> None:
        """Mark every pid of the node except ``keep`` (a worker_id) offline."""
        for worker_id, worker in self._workers.items():
            if worker.hostname == hostname and worker_id != keep:
                worker.status = WorkerStatus.OFFLINE

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
            self._event_generation[hostname] = (
                self._event_generation.get(hostname, 0) + 1
            )
            worker = self._workers.get(f"{hostname}:{pid}")
            if worker is not None:
                worker.status = WorkerStatus.OFFLINE
                worker.shutdown_reported = True

    def mark_seen(
        self, hostname: str, timestamp: datetime, *, now: datetime | None = None
    ) -> None:
        """Record a task event from ``hostname``: its worker is alive.

        Without this a busy worker went offline ``WORKER_STALE_TIMEOUT_MINUTES``
        after its worker_ready, since only that and inspect replies moved
        ``last_seen``. A task event names no pid, so it refreshes the node's
        live entry, the pid registered last (a nodename has one live process).
        ``timestamp`` is the event's (the worker's clock), as for worker_ready,
        so a server reading back an old stream doesn't make a dead worker look
        alive; a time ahead of the server's clock counts as now (a worker clock
        running ahead would otherwise keep it online after it stopped), a naive
        one as UTC, and ``last_seen`` never moves back. The worker comes back
        online only if that time is within the stale window, and never after it
        reported worker_shutdown (a task's last events can arrive after it).

        Args:
            hostname: Worker nodename from the task event.
            timestamp: The event's time.
            now: Current time (tests); defaults to now.
        """
        with self._lock:
            live_id = self._newest_by_node.get(hostname)
            worker = self._workers.get(live_id) if live_id else None
            if worker is None:
                return
            current = now or datetime.now(timezone.utc)
            seen_at = min(_ensure_tz_aware(timestamp), current)
            if seen_at > worker.last_seen:
                worker.last_seen = seen_at
            recent = seen_at >= current - timedelta(
                minutes=WORKER_STALE_TIMEOUT_MINUTES
            )
            if recent and not worker.shutdown_reported:
                worker.status = WorkerStatus.ONLINE

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
            live_id = (
                self._newest_by_node.get(hostname)
                if pid is None
                else f"{hostname}:{pid}"
            )
            live = self._workers.get(live_id) if live_id else None
            self._retire_other_pids(hostname, keep=live_id or "")

            if live is None or (pid is None and live.shutdown_reported):
                return
            live.status = WorkerStatus.ONLINE
            live.shutdown_reported = False
            live.last_seen = datetime.now(timezone.utc)

    def event_generations(self) -> dict[str, int]:
        """Snapshot, per nodename, how many worker events were applied so far.

        Take it before asking Celery to inspect, and hand it to
        :meth:`apply_stats_reply` with the reply.

        Returns:
            Nodename -> event count (a missing nodename means 0).
        """
        with self._lock:
            return dict(self._event_generation)

    def _is_stale(self, hostname: str, seen: Mapping[str, int]) -> bool:
        """Whether a worker event for the node arrived after ``seen`` was taken."""
        return self._event_generation.get(hostname, 0) != seen.get(hostname, 0)

    def apply_stats_reply(
        self,
        seen: Mapping[str, int],
        replies: Mapping[str, tuple[int | None, list[str] | None]],
    ) -> None:
        """Apply a non-empty ``inspect.stats()`` reply, atomically.

        Source precedence, per nodename: a worker event applied after ``seen``
        was taken beats the reply (the reply was produced before that event,
        up to 0.5 s earlier, so it would undo it); a reply beats events applied
        before ``seen`` (it is the fresher observation, e.g. a pid the OS
        reused after worker_shutdown). Nodes whose events are newer are left
        alone; the next refresh applies a reply that includes them.

        Known nodes missing from ``replies`` go offline. For each replying node:
        its pid is registered (event timestamps don't apply: it is observed
        now) and its other pids retire; a reply without a pid acts like a ping.

        Args:
            seen: ``event_generations()`` taken before the inspect call.
            replies: Nodename -> (pid or None, registered task names or None).
                None tasks keep the worker's current list.
        """
        with self._lock:
            for worker in self._workers.values():
                if worker.hostname not in replies and not self._is_stale(
                    worker.hostname, seen
                ):
                    worker.status = WorkerStatus.OFFLINE

            now = datetime.now(timezone.utc)
            for nodename, (pid, tasks) in replies.items():
                if self._is_stale(nodename, seen):
                    continue
                if pid is not None:
                    current = self.get_registered_tasks(nodename, pid)
                    self._register(
                        nodename, pid, current if tasks is None else tasks, None, now
                    )
                self.mark_node_alive(nodename, pid)

    def apply_ping_reply(self, nodenames: Iterable[str]) -> None:
        """Apply an ``inspect.ping()`` reply.

        Unlike a stats reply, a ping can't undo a worker event that arrived
        while it ran, so it needs no event snapshot: it names no pid, so it
        only revives the node's newest pid (never one that reported
        worker_shutdown) and retires the others, the state any such event
        leaves anyway.

        Args:
            nodenames: Nodenames that answered.
        """
        with self._lock:
            for nodename in nodenames:
                self.mark_node_alive(nodename)

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
        stale_timeout_minutes: int = WORKER_STALE_TIMEOUT_MINUTES,
        cleanup_timeout_minutes: int = 30,
        *,
        now: datetime | None = None,
    ) -> None:
        """Mark workers as offline if not seen recently, remove very old ones.

        Args:
            stale_timeout_minutes: Minutes of inactivity before marking stale.
            cleanup_timeout_minutes: Minutes since last seen before removing
                offline workers entirely (reduces clutter from old sessions).
            now: Current time (tests); defaults to now.
        """
        with self._lock:
            now = now or datetime.now(timezone.utc)
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
        name_exact: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        hostname: str | None = None,
        queue: str | None = None,
        exception_type: str | None = None,
        task_names: Collection[str] | None = None,
    ) -> tuple[list[TaskNode], int]:
        """Get nodes with optional filtering, most recent first.

        Excludes synthetic nodes (GROUP, CHORD) which are for graph
        visualization only.

        ``name_contains`` matches a case-insensitive substring of the task
        name; ``name_exact`` matches the whole name, case-sensitively like
        Celery's task registry (``app.add`` does not match ``app.add_numbers``
        or ``app.Add``). Both can be given; a node must pass each.
        ``hostname``, ``queue`` and ``exception_type`` match the node's
        value (see ``TaskNode._latest``) exactly; computing it reads all of
        a node's events, so these cost several times the name filters.
        ``task_names`` keeps the tasks with one of these names (the API's
        task group and tag filters resolve names to it beforehand, outside
        the lock); pass a set for O(1) lookups.

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
            if name_exact is not None:
                nodes = [n for n in nodes if n.name == name_exact]
            if hostname is not None:
                nodes = [n for n in nodes if n.hostname == hostname]
            if queue is not None:
                nodes = [n for n in nodes if n.queue == queue]
            if exception_type is not None:
                nodes = [n for n in nodes if n.exception_type == exception_type]
            if task_names is not None:
                nodes = [n for n in nodes if n.name in task_names]
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
        task_names: Collection[str] | None = None,
    ) -> tuple[list[TaskNode], dict[str, TaskNode], int]:
        """Get root nodes and their direct children from one consistent snapshot.

        With ``task_names``, only graphs with a task of one of these names
        (anywhere in the graph, not only the root) are listed.

        Returns:
            Tuple of (filtered root nodes, map of task_id to node for the roots
            and their direct children, total count matching filters).
        """
        with self._lock:
            live, total = self._root_nodes_locked(
                limit=limit,
                offset=offset,
                from_date=from_date,
                to_date=to_date,
                task_names=task_names,
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
        task_names: Collection[str] | None = None,
    ) -> tuple[list[TaskNode], int]:
        """Filter, sort and page live root nodes. Call with the lock held."""
        root_nodes = [
            self._graph.nodes[rid]
            for rid in self._graph.root_ids
            if rid in self._graph.nodes
        ]
        if task_names is not None:
            matching = self._roots_with_task_locked(task_names)
            root_nodes = [n for n in root_nodes if n.task_id in matching]

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

    def _roots_with_task_locked(self, task_names: Collection[str]) -> set[str]:
        """IDs of the roots whose graph has a task named one of ``task_names``.

        Walks up from each accepted task until a root (every node's parent
        chain ends at one) or a node an earlier walk passed, whose root is
        already found; so the whole store costs O(nodes). Call with the lock
        held.
        """
        nodes = self._graph.nodes
        seen: set[str] = set()
        found: set[str] = set()
        for node in nodes.values():
            if node.node_type != NodeType.TASK or node.name not in task_names:
                continue
            current = node
            while current.task_id not in seen:
                seen.add(current.task_id)
                parent = nodes.get(current.parent_id or "")
                if parent is None:
                    found.add(current.task_id)
                    break
                current = parent
        return found

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
        """Evict the oldest units down to 90% of capacity. Call with lock held.

        A group or chord goes whole, with its members and callback, so its
        state never comes from part of them; one eviction can free over 10%.
        A unit too big to keep (over 90% of ``max_nodes``) loses its oldest
        members instead.
        """
        if len(self._graph.nodes) <= self._max_nodes:
            return
        keep = int(self._max_nodes * 0.9)
        units = sorted(
            _eviction_units(self._graph),
            key=lambda unit: max(_eviction_age(n, self._graph) for n in unit),
        )
        for unit in units:
            excess = len(self._graph.nodes) - keep
            if excess <= 0:
                return
            # A unit is as old as its newest node (see _eviction_units).
            if len(unit) > keep:
                unit = sorted(unit, key=lambda n: _eviction_age(n, self._graph))
                unit = unit[:excess]
            for node in unit:
                self._graph.remove_node(node.task_id)
