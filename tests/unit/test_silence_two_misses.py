"""D4d: silent with evidence needs two consecutive unanswered ``stats()`` broadcasts.

Rows 28l-28o of ``.context/features/anomaly-detection.md`` ("D4d silence needs two
misses"), through the real ``WorkerRegistry`` and the real refresh function against a
fake Celery ``Inspect`` (Celery 5.6.3: ``stats()`` returns None when nobody answers).
Row 28p, the probe's confirm, is in ``test_fleet_alert_probe.py``.
"""

import random
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

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
    apply_miss,
    fleet_items,
    prove_channel,
    refresh_at,
    report_at,
)
from tests.unit.test_inspect_probe_rows import BOUND_S, FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable

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
    apply_miss(registry, at)


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
        registry.mark_seen(SOLO, between, now=between)
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


def test_row28n_a_worker_ready_stamped_after_the_miss_drops_it() -> None:
    registry = idle_fleet(SOLO)
    miss(registry, FIRST_MISS_AT)
    between = FIRST_MISS_AT + TWO_S
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=between)
    miss(registry, between + TWO_S)

    assert silent(registry, FIRST_MISS_AT + 20 * MINUTE) is False


@pytest.mark.parametrize("sign", ["old-event", "old-worker-ready"])
def test_reading2_a_sign_that_does_not_move_last_seen_drops_nothing(
    sign: str,
) -> None:
    registry = idle_fleet(SOLO)
    miss(registry, FIRST_MISS_AT)
    if sign == "old-event":
        registry.mark_seen(SOLO, T0, now=FIRST_MISS_AT + TWO_S)  # a replayed event
    else:
        registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    miss(registry, FIRST_MISS_AT + 2 * TWO_S)

    assert silent(registry, FIRST_MISS_AT + 2 * TWO_S) is True


def test_reading2_an_event_between_two_counted_misses_drops_only_the_first() -> None:
    registry = idle_fleet(SOLO)
    miss(registry, FIRST_MISS_AT)
    miss(registry, FIRST_MISS_AT + 10 * TWO_S)
    # Moves last_seen past the first miss only; the evaluation is past the window.
    registry.mark_seen(SOLO, FIRST_MISS_AT + 5 * TWO_S, now=FIRST_MISS_AT + 11 * TWO_S)

    assert silent(registry, FIRST_MISS_AT + 20 * MINUTE) is False


def test_reading3_the_gap_is_measured_on_the_monotonic_clock() -> None:
    registry = idle_fleet(SOLO)
    # An NTP step puts the wall clock 30 s back between the two completions.
    registry.apply_stats_reply({}, {}, now=FIRST_MISS_AT, monotonic=100.0)
    registry.apply_stats_reply(
        {}, {}, now=FIRST_MISS_AT - 30 * TWO_S + 15 * TWO_S, monotonic=103.0
    )

    assert silent(registry, FIRST_MISS_AT + MINUTE) is True


def test_reading3_a_wall_clock_gap_alone_does_not_count() -> None:
    registry = idle_fleet(SOLO)
    registry.apply_stats_reply({}, {}, now=FIRST_MISS_AT, monotonic=100.0)
    registry.apply_stats_reply({}, {}, now=FIRST_MISS_AT + 10 * MINUTE, monotonic=101.0)

    assert silent(registry, FIRST_MISS_AT + 10 * MINUTE) is False


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
    assert {len(counted) for counted in registry._misses.values()} == {2}

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
    """A probe that records its confirms instead of running them."""

    def __init__(self) -> None:
        super().__init__()
        self.confirms = 0

    def confirm(self) -> None:
        self.confirms += 1


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

    assert probe.confirms == 1


# --- Reading 1: confirms are bounded per worker and never chain ---------------------------


def test_row28q_registry_events_and_later_misses_do_not_re_arm_a_spent_confirm() -> (
    None
):
    registry = idle_fleet(SOLO)
    calls = count_first_misses(registry)

    for step in range(20):
        at = FIRST_MISS_AT + step * TWO_S
        miss(registry, at)  # a worker that keeps sending events but never answers
        registry.mark_seen(
            SOLO, at + timedelta(seconds=1), now=at + timedelta(seconds=1)
        )

    assert calls == [1]


def test_reading1_an_answer_re_arms_the_confirm() -> None:
    registry = idle_fleet(SOLO)
    calls = count_first_misses(registry)

    miss(registry, FIRST_MISS_AT)
    registry.apply_ping_reply([SOLO], now=FIRST_MISS_AT + TWO_S)
    miss(registry, FIRST_MISS_AT + 2 * TWO_S)

    assert calls == [2]


class ScriptedFleet:
    """What the probe drives: each attempt feeds events, then a broadcast, to a real registry.

    ``losses`` says which of the nodes miss a given attempt; the answerers reply.
    """

    def __init__(
        self, registry: WorkerRegistry, clock: FakeClock, nodes: list[str]
    ) -> None:
        self.registry, self.clock, self.nodes = registry, clock, nodes
        self.attempts = 0
        self.done = threading.Semaphore(0)
        self.lost: Callable[[str, int], bool] = lambda _node, _attempt: False
        self.events_between: list[str] = []
        self.calls = 0
        self.busy_on_calls: set[int] = set()  # calls that find the refresh slot taken

    def ready_in(self) -> float:
        return 0.0

    def refresh_blocking(self) -> frozenset[str] | None:
        self.calls += 1
        if self.calls in self.busy_on_calls:
            return None
        self.attempts += 1
        wall = T0 + timedelta(seconds=self.clock.now())
        for node in self.events_between:  # events arrive between broadcasts
            self.registry.mark_seen(node, wall, now=wall)
        done = wall + timedelta(milliseconds=100)
        answered = {
            n: (1, [])
            for n in self.nodes
            if n not in self.events_between and not self.lost(n, self.attempts)
        }
        self.registry.apply_stats_reply(
            {}, answered, now=done, monotonic=self.clock.now() + 0.1
        )
        self.done.release()
        return frozenset(answered)


def drain(fleet: ScriptedFleet) -> int:
    """Wait for the probe's attempts to stop; return the attempts made so far."""
    assert fleet.done.acquire(timeout=BOUND_S)
    while fleet.done.acquire(timeout=0.4):
        assert fleet.attempts < 30, "the probe keeps confirming"
    return fleet.attempts


def make_probe(fleet: ScriptedFleet) -> InspectProbe:
    """A probe on the fleet's fake clock whose first misses owe it confirms."""
    probe = InspectProbe(clock=fleet.clock.now, wait=fleet.clock.wait)
    probe.bind(fleet)
    fleet.registry.add_first_miss_listener(probe.confirm)
    return probe


def run_probe_until_idle(fleet: ScriptedFleet) -> int:
    """Start a probe on a fake clock over ``fleet``; return the attempts made once it idles."""
    probe = make_probe(fleet)
    try:
        probe.start()
        return drain(fleet)
    finally:
        probe.stop()


def test_row28q_a_worker_with_events_and_no_answers_costs_one_confirm_with_no_requests() -> (
    None
):
    registry = WorkerRegistry()
    clock = FakeClock()
    fleet = ScriptedFleet(registry, clock, ["celery@a", "celery@x"])
    fleet.events_between = ["celery@x"]  # x sends an event every attempt, never answers
    registry.apply_stats_reply(
        {}, {"celery@a": (1, [])}, now=T0 - MINUTE, monotonic=-60.0
    )
    registry.mark_seen("celery@x", T0 - MINUTE, now=T0 - MINUTE)

    attempts = run_probe_until_idle(fleet)

    assert attempts == 2  # the start attempt and one confirm


@pytest.mark.parametrize("seed", range(5))
def test_row28r_fifty_workers_with_ten_percent_loss_cost_at_most_one_confirm(
    seed: int,
) -> None:
    rng = random.Random(seed)
    nodes = [f"celery@w{i}" for i in range(50)]
    registry = WorkerRegistry()
    clock = FakeClock()
    fleet = ScriptedFleet(registry, clock, nodes)
    fleet.lost = lambda _node, _attempt: rng.random() < 0.10
    for node in nodes:
        registry.apply_stats_reply(
            {}, {node: (1, [])}, now=T0 - MINUTE, monotonic=-60.0
        )

    attempts = run_probe_until_idle(fleet)

    assert attempts == 2  # the start attempt and exactly one confirm


def test_a_confirm_attempt_coalesced_with_a_join_is_still_a_confirm() -> None:
    registry = WorkerRegistry()
    clock = FakeClock()
    nodes = ["celery@a", "celery@b"]
    fleet = ScriptedFleet(registry, clock, nodes)
    # b misses the first attempt (a first miss owes a confirm); c misses only the second.
    fleet.lost = lambda node, attempt: (
        (node == "celery@b" and attempt == 1) or (node == "celery@c" and attempt == 2)
    )
    fleet.nodes = [*nodes, "celery@c"]
    for node in fleet.nodes:
        registry.apply_stats_reply(
            {}, {node: (1, [])}, now=T0 - MINUTE, monotonic=-60.0
        )

    attempts = run_probe_until_idle(fleet)

    assert attempts == 2  # c's first miss, seen by the confirm attempt, owes nothing


# --- Reading 7: a new process re-arms the confirm (row 28s) ---------------------------------


@pytest.mark.parametrize("same_pid", [True, False], ids=["same-pid", "new-pid"])
def test_row28s_a_restarted_nodename_that_dies_again_is_confirmed_again(
    same_pid: bool,
) -> None:
    registry = WorkerRegistry()
    clock = FakeClock()
    fleet = ScriptedFleet(registry, clock, ["celery@a", "celery@x"])
    fleet.lost = lambda node, _attempt: node == "celery@x"  # x is dead from the start
    for node in fleet.nodes:
        registry.apply_stats_reply(
            {}, {node: (1, [])}, now=T0 - MINUTE, monotonic=-60.0
        )
    probe = make_probe(fleet)
    try:
        probe.start()
        assert drain(fleet) == 2  # the first miss and its confirm
        # x restarts under the same name (a worker_ready, no inspect answer), then dies again.
        clock._now += 180
        ready_at = T0 + timedelta(seconds=clock.now())
        registry.register_worker(
            "celery@x", 1 if same_pid else 2, [], event_timestamp=ready_at
        )
        clock._now += 180
        probe.trigger()  # one request, nothing else
        attempts = drain(fleet)
    finally:
        probe.stop()

    assert attempts == 4  # the request's attempt owes, and gets, one more confirm


def test_row28s_registry_a_worker_ready_re_arms_and_an_ordinary_event_does_not() -> (
    None
):
    registry = idle_fleet(SOLO, OTHER)
    calls = count_first_misses(registry)
    miss(registry, FIRST_MISS_AT)
    assert calls == [1]
    # An ordinary event (28q) moves last_seen but owes nothing on the next first miss.
    event_at = FIRST_MISS_AT + 10 * TWO_S
    registry.mark_seen(SOLO, event_at, now=event_at)
    registry.mark_seen(OTHER, event_at, now=event_at)
    miss(registry, event_at + TWO_S)
    assert calls == [1]
    # A worker_ready for each nodename (a new process): the next first miss owes again.
    ready_at = event_at + 20 * TWO_S
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=ready_at)
    registry.register_worker(OTHER, 9, ["app.known"], event_timestamp=ready_at)
    miss(registry, ready_at + TWO_S)

    assert calls == [2]


def test_reading1_a_confirm_that_finds_the_slot_busy_is_still_owed_as_a_confirm() -> (
    None
):
    registry = WorkerRegistry()
    clock = FakeClock()
    nodes = ["celery@a", "celery@x", "celery@y"]
    fleet = ScriptedFleet(registry, clock, nodes)
    # x misses the first attempt; the confirm's first try finds the slot busy; y misses
    # only the retried attempt, which is still the confirm and so owes nothing.
    fleet.lost = lambda node, attempt: (
        (node == "celery@x" and attempt == 1) or (node == "celery@y" and attempt == 2)
    )
    fleet.busy_on_calls = {2}
    for node in nodes:
        registry.apply_stats_reply(
            {}, {node: (1, [])}, now=T0 - MINUTE, monotonic=-60.0
        )

    assert run_probe_until_idle(fleet) == 2
