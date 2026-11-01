"""The task registry reads the store once per request, however many names it has."""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

_FLOWS = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


class CountingLock:
    """Re-entrant lock that counts outermost acquisitions."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.acquisitions = 0
        self._depth = 0

    def __enter__(self) -> "CountingLock":
        self._inner.acquire()
        if self._depth == 0:
            self.acquisitions += 1
        self._depth += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self._depth -= 1
        self._inner.release()


def _reference_count(store: GraphStore, name: str) -> int:
    """Copy of the old per-name ``get_task_execution_count`` (a full scan)."""
    with store._lock:
        return sum(
            1
            for node in store._graph.nodes.values()
            if node.name == name and node.node_type == NodeType.TASK
        )


def _reference_last_run(store: GraphStore, name: str) -> datetime | None:
    """Copy of the old per-name ``get_last_execution_time`` (a full scan)."""
    with store._lock:
        last: datetime | None = None
        for node in store._graph.nodes.values():
            if (
                node.name == name
                and node.node_type == NodeType.TASK
                and node.last_updated is not None
                and (last is None or node.last_updated > last)
            ):
                last = node.last_updated
        return last


def _client(store: GraphStore) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(store))
    return TestClient(app)


def _expected_rows(store: GraphStore) -> list[dict[str, Any]]:
    """What the registry must return, computed with the reference scans."""
    with store._lock:
        names = {
            n.name for n in store._graph.nodes.values() if n.node_type == NodeType.TASK
        }
    rows = []
    for name in sorted(names):
        last = _reference_last_run(store, name)
        rows.append(
            {
                "name": name,
                "execution_count": _reference_count(store, name),
                "last_run": last.isoformat() if last else None,
            }
        )
    return rows


def _actual_rows(store: GraphStore) -> list[dict[str, Any]]:
    tasks = _client(store).get("/api/tasks/registry").json()["tasks"]
    return [
        {
            "name": t["name"],
            "execution_count": t["execution_count"],
            "last_run": datetime.fromisoformat(t["last_run"]).isoformat()
            if t["last_run"]
            else None,
        }
        for t in tasks
    ]


def _flow_events() -> list[TaskEvent]:
    return [TaskEvent.model_validate(raw) for flow in _FLOWS.values() for raw in flow]


class TestRegistryReadsStoreOnce:
    @pytest.mark.parametrize("names", [1, 5, 40])
    def test_registry_with_n_names_takes_the_store_lock_once(self, names: int) -> None:
        store = GraphStore()
        for i in range(names):
            for run in range(3):
                store.add_event(
                    TaskEvent(
                        task_id=f"t{i}-{run}",
                        name=f"app.task_{i}",
                        state=TaskState.SUCCESS,
                        timestamp=_BASE + timedelta(seconds=i * 10 + run),
                    )
                )
        spy = CountingLock(store._lock)
        store._lock = spy  # type: ignore[assignment]  # counting test double

        response = _client(store).get("/api/tasks/registry")

        assert response.json()["total"] == names
        assert spy.acquisitions == 1


class TestRegistryMatchesPerNameScans:
    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
    def test_registry_over_all_captures_matches_reference_scans(
        self, seed: int
    ) -> None:
        events = _flow_events()
        random.Random(seed).shuffle(events)
        store = GraphStore()
        for event in events:
            store.add_event(event)

        expected = _expected_rows(store)

        assert len(expected) > 5
        assert _actual_rows(store) == expected

    @pytest.mark.parametrize("seed", [0, 1, 2])
    def test_registry_after_eviction_matches_reference_scans(self, seed: int) -> None:
        events = _flow_events()
        random.Random(seed).shuffle(events)
        store = GraphStore(max_nodes=3)
        for event in events:
            store.add_event(event)

        expected = _expected_rows(store)

        assert 0 < store.node_count <= 3
        assert _actual_rows(store) == expected

    def test_registry_lists_no_synthetic_group_or_chord_names(self) -> None:
        store = GraphStore()
        for event in _flow_events():
            store.add_event(event)

        names = {row["name"] for row in _actual_rows(store)}

        assert "group" not in names
        assert "chord" not in names
