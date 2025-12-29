"""Thread-safe in-memory graph store."""

from __future__ import annotations

import contextlib
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from stemtrace.core.graph import NodeType, TaskGraph, TaskNode


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


# Fallback for nodes with no events (synthetic nodes)
_MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)


def _get_node_timestamp(node: TaskNode, graph: TaskGraph) -> datetime:
    """Get the most recent timestamp for a node (including children for synthetic nodes)."""
    if node.events:
        return node.events[-1].timestamp

    # For synthetic nodes (GROUP/CHORD), use the latest child timestamp
    if node.node_type in (NodeType.GROUP, NodeType.CHORD) and node.children:
        child_timestamps: list[datetime] = []
        for child_id in node.children:
            child = graph.get_node(child_id)
            if child and child.events:
                child_timestamps.append(child.events[-1].timestamp)
        if child_timestamps:
            return max(child_timestamps)

    return _MIN_DATETIME


def _get_first_timestamp(node: TaskNode, graph: TaskGraph) -> datetime:
    """Get the first timestamp for a node (including children for synthetic nodes)."""
    if node.events:
        return node.events[0].timestamp

    # For synthetic nodes (GROUP/CHORD), use the earliest child timestamp
    if node.node_type in (NodeType.GROUP, NodeType.CHORD) and node.children:
        child_timestamps: list[datetime] = []
        for child_id in node.children:
            child = graph.get_node(child_id)
            if child and child.events:
                child_timestamps.append(child.events[0].timestamp)
        if child_timestamps:
            return min(child_timestamps)

    return _MIN_DATETIME


if TYPE_CHECKING:
    from collections.abc import Callable

    from stemtrace.core.events import TaskEvent, TaskState


class GraphStore:
    """Thread-safe in-memory store for TaskGraph with LRU eviction."""

    def __init__(self, max_nodes: int = 10000) -> None:
        """Initialize store with optional maximum node limit for LRU eviction."""
        self._graph = TaskGraph()
        self._lock = threading.RLock()
        self._max_nodes = max_nodes
        self._listeners: list[Callable[[TaskEvent], None]] = []

    def add_event(self, event: TaskEvent) -> None:
        """Add event to graph and notify listeners."""
        with self._lock:
            self._graph.add_event(event)
            self._maybe_evict()

        for listener in self._listeners:
            with contextlib.suppress(Exception):
                listener(event)

    def get_node(self, task_id: str) -> TaskNode | None:
        """Get node by ID, or None if not found."""
        with self._lock:
            return self._graph.get_node(task_id)

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

        Returns:
            Tuple of (filtered nodes, total count matching filters).
        """
        with self._lock:
            nodes = list(self._graph.nodes.values())

        if state is not None:
            nodes = [n for n in nodes if n.state == state]
        if name_contains is not None:
            name_lower = name_contains.lower()
            nodes = [n for n in nodes if name_lower in n.name.lower()]
        if from_date is not None:
            from_dt = _ensure_tz_aware(from_date)
            nodes = [n for n in nodes if n.events and n.events[-1].timestamp >= from_dt]
        if to_date is not None:
            to_dt = _ensure_end_of_day(to_date)
            nodes = [n for n in nodes if n.events and n.events[0].timestamp <= to_dt]

        nodes.sort(
            key=lambda n: n.events[-1].timestamp if n.events else _MIN_DATETIME,
            reverse=True,
        )
        total = len(nodes)
        return nodes[offset : offset + limit], total

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
            root_nodes = [
                self._graph.nodes[rid]
                for rid in self._graph.root_ids
                if rid in self._graph.nodes
            ]

            # Date filtering - use child timestamps for synthetic nodes
            if from_date is not None:
                from_dt = _ensure_tz_aware(from_date)
                root_nodes = [
                    n
                    for n in root_nodes
                    if _get_node_timestamp(n, self._graph) >= from_dt
                ]
            if to_date is not None:
                to_dt = _ensure_end_of_day(to_date)
                root_nodes = [
                    n
                    for n in root_nodes
                    if _get_first_timestamp(n, self._graph) <= to_dt
                ]

            # Sort while holding lock since we need access to graph for children
            root_nodes.sort(
                key=lambda n: _get_node_timestamp(n, self._graph),
                reverse=True,
            )
            total = len(root_nodes)
        return root_nodes[offset : offset + limit], total

    def get_children(self, task_id: str) -> list[TaskNode]:
        """Get child nodes of a task."""
        with self._lock:
            node = self._graph.get_node(task_id)
            if node is None:
                return []
            return [
                self._graph.nodes[cid]
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
                result[current_id] = node
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
        """Get all unique task names seen in events."""
        with self._lock:
            return {node.name for node in self._graph.nodes.values()}

    def _maybe_evict(self) -> None:
        """Evict oldest 10% when over capacity. Call with lock held."""
        if len(self._graph.nodes) <= self._max_nodes:
            return

        nodes_by_age = sorted(
            self._graph.nodes.values(),
            key=lambda n: n.events[0].timestamp if n.events else _MIN_DATETIME,
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
