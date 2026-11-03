"""Which workers' names count (D4, owner 2026-11-02).

A name counts while its worker is *live*: no ``worker_shutdown`` or
``worker_terminating`` since its latest ``worker_ready``, and no newer pid of
the same nodename. A missed inspect broadcast or the stale rule alone drops no
names; the 30-minute cleanup does. Reviewer PoC for the missed broadcast: a busy ``celery@solo``.
"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from stemtrace.core.anomalies import (
    DEFAULT_SETTINGS,
    AnomalyKind,
    AnomalyReport,
    AnomalySettings,
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
    registry: WorkerRegistry, now: datetime | None = None, *, broker: bool = True
) -> RegistryFacts:
    return registry.registry_facts(broker_configured=broker, now=now)


def classify(
    store: GraphStore,
    registry_facts: RegistryFacts,
    now: datetime,
    settings: AnomalySettings = SMALL_GRACE,
) -> AnomalyReport:
    f = classifier_facts(store.run_records(), {}, now)
    return classify_anomalies(
        **f._asdict(), now=now, settings=settings, registry=registry_facts
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


def test_a_silent_worker_keeps_its_names_until_the_cleanup() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.solo"], event_timestamp=D)
    now = D + 10 * MINUTE
    registry.remove_stale_workers(now=now)  # the consumer's 3-minute rule

    got = facts(registry, now)

    assert got.live_names == {"app.solo"}


def test_the_thirty_minute_cleanup_drops_the_names() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.solo"], event_timestamp=D)
    now = D + 45 * MINUTE
    registry.remove_stale_workers(now=now)

    got = facts(registry, now)

    assert got.live_names == frozenset()


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


# --- Row 28: the fleet item follows a reported stop, never bare silence --------------


@pytest.mark.parametrize("stopped", [True, False])
@pytest.mark.parametrize("minutes", [2, 6, 12, 29, 45, 120])
@pytest.mark.parametrize("age_s", [0, 300, 660])
@pytest.mark.parametrize("broker", [True, False])
def test_row28_the_fleet_item_is_emitted_for_the_whole_outage_only_after_a_reported_stop(
    broker: bool, age_s: int, minutes: int, stopped: bool
) -> None:
    """The fleet's last sign of life is ``age_s`` before ``D``; then nothing arrives."""
    registry = WorkerRegistry()
    last_sign = D - timedelta(seconds=age_s)
    if broker:
        registry.apply_stats_reply({}, {SOLO: (1, ["app.known"])}, now=last_sign)
    else:
        registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=last_sign)
    if stopped:
        registry.mark_shutdown(SOLO, 1)
    now = D + minutes * MINUTE
    registry.remove_stale_workers(now=now)  # what the consumer runs periodically
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, -3600, name="app.known"))

    report = classify(store, facts(registry, now, broker=broker), now)

    # No inspect completed after the last sign, so silence alone is no evidence.
    assert report.counts[AnomalyKind.NO_WORKERS_ONLINE] == int(stopped)
    assert [a.kind for a in report.items] == (
        [AnomalyKind.NO_WORKERS_ONLINE] if stopped else []
    )


@pytest.mark.parametrize("minutes", [2, 6, 29])
def test_row28_a_sigkilled_fleet_keeps_its_names_until_the_cleanup(
    minutes: int,
) -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {SOLO: (1, ["app.known"])}, now=D)
    now = D + minutes * MINUTE
    registry.remove_stale_workers(now=now)
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, -3600, name="app.known"))

    report = classify(store, facts(registry, now), now)

    assert list(report.items) == []


def test_row28_after_the_cleanup_the_gate_is_closed_and_nothing_fires() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {SOLO: (1, ["app.known"])}, now=D)
    now = D + 45 * MINUTE
    registry.remove_stale_workers(now=now)
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, -3600, name="app.known"))

    report = classify(store, facts(registry, now), now)

    assert list(report.items) == []


# --- Superseded pids ----------------------------------------------------------


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


def test_a_superseded_pid_does_not_keep_names_after_the_new_one_stops() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.old"], event_timestamp=D)
    registry.register_worker(SOLO, 2, ["app.new"], event_timestamp=D + MINUTE)
    registry.mark_shutdown(SOLO, 2)

    got = facts(registry, D + MINUTE)

    assert got.live_names == frozenset()


# --- A fleet that is down raises no per-name flag (owner gap 3) -----------------


@pytest.mark.parametrize("minutes", [6, 9])
def test_the_reviewer_poc_two_workers_stop_cleanly_after_a_complete_reply(
    minutes: int,
) -> None:
    registry = WorkerRegistry()
    names = ["app.a", "app.b", "app.c"]
    registry.apply_stats_reply({}, {SOLO: (1, names), OTHER: (2, names)}, now=D)
    registry.mark_shutdown(SOLO, 1)
    registry.mark_shutdown(OTHER, 2)
    store = GraphStore()
    for i, name in enumerate(names):
        store.add_event(task_event(f"t{i}", TaskState.PENDING, 10, name=name))
    now = D + minutes * MINUTE  # past the 5-minute grace, inside the 10-minute window

    report = classify(store, facts(registry, now), now, DEFAULT_SETTINGS)

    # Owner gap 3: no per-name item; the fleet item says it once.
    assert [(a.kind, a.name) for a in report.items] == [
        (AnomalyKind.NO_WORKERS_ONLINE, None)
    ]
    assert report.anomalies == {}


@pytest.mark.parametrize("broker", [True, False])
@pytest.mark.parametrize("authority_age_s", [30, 3600], ids=["fresh", "stale"])
@pytest.mark.parametrize("stopped", [2, 1], ids=["all", "all-but-one"])
def test_gate_closes_while_every_worker_is_stopped(
    stopped: int, authority_age_s: int, broker: bool
) -> None:
    now = D + 2 * MINUTE
    registry = WorkerRegistry()
    both = {SOLO: (1, ["app.solo"]), OTHER: (2, ["app.other"])}
    registry.apply_stats_reply({}, both, now=now - timedelta(seconds=authority_age_s))
    registry.mark_shutdown(SOLO, 1)
    if stopped == 2:
        registry.mark_shutdown(OTHER, 2)
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 0, name="app.solo"))

    report = classify(store, facts(registry, now, broker=broker), now)

    # A name only the stopped worker registered is flagged only while some worker
    # is live and the registry is trusted (a recent reply, or no broker URL).
    trusted = authority_age_s <= 120 or not broker
    flagged = stopped == 1 and trusted
    if stopped == 2:
        assert [a.kind for a in report.items] == [AnomalyKind.NO_WORKERS_ONLINE]
    else:
        assert [a.name for a in report.items] == (["app.solo"] if flagged else [])
