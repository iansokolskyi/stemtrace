"""The server adapter builds full facts only for tasks that can be flagged."""

from stemtrace.core.anomalies import (
    NodeFacts,
    NodeLink,
    TimelineEntry,
    classify_anomalies,
)
from stemtrace.core.events import TaskEvent, TaskState, WorkerEventType
from stemtrace.server.anomaly_facts import classifier_facts, node_facts, runtime_facts
from stemtrace.server.store import GraphStore
from tests.unit.anomaly_pipeline import (
    SMALL,
    at,
    classify,
    completed_runs,
    task_event,
)


def _chain_below(parent: str, length: int, seconds: float) -> list[TaskEvent]:
    """``length`` finished tasks in a chain below ``parent``, the last event at ``seconds``."""
    events: list[TaskEvent] = []
    for i in range(length):
        task_id = f"c{i}"
        above = parent if i == 0 else f"c{i - 1}"
        events.append(task_event(task_id, TaskState.STARTED, seconds - 1, parent=above))
        events.append(task_event(task_id, TaskState.SUCCESS, seconds, parent=above))
    return events


def test_started_parent_with_a_long_finished_subtree_is_held_by_rule_b() -> None:
    parent = task_event("P", TaskState.STARTED, 0, name="app.parent", host="w@a")
    below = _chain_below("P", 60, 900)

    report = classify([parent, *below], now=1000, settings=SMALL)

    # None of the 60 descendants can be flagged, yet the newest of them (900 s)
    # keeps P from the 300 s ceiling that 1,000 s without progress would pass.
    assert report.anomalies == {}


def test_started_parent_without_the_subtree_is_stuck() -> None:
    parent = task_event("P", TaskState.STARTED, 0, name="app.parent", host="w@a")

    report = classify([parent], now=1000, settings=SMALL)

    assert list(report.anomalies) == ["P"]


def test_classifier_facts_keep_a_link_for_every_task_that_cannot_be_flagged() -> None:
    store = GraphStore()
    for event in [
        task_event("P", TaskState.STARTED, 0, name="app.parent", host="w@a"),
        *_chain_below("P", 3, 50),
        task_event("r", TaskState.RETRY, 5, host="w@a", retries=1),
    ]:
        store.add_event(event)

    facts = classifier_facts(store.run_records(), {}, at(100))

    assert [n.task_id for n in facts.nodes] == ["P"]
    assert sorted(
        NodeLink(link.task_id, link.reported_parent_id, link.last_updated)
        for link in facts.links
    ) == [
        NodeLink("c0", "P", at(50)),
        NodeLink("c1", "c0", at(50)),
        NodeLink("c2", "c1", at(50)),
        NodeLink("r", None, at(5)),
    ]


def test_runtime_facts_for_some_names_equal_the_same_rows_of_all_names() -> None:
    store = GraphStore()
    for event in [
        *completed_runs("app.a", 4, 2),
        *completed_runs("app.b", 3, 9),
        *completed_runs("app.c", 5, 1),
    ]:
        store.add_event(event)
    records = store.run_records()

    everything = runtime_facts(records, at(0))
    some = runtime_facts(records, at(0), {"app.a", "app.c"})

    assert set(some) == {"app.a", "app.c"}
    assert some == {name: everything[name] for name in some}


def test_cancelled_retry_gets_full_facts_and_its_name_no_runtime_stats() -> None:
    store = GraphStore()
    for event in [
        task_event("s", TaskState.STARTED, 1, host="w@a", retries=1),
        task_event(
            "s",
            TaskState.RETRY,
            2,
            host="w@a",
            retries=1,
            exception="cancelled by Celery",
        ),
        *completed_runs("app.work", 5, 1),
    ]:
        store.add_event(event)

    facts = classifier_facts(store.run_records(), {}, at(100))

    assert [n.task_id for n in facts.nodes] == ["s"]
    assert facts.runtime_stats == {}
    assert len(node_facts(store.run_records())) == 6


def test_classifier_given_full_facts_for_an_ended_task_never_flags_it() -> None:
    ended = NodeFacts(
        task_id="done",
        name="app.work",
        state=TaskState.SUCCESS,
        last_updated=at(10),
        state_since=at(10),
        state_host="w@a",
    )

    report = classify_anomalies(
        [ended],
        now=at(10_000),
        runtime_stats={},
        timeline={"w@a": [TimelineEntry(at(50), WorkerEventType.WORKER_SHUTDOWN)]},
        settings=SMALL,
    )

    assert report.anomalies == {}


def test_classifier_facts_list_each_pending_task_once_and_keep_it_as_a_link() -> None:
    store = GraphStore()
    for event in [
        task_event("p1", TaskState.PENDING, 0, name="app.a"),
        task_event("p2", TaskState.PENDING, 1, name="celery.chord_unlock"),
        task_event("r1", TaskState.RECEIVED, 2, name="app.b"),
        task_event("s1", TaskState.STARTED, 3, name="app.c", host="w@a"),
    ]:
        store.add_event(event)

    facts = classifier_facts(store.run_records(), {}, at(100))

    assert sorted(p.task_id for p in facts.pending) == ["p1", "p2"]
    assert sorted(link.task_id for link in facts.links) == ["p1", "p2", "r1"]
    assert [n.task_id for n in facts.nodes] == ["s1"]
