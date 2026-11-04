"""D4d: silent with evidence needs two consecutive unanswered ``stats()`` broadcasts.

Rows 28l-28o of ``.context/features/anomaly-detection.md`` ("D4d silence needs two
misses"), through the real ``WorkerRegistry`` and the real refresh function against a
fake Celery ``Inspect`` (Celery 5.6.3: ``stats()`` returns None when nobody answers).
Row 28p, the probe's confirm, is in ``test_fleet_alert_probe.py``.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI

from stemtrace.core.anomalies import AnomalyKind
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.inspect_probe import (
    INSPECT_REFRESH_MIN_INTERVAL_SECONDS,
    InspectProbe,
)
from stemtrace.server.store import GraphStore, WorkerRegistry
from tests.unit.anomaly_pipeline import T0
from tests.unit.test_fleet_alert import (
    MINUTE,
    OTHER,
    SOLO,
    TWO_S,
    FakeInspect,
    answer_pairs,
    fleet_items,
    prove_channel,
    refresh_at,
    report_at,
)

BROKER = "redis://broker:6379/0"
# Past the stale window, so only the two-miss rule can keep the fleet online.
FIRST_MISS_AT = T0 + 10 * MINUTE


def idle_fleet(*nodes: str) -> WorkerRegistry:
    """Workers whose last sign of life is at ``T0``, on a server whose channel is proven."""
    registry = WorkerRegistry()
    prove_channel(registry)
    for pid, node in enumerate(nodes, start=1):
        registry.register_worker(node, pid, ["app.known"], event_timestamp=T0)
    return registry


def miss(registry: WorkerRegistry, at: datetime) -> None:
    """A completed ``stats()`` broadcast nobody answered."""
    registry.apply_stats_reply({}, {}, now=at)


def silent(registry: WorkerRegistry, at: datetime) -> bool:
    """Whether the fleet item shows at ``at``."""
    return fleet_items(report_at(registry, at)) != []


# --- 28l: one broadcast missed by every worker -----------------------------------------


@pytest.mark.parametrize("via", ["empty-stats", "refresh-no-ping"])
def test_row28l_a_fleet_that_misses_one_broadcast_gives_no_item(via: str) -> None:
    registry = idle_fleet(SOLO, OTHER)

    if via == "empty-stats":
        miss(registry, FIRST_MISS_AT)
    else:
        refresh_at(registry, FIRST_MISS_AT, FakeInspect(stats=None, ping=None))
    report = report_at(registry, FIRST_MISS_AT)

    assert report.items == []
    assert report.counts[AnomalyKind.NO_WORKERS_ONLINE] == 0


# --- 28m: two consecutive misses at least the shared interval apart ----------------------


@pytest.mark.parametrize("gap_s", [2, 3, 60, 600])
def test_row28m_two_misses_at_least_two_seconds_apart_give_the_item(
    gap_s: int,
) -> None:
    registry = idle_fleet(SOLO, OTHER)
    second = FIRST_MISS_AT + timedelta(seconds=gap_s)

    miss(registry, FIRST_MISS_AT)
    miss(registry, second)

    assert fleet_items(report_at(registry, second)) == [(None, None, 1)]


def test_row28m_a_third_miss_changes_nothing() -> None:
    registry = idle_fleet(SOLO)
    for step in range(3):
        miss(registry, FIRST_MISS_AT + step * TWO_S)

    assert silent(registry, FIRST_MISS_AT + 4 * TWO_S) is True


def test_row28m_one_worker_answering_the_second_broadcast_keeps_the_fleet_online() -> (
    None
):
    registry = idle_fleet(SOLO, OTHER)

    miss(registry, FIRST_MISS_AT)
    registry.apply_stats_reply({}, answer_pairs(OTHER), now=FIRST_MISS_AT + TWO_S)

    assert silent(registry, FIRST_MISS_AT + TWO_S) is False


# --- 28n: an answer or an event between two misses starts the count over --------------


@pytest.mark.parametrize("sign", ["stats", "ping", "event"])
def test_row28n_miss_then_a_sign_of_life_then_miss_gives_no_item(sign: str) -> None:
    registry = idle_fleet(SOLO)
    miss(registry, FIRST_MISS_AT)
    between = FIRST_MISS_AT + TWO_S

    if sign == "stats":
        registry.apply_stats_reply({}, answer_pairs(SOLO), now=between)
    elif sign == "ping":
        registry.apply_ping_reply([SOLO], now=between)
    else:
        # A replayed event stamped long ago: it moves no clock, only the count.
        registry.mark_seen(SOLO, T0, now=between)
    miss(registry, between + TWO_S)

    # The next broadcast is the first miss since: a later evaluation past the
    # stale window still isn't evidence.
    assert silent(registry, FIRST_MISS_AT + 20 * MINUTE) is False


def test_row28n_after_the_reset_two_more_misses_give_the_item() -> None:
    registry = idle_fleet(SOLO)
    miss(registry, FIRST_MISS_AT)
    registry.apply_ping_reply([SOLO], now=FIRST_MISS_AT + TWO_S)
    first = FIRST_MISS_AT + 5 * MINUTE
    miss(registry, first)
    miss(registry, first + TWO_S)

    assert silent(registry, first + TWO_S + 4 * MINUTE) is True


def test_row28n_a_worker_ready_resets_the_count() -> None:
    registry = idle_fleet(SOLO)
    miss(registry, FIRST_MISS_AT)
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)  # old stamp
    miss(registry, FIRST_MISS_AT + TWO_S)

    assert silent(registry, FIRST_MISS_AT + TWO_S) is False


# --- 28o: misses closer than the shared interval count once ------------------------------


@pytest.mark.parametrize("gap_s", [0, 0.5, 1, 1.9])
def test_row28o_two_misses_less_than_two_seconds_apart_give_no_item(
    gap_s: float,
) -> None:
    registry = idle_fleet(SOLO, OTHER)
    second = FIRST_MISS_AT + timedelta(seconds=gap_s)

    miss(registry, FIRST_MISS_AT)
    miss(registry, second)

    assert silent(registry, second) is False


def test_row28o_the_too_close_miss_does_not_move_the_clock_the_next_one_is_measured_from() -> (
    None
):
    registry = idle_fleet(SOLO)

    miss(registry, FIRST_MISS_AT)
    miss(registry, FIRST_MISS_AT + timedelta(seconds=1))  # not counted
    miss(registry, FIRST_MISS_AT + TWO_S)  # 2 s after the counted one

    assert silent(registry, FIRST_MISS_AT + TWO_S) is True


def test_the_interval_the_two_misses_need_is_the_shared_inspect_interval() -> None:
    assert timedelta(seconds=INSPECT_REFRESH_MIN_INTERVAL_SECONDS) == TWO_S


# --- Both broadcasts complete after last_seen; the channel is proven first --------------


def test_a_broadcast_completed_before_the_last_sign_of_life_is_not_a_miss() -> None:
    registry = WorkerRegistry()
    prove_channel(registry)
    # The worker's clock runs ahead: its last sign is stamped after the first broadcast.
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 + 6 * MINUTE)

    miss(registry, T0 + 5 * MINUTE)  # completed before last_seen
    miss(registry, FIRST_MISS_AT)  # the first counted miss

    assert silent(registry, FIRST_MISS_AT) is False


def test_misses_before_any_worker_answered_do_not_accumulate() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    miss(registry, FIRST_MISS_AT)
    miss(registry, FIRST_MISS_AT + TWO_S)  # nobody has ever answered: not evidence
    registry.apply_ping_reply(["celery@prover"])  # now the channel is proven

    miss(registry, FIRST_MISS_AT + 2 * TWO_S)

    assert silent(registry, FIRST_MISS_AT + 2 * TWO_S) is False


def test_a_stopped_worker_is_offline_at_once_without_misses() -> None:
    registry = idle_fleet(SOLO)
    registry.mark_shutdown(SOLO, 1, now=FIRST_MISS_AT)

    assert silent(registry, FIRST_MISS_AT) is True


# --- Counts are bounded and dropped with the worker ------------------------------------


def test_counts_are_dropped_with_a_stopped_worker_and_stay_two_at_most() -> None:
    registry = idle_fleet(SOLO, OTHER)
    for step in range(5):
        miss(registry, FIRST_MISS_AT + step * TWO_S)
    assert sorted(registry._misses) == [OTHER, SOLO]
    assert {count for count, _ in registry._misses.values()} == {2}

    registry.mark_shutdown(OTHER, 2, now=FIRST_MISS_AT)

    assert sorted(registry._misses) == [SOLO]


def test_a_node_that_left_the_registry_is_pruned_by_the_next_broadcast() -> None:
    registry = idle_fleet(SOLO, OTHER)
    miss(registry, FIRST_MISS_AT)
    # OTHER's entry is gone from the live set without a mark_shutdown on this registry
    # path: superseded by a newer pid, then that pid stopped.
    registry.register_worker(OTHER, 3, ["app.known"], event_timestamp=T0)
    registry.mark_shutdown(OTHER, 3, now=FIRST_MISS_AT)

    miss(registry, FIRST_MISS_AT + TWO_S)

    assert sorted(registry._misses) == [SOLO]


# --- The first miss tells its listeners once, so one confirm is owed ---------------------


def count_first_misses(registry: WorkerRegistry) -> list[int]:
    """Register a listener; its call count is ``calls[0]``."""
    calls = [0]

    def listener() -> None:
        calls[0] += 1

    registry.add_first_miss_listener(listener)
    return calls


def test_a_first_miss_calls_the_listener_once_and_a_run_of_misses_adds_no_calls() -> (
    None
):
    registry = idle_fleet(SOLO, OTHER)
    calls = count_first_misses(registry)

    miss(registry, FIRST_MISS_AT)
    assert calls == [1]  # two workers, one broadcast: one call
    for step in range(1, 6):
        miss(registry, FIRST_MISS_AT + step * TWO_S)

    assert calls == [1]


def test_a_miss_after_an_answer_is_a_new_first_miss() -> None:
    registry = idle_fleet(SOLO)
    calls = count_first_misses(registry)

    miss(registry, FIRST_MISS_AT)
    registry.apply_ping_reply([SOLO], now=FIRST_MISS_AT + TWO_S)
    miss(registry, FIRST_MISS_AT + 2 * TWO_S)

    assert calls == [2]


def test_a_too_close_second_miss_owes_no_second_confirm() -> None:
    registry = idle_fleet(SOLO)
    calls = count_first_misses(registry)

    miss(registry, FIRST_MISS_AT)
    miss(registry, FIRST_MISS_AT + timedelta(seconds=1))

    assert calls == [1]


def test_nothing_is_owed_when_nobody_missed_or_nobody_can_be_heard() -> None:
    answered = idle_fleet(SOLO)
    calls = count_first_misses(answered)
    answered.apply_stats_reply({}, answer_pairs(SOLO), now=FIRST_MISS_AT)

    unproven = WorkerRegistry()
    unproven.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    unproven_calls = count_first_misses(unproven)
    miss(unproven, FIRST_MISS_AT)

    stopped = idle_fleet(SOLO)
    stopped_calls = count_first_misses(stopped)
    stopped.mark_shutdown(SOLO, 1, now=T0)
    miss(stopped, FIRST_MISS_AT)

    assert (calls, unproven_calls, stopped_calls) == ([0], [0], [0])


def test_a_failed_stats_call_is_no_broadcast_and_owes_no_confirm() -> None:
    registry = idle_fleet(SOLO)
    calls = count_first_misses(registry)

    refresh_at(registry, FIRST_MISS_AT, FakeInspect(stats=OSError("broker down")))

    assert (calls, registry._misses) == ([0], {})


def test_a_broadcast_before_the_last_sign_of_life_owes_nothing() -> None:
    registry = WorkerRegistry()
    prove_channel(registry)
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 + MINUTE)
    calls = count_first_misses(registry)

    miss(registry, T0)

    assert calls == [0]


def test_a_listener_that_raises_does_not_break_the_broadcast() -> None:
    registry = idle_fleet(SOLO)

    def broken() -> None:
        raise RuntimeError("listener bug")

    registry.add_first_miss_listener(broken)
    calls = count_first_misses(registry)

    miss(registry, FIRST_MISS_AT)

    assert calls == [1]


class RecordingProbe(InspectProbe):
    """A probe that records its triggers instead of running them."""

    def __init__(self) -> None:
        super().__init__()
        self.triggers: list[str | None] = []

    def trigger(self, nodename: str | None = None) -> None:
        self.triggers.append(nodename)


def test_the_router_asks_the_probe_for_one_confirm_per_first_miss() -> None:
    registry = idle_fleet(SOLO)
    probe = RecordingProbe()
    app = FastAPI()
    app.include_router(
        create_api_router(
            GraphStore(),
            worker_registry=registry,
            broker_url=BROKER,
            inspect_probe=probe,
        )
    )

    miss(registry, FIRST_MISS_AT)
    miss(registry, FIRST_MISS_AT + TWO_S)

    # A trigger without a nodename: one refresh at the end of the shared interval.
    assert probe.triggers == [None]
