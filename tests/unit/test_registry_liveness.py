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


@pytest.mark.parametrize("broker", [True, False])
@pytest.mark.parametrize("age_s", [0, 300, 660])
@pytest.mark.parametrize("minutes", [2, 6, 12, 29, 45, 120])
def test_row28_the_fleet_item_holds_for_the_whole_outage_after_a_sigkill(
    minutes: int, age_s: int, broker: bool
) -> None:
    """The fleet dies silently; its last sign of life was ``age_s`` before ``D``."""
    registry = WorkerRegistry()
    last_sign = D - timedelta(seconds=age_s)
    if broker:
        registry.apply_stats_reply({}, {SOLO: (1, ["app.known"])}, now=last_sign)
    else:
        registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=last_sign)
    now = D + minutes * MINUTE
    registry.remove_stale_workers(now=now)  # what the consumer runs periodically
    store = GraphStore()
    store.add_event(
        task_event("t1", TaskState.PENDING, -3600, name="app.known")
    )  # waiting an hour
    silent_for = now - last_sign

    report = classify(store, facts(registry, now, broker=broker), now)

    if silent_for > timedelta(minutes=3):
        assert [(a.kind, a.task_count) for a in report.items] == [
            (AnomalyKind.NO_WORKERS_ONLINE, 1)
        ]
        assert dict(report.anomalies) == {}
    else:
        assert list(report.items) == []  # still inside the stale window


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
