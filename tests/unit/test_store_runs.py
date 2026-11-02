"""Tests for the store's run index (``GraphStore.run_records``)."""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core import attribution
from stemtrace.core.events import CANCELLED_RETRY_REASON, TaskEvent, TaskState
from stemtrace.core.graph import TERMINAL_STATES, NodeType, TaskGraph, TaskNode
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
        task_id="t1",
        name="app.work",
        state=TaskState.STARTED,
        last_updated=T0,
        state_since=T0,
        attempt_started=T0,
    )
    assert _records(store)["t1"] == RunRecord(
        task_id="t1",
        name="app.work",
        state=TaskState.FAILURE,
        last_updated=T0 + timedelta(seconds=5),
        exception_type="myapp.PartnerError",
        runtime_ms=5000,
        state_since=T0 + timedelta(seconds=5),
        attempt_started=T0,
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
    # Oracle read off the node, not through from_node: the last run's runtime
    # once the task is terminal, None before.
    for task_id, record in _records(store).items():
        node = store.get_node(task_id)
        assert node is not None
        want = node.runtime_ms if node.state in TERMINAL_STATES else None
        assert record.runtime_ms == want, task_id
        _assert_state_evidence(record, node)


def _assert_state_evidence(record: RunRecord, node: TaskNode) -> None:
    """The state-event fields equal a reference that scans every raw event."""
    assert node.events
    state = max(node.events, key=attribution.precedence)
    started = [
        e.timestamp
        for e in node.events
        if e.state == TaskState.STARTED and e.retries == state.retries
    ]
    due = [
        max(e.timestamp, e.eta or e.timestamp)
        for e in node.events
        if e.state in (TaskState.PENDING, TaskState.RECEIVED)
        and e.parent_id is not None
        and e.parent_id != node.reported_parent_id
    ]
    assert node.state_event == state, record.task_id
    assert record.state_since == state.timestamp, record.task_id
    assert record.state_host == state.hostname, record.task_id
    assert record.cancelled == (
        state.state == TaskState.RETRY and state.exception == CANCELLED_RETRY_REASON
    ), record.task_id
    assert record.reported_parent_id == node.reported_parent_id, record.task_id
    assert record.attempt_started == (max(started) if started else None), record.task_id
    assert record.replaced_progress == (max(due) if due else None), record.task_id


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
def test_record_follows_a_rename_caused_by_eviction(
    flow: str, seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a node while evicting can rename a surviving task."""
    store = GraphStore(max_nodes=2)
    evicting = False
    renamed_while_evicting: list[str] = []
    rename, evict = TaskGraph._renamed, store._maybe_evict

    def spying_rename(graph: TaskGraph, node: TaskNode, old_name: str) -> None:
        if evicting:
            renamed_while_evicting.append(node.task_id)
        rename(graph, node, old_name)

    def spying_evict() -> None:
        nonlocal evicting
        evicting = True
        try:
            evict()
        finally:
            evicting = False

    monkeypatch.setattr(TaskGraph, "_renamed", spying_rename)
    monkeypatch.setattr(store, "_maybe_evict", spying_evict)
    for event in _shuffled(flow, seed):
        store.add_event(event)
        _assert_index_matches_graph(store)

    assert renamed_while_evicting  # the premise: an eviction renamed a task


@pytest.mark.parametrize("flow", sorted(_CAPTURES))
@pytest.mark.parametrize("max_nodes", [10_000, 3])
def test_index_matches_the_graph_on_real_captures(flow: str, max_nodes: int) -> None:
    """Recorded and shuffled orders, with and without eviction (renames included)."""
    for seed in range(4):
        store = GraphStore(max_nodes=max_nodes)
        for event in _shuffled(flow, seed):
            store.add_event(event)
            _assert_index_matches_graph(store)


def test_captures_include_runs_with_a_runtime() -> None:
    """The oracle above is not vacuous: some capture ends with a measured runtime."""
    measured = 0
    for flow in _CAPTURES:
        store = GraphStore()
        for event in _shuffled(flow, seed=0):
            store.add_event(event)
        measured += sum(r.runtime_ms is not None for r in store.run_records())
    assert measured > 0


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


def test_record_runtime_is_the_last_runs_not_the_total() -> None:
    """A retried task has three runs of 1s, 2s and 4s: the record holds the 4s."""
    store = GraphStore()
    plan = [
        (TaskState.STARTED, 0, 0),
        (TaskState.RETRY, 1, 0),
        (TaskState.STARTED, 10, 1),
        (TaskState.RETRY, 12, 1),
        (TaskState.STARTED, 20, 2),
        (TaskState.SUCCESS, 24, 2),
    ]
    for state, seconds, retries in plan:
        store.add_event(
            TaskEvent(
                task_id="t",
                name="app.flaky",
                state=state,
                retries=retries,
                timestamp=T0 + timedelta(seconds=seconds),
            )
        )

    node = store.get_node("t")
    assert node is not None
    assert node.run_times() == (4000, 7000)
    assert _records(store)["t"].runtime_ms == 4000


def test_record_has_no_runtime_before_the_task_ends() -> None:
    store = GraphStore()
    store.add_event(_event("t", TaskState.STARTED, 0))
    store.add_event(_event("t", TaskState.RETRY, 3))

    assert _records(store)["t"].runtime_ms is None


def test_record_runtime_follows_a_late_terminal_event() -> None:
    """STARTED then SUCCESS arriving out of order still gives the 5s run."""
    store = GraphStore()
    store.add_event(_event("t", TaskState.SUCCESS, 5))
    assert _records(store)["t"].runtime_ms is None  # no STARTED yet

    store.add_event(_event("t", TaskState.STARTED, 0))

    assert _records(store)["t"].runtime_ms == 5000


def test_non_terminal_events_never_compute_run_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Computing it scans and sorts every event: on each event under the lock, ingest went quadratic."""
    calls: list[int] = []
    real = attribution.run_times

    def counting(events: list[TaskEvent]) -> tuple[int | None, int | None]:
        calls.append(len(events))
        return real(events)

    monkeypatch.setattr(attribution, "run_times", counting)
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
            )
        )
    store.add_event(
        TaskEvent(
            task_id="t",
            name="app.poll",
            state=TaskState.STARTED,
            retries=100,
            timestamp=T0 + timedelta(seconds=300),
        )
    )
    assert calls == []

    store.add_event(
        TaskEvent(
            task_id="t",
            name="app.poll",
            state=TaskState.SUCCESS,
            retries=100,
            timestamp=T0 + timedelta(seconds=307),
        )
    )

    assert len(calls) == 1
    assert _records(store)["t"].runtime_ms == 7000


def _run_event(
    state: TaskState,
    seconds: float,
    *,
    retries: int = 0,
    hostname: str | None = None,
    exception: str | None = None,
    parent_id: str | None = None,
    eta_seconds: float | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id="t",
        name="app.work",
        state=state,
        retries=retries,
        timestamp=T0 + timedelta(seconds=seconds),
        hostname=hostname,
        exception=exception,
        parent_id=parent_id,
        eta=None if eta_seconds is None else T0 + timedelta(seconds=eta_seconds),
    )


def _record_of(*events: TaskEvent) -> RunRecord:
    store = GraphStore()
    for event in events:
        store.add_event(event)
    _assert_index_matches_graph(store)
    return _records(store)["t"]


def test_record_state_host_and_time_come_from_the_state_event() -> None:
    """The RETRY has no host: the STARTED's must not leak into the record."""
    record = _record_of(
        _run_event(TaskState.STARTED, 0, hostname="w1@a"),
        _run_event(TaskState.RETRY, 5, exception="boom"),
    )

    assert record.state == TaskState.RETRY
    assert record.state_since == T0 + timedelta(seconds=5)
    assert record.state_host is None
    assert record.cancelled is False


def test_record_marks_a_cold_stop_cancelled_retry_with_its_attempts_start() -> None:
    record = _record_of(
        _run_event(TaskState.STARTED, 0, hostname="w1@a"),
        _run_event(TaskState.STARTED, 20, retries=1, hostname="w1@a"),
        _run_event(
            TaskState.RETRY,
            25,
            retries=1,
            hostname="w1@a",
            exception=CANCELLED_RETRY_REASON,
        ),
    )

    assert record.cancelled is True
    assert record.state_host == "w1@a"
    assert record.state_since == T0 + timedelta(seconds=25)
    assert record.attempt_started == T0 + timedelta(seconds=20)


def test_record_cancelled_retry_without_its_started_has_no_attempt_start() -> None:
    record = _record_of(
        _run_event(TaskState.STARTED, 0, retries=0, hostname="w1@a"),
        _run_event(
            TaskState.RETRY,
            25,
            retries=1,
            hostname="w1@a",
            exception=CANCELLED_RETRY_REASON,
        ),
    )

    assert record.cancelled is True
    assert record.attempt_started is None


def test_record_cancelled_is_false_for_other_retries_and_states() -> None:
    other_reason = _record_of(
        _run_event(TaskState.RETRY, 5, exception="Cancelled by Celery!")
    )
    started = _record_of(_run_event(TaskState.STARTED, 5, exception="x"))

    assert (other_reason.cancelled, started.cancelled) == (False, False)


def test_record_redelivered_started_moves_the_attempt_and_host() -> None:
    """``acks_late``: a second STARTED of the same attempt on another host."""
    record = _record_of(
        _run_event(TaskState.STARTED, 0, hostname="w1@a"),
        _run_event(TaskState.STARTED, 90, hostname="w2@b"),
    )

    assert record.state_host == "w2@b"
    assert record.state_since == T0 + timedelta(seconds=90)
    assert record.attempt_started == T0 + timedelta(seconds=90)


def test_record_replacement_pending_sets_progress_to_its_eta() -> None:
    """Fact 12: the replacement's PENDING sorts after STARTED but ``last_updated`` stays."""
    sender = _run_event(TaskState.PENDING, 0, parent_id="p")
    started = _run_event(TaskState.STARTED, 10, parent_id="p")
    store = GraphStore()
    for event in (sender, started):
        store.add_event(event)
    before = _records(store)["t"]
    store.add_event(
        _run_event(TaskState.PENDING, 10.5, parent_id="t", eta_seconds=11.5)
    )
    after = _records(store)["t"]
    _assert_index_matches_graph(store)

    assert before.replaced_progress is None
    assert after.reported_parent_id == "p"
    assert after.replaced_progress == T0 + timedelta(seconds=11.5)
    assert after.last_updated == T0 + timedelta(seconds=10)
    assert after.state_since == T0 + timedelta(seconds=10)


def test_record_sender_pending_with_a_future_eta_is_not_progress() -> None:
    """The original parent's PENDING (a sender clock ahead) must not hide stuck."""
    record = _record_of(
        _run_event(TaskState.PENDING, 0, parent_id="p", eta_seconds=7200),
        _run_event(TaskState.STARTED, 10, parent_id="p"),
    )

    assert record.replaced_progress is None


def test_record_replacement_progress_is_the_latest_across_replacements() -> None:
    record = _record_of(
        _run_event(TaskState.PENDING, 0, parent_id="p"),
        _run_event(TaskState.STARTED, 10, parent_id="p"),
        _run_event(TaskState.PENDING, 11, parent_id="t", eta_seconds=40),
        _run_event(TaskState.RECEIVED, 50, parent_id="q"),
    )

    assert record.replaced_progress == T0 + timedelta(seconds=50)


def test_state_event_is_none_for_a_node_without_events() -> None:
    assert TaskNode(task_id="x", name="n", state=TaskState.PENDING).state_event is None
    assert (
        TaskNode(task_id="x", name="n", state=TaskState.PENDING).attempt_started is None
    )


@pytest.mark.parametrize("flow", sorted(_CAPTURES))
def test_snapshot_and_index_rebuild_give_the_same_state_event(flow: str) -> None:
    for seed in range(3):
        store = GraphStore()
        for event in _shuffled(flow, seed):
            store.add_event(event)
        with store._lock:
            nodes = [
                n for n in store._graph.nodes.values() if n.node_type == NodeType.TASK
            ]
            for node in nodes:
                live = (
                    node.state_event,
                    node.attempt_started,
                    node.replaced_progress,
                )
                copy = node.snapshot()
                rebuilt = (
                    copy.state_event,
                    copy.attempt_started,
                    copy.replaced_progress,
                )
                assert rebuilt == live, node.task_id


def test_hostname_in_captures_is_absent_so_hand_built_cells_are_needed() -> None:
    """Fact 14: why the tests above build hostnames and cancelled RETRYs by hand."""
    events = [e for flow in _CAPTURES.values() for e in flow]
    assert not any(e.get("hostname") for e in events)
    assert not any(e.get("exception") == CANCELLED_RETRY_REASON for e in events)


def test_ingesting_events_never_rescans_a_nodes_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state event is compared with the new event only: O(1) calls per event."""
    calls: list[str] = []
    real_precedence, real_latest = attribution.precedence, attribution.latest

    def counting_precedence(event: TaskEvent) -> Any:
        calls.append("precedence")
        return real_precedence(event)

    def counting_latest(events: list[TaskEvent], value: Any) -> Any:
        calls.append("latest")
        return real_latest(events, value)

    monkeypatch.setattr(attribution, "precedence", counting_precedence)
    monkeypatch.setattr(attribution, "latest", counting_latest)
    store = GraphStore()
    count = 400
    for i in range(count):
        state = TaskState.STARTED if i % 2 == 0 else TaskState.RETRY
        store.add_event(
            TaskEvent(
                task_id="t",
                name="app.poll",
                state=state,
                retries=i // 2,
                timestamp=T0 + timedelta(seconds=i),
                hostname="w@h",
                parent_id="t" if i else "p",
            )
        )

    assert "latest" not in calls
    assert calls.count("precedence") <= 2 * count
    assert _records(store)["t"].state_host == "w@h"


def test_record_state_since_is_the_state_events_not_the_last_events() -> None:
    """IGNORED loses to FAILURE yet sorts last: ``last_updated`` is its time."""
    record = _record_of(
        _run_event(TaskState.FAILURE, 5, hostname="w1@a"),
        _run_event(TaskState.IGNORED, 10, hostname="w2@b"),
    )

    assert record.last_updated == T0 + timedelta(seconds=10)
    assert record.state_since == T0 + timedelta(seconds=5)
    assert record.state_host == "w1@a"


def test_captures_include_runs_with_replacement_progress() -> None:
    """The differential is not vacuous for rule (a): some capture has a replacement."""
    found = 0
    for flow in _CAPTURES:
        store = GraphStore()
        for event in _shuffled(flow, seed=0):
            store.add_event(event)
        found += sum(r.replaced_progress is not None for r in store.run_records())
    assert found > 0
