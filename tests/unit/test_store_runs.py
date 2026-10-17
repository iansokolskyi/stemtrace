"""Tests for the store's run index (``GraphStore.run_records``)."""

from datetime import datetime, timedelta, timezone

from stemtrace.core.events import TaskEvent, TaskState
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
