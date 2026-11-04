"""Rows 28aa/28ab: a same-pid worker_ready never moves last_seen back (D4e reading 16)."""

from datetime import timedelta

import pytest

from stemtrace.server.store import WorkerRegistry
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


@pytest.mark.parametrize("stopped", [False, True], ids=["live", "stopped"])
def test_row28ab_an_older_same_pid_ready_never_moves_last_seen_back(
    stopped: bool,
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.mark_seen(SOLO, T0 + MINUTE, now=T0 + MINUTE)
    if stopped:
        registry.mark_shutdown(SOLO, 1, T0 + 2 * MINUTE, now=T0 + 2 * MINUTE)

    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 - MINUTE)

    worker = registry.get_worker(SOLO, 1)
    assert worker is not None
    assert worker.last_seen == T0 + MINUTE


def test_row28ab_a_naive_stamp_then_a_second_ready_does_not_raise() -> None:
    registry = WorkerRegistry()
    registry.register_worker(
        SOLO, 1, ["app.known"], event_timestamp=T0.replace(tzinfo=None)
    )

    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 + MINUTE)

    worker = registry.get_worker(SOLO, 1)
    assert worker is not None
    assert worker.last_seen == T0 + MINUTE
