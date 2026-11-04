"""A clean stop of a restart whose ``worker_ready`` was missed (D4e readings, rows 28t-28v).

Spec: ``.context/features/anomaly-detection.md``, "D4e readings". A nodename has
one live process, so a ``worker_shutdown`` for a pid the registry doesn't list
ends the listed one too, unless it is stamped before that pid's registration.
"""

from collections import Counter
from datetime import timedelta

import pytest

from stemtrace.server.store import WorkerRegistry
from tests.unit.fleet_world import ROWS, World, patched_clock
from tests.unit.test_fleet_alert import (
    MINUTE,
    SOLO,
    T0,
    fleet_items,
    report_at,
)

STOPPED = [(None, None, 1)]


def listed_then_restarted_unannounced() -> WorkerRegistry:
    """Pid 1 registered, killed; pid 2 runs tasks (its ``worker_ready`` missed), then stops."""
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 - 5 * MINUTE)
    registry.mark_seen(SOLO, T0 - MINUTE, now=T0 - MINUTE)
    registry.mark_shutdown(SOLO, 2, T0, now=T0)
    return registry


@pytest.mark.parametrize("broker", [True, False], ids=["broker", "no-broker"])
def test_row28t_clean_stop_of_an_unannounced_restart_reads_offline_at_once(
    broker: bool,
) -> None:
    registry = listed_then_restarted_unannounced()

    report = report_at(registry, T0, broker=broker)

    assert fleet_items(report) == STOPPED
    assert [(w.pid, w.shutdown_reported) for w in registry.get_all_workers()] == [
        (1, True)
    ]


@pytest.mark.parametrize(
    ("delta_s", "stops_listed"),
    [(-10, False), (0, True), (10, True)],
    ids=["before", "at", "after"],
)
def test_row28u_the_stop_ends_the_listed_pid_only_when_stamped_at_or_after_its_registration(
    delta_s: int, stops_listed: bool
) -> None:
    registry = WorkerRegistry()
    registered = T0 - 5 * MINUTE
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=registered)
    registry.mark_seen(SOLO, T0 - MINUTE, now=T0 - MINUTE)

    registry.mark_shutdown(SOLO, 2, registered + timedelta(seconds=delta_s), now=T0)

    report = report_at(registry, T0)
    assert fleet_items(report) == (STOPPED if stops_listed else [])


def test_row28u_only_the_newest_listed_pid_is_stopped_the_older_one_stays_superseded() -> (
    None
):
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 - 5 * MINUTE)
    registry.register_worker(SOLO, 2, ["app.known"], event_timestamp=T0 - 4 * MINUTE)

    registry.mark_shutdown(SOLO, 3, T0 - 3 * MINUTE, now=T0)

    states = {
        w.pid: (w.superseded, w.shutdown_reported) for w in registry.get_all_workers()
    }
    assert states == {1: (True, False), 2: (False, True)}


def test_row28v_an_event_after_the_stop_revives_it_and_a_ping_does_not() -> None:
    registry = listed_then_restarted_unannounced()
    registry.apply_ping_reply([SOLO], now=T0 + MINUTE)
    assert fleet_items(report_at(registry, T0 + MINUTE)) == STOPPED

    registry.mark_seen(SOLO, T0 + 2 * MINUTE, now=T0 + 2 * MINUTE)

    assert fleet_items(report_at(registry, T0 + 2 * MINUTE)) == []


def test_row28v_a_stats_reply_naming_the_old_pid_registers_it_again() -> None:
    registry = listed_then_restarted_unannounced()

    registry.apply_stats_reply(
        registry.event_generations(),
        {SOLO: (1, ["app.known"])},
        now=T0 + MINUTE,
    )

    assert fleet_items(report_at(registry, T0 + MINUTE)) == []


# --- Row 28w: "registration" is the newest worker_ready stamp (D4e reading 5) -----


def pids_state(registry: WorkerRegistry) -> list[tuple[int, bool]]:
    """(pid, stopped) of every listed worker."""
    return [(w.pid, w.shutdown_reported) for w in registry.get_all_workers()]


def test_row28w_a_reannounced_same_pid_moves_the_stamp_so_an_older_stop_is_ignored() -> (
    None
):
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 7, ["app.known"], event_timestamp=T0 - 30 * MINUTE)
    registry.register_worker(SOLO, 7, ["app.known"], event_timestamp=T0)

    registry.mark_shutdown(SOLO, 8, T0 - 10 * MINUTE, now=T0)

    assert pids_state(registry) == [(7, False)]
    assert fleet_items(report_at(registry, T0)) == []


def test_row28w_a_replayed_older_ready_does_not_move_the_stamp_back() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 7, ["app.known"], event_timestamp=T0)
    registry.register_worker(SOLO, 7, ["app.known"], event_timestamp=T0 - 30 * MINUTE)

    registry.mark_shutdown(SOLO, 8, T0 - 10 * MINUTE, now=T0)

    assert pids_state(registry) == [(7, False)]


def test_row28w_stats_replies_keep_the_first_registration_time() -> None:
    registry = WorkerRegistry()
    for at in (T0 - 30 * MINUTE, T0):
        registry.apply_stats_reply(
            registry.event_generations(), {SOLO: (7, ["app.known"])}, now=at
        )

    registry.mark_shutdown(SOLO, 8, T0 - 10 * MINUTE, now=T0)

    assert pids_state(registry) == [(7, True)]


def test_row28w_a_stats_reply_after_a_ready_does_not_move_the_stamp() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 7, ["app.known"], event_timestamp=T0 - 30 * MINUTE)
    registry.apply_stats_reply(
        registry.event_generations(), {SOLO: (7, ["app.known"])}, now=T0
    )

    registry.mark_shutdown(SOLO, 8, T0 - 10 * MINUTE, now=T0)

    assert pids_state(registry) == [(7, True)]


# --- Row 28x: a stop stamped before the pid's newest ready is a stop of an older process ---


def test_row28x_a_late_stop_naming_the_re_announced_pid_leaves_it_live() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 7, ["app.known"], event_timestamp=T0 - 30 * MINUTE)
    registry.register_worker(SOLO, 7, ["app.known"], event_timestamp=T0)

    registry.mark_shutdown(SOLO, 7, T0 - 10 * MINUTE, now=T0)

    assert pids_state(registry) == [(7, False)]
    assert fleet_items(report_at(registry, T0)) == []


@pytest.mark.parametrize(
    ("delta_s", "stopped"),
    [(-1, False), (0, True), (60, True)],
    ids=["before", "at", "after"],
)
def test_row28x_the_exact_pid_stop_is_compared_with_its_newest_ready(
    delta_s: int, stopped: bool
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 7, ["app.known"], event_timestamp=T0)

    registry.mark_shutdown(SOLO, 7, T0 + timedelta(seconds=delta_s), now=T0)

    assert pids_state(registry) == [(7, stopped)]


def test_row28x_a_pid_known_only_from_stats_is_stopped_by_any_stop_naming_it() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply(
        registry.event_generations(), {SOLO: (7, ["app.known"])}, now=T0
    )

    registry.mark_shutdown(SOLO, 7, T0 - 10 * MINUTE, now=T0)

    assert pids_state(registry) == [(7, True)]


# Seeds that failed on a17d2d3 with the unannounced-restart state on.
REGRESSION_SEEDS = [34, 51, 65, 74, 89, 111]


@pytest.mark.parametrize("seed", REGRESSION_SEEDS)
def test_fuzz_regression_seeds_agree_with_the_oracle(
    monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    reached: Counter[str] = Counter()
    world = World(seed, reached, broker=seed % 4 != 0, deaf=seed % 4 == 1)
    patched_clock(monkeypatch, world)

    world.run(70)

    assert set(reached) <= set(ROWS)
