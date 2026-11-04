"""Rows 28aa/28ab: a stale same-pid worker_ready changes nothing (D4e reading 15)."""

from datetime import timedelta

import pytest

from stemtrace.server.store import WorkerRegistry, WorkerStatus
from tests.unit.anomaly_pipeline import T0
from tests.unit.test_fleet_alert import (
    MINUTE,
    OTHER,
    SOLO,
    TWO_S,
    fleet_items,
    mono,
    prove_channel,
    report_at,
)


def answer_without_solo(registry: WorkerRegistry, at: timedelta) -> None:
    """A completed ``stats()`` broadcast only OTHER answered, so SOLO missed it."""
    now = T0 + at
    registry.apply_stats_reply(
        registry.event_generations(),
        {OTHER: (2, ["app.known"])},
        now=now,
        monotonic=mono(now),
    )


def test_row28aa_an_older_ready_does_not_bring_counted_misses_back() -> None:
    registry = WorkerRegistry()
    prove_channel(registry, T0 - MINUTE)
    # Registered from stats replies: no worker_ready stamp, so only `last_seen` guards it.
    registry.apply_stats_reply(
        registry.event_generations(),
        {SOLO: (1, ["app.known"]), OTHER: (2, ["app.known"])},
        now=T0,
        monotonic=mono(T0),
    )
    answer_without_solo(registry, TWO_S)
    answer_without_solo(registry, 2 * TWO_S)
    event_at = T0 + timedelta(seconds=10)
    registry.mark_seen(SOLO, event_at, now=event_at)
    registry.mark_shutdown(OTHER, 2, event_at + timedelta(seconds=1), now=event_at)

    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 - 10 * MINUTE)

    assert fleet_items(report_at(registry, T0 + timedelta(seconds=12))) == []
    solo = registry.get_worker(SOLO, 1)
    assert solo is not None
    assert solo.last_seen == event_at


def test_row28aa_a_ready_stamped_before_the_last_seen_keeps_it() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply(
        registry.event_generations(), {SOLO: (1, ["app.known"])}, now=T0 + MINUTE
    )

    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)

    worker = registry.get_worker(SOLO, 1)
    assert worker is not None
    assert worker.last_seen == T0 + MINUTE


@pytest.mark.parametrize(
    "replay",
    [-10 * MINUTE, timedelta(0), timedelta(seconds=30)],
    ids=["older", "duplicate", "just-before-the-stop"],
)
@pytest.mark.parametrize("announced", [True, False], ids=["ready", "stats"])
def test_row28ab_a_stale_ready_does_not_revive_a_stopped_pid(
    replay: timedelta, announced: bool
) -> None:
    registry = WorkerRegistry()
    if announced:
        registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    else:
        registry.apply_stats_reply(
            registry.event_generations(), {SOLO: (1, ["app.known"])}, now=T0
        )
    registry.mark_shutdown(SOLO, 1, T0 + MINUTE, now=T0 + MINUTE)

    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 + replay)

    worker = registry.get_worker(SOLO, 1)
    assert worker is not None
    assert (worker.status, worker.shutdown_reported) == (WorkerStatus.OFFLINE, True)


def test_row28ab_a_ready_stamped_after_the_stop_revives_the_pid() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.mark_shutdown(SOLO, 1, T0 + MINUTE, now=T0 + MINUTE)

    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 + 2 * MINUTE)

    worker = registry.get_worker(SOLO, 1)
    assert worker is not None
    assert (worker.status, worker.shutdown_reported) == (WorkerStatus.ONLINE, False)


def test_row28ab_a_stale_ready_does_not_supersede_the_newer_pid() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.register_worker(SOLO, 2, ["app.known"], event_timestamp=T0 + MINUTE)

    registry.register_worker(SOLO, 1, ["app.old"], event_timestamp=T0 - MINUTE)

    older, newer = registry.get_worker(SOLO, 1), registry.get_worker(SOLO, 2)
    assert older is not None
    assert newer is not None
    assert (older.status, older.superseded) == (WorkerStatus.OFFLINE, True)
    assert (newer.status, newer.superseded) == (WorkerStatus.ONLINE, False)
    assert older.registered_tasks == ["app.known"]
