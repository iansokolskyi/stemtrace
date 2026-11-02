"""Which workers' names count and when the fleet is offline (D4, owner 2026-11-02).

A name counts while its worker is *live* (no ``worker_shutdown`` or
``worker_terminating`` since its latest ``worker_ready``); the fleet is offline
when no live worker was seen within ``WORKER_STALE_TIMEOUT_MINUTES``. A missed
inspect broadcast or the stale rule alone drops no names; the 30-minute
cleanup does. Reviewer PoC for the missed broadcast: a busy ``celery@solo``.
"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from stemtrace.core.anomalies import (
    AnomalyKind,
    AnomalyReport,
    RegistryFacts,
    classify_anomalies,
)
from stemtrace.core.events import TaskState
from stemtrace.server.anomaly_facts import classifier_facts
from stemtrace.server.store import GraphStore, WorkerRegistry
from tests.unit.anomaly_pipeline import SMALL, T0, task_event

SOLO, OTHER = "celery@solo", "celery@other"
SMALL_GRACE = replace(
    SMALL,
    pending_grace=timedelta(seconds=60),
    authority_window=timedelta(seconds=120),
)
D = T0  # when the last reply or event reached the server
MINUTE = timedelta(minutes=1)


def facts(
    registry: WorkerRegistry, now: datetime, *, broker: bool = True
) -> RegistryFacts:
    return registry.registry_facts(broker_configured=broker, now=now)


def classify(
    store: GraphStore, registry_facts: RegistryFacts, now: datetime
) -> AnomalyReport:
    f = classifier_facts(store.run_records(), {}, now)
    return classify_anomalies(
        **f._asdict(), now=now, settings=SMALL_GRACE, registry=registry_facts
    )


# --- Names count from every live worker (row 28b, 28c, 33) --------------------


def test_row28b_a_live_worker_missing_from_a_stats_reply_keeps_its_names() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply(
        {}, {SOLO: (1, ["app.solo"]), OTHER: (2, ["app.other"])}, now=D
    )

    # The next reply misses the busy solo worker (it missed the 0.5 s broadcast).
    registry.apply_stats_reply({}, {OTHER: (2, ["app.other"])}, now=D + MINUTE)
    got = facts(registry, D + MINUTE)

    assert got.live_names == {"app.solo", "app.other"}
    assert got.any_online is True


def test_row28b_the_reviewer_poc_does_not_flag_the_solo_workers_pending_task() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {SOLO: (1, ["app.solo"])}, now=D)
    now = D + 10 * MINUTE
    # The latest complete reply misses the busy solo worker.
    registry.apply_stats_reply({}, {OTHER: (2, ["app.other"])}, now=now - MINUTE / 2)
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 1, name="app.solo"))

    report = classify(store, facts(registry, now), now)

    assert list(report.items) == []


def test_row33_a_name_only_a_stopped_worker_registered_does_not_count() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.old"], event_timestamp=D)
    registry.register_worker(OTHER, 2, ["app.other"], event_timestamp=D)
    registry.mark_shutdown(SOLO, 1)

    assert facts(registry, D).live_names == {"app.other"}


def test_a_worker_ready_after_a_stop_makes_its_names_count_again() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.old"], event_timestamp=D)
    registry.mark_shutdown(SOLO, 1)
    registry.register_worker(SOLO, 3, ["app.new"], event_timestamp=D + MINUTE)

    got = facts(registry, D + MINUTE)

    assert "app.new" in got.live_names
    assert "app.old" not in got.live_names


def test_row28c_a_silent_worker_keeps_its_names_but_the_fleet_is_offline() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.solo"], event_timestamp=D)
    now = D + 10 * MINUTE
    registry.remove_stale_workers(now=now)  # the consumer's 3-minute rule

    got = facts(registry, now)

    assert got.live_names == {"app.solo"}
    assert (got.any_online, got.any_seen) == (False, True)


def test_the_thirty_minute_cleanup_drops_the_names_but_not_the_seen_flag() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.solo"], event_timestamp=D)
    now = D + 45 * MINUTE
    registry.remove_stale_workers(now=now)

    got = facts(registry, now)

    assert (got.live_names, got.any_online, got.any_seen) == (frozenset(), False, True)


def test_a_new_registry_has_seen_no_worker() -> None:
    got = facts(WorkerRegistry(), D)

    assert (got.any_seen, got.any_online) == (False, False)


@pytest.mark.parametrize(
    ("seconds", "online"), [(0, True), (179, True), (181, False), (3600, False)]
)
def test_the_fleet_is_online_while_a_live_worker_was_seen_within_three_minutes(
    seconds: int, online: bool
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.solo"], event_timestamp=D)

    assert facts(registry, D + timedelta(seconds=seconds)).any_online is online


def test_a_stopped_worker_never_counts_as_online_however_recent() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.solo"], event_timestamp=D)
    registry.mark_shutdown(SOLO, 1)

    assert facts(registry, D).any_online is False


# --- A reply whose nodes were all skipped as stale doesn't stamp ---------------


def test_a_reply_whose_nodes_all_changed_since_it_was_asked_does_not_stamp() -> None:
    registry = WorkerRegistry()
    seen = registry.event_generations()
    registry.register_worker(SOLO, 1, ["app.solo"], event_timestamp=D)  # newer event

    registry.apply_stats_reply(seen, {SOLO: (1, ["app.solo"])}, now=D)

    assert facts(registry, D).authority_at is None


def test_a_reply_with_one_fresh_node_stamps() -> None:
    registry = WorkerRegistry()
    seen = registry.event_generations()
    registry.register_worker(SOLO, 1, ["app.solo"], event_timestamp=D)

    registry.apply_stats_reply(
        seen, {SOLO: (1, ["app.solo"]), OTHER: (2, ["app.other"])}, now=D
    )

    assert facts(registry, D).authority_at == D


# --- Row 28: the fleet item holds for the whole outage ----------------------------


@pytest.mark.parametrize("stopped", [True, False])
@pytest.mark.parametrize("minutes", [2, 6, 29, 45, 120])
@pytest.mark.parametrize(
    "completed_ago_s", [None, 60, 600], ids=["never", "within-window", "past-window"]
)
@pytest.mark.parametrize("broker", [True, False])
def test_row28_the_fleet_item_follows_stops_and_inspect_evidence_for_the_whole_outage(
    broker: bool, completed_ago_s: int | None, minutes: int, stopped: bool
) -> None:
    """The fleet's last sign of life is at ``D``; then nothing arrives."""
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=D)
    if stopped:
        registry.mark_shutdown(SOLO, 1)  # a clean stop
    now = D + minutes * MINUTE
    if completed_ago_s is not None:
        registry.note_inspect_completed(now - timedelta(seconds=completed_ago_s))
    registry.remove_stale_workers(now=now)  # what the consumer runs periodically
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, -3600, name="app.known"))
    silent = minutes > 3
    evidence = completed_ago_s is not None and completed_ago_s <= 120
    deleted = minutes > 30  # the 30-minute cleanup removed the worker

    report = classify(store, facts(registry, now, broker=broker), now)

    expected = stopped or deleted or (silent and evidence)
    assert [(a.kind, a.task_count) for a in report.items] == (
        [(AnomalyKind.NO_WORKERS_ONLINE, 1)] if expected else []
    )
    assert dict(report.anomalies) == {}


def test_row28d_the_reviewer_poc_an_idle_fleet_flags_the_new_name_not_the_fleet() -> (
    None
):
    # broker_configured=False, worker_ready at T, PENDING app.new at T+1 min,
    # judged at T+6.5 min: nothing refreshed last_seen, but nothing says it died.
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=D)
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 60, name="app.new"))
    now = D + timedelta(minutes=6, seconds=30)

    report = classify(store, facts(registry, now, broker=False), now)

    assert [(a.kind, a.name) for a in report.items] == [
        (AnomalyKind.UNREGISTERED, "app.new")
    ]


def test_row28e_a_sigkilled_fleet_without_inspect_gives_no_fleet_item() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=D)
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 60, name="app.known"))
    now = D + 20 * MINUTE

    report = classify(store, facts(registry, now, broker=False), now)

    assert list(report.items) == []


def test_a_clean_shutdown_of_every_worker_gives_the_fleet_item_at_once() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=D)
    registry.mark_shutdown(SOLO, 1)
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 0, name="app.known"))
    now = D + 2 * MINUTE

    report = classify(store, facts(registry, now, broker=False), now)

    assert [a.kind for a in report.items] == [AnomalyKind.NO_WORKERS_ONLINE]


# --- Superseded pids, events from unknown nodes, the inspect clock -------------


def test_a_pid_superseded_by_a_newer_worker_ready_is_not_live() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.old"], event_timestamp=D)
    registry.register_worker(SOLO, 2, ["app.new"], event_timestamp=D + MINUTE)

    got = facts(registry, D + MINUTE)

    assert got.live_names == {"app.new"}


def test_a_pid_a_stats_reply_supersedes_is_not_live() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.old"], event_timestamp=D)
    seen = registry.event_generations()
    registry.apply_stats_reply(seen, {SOLO: (2, ["app.new"])}, now=D + MINUTE)

    assert facts(registry, D + MINUTE).live_names == {"app.new"}


def test_a_superseded_pid_does_not_keep_the_fleet_live_after_the_new_one_stops() -> (
    None
):
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.old"], event_timestamp=D)
    registry.register_worker(SOLO, 2, ["app.new"], event_timestamp=D + MINUTE)
    registry.mark_shutdown(SOLO, 2)

    got = facts(registry, D + MINUTE)

    assert (got.any_live, got.live_names) == (False, frozenset())


UNKNOWN = "celery@unknown"


def test_a_task_event_from_an_unknown_nodename_makes_it_known_live_and_online() -> None:
    registry = WorkerRegistry()

    registry.mark_seen(UNKNOWN, D, now=D)

    got = facts(registry, D)
    assert (got.any_seen, got.any_live, got.any_online) == (True, True, True)
    assert got.live_names == frozenset()  # no names, so the no-broker gate stays shut


def test_an_unknown_nodename_adds_no_row_to_the_workers_page() -> None:
    registry = WorkerRegistry()

    registry.mark_seen(UNKNOWN, D, now=D)

    assert registry.get_all_workers() == []


@pytest.mark.parametrize(
    "completed", [True, False], ids=["inspect-completed", "no-inspect"]
)
@pytest.mark.parametrize("age_s", [1, 179, 181, 600])
def test_row28f_a_busy_worker_unknown_to_the_registry_is_never_no_workers_online(
    age_s: int, completed: bool
) -> None:
    # An empty registry (restart without replay) and task events from a worker
    # that is running tasks: it must not read as an offline fleet.
    registry = WorkerRegistry()
    now = D + 20 * MINUTE
    registry.mark_seen(UNKNOWN, now - timedelta(seconds=age_s), now=now)
    if completed:
        registry.note_inspect_completed(now - timedelta(seconds=30))
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 0, name="app.new"))

    report = classify(store, facts(registry, now, broker=completed), now)

    silent_and_heard = age_s > 180 and completed
    assert [a.kind for a in report.items] == (
        [AnomalyKind.NO_WORKERS_ONLINE] if silent_and_heard else []
    )


def test_a_later_worker_ready_for_an_unknown_nodename_registers_its_names() -> None:
    registry = WorkerRegistry()
    registry.mark_seen(UNKNOWN, D, now=D)

    registry.register_worker(UNKNOWN, 7, ["app.new"], event_timestamp=D + MINUTE)

    got = facts(registry, D + MINUTE)
    assert got.live_names == {"app.new"}
    assert [(w.hostname, w.pid) for w in registry.get_all_workers()] == [(UNKNOWN, 7)]


def test_a_stopped_worker_is_not_revived_by_the_unknown_nodename_record() -> None:
    registry = WorkerRegistry()
    registry.mark_seen(UNKNOWN, D, now=D)
    registry.register_worker(UNKNOWN, 7, ["app.new"], event_timestamp=D)
    registry.mark_shutdown(UNKNOWN, 7)

    got = facts(registry, D)

    assert (got.any_live, got.any_online) == (False, False)


def test_the_cleanup_forgets_an_unknown_nodename_after_thirty_minutes() -> None:
    registry = WorkerRegistry()
    registry.mark_seen(UNKNOWN, D, now=D)
    now = D + 45 * MINUTE
    registry.remove_stale_workers(now=now)

    got = facts(registry, now)

    assert (got.any_seen, got.any_live) == (True, False)


def test_a_new_registry_has_no_inspect_time_and_note_inspect_completed_sets_it() -> (
    None
):
    registry = WorkerRegistry()
    assert facts(registry, D).inspect_completed_at is None

    registry.note_inspect_completed(D)

    assert facts(registry, D).inspect_completed_at == D


def test_the_inspect_time_is_separate_from_the_authority_time() -> None:
    registry = WorkerRegistry()
    registry.note_inspect_completed(D)

    assert facts(registry, D).authority_at is None


def test_registry_facts_say_whether_a_live_worker_exists() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=D)
    silent = facts(registry, D + 20 * MINUTE)

    assert (silent.any_live, silent.any_online) == (True, False)


def test_row28b_a_missed_broadcast_alone_never_makes_the_fleet_offline() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {SOLO: (1, ["app.solo"])}, now=D)
    # A reply that misses the solo worker marks it OFFLINE; then its only
    # peer stops. The solo worker was seen a minute ago, so it is still online.
    registry.apply_stats_reply({}, {OTHER: (2, ["app.other"])}, now=D + MINUTE)
    registry.mark_shutdown(OTHER, 2)

    got = facts(registry, D + 2 * MINUTE)

    assert got.any_online is True
    assert got.live_names == {"app.solo"}
