"""Tests for the worker timeline (``WorkerTimeline``) and its consumer feed."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.events import WorkerEvent, WorkerEventType
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.consumer import EventConsumer
from stemtrace.server.store import (
    WORKER_TIMELINE_EVENTS_PER_NODE,
    WORKER_TIMELINE_MAX_NODES,
    GraphStore,
    WorkerRegistry,
    WorkerTimeline,
    WorkerTimelineEntry,
)

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
READY = WorkerEventType.WORKER_READY
SHUTDOWN = WorkerEventType.WORKER_SHUTDOWN
TERMINATING = WorkerEventType.WORKER_TERMINATING


def _event(
    event_type: WorkerEventType = READY,
    *,
    hostname: str = "celery@w1",
    pid: int = 100,
    seconds: float = 0,
    base: datetime = T0,
) -> WorkerEvent:
    return WorkerEvent(
        event_type=event_type,
        hostname=hostname,
        pid=pid,
        timestamp=base + timedelta(seconds=seconds),
    )


def _consumer(
    timeline: WorkerTimeline | None, registry: WorkerRegistry | None = None
) -> EventConsumer:
    return EventConsumer(
        "memory://",
        GraphStore(),
        worker_registry=registry,
        worker_timeline=timeline,
    )


@pytest.mark.parametrize("event_type", list(WorkerEventType))
def test_consumer_feed_worker_event_records_type_pid_and_timestamp(
    event_type: WorkerEventType,
) -> None:
    timeline = WorkerTimeline()
    _consumer(timeline, WorkerRegistry())._process_event(
        _event(event_type, pid=42, seconds=7)
    )

    assert timeline.events_for("celery@w1") == [
        WorkerTimelineEntry(T0 + timedelta(seconds=7), event_type, 42)
    ]


def test_consumer_feed_without_registry_still_records_timeline() -> None:
    timeline = WorkerTimeline()
    _consumer(timeline, None)._process_event(_event(SHUTDOWN, seconds=3))

    assert [e.event_type for e in timeline.events_for("celery@w1")] == [SHUTDOWN]


def test_consumer_feed_without_timeline_still_registers_worker() -> None:
    registry = WorkerRegistry()
    _consumer(None, registry)._process_event(_event(READY))

    assert len(registry.get_all_workers()) == 1


def test_record_over_event_bound_keeps_newest_events_per_node() -> None:
    timeline = WorkerTimeline()
    total = WORKER_TIMELINE_EVENTS_PER_NODE + 5
    for i in range(total):
        timeline.record(_event(seconds=i, pid=i))

    kept = timeline.events_for("celery@w1")
    assert WORKER_TIMELINE_EVENTS_PER_NODE == 32
    assert [e.pid for e in kept] == list(range(5, total))


def test_record_over_node_bound_evicts_node_with_oldest_newest_event() -> None:
    timeline = WorkerTimeline()
    # Node n0 has the oldest newest-event even though it was written last.
    for i in range(1, WORKER_TIMELINE_MAX_NODES):
        timeline.record(_event(hostname=f"n{i}", seconds=100 + i))
    timeline.record(_event(hostname="n0", seconds=1))
    assert len(timeline.snapshot()) == WORKER_TIMELINE_MAX_NODES
    assert WORKER_TIMELINE_MAX_NODES == 1024

    timeline.record(_event(hostname="newcomer", seconds=5000))

    nodes = timeline.snapshot()
    assert len(nodes) == WORKER_TIMELINE_MAX_NODES
    assert "n0" not in nodes
    assert "n1" in nodes
    assert "newcomer" in nodes


def test_record_over_node_bound_evicts_incoming_node_when_it_is_oldest() -> None:
    timeline = WorkerTimeline(max_nodes=2)
    timeline.record(_event(hostname="a", seconds=10))
    timeline.record(_event(hostname="b", seconds=20))

    timeline.record(_event(hostname="stale", seconds=1))

    assert sorted(timeline.snapshot()) == ["a", "b"]


def test_record_node_eviction_tie_drops_smallest_nodename() -> None:
    timeline = WorkerTimeline(max_nodes=2)
    for host in ("b", "a", "c"):
        timeline.record(_event(hostname=host, seconds=10))

    assert sorted(timeline.snapshot()) == ["b", "c"]


def test_remove_stale_workers_cleanup_leaves_timeline_intact() -> None:
    registry = WorkerRegistry()
    timeline = WorkerTimeline()
    consumer = _consumer(timeline, registry)
    for event in (_event(READY, seconds=0), _event(SHUTDOWN, seconds=60)):
        consumer._process_event(event)
    before = timeline.snapshot()

    registry.remove_stale_workers(now=T0 + timedelta(hours=2))

    assert registry.get_all_workers() == []
    assert timeline.snapshot() == before
    assert [e.event_type for e in before["celery@w1"]] == [READY, SHUTDOWN]


def test_consumer_stale_sweep_removing_worker_leaves_timeline_intact() -> None:
    registry = WorkerRegistry()
    timeline = WorkerTimeline()
    consumer = _consumer(timeline, registry)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    event = WorkerEvent(
        event_type=READY, hostname="celery@w1", pid=7, timestamp=long_ago
    )
    consumer._process_event(event)
    assert len(registry.get_all_workers()) == 1

    consumer._maybe_check_stale_workers()  # first call: interval 0 has elapsed

    assert registry.get_all_workers() == []
    assert timeline.events_for("celery@w1") == [WorkerTimelineEntry(long_ago, READY, 7)]


def test_record_duplicate_event_is_stored_once() -> None:
    timeline = WorkerTimeline()
    for _ in range(3):
        timeline.record(_event(TERMINATING, seconds=9))

    assert len(timeline.events_for("celery@w1")) == 1


def test_record_same_instant_events_are_ordered_by_type_then_pid() -> None:
    timeline = WorkerTimeline()
    timeline.record(_event(TERMINATING, pid=2))
    timeline.record(_event(SHUTDOWN, pid=9))
    timeline.record(_event(READY, pid=5))
    timeline.record(_event(READY, pid=1))

    kept = timeline.events_for("celery@w1")
    assert [(e.event_type.value, e.pid) for e in kept] == [
        ("worker_ready", 1),
        ("worker_ready", 5),
        ("worker_shutdown", 9),
        ("worker_terminating", 2),
    ]


@pytest.mark.parametrize("seed", range(8))
def test_record_shuffled_arrival_orders_give_same_timeline(seed: int) -> None:
    rng = random.Random(seed)
    events = [
        _event(
            rng.choice(list(WorkerEventType)),
            hostname=f"n{i % 3}",
            pid=rng.randrange(3),
            seconds=rng.randrange(60),
        )
        for i in range(120)  # per node ~40 events with ties: the K bound bites
    ]

    def build(order: list[WorkerEvent]) -> dict[str, list[WorkerTimelineEntry]]:
        timeline = WorkerTimeline()
        for event in order:
            timeline.record(event)
        return timeline.snapshot()

    expected = build(events)
    shuffled = events[:]
    rng.shuffle(shuffled)

    assert build(shuffled) == expected
    assert build(sorted(events, key=lambda e: e.timestamp, reverse=True)) == expected
    assert all(len(v) <= WORKER_TIMELINE_EVENTS_PER_NODE for v in expected.values())


def test_snapshot_returns_copy_that_later_writes_do_not_change() -> None:
    timeline = WorkerTimeline()
    timeline.record(_event(READY, seconds=1))
    snap = timeline.snapshot()
    per_node = timeline.events_for("celery@w1")

    snap["celery@w1"].clear()
    per_node.clear()
    snap["other"] = []
    timeline.record(_event(SHUTDOWN, seconds=2))

    assert [e.event_type for e in timeline.events_for("celery@w1")] == [READY, SHUTDOWN]
    assert sorted(timeline.snapshot()) == ["celery@w1"]


def test_events_for_unknown_nodename_returns_empty_list() -> None:
    assert WorkerTimeline().events_for("nobody@nowhere") == []


def _naive_event(
    event_type: WorkerEventType,
    *,
    hostname: str = "celery@w1",
    seconds: float = 0,
    base: datetime = T0,
) -> WorkerEvent:
    return WorkerEvent(
        event_type=event_type,
        hostname=hostname,
        pid=100,
        timestamp=(base + timedelta(seconds=seconds)).replace(tzinfo=None),
    )


def test_record_naive_and_aware_timestamps_on_one_node_both_kept_as_utc() -> None:
    timeline = WorkerTimeline()
    timeline.record(_event(READY, seconds=10))
    timeline.record(_naive_event(SHUTDOWN, seconds=20))

    assert timeline.events_for("celery@w1") == [
        WorkerTimelineEntry(T0 + timedelta(seconds=10), READY, 100),
        WorkerTimelineEntry(T0 + timedelta(seconds=20), SHUTDOWN, 100),
    ]


def test_record_node_bound_holds_when_a_naive_node_is_present() -> None:
    timeline = WorkerTimeline(max_nodes=2)
    timeline.record(_event(hostname="a", seconds=10))
    timeline.record(_naive_event(READY, hostname="b", seconds=20))
    timeline.record(_event(hostname="c", seconds=30))
    timeline.record(_event(hostname="d", seconds=40))

    assert sorted(timeline.snapshot()) == ["c", "d"]


def test_consumer_naive_shutdown_after_aware_ready_still_marks_worker_offline() -> None:
    registry = WorkerRegistry()
    consumer = _consumer(WorkerTimeline(), registry)
    # _handle_event runs the wall-clock stale check, so stamp the events now.
    now = datetime.now(timezone.utc)
    consumer._handle_event(_event(READY, base=now))
    consumer._handle_event(_naive_event(SHUTDOWN, seconds=10, base=now))

    assert [w.status for w in registry.get_all_workers()] == [WorkerStatus.OFFLINE]


class _FailingTimeline(WorkerTimeline):
    def record(self, event: WorkerEvent) -> None:
        raise RuntimeError("timeline failure")


def test_consumer_failing_timeline_still_updates_registry() -> None:
    registry = WorkerRegistry()
    consumer = _consumer(_FailingTimeline(), registry)
    now = datetime.now(timezone.utc)

    consumer._handle_event(_event(READY, base=now))
    consumer._handle_event(_event(SHUTDOWN, seconds=10, base=now))

    assert [w.status for w in registry.get_all_workers()] == [WorkerStatus.OFFLINE]


def test_record_eviction_judges_node_by_newest_event_not_oldest() -> None:
    timeline = WorkerTimeline(max_nodes=2)
    timeline.record(_event(hostname="long", seconds=1))
    timeline.record(_event(hostname="long", seconds=100, pid=2))
    timeline.record(_event(hostname="mid", seconds=50))

    timeline.record(_event(hostname="new", seconds=200))

    assert sorted(timeline.snapshot()) == ["long", "new"]


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"max_events_per_node": 0}, "max_events_per_node"),
        ({"max_nodes": 0}, "max_nodes"),
        ({"max_events_per_node": -1}, "max_events_per_node"),
    ],
)
def test_init_bound_below_one_raises_value_error(
    kwargs: dict[str, int], name: str
) -> None:
    with pytest.raises(ValueError, match=name):
        WorkerTimeline(**kwargs)
