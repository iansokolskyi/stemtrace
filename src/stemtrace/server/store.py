"""Thread-safe in-memory graph store."""

from __future__ import annotations

import bisect
import contextlib
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, NamedTuple

from pydantic import BaseModel

from stemtrace.core.anomalies import RegistryFacts
from stemtrace.core.events import TaskState, WorkerEventType
from stemtrace.core.graph import NodeType, TaskGraph, TaskNode
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.inspect_probe import INSPECT_REFRESH_MIN_INTERVAL_SECONDS
from stemtrace.server.stats import RunRecord

if TYPE_CHECKING:
    from collections.abc import Collection

    from stemtrace.core.events import (
        RegisteredTaskDefinition,
        TaskEvent,
        WorkerEvent,
    )


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
    # True while a newer pid of the same nodename is the live process.
    superseded: bool = False
    # Stamp of the newest worker_ready for this pid; None if it was only ever
    # seen in a stats reply (D4e reading 5: a stop for an unlisted pid is
    # compared with this, else with registered_at).
    ready_at: datetime | None = None


def ensure_tz_aware(dt: datetime) -> datetime:
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


def _root_of_each(nodes: Mapping[str, TaskNode]) -> dict[str, str | None]:
    """Each node's root: the end of its parent chain, walked once per node.

    A walk stops at a node an earlier walk resolved, so the whole store costs
    O(nodes). A node on a parent loop has no root (``None``).
    """
    root_of: dict[str, str | None] = {}
    for start, node in nodes.items():
        parent = node.parent_id
        if parent is None or parent not in nodes:
            root_of[start] = start
            continue
        if parent in root_of and start not in root_of:
            root_of[start] = root_of[parent]  # the usual case: parents come first
            continue
        if start in root_of:
            continue
        path: list[str] = []
        current = start
        while current not in root_of:
            root_of[current] = None  # met again on this walk: a parent loop
            path.append(current)
            parent = nodes[current].parent_id
            if parent is None or parent not in nodes:
                root_of[current] = current
                path.pop()
                break
            current = parent
        root = root_of[current]
        for node_id in path:
            root_of[node_id] = root
    return root_of


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
    """The graph's nodes, each GROUP/CHORD together with what shows its state.

    That is every node drawn directly inside it (a GROUP/CHORD too), its member
    tasks, and the tasks between a member and it (a chain's earlier steps, the
    callback's chain). Tasks a member starts, and a workflow its callback
    starts, are units of their own.
    """
    root = {node_id: node_id for node_id in graph.nodes}

    def find(node_id: str) -> str:
        """The representative of ``node_id``'s unit."""
        while root[node_id] != node_id:
            root[node_id] = root[root[node_id]]
            node_id = root[node_id]
        return node_id

    for node_id, owner in _unit_links(graph):
        root[find(node_id)] = find(owner)
    units: dict[str, list[TaskNode]] = {}
    for node in graph.nodes.values():
        units.setdefault(find(node.task_id), []).append(node)
    return list(units.values())


def _unit_links(graph: TaskGraph) -> list[tuple[str, str]]:
    """Pairs of nodes in one eviction unit (see ``_eviction_units``)."""
    links: list[tuple[str, str]] = []
    containers: dict[str, set[str]] = {}
    for node in graph.nodes.values():
        if node.node_type != NodeType.TASK and node.group_id is not None:
            containers.setdefault(node.group_id, set()).add(node.task_id)
        parent = graph.nodes.get(node.parent_id) if node.parent_id else None
        if parent is not None and parent.node_type != NodeType.TASK:
            links.append((node.task_id, parent.task_id))  # drawn directly inside
    for node in graph.nodes.values():
        owners = containers.get(node.group_id or "")
        if node.node_type != NodeType.TASK or owners is None:
            continue
        steps, above = _path_up(graph, node)
        # Stamped with this group but drawn inside another container (a
        # pipeline's last task): the drawn-inside links put it in that one.
        if above is None or above in owners:
            links.extend((node.task_id, owner) for owner in owners)
        links.extend((step, node.task_id) for step in steps)
    return links


def _path_up(graph: TaskGraph, node: TaskNode) -> tuple[list[str], str | None]:
    """The tasks above ``node`` up to the first GROUP/CHORD, and that container.

    ``(no tasks, None)`` when the walk ends without one: at a root, a parent
    that isn't stored, or a parent loop.
    """
    steps: list[str] = []
    current = node
    while current.parent_id is not None and current.parent_id not in steps:
        parent = graph.nodes.get(current.parent_id)
        if parent is None:
            break
        if parent.node_type != NodeType.TASK:
            return steps, parent.task_id
        steps.append(parent.task_id)
        current = parent
    return [], None


if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping

    from stemtrace.core.events import TaskEvent


def _monotonic() -> float:
    """A monotonic reading (a seam for tests); the miss gap is measured on it."""
    return time.monotonic()


def _utcnow() -> datetime:
    """The registry's clock (a seam for tests that replay a timeline)."""
    return datetime.now(timezone.utc)


# A worker not seen for this long is shown offline (see remove_stale_workers).
# Seen means a worker_ready, a task event from its nodename, or an inspect reply.
WORKER_STALE_TIMEOUT_MINUTES = 3


# Bounds of the worker timeline: events kept per nodename, nodenames kept.
WORKER_TIMELINE_EVENTS_PER_NODE = 32
WORKER_TIMELINE_MAX_NODES = 1024
# Nodenames kept that the registry doesn't list (see WorkerRegistry._unlisted).
WORKER_UNLISTED_MAX_NODES = 1024

# Nodenames that sent task events but never registered, remembered so each one
# announces itself once instead of on every event.
UNREGISTERED_NODES_REMEMBERED = 1024


class WorkerTimelineEntry(NamedTuple):
    """One worker event of a nodename, as the worker's clock stamped it."""

    timestamp: datetime
    event_type: WorkerEventType
    pid: int


class WorkerTimeline:
    """Bounded per-nodename history of worker events, kept apart from the registry.

    ``WorkerRegistry`` deletes offline workers after 30 minutes and
    ``mark_shutdown`` drops the event time; this keeps ``(timestamp, type, pid)``.
    """

    # Threading model: the consumer thread writes, request threads read; every
    # access holds ``_lock``. Readers get copies of immutable entries.

    def __init__(
        self,
        max_events_per_node: int = WORKER_TIMELINE_EVENTS_PER_NODE,
        max_nodes: int = WORKER_TIMELINE_MAX_NODES,
    ) -> None:
        """Initialize an empty timeline with the given bounds.

        Raises:
            ValueError: If either bound is below 1.
        """
        if max_events_per_node < 1 or max_nodes < 1:
            raise ValueError("max_events_per_node and max_nodes must be at least 1")
        self._max_events_per_node = max_events_per_node
        self._max_nodes = max_nodes
        # Nodename -> entries sorted ascending by (timestamp, type, pid), no
        # duplicates. Sorting by the whole entry makes the kept set (the newest
        # K) independent of arrival order, which Redis replay and RabbitMQ
        # don't guarantee.
        self._by_node: dict[str, list[WorkerTimelineEntry]] = {}
        self._lock = threading.RLock()

    def record(self, event: WorkerEvent) -> None:
        """Add a worker event (any type) to its nodename's timeline."""
        # A naive timestamp (older worker) can't be compared with aware ones.
        entry = WorkerTimelineEntry(
            ensure_tz_aware(event.timestamp), event.event_type, event.pid
        )
        with self._lock:
            entries = self._by_node.get(event.hostname, [])
            index = bisect.bisect_left(entries, entry)
            if index == len(entries) or entries[index] != entry:
                entries.insert(index, entry)
                del entries[: -self._max_events_per_node]
            self._by_node[event.hostname] = entries
            if len(self._by_node) > self._max_nodes:
                self._evict_oldest_node()

    def events_for(self, nodename: str) -> list[WorkerTimelineEntry]:
        """Return a copy of the nodename's entries, oldest first (empty if unknown)."""
        with self._lock:
            return list(self._by_node.get(nodename, ()))

    def snapshot(self) -> dict[str, list[WorkerTimelineEntry]]:
        """Return a copy of every nodename's entries, taken in one locked pass."""
        with self._lock:
            return {node: list(entries) for node, entries in self._by_node.items()}

    def _evict_oldest_node(self) -> None:
        """Drop the nodename whose newest event is oldest (ties: smallest name)."""
        oldest = min(self._by_node, key=lambda n: (self._by_node[n][-1], n))
        del self._by_node[oldest]


class WorkerRegistry:
    """Thread-safe registry for worker lifecycle tracking.

    Maintains per-worker task lists and online status from
    worker_ready and worker_shutdown events.
    """

    def __init__(self, max_unlisted: int = WORKER_UNLISTED_MAX_NODES) -> None:
        """Initialize empty worker registry.

        Args:
            max_unlisted: Bound on the nodenames kept outside the worker list.
        """
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
        # When the last inspect reply listed the tasks of every node that
        # answered. Not the refresher's clock, which also moves on empty and
        # ping-only replies; kept apart from the workers, so the cleanup that
        # deletes them leaves it.
        self._authority_at: datetime | None = None
        # Fleet rule (no_workers_online) state. All of it is anomaly-only: the
        # Workers page never shows it, and the cleanup that deletes workers
        # leaves ``_ever_seen``.
        self._ever_seen = False
        # Live nodenames the Workers page doesn't list: named by a task event
        # or a pid-less reply before worker_ready or a stats reply did, or
        # removed by the cleanup with no evidence they stopped. Nodename ->
        # last_seen. Bounded.
        self._unlisted: dict[str, datetime] = {}
        # Nodename -> newest task-event time (the worker's clock) of an unlisted
        # nodename. A stop is compared with this, never with ``_unlisted``, whose
        # value a ping moves to server time (D4e reading 12).
        self._event_at: dict[str, datetime] = {}
        self._max_unlisted = max_unlisted
        # Nodename -> time of its latest reported stop (the event's clock), so
        # a task's last events, stamped before it and arriving after, don't
        # make an unlisted nodename live again. Cleared by a worker_ready or
        # stats reply naming a pid; bounded like ``_unlisted``.
        self._stopped_at: dict[str, tuple[datetime, datetime]] = {}
        # Nodename -> its last two counted misses (completion on the wall clock,
        # for comparing with ``last_seen``, and on the monotonic clock, for the
        # 2 s gap), stamped in the locked step that applied that broadcast's
        # answers. A miss is evidence only while it completed after the node's
        # ``last_seen``; two make it silent (D4d). An answer drops them.
        self._misses: dict[str, list[tuple[datetime, float]]] = {}
        # Nodenames that already owed a confirm since they last answered: events
        # and later misses don't re-arm it (D4d reading 1).
        self._confirm_spent: set[str] = set()
        # Whether any worker has answered an inspect since the server started.
        # Until then an unanswered broadcast may only mean the server can't hear
        # workers (remote control off, an unshared control setting), not that
        # they are gone.
        self._channel_proven = False
        self._new_node_listeners: list[Callable[[str], None]] = []
        self._first_miss_listeners: list[Callable[[], None]] = []
        self._announced_unregistered: dict[str, None] = {}
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
            is_new = hostname not in self._newest_by_node
            self._register(hostname, pid, tasks, task_definitions, event_timestamp)
            ready = self._workers[f"{hostname}:{pid}"]
            stamp = ensure_tz_aware(event_timestamp or ready.last_seen)
            ready.ready_at = max(ready.ready_at or stamp, stamp)
            self._event_at.pop(
                hostname, None
            )  # a new process: older events aren't its own
            self._event_generation[hostname] = (
                self._event_generation.get(hostname, 0) + 1
            )
            self._retire_other_pids(hostname, keep=f"{hostname}:{pid}")
        if is_new:
            self._notify_new_node([hostname])

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
            # A worker_ready is a new process (D4d reading 7): it can owe a confirm again.
            self._confirm_spent.discard(hostname)
            worker_id = f"{hostname}:{pid}"
            # Use event timestamp for last_seen (important for Redis replay)
            # Fall back to now() for direct calls (e.g., tests)
            timestamp = ensure_tz_aware(event_timestamp or _utcnow())

            # Update global task definitions map (best-effort, last-writer-wins).
            if task_definitions:
                self._task_definitions_by_name.update(task_definitions)

            # If worker already exists, update it (restart scenario)
            if worker_id in self._workers:
                self._workers[worker_id].registered_tasks = tasks
                self._workers[worker_id].pid = pid
                # Never back: older counted misses would count again (D4d reading 2).
                self._workers[worker_id].last_seen = max(
                    self._workers[worker_id].last_seen, ensure_tz_aware(timestamp)
                )
                self._workers[worker_id].status = WorkerStatus.ONLINE
                self._workers[worker_id].shutdown_reported = False
                self._workers[worker_id].superseded = False
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
            self._unlisted.pop(hostname, None)
            self._stopped_at.pop(hostname, None)
            self._ever_seen = True
            self._announced_unregistered.pop(hostname, None)

    def _retire_other_pids(self, hostname: str, keep: str) -> None:
        """Mark every pid of the node except ``keep`` (a worker_id) offline and not live."""
        for worker_id, worker in self._workers.items():
            if worker.hostname == hostname and worker_id != keep:
                worker.status = WorkerStatus.OFFLINE
                worker.superseded = True

    def get_task_definition(self, name: str) -> RegisteredTaskDefinition | None:
        """Get task definition metadata by task name.

        Args:
            name: Fully qualified task name.

        Returns:
            RegisteredTaskDefinition if known, otherwise None.
        """
        with self._lock:
            return self._task_definitions_by_name.get(name)

    def mark_shutdown(
        self,
        hostname: str,
        pid: int,
        timestamp: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        """Record a worker_shutdown event: the process is gone for good.

        Unlike :meth:`mark_offline`, a later ping reply can't bring it back;
        only registering the worker again (worker_ready, or stats naming this
        pid) does.

        Args:
            hostname: Worker nodename.
            pid: Worker process ID.
            timestamp: The event's time (the worker's clock); defaults to now.
            now: When the server received it (tests); defaults to now.
        """
        with self._lock:
            self._forget_misses(hostname)
            self._event_generation[hostname] = (
                self._event_generation.get(hostname, 0) + 1
            )
            worker = self._workers.get(f"{hostname}:{pid}")
            received = now or _utcnow()
            stopped = ensure_tz_aware(timestamp) if timestamp else received
            if worker is None:
                worker = self._newest_listed_before(hostname, stopped)
            elif worker.ready_at is not None and stopped < ensure_tz_aware(
                worker.ready_at
            ):
                # A stop of an older process that had this pid number (D4e reading 5).
                worker = None
            if worker is not None:
                worker.status = WorkerStatus.OFFLINE
                worker.shutdown_reported = True
            # A stop stamped before the nodename's last event is a late one: it leaves
            # stop memory but not the liveness the event gave (D4e reading 10).
            last_event = self._event_at.get(hostname)
            if last_event is None or stopped >= last_event:
                self._unlisted.pop(hostname, None)
                self._event_at.pop(hostname, None)
            previous = self._stopped_at.get(hostname)
            if previous is not None:
                stopped = max(stopped, previous[0])
            self._stopped_at[hostname] = (stopped, received)
            if len(self._stopped_at) > self._max_unlisted:
                oldest = min(
                    self._stopped_at, key=lambda n: (self._stopped_at[n][1], n)
                )
                del self._stopped_at[oldest]

    def _newest_listed_before(
        self, hostname: str, stopped: datetime
    ) -> WorkerInfo | None:
        """The node's live listed pid that a stop for an unlisted pid ends (D4e reading 1).

        A nodename has one live process, so a stop stamped at or after the
        pid's newest worker_ready (else its first registration) came from a
        newer process whose worker_ready was missed. Hold the lock.
        """
        newest = self._workers.get(self._newest_by_node.get(hostname, ""))
        if newest is None or newest.shutdown_reported:
            return None
        registered = ensure_tz_aware(newest.ready_at or newest.registered_at)
        return newest if stopped >= registered else None

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
            self._ever_seen = True
            live_id = self._newest_by_node.get(hostname)
            worker = self._workers.get(live_id) if live_id else None
            current = now or _utcnow()
            seen_at = min(ensure_tz_aware(timestamp), current)
            self._event_at[hostname] = max(
                seen_at, self._event_at.get(hostname, seen_at)
            )
            stop = self._stopped_at.get(hostname)
            after_stop = stop is not None and ensure_tz_aware(timestamp) > stop[0]
            announce = False
            if worker is None:
                if stop is None or after_stop:
                    self._see_unlisted(hostname, seen_at)
                announce = self._announce_unregistered(hostname)
            else:
                if worker.shutdown_reported and (
                    after_stop or stop is None or hostname in self._unlisted
                ):
                    # A restart whose worker_ready was missed: the nodename runs
                    # tasks again, though its listed pid stays stopped. Once revived
                    # (or once the stop is forgotten) every event refreshes it.
                    self._see_unlisted(hostname, seen_at)
                if seen_at > worker.last_seen:
                    worker.last_seen = seen_at
                recent = seen_at >= current - timedelta(
                    minutes=WORKER_STALE_TIMEOUT_MINUTES
                )
                if recent and not worker.shutdown_reported:
                    worker.status = WorkerStatus.ONLINE
        if announce:
            self._notify_new_node([hostname])

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

    def mark_node_alive(
        self, hostname: str, pid: int | None = None, *, now: datetime | None = None
    ) -> None:
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
            now: When the node answered (tests); defaults to now.
        """
        with self._lock:
            self._forget_misses(hostname)
            live_id = (
                self._newest_by_node.get(hostname)
                if pid is None
                else f"{hostname}:{pid}"
            )
            live = self._workers.get(live_id) if live_id else None
            self._retire_other_pids(hostname, keep=live_id or "")

            if live is None:
                unlisted = hostname not in self._newest_by_node
                known = hostname in self._unlisted
                if (
                    pid is None
                    and unlisted
                    and (known or hostname not in self._stopped_at)
                ):
                    self._ever_seen = True
                    self._see_unlisted(hostname, now or _utcnow())
                return
            if pid is None and live.shutdown_reported:
                if (
                    hostname in self._unlisted
                ):  # revived by a task event: a ping refreshes it
                    self._see_unlisted(hostname, now or _utcnow())
                return
            live.status = WorkerStatus.ONLINE
            live.shutdown_reported = False
            live.superseded = False
            live.last_seen = now or _utcnow()

    def add_new_node_listener(self, listener: Callable[[str], None]) -> None:
        """Call ``listener(nodename)`` whenever the registry learns a nodename it didn't have.

        It runs on the thread that applied the event or reply, after the
        registry lock is released, so it must return quickly. Adding the same
        listener again is a no-op.
        """
        with self._lock:
            if listener not in self._new_node_listeners:
                self._new_node_listeners.append(listener)

    def add_first_miss_listener(self, listener: Callable[[], None]) -> None:
        """Call ``listener()`` when a completed broadcast is a nodename's first miss.

        It owes that nodename one confirming broadcast (D4d). Same threading
        contract as :meth:`add_new_node_listener`; adding it again is a no-op.
        """
        with self._lock:
            if listener not in self._first_miss_listeners:
                self._first_miss_listeners.append(listener)

    def _announce_unregistered(self, hostname: str) -> bool:
        """Whether a task event from an unregistered node is the first one seen."""
        with self._lock:
            if hostname in self._announced_unregistered:
                return False
            self._announced_unregistered[hostname] = None
            if len(self._announced_unregistered) > UNREGISTERED_NODES_REMEMBERED:
                del self._announced_unregistered[
                    next(iter(self._announced_unregistered))
                ]
            return True

    def _notify_new_node(self, nodenames: Iterable[str]) -> None:
        """Tell the listeners which nodenames were learned. Call without the lock."""
        with self._lock:
            listeners = list(self._new_node_listeners)
        for nodename in nodenames:
            for listener in listeners:
                # A listener is a side channel; it must not break event handling.
                with contextlib.suppress(Exception):
                    listener(nodename)

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
        *,
        now: datetime | None = None,
        monotonic: float | None = None,
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
            now: When the reply arrived (tests); defaults to now.
            monotonic: The monotonic reading at that moment (tests); defaults to now.
        """
        with self._lock:
            known = set(self._newest_by_node)
            for worker in self._workers.values():
                if worker.hostname not in replies and not self._is_stale(
                    worker.hostname, seen
                ):
                    worker.status = WorkerStatus.OFFLINE

            now = now or _utcnow()
            # A node with no list (``registered()`` failed or missed it) keeps
            # its old one, which may be stale: the registry isn't authoritative.
            # Nodes skipped as stale were not applied, so they confirm nothing.
            fresh = [n for n in replies if not self._is_stale(n, seen)]
            if fresh and all(tasks is not None for _, tasks in replies.values()):
                self._authority_at = now
            for nodename, (pid, tasks) in replies.items():
                if self._is_stale(nodename, seen):
                    continue
                if pid is not None:
                    current = self.get_registered_tasks(nodename, pid)
                    self._register(
                        nodename, pid, current if tasks is None else tasks, None, now
                    )
                self.mark_node_alive(nodename, pid, now=now)
            # The broadcast completed: stamped here, after its answers are
            # applied and in the same locked step, so no evaluation sees it
            # completed while a worker that answered still looks silent.
            first_miss = self._finish_inspect(now, replies, monotonic)
            learned = self._registered_since(known, replies)
        self._notify_new_node(learned)
        self._notify_first_miss(first_miss)

    def _registered_since(self, known: set[str], nodenames: Iterable[str]) -> list[str]:
        """The nodenames now registered that were not in ``known``. Call with the lock."""
        return [n for n in nodenames if n in self._newest_by_node and n not in known]

    def _forget_misses(self, hostname: str) -> None:
        """Drop the counted misses and the spent confirm of a nodename. Hold the lock."""
        self._misses.pop(hostname, None)
        self._confirm_spent.discard(hostname)

    def _notify_first_miss(self, first_miss: bool) -> None:
        """Tell the listeners a first miss was stamped. Call without the lock."""
        if not first_miss:
            return
        with self._lock:
            listeners = list(self._first_miss_listeners)
        for listener in listeners:
            # A listener is a side channel; it must not break event handling.
            with contextlib.suppress(Exception):
                listener()

    def _live_last_seen(self) -> dict[str, datetime]:
        """Nodename -> latest sign of life, for every nodename not stopped. Hold the lock."""
        seen: dict[str, datetime] = {}
        for w in self._workers.values():
            if not (w.shutdown_reported or w.superseded):
                seen[w.hostname] = max(w.last_seen, seen.get(w.hostname, w.last_seen))
        for node, at in self._unlisted.items():
            seen[node] = max(at, seen.get(node, at))
        return seen

    def _finish_inspect(
        self, now: datetime, answered: Iterable[str], monotonic: float | None
    ) -> bool:
        """Count a completed ``stats()`` broadcast as a miss for each live node that didn't answer it.

        A miss counts when the broadcast completed after the node's last sign of
        life and at least ``INSPECT_REFRESH_MIN_INTERVAL_SECONDS`` (monotonic)
        after its previous counted miss. Hold the lock.

        Returns:
            Whether some node's first miss since it last answered is owed a confirm.
        """
        replied = frozenset(answered)
        self._channel_proven = self._channel_proven or bool(replied)
        for node in replied:
            self._forget_misses(node)
        live = self._live_last_seen()
        mono = _monotonic() if monotonic is None else monotonic
        owes_confirm = False
        misses: dict[str, list[tuple[datetime, float]]] = {}
        for node, last_seen in live.items():
            if node in replied or not self._channel_proven:
                continue
            kept = [m for m in self._misses.get(node, []) if m[0] > last_seen]
            if not kept and now > last_seen:
                kept = [(now, mono)]
                if node not in self._confirm_spent:
                    self._confirm_spent.add(node)
                    owes_confirm = True
            elif kept and mono - kept[-1][1] >= INSPECT_REFRESH_MIN_INTERVAL_SECONDS:
                kept = [*kept, (now, mono)][-2:]
            if kept:
                misses[node] = kept
        self._misses = misses
        self._confirm_spent &= live.keys()
        return owes_confirm

    def _see_unlisted(self, nodename: str, seen_at: datetime) -> None:
        """A task event or reply names a nodename with no worker: it is live, until stopped."""
        self._keep_unlisted(
            nodename, max(seen_at, self._unlisted.get(nodename, seen_at))
        )

    def _keep_unlisted(self, nodename: str, last_seen: datetime) -> None:
        """Store the nodename, evicting the oldest one when over the bound."""
        self._unlisted[nodename] = last_seen
        if len(self._unlisted) > self._max_unlisted:
            oldest = min(self._unlisted, key=lambda n: (self._unlisted[n], n))
            del self._unlisted[oldest]

    def _is_silent(self, nodename: str, last_seen: datetime, cutoff: datetime) -> bool:
        """Silent with evidence: two counted ``stats()`` misses after ``last_seen``.

        ``cutoff`` is now minus ``WORKER_STALE_TIMEOUT_MINUTES``.
        """
        misses = self._misses.get(nodename, [])
        return (
            self._channel_proven
            and len(misses) >= 2
            and misses[0][0] > last_seen
            and last_seen < cutoff
        )

    def registry_facts(
        self, *, broker_configured: bool, now: datetime | None = None
    ) -> RegistryFacts:
        """Copy what the anomaly classifier needs from the registry.

        Names count from every live worker (no stop reported since its last
        ``worker_ready``, not superseded), however long ago it answered.

        The fleet is online while a worker is not stopped, superseded or silent
        with evidence (see ``_is_silent``).

        Args:
            broker_configured: Whether the server can inspect a broker.
            now: The time to judge the stale window at (tests); defaults to now.
        """
        cutoff = (now or _utcnow()) - timedelta(minutes=WORKER_STALE_TIMEOUT_MINUTES)
        with self._lock:
            live = [
                w
                for w in self._workers.values()
                if not (w.shutdown_reported or w.superseded)
            ]
            return RegistryFacts(
                live_names=frozenset(t for w in live for t in w.registered_tasks),
                any_live=bool(live),
                authority_at=self._authority_at,
                broker_configured=broker_configured,
                any_seen=self._ever_seen,
                any_online=any(
                    not self._is_silent(w.hostname, w.last_seen, cutoff) for w in live
                )
                or any(
                    not self._is_silent(n, seen, cutoff)
                    for n, seen in self._unlisted.items()
                ),
            )

    def apply_ping_reply(
        self,
        nodenames: Iterable[str],
        *,
        stats_completed: bool = False,
        now: datetime | None = None,
        monotonic: float | None = None,
    ) -> None:
        """Apply an ``inspect.ping()`` reply.

        Unlike a stats reply, a ping can't undo a worker event that arrived
        while it ran, so it needs no event snapshot: it names no pid, so it
        only revives the node's newest pid (never one that reported
        worker_shutdown) and retires the others, the state any such event
        leaves anyway.

        Args:
            nodenames: Nodenames that answered.
            stats_completed: Whether the ``stats()`` call before it completed
                with an empty reply: then the broadcast is stamped here, after
                the pings are applied.
            now: When the reply arrived (tests); defaults to now.
            monotonic: The monotonic reading at that moment (tests); defaults to now.
        """
        with self._lock:
            answered = list(nodenames)
            now = now or _utcnow()
            for nodename in answered:
                self.mark_node_alive(nodename, now=now)
            first_miss = False
            if stats_completed:
                first_miss = self._finish_inspect(now, answered, monotonic)
            elif answered:
                self._channel_proven = True
        self._notify_first_miss(first_miss)

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
            now = now or _utcnow()
            stale_cutoff = now - timedelta(minutes=stale_timeout_minutes)
            cleanup_cutoff = now - timedelta(minutes=cleanup_timeout_minutes)

            # Mark stale workers offline, remove very old offline workers
            self._drop_offline_unlisted(cleanup_cutoff, stale_cutoff)
            self._stopped_at = {
                n: t for n, t in self._stopped_at.items() if t[1] >= cleanup_cutoff
            }
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
                    self._keep_if_unproven(worker, stale_cutoff)
            # Event stamps only matter for a nodename that is still live.
            self._event_at = {
                n: t
                for n, t in self._event_at.items()
                if n in self._newest_by_node or n in self._unlisted
            }

    def _keep_if_unproven(self, worker: WorkerInfo, stale_cutoff: datetime) -> None:
        """Remember a removed worker that no stop or silence evidence closed.

        Without it, an idle fleet with no broker would turn "offline" when the
        cleanup empties the registry, which is silence taken as evidence.
        """
        if worker.shutdown_reported or worker.superseded:
            return
        if self._is_silent(worker.hostname, worker.last_seen, stale_cutoff):
            return
        self._keep_unlisted(worker.hostname, worker.last_seen)

    def _drop_offline_unlisted(
        self, cleanup_cutoff: datetime, stale_cutoff: datetime
    ) -> None:
        """Forget unlisted nodenames not seen for the cleanup time and silent with evidence.

        One with no evidence stays (silence alone isn't proof), until the bound evicts it.
        """
        self._unlisted = {
            node: seen
            for node, seen in self._unlisted.items()
            if seen >= cleanup_cutoff or not self._is_silent(node, seen, stale_cutoff)
        }


class TaskRunSummary(NamedTuple):
    """How often a task name ran and when it last did (``None`` if never stamped)."""

    execution_count: int
    last_run: datetime | None


class GraphStore:
    """Thread-safe in-memory store for TaskGraph with LRU eviction."""

    def __init__(self, max_nodes: int = 10000) -> None:
        """Initialize store with optional maximum node limit for LRU eviction."""
        self._graph = TaskGraph()
        self._lock = threading.RLock()
        self._max_nodes = max_nodes
        self._listeners: list[Callable[[TaskEvent], None]] = []
        # One immutable record per TASK node, kept in step with the graph
        # under the same lock, so summaries never walk the live graph. An
        # event changes its own node's state and events; names can also
        # change on other nodes, which the graph reports (take_renamed).
        self._runs: dict[str, RunRecord] = {}

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
            self._index_run(stored.task_id)
            self._maybe_evict()
            # A task.replace() reuse can rename other tasks (here or while
            # evicting); refresh those records too.
            for task_id in self._graph.take_renamed():
                self._index_run(task_id)

        for listener in self._listeners:
            with contextlib.suppress(Exception):
                listener(stored)

    def _index_run(self, task_id: str) -> None:
        """Refresh one task's run record. Call with the lock held."""
        node = self._graph.get_node(task_id)
        record = RunRecord.from_node(node) if node is not None else None
        if record is None:
            self._runs.pop(task_id, None)
        else:
            self._runs[task_id] = record

    def run_records(self) -> list[RunRecord]:
        """Every task run's record, for aggregates computed outside the lock.

        Holds the lock only to copy the references (at most ``max_nodes``);
        records are immutable, so callers can read them freely.
        """
        with self._lock:
            return list(self._runs.values())

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
        state: TaskState | Collection[TaskState] | None = None,
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
                wanted = {state} if isinstance(state, TaskState) else set(state)
                nodes = [n for n in nodes if n.state in wanted]
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
                from_dt = ensure_tz_aware(from_date)
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

        # A flow's time is over its whole graph, not the root's own events.
        times = self._flow_times_locked(with_first=to_date is not None)
        if from_date is not None:
            from_dt = ensure_tz_aware(from_date)
            root_nodes = [n for n in root_nodes if times[n.task_id][1] >= from_dt]
        if to_date is not None:
            to_dt = _ensure_end_of_day(to_date)
            root_nodes = [n for n in root_nodes if times[n.task_id][0] <= to_dt]

        # Newest first; equal times by root ID (the sort is stable).
        root_nodes.sort(key=lambda n: n.task_id)
        root_nodes.sort(key=lambda n: times[n.task_id][1], reverse=True)
        total = len(root_nodes)
        return root_nodes[offset : offset + limit], total

    def _flow_times_locked(
        self, *, with_first: bool
    ) -> dict[str, tuple[datetime, datetime]]:
        """Each root's flow: its (first, latest) time over every node under it.

        One pass over the nodes (see ``_root_of_each``). A flow with no timed
        node is ``_MIN_DATETIME`` in both; ``first`` is only worked out
        ``with_first``. Call with the lock held.
        """
        nodes = self._graph.nodes
        firsts: dict[str, datetime] = {}
        lasts: dict[str, datetime] = {}
        for node_id, root in _root_of_each(nodes).items():
            node = nodes[node_id]
            # GROUP/CHORD nodes have no events: their members are nodes of the
            # flow themselves, so they count through those.
            last = node.last_updated
            if root is None or last is None:
                continue
            if last > lasts.get(root, _MIN_DATETIME):
                lasts[root] = last
            first = node.first_seen if with_first else None
            if first is not None and first < firsts.get(root, first):
                firsts[root] = first
            elif first is not None:
                firsts.setdefault(root, first)
        return {
            root: (firsts.get(root, _MIN_DATETIME), lasts.get(root, _MIN_DATETIME))
            for root in self._graph.root_ids
        }

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

    def get_task_run_summaries(self) -> dict[str, TaskRunSummary]:
        """Count runs and find the latest activity of every task name, in one pass.

        Takes the lock once. Excludes synthetic nodes (GROUP, CHORD). A run
        counts in any state, and ``last_run`` is the newest ``last_updated``.
        """
        counts: dict[str, int] = {}
        last_runs: dict[str, datetime | None] = {}
        with self._lock:
            for node in self._graph.nodes.values():
                if node.node_type != NodeType.TASK:
                    continue
                counts[node.name] = counts.get(node.name, 0) + 1
                best = last_runs.setdefault(node.name, None)
                updated = node.last_updated
                if updated is not None and (best is None or updated > best):
                    last_runs[node.name] = updated
        return {
            name: TaskRunSummary(count, last_runs[name])
            for name, count in counts.items()
        }

    def _maybe_evict(self) -> None:
        """Evict the oldest units down to 90% of capacity. Call with lock held.

        A group or chord goes whole, with its members and callback, so its
        state never comes from part of them; one eviction can free over 10%.
        A unit too big to keep (over 90% of ``max_nodes``) loses its oldest
        nodes instead.
        """
        if len(self._graph.nodes) <= self._max_nodes:
            return
        keep = int(self._max_nodes * 0.9)
        # A unit is as old as its newest node.
        units = sorted(
            _eviction_units(self._graph),
            key=lambda unit: max(_eviction_age(n, self._graph) for n in unit),
        )
        for unit in units:
            excess = len(self._graph.nodes) - keep
            if excess <= 0:
                return
            if len(unit) > keep:
                unit = sorted(unit, key=lambda n: _eviction_age(n, self._graph))
                unit = unit[:excess]
            for node in unit:
                self._graph.remove_node(node.task_id)
                self._runs.pop(node.task_id, None)
