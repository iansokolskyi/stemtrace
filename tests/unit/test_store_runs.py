"""Tests for the store's run index (``GraphStore.run_records``)."""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph, TaskNode
from stemtrace.server.stats import RunRecord
from stemtrace.server.store import GraphStore

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _event(
    task_id: str,
    state: TaskState,
    seconds: int,
    *,
    name: str = "app.work",
    group_id: str | None = None,
    exception_type: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=state,
        timestamp=T0 + timedelta(seconds=seconds),
        group_id=group_id,
        exception_type=exception_type,
    )


def _records(store: GraphStore) -> dict[str, RunRecord]:
    return {r.task_id: r for r in store.run_records()}


def test_run_record_follows_the_task_through_its_states() -> None:
    store = GraphStore()

    store.add_event(_event("t1", TaskState.STARTED, 0))
    started = _records(store)["t1"]
    store.add_event(
        _event("t1", TaskState.FAILURE, 5, exception_type="myapp.PartnerError")
    )

    assert started == RunRecord(
        task_id="t1", name="app.work", state=TaskState.STARTED, last_updated=T0
    )
    assert _records(store)["t1"] == RunRecord(
        task_id="t1",
        name="app.work",
        state=TaskState.FAILURE,
        last_updated=T0 + timedelta(seconds=5),
        exception_type="myapp.PartnerError",
    )


def test_late_earlier_event_keeps_the_final_state_and_time() -> None:
    store = GraphStore()
    store.add_event(_event("t1", TaskState.SUCCESS, 10))
    store.add_event(_event("t1", TaskState.STARTED, 2))

    record = _records(store)["t1"]

    assert (record.state, record.last_updated) == (
        TaskState.SUCCESS,
        T0 + timedelta(seconds=10),
    )


def test_synthetic_group_nodes_have_no_record() -> None:
    store = GraphStore()
    for i in range(2):
        store.add_event(_event(f"m{i}", TaskState.SUCCESS, i, group_id="g1"))

    assert set(_records(store)) == {"m0", "m1"}
    assert store.get_node("group:g1") is not None


def test_eviction_drops_the_evicted_tasks_records() -> None:
    store = GraphStore(max_nodes=10)
    for i in range(11):
        store.add_event(_event(f"t{i:02d}", TaskState.SUCCESS, i))

    kept = {n.task_id for n in store.get_nodes(limit=500)[0]}

    assert set(_records(store)) == kept
    assert len(kept) == 9  # evicted down to 90%
    assert "t00" not in kept


def test_duplicate_event_leaves_the_record_unchanged() -> None:
    store = GraphStore()
    store.add_event(_event("t1", TaskState.SUCCESS, 1))
    before = store.run_records()

    store.add_event(_event("t1", TaskState.SUCCESS, 1))

    assert store.run_records() == before


def test_run_records_returns_a_copy() -> None:
    store = GraphStore()
    store.add_event(_event("t1", TaskState.SUCCESS, 1))

    records = store.run_records()
    records.clear()

    assert len(store.run_records()) == 1


_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)


def _assert_index_matches_graph(store: GraphStore) -> None:
    """Every record equals one built from the live node now."""
    with store._lock:
        expected = {
            node.task_id: RunRecord.from_node(node)
            for node in store._graph.nodes.values()
            if node.node_type == NodeType.TASK
        }
    assert _records(store) == expected


def _shuffled(flow: str, seed: int) -> list[TaskEvent]:
    order = [TaskEvent.model_validate(e) for e in _CAPTURES[flow]]
    if seed:
        random.Random(seed).shuffle(order)
    return order


def test_record_follows_a_rename_on_another_tasks_event() -> None:
    """A ``task.replace()`` reuse re-picks a task's name on another task's event."""
    order = _shuffled("nested_replace_header@worker_process", seed=1)
    at = 10  # found by search: this event renames a task other than its own
    graph, store = TaskGraph(), GraphStore()
    for event in order[:at]:
        graph.add_event(event)
        store.add_event(event)
    graph.take_renamed()

    graph.add_event(order[at])
    renamed = graph.take_renamed() - {order[at].task_id}
    store.add_event(order[at])

    assert renamed  # the premise: another task was renamed
    records = _records(store)
    for task_id in renamed:
        node = store.get_node(task_id)
        assert node is not None
        assert records[task_id].name == node.name
    _assert_index_matches_graph(store)


@pytest.mark.parametrize(
    ("flow", "seed"),
    [
        ("nested_replace_last@worker_process", 13),
        ("nested_replace_last@worker_process", 14),
    ],
)
def test_record_follows_a_rename_caused_by_eviction(flow: str, seed: int) -> None:
    """Removing a node while evicting can rename a surviving task."""
    store = GraphStore(max_nodes=2)
    for event in _shuffled(flow, seed):
        store.add_event(event)
        _assert_index_matches_graph(store)


@pytest.mark.parametrize("flow", sorted(_CAPTURES))
@pytest.mark.parametrize("max_nodes", [10_000, 3])
def test_index_matches_the_graph_on_real_captures(flow: str, max_nodes: int) -> None:
    """Recorded and shuffled orders, with and without eviction (renames included)."""
    events = [TaskEvent.model_validate(e) for e in _CAPTURES[flow]]
    for seed in range(4):
        order = list(events)
        if seed:
            random.Random(seed).shuffle(order)
        store = GraphStore(max_nodes=max_nodes)
        for event in order:
            store.add_event(event)
            _assert_index_matches_graph(store)


def test_retry_events_never_read_the_exception_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading it scans every event: on each event under the lock, ingest went quadratic."""
    reads: list[str] = []
    real = TaskNode.exception_type

    def counting(node: TaskNode) -> str | None:
        reads.append(node.task_id)
        return real.fget(node)  # type: ignore[attr-defined, no-any-return]  # a property

    monkeypatch.setattr(TaskNode, "exception_type", property(counting))
    store = GraphStore()
    for i in range(200):
        state = TaskState.STARTED if i % 2 == 0 else TaskState.RETRY
        store.add_event(
            TaskEvent(
                task_id="t",
                name="app.poll",
                state=state,
                retries=i // 2,
                timestamp=T0 + timedelta(seconds=i),
                exception_type="E",
            )
        )
    assert reads == []

    store.add_event(
        TaskEvent(
            task_id="t",
            name="app.poll",
            state=TaskState.FAILURE,
            retries=100,
            timestamp=T0 + timedelta(seconds=300),
            exception_type="Final",
        )
    )

    assert reads == ["t"]
    assert _records(store)["t"].exception_type == "Final"
