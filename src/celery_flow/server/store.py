"""Thread-safe in-memory graph store."""

from __future__ import annotations

import contextlib
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from celery_flow.core.graph import NodeType, TaskGraph, TaskNode

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


if TYPE_CHECKING:
    from collections.abc import Callable

    from celery_flow.core.events import TaskEvent, TaskState


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
    ) -> list[TaskNode]:
        """Get nodes with optional filtering, most recent first."""
        with self._lock:
            nodes = list(self._graph.nodes.values())

        if state is not None:
            nodes = [n for n in nodes if n.state == state]
        if name_contains is not None:
            name_lower = name_contains.lower()
            nodes = [n for n in nodes if name_lower in n.name.lower()]

        nodes.sort(
            key=lambda n: n.events[-1].timestamp if n.events else _MIN_DATETIME,
            reverse=True,
        )
        return nodes[offset : offset + limit]

    def get_root_nodes(self, limit: int = 50) -> list[TaskNode]:
        """Get root nodes (no parent), most recent first."""
        with self._lock:
            root_nodes = [
                self._graph.nodes[rid]
                for rid in self._graph.root_ids
                if rid in self._graph.nodes
            ]
            # Sort while holding lock since we need access to graph for children
            root_nodes.sort(
                key=lambda n: _get_node_timestamp(n, self._graph),
                reverse=True,
            )
        return root_nodes[:limit]

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
