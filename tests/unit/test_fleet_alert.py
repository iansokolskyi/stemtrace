"""The ``no_workers_online`` fleet item (D4b): one test group per spec row.

Rows 28, 28a-28e and 28g-28i of ``.context/features/anomaly-detection.md``
("D4b fleet item"). Registry state goes through the real ``WorkerRegistry``;
28g and 28h run the real ``_refresh_worker_registry_from_inspect`` against a fake
Celery ``Inspect`` (Celery 5.6.3: ``stats()`` returns ``{nodename: ...}`` or None
for no reply, and raises when the broker is unreachable).
"""

from collections.abc import Callable
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
from stemtrace.server import store as store_module
from stemtrace.server.anomaly_facts import classifier_facts
from stemtrace.server.api.routes import _refresh_worker_registry_from_inspect
from stemtrace.server.store import GraphStore, WorkerRegistry
from tests.unit.anomaly_pipeline import SMALL, T0, task_event

SOLO, OTHER = "celery@solo", "celery@other"
PROBE = "celery@probe"
MINUTE = timedelta(minutes=1)
SETTINGS = replace(
    SMALL,
    pending_grace=timedelta(seconds=60),
    authority_window=timedelta(seconds=120),
)
FLEET = AnomalyKind.NO_WORKERS_ONLINE
KIND_UNREGISTERED = AnomalyKind.UNREGISTERED


def report_at(
    registry: WorkerRegistry,
    now: datetime,
    *,
    broker: bool = True,
    names: tuple[str, ...] = ("app.known",),
) -> AnomalyReport:
    """Classify PENDING tasks (stamped an hour before ``T0``) of ``names`` at ``now``."""
    store = GraphStore()
    for i, name in enumerate(names):
        store.add_event(task_event(f"t{i}", TaskState.PENDING, -3600, name=name))
    facts = classifier_facts(store.run_records(), {}, now)
    return classify_anomalies(
        **facts._asdict(),
        now=now,
        settings=SETTINGS,
        registry=registry.registry_facts(broker_configured=broker, now=now),
    )


def fleet_items(report: AnomalyReport) -> list[tuple[str | None, str | None, int]]:
    """The fleet items as (task_id, name, task_count)."""
    return [(a.task_id, a.name, a.task_count) for a in report.items if a.kind == FLEET]


class Clock:
    """The registry's clock for the tests that go through the refresh function."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, start: datetime) -> None:
        self.now = start
        monkeypatch.setattr(store_module, "_utcnow", lambda: self.now)

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeInspect:
    """A Celery ``Inspect``: ``stats()``/``ping()`` reply, or raise; hooks run inside a call."""

    def __init__(
        self,
        *,
        stats: dict[str, object] | Exception | None,
        registered: dict[str, object] | None = None,
        ping: dict[str, object] | Exception | None = None,
        during_registered: Callable[[], None] | None = None,
        during_ping: Callable[[], None] | None = None,
    ) -> None:
        self._stats, self._registered, self._ping = stats, registered, ping
        self._during_registered, self._during_ping = during_registered, during_ping

    def stats(self) -> dict[str, object] | None:
        if isinstance(self._stats, Exception):
            raise self._stats
        return self._stats

    def registered(self) -> dict[str, object] | None:
        if self._during_registered is not None:
            self._during_registered()
        return self._registered

    def ping(self) -> dict[str, object] | None:
        if self._during_ping is not None:
            self._during_ping()
        if isinstance(self._ping, Exception):
            raise self._ping
        return self._ping


def refresh(registry: WorkerRegistry, inspect: FakeInspect) -> None:
    """Run the real refresh against ``inspect``."""
    _refresh_worker_registry_from_inspect(registry, inspect)  # type: ignore[arg-type]  # a fake of Celery's Inspect


def prove_channel(registry: WorkerRegistry, at: datetime = T0) -> None:
    """Let some worker answer an inspect (then stop), so later silence is evidence."""
    registry.apply_stats_reply({}, {PROBE: (9, [])}, now=at)
    registry.mark_shutdown(PROBE, 9, now=at)


def answer_stats(*nodes: str, pid: int = 1) -> dict[str, object]:
    """A ``stats()`` reply from ``nodes``."""
    return {n: {"pid": pid} for n in nodes}


# --- Row 28: the whole fleet stopped, for the whole outage ------------------------


@pytest.mark.parametrize("broker", [True, False], ids=["broker", "no-broker"])
@pytest.mark.parametrize("minutes", [0, 2, 6, 12, 29, 45, 120])
@pytest.mark.parametrize("via_stats", [True, False], ids=["stats", "ready"])
def test_row28_a_fleet_that_reported_its_stop_gives_one_item_before_and_after_the_cleanup(
    via_stats: bool, minutes: int, broker: bool
) -> None:
    registry = WorkerRegistry()
    for pid, node in enumerate((SOLO, OTHER), start=1):
        if via_stats:
            registry.apply_stats_reply({}, {node: (pid, ["app.known"])}, now=T0)
        else:
            registry.register_worker(node, pid, ["app.known"], event_timestamp=T0)
        registry.mark_shutdown(node, pid)
    now = T0 + minutes * MINUTE
    registry.remove_stale_workers(now=now)  # what the consumer runs periodically

    report = report_at(registry, now, broker=broker, names=("app.known", "app.new"))

    assert fleet_items(report) == [(None, None, 2)]
    assert report.anomalies == {}  # no per-name item and no node anomaly
    assert [a.kind for a in report.items] == [FLEET]
    assert report.counts[FLEET] == 1
    assert report.counts[KIND_UNREGISTERED] == 0


def test_row28_the_item_counts_eligible_nodes_and_starts_at_the_oldest_pending() -> (
    None
):
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.a"], event_timestamp=T0)
    registry.mark_shutdown(SOLO, 1)
    store = GraphStore()
    store.add_event(task_event("old", TaskState.PENDING, -3600, name="app.a"))
    store.add_event(task_event("newer", TaskState.PENDING, -1800, name="app.b"))
    store.add_event(task_event("celery", TaskState.PENDING, -3600, name="celery.x"))
    store.add_event(task_event("fresh", TaskState.PENDING, 100, name="app.c"))
    now = T0 + 120 * timedelta(seconds=1)
    facts = classifier_facts(store.run_records(), {}, now)

    report = classify_anomalies(
        **facts._asdict(),
        now=now,
        settings=SETTINGS,
        registry=registry.registry_facts(broker_configured=True, now=now),
    )

    [item] = report.items
    assert (item.kind, item.task_id, item.name) == (FLEET, None, None)
    assert item.task_count == 2  # "celery.x" and the task inside the grace don't count
    assert item.since == T0 - timedelta(hours=1)


def test_row28_with_no_eligible_node_nothing_is_emitted() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.a"], event_timestamp=T0)
    registry.mark_shutdown(SOLO, 1)

    report = report_at(registry, T0 + MINUTE, names=())

    assert list(report.items) == []


def test_row28_a_pid_superseded_by_a_newer_one_that_then_stopped_is_offline() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.register_worker(SOLO, 2, ["app.known"], event_timestamp=T0 + MINUTE / 2)
    registry.mark_shutdown(SOLO, 2)

    report = report_at(registry, T0 + MINUTE)

    assert fleet_items(report) == [(None, None, 1)]


def test_row28_a_stopped_worker_next_to_a_live_one_is_not_the_whole_fleet() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.register_worker(OTHER, 2, ["app.known"], event_timestamp=T0)
    registry.mark_shutdown(SOLO, 1)

    report = report_at(registry, T0 + MINUTE)

    assert fleet_items(report) == []


# --- Row 28a: the server started while the fleet was already down -------------------


@pytest.mark.parametrize("broker", [True, False], ids=["broker", "no-broker"])
def test_row28a_a_server_that_has_seen_no_worker_emits_nothing(broker: bool) -> None:
    registry = WorkerRegistry()
    registry.mark_shutdown(SOLO, 1)  # a stop of a worker the server never saw
    registry.apply_stats_reply({}, {}, now=T0)  # inspect completed, nobody answered

    report = report_at(registry, T0 + 20 * MINUTE, broker=broker)

    assert list(report.items) == []


def test_row28a_the_first_sign_of_a_worker_arms_the_item() -> None:
    registry = WorkerRegistry()
    registry.mark_seen(SOLO, T0, now=T0)
    registry.mark_shutdown(SOLO, 1)

    assert fleet_items(report_at(registry, T0 + MINUTE)) == [(None, None, 1)]


# --- Row 28b: a live worker misses one broadcast ------------------------------------


@pytest.mark.parametrize("after_s", [1, 90, 179])
def test_row28b_a_worker_that_misses_one_broadcast_inside_the_window_stays_online(
    after_s: int,
) -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, answer_pairs(SOLO), now=T0)
    registry.apply_stats_reply({}, {}, now=T0 + timedelta(seconds=after_s))  # missed

    report = report_at(registry, T0 + timedelta(seconds=after_s))

    assert fleet_items(report) == []


def test_row28b_with_one_of_two_workers_answering_the_fleet_is_online() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply(
        {}, {**answer_pairs(SOLO), **answer_pairs(OTHER)}, now=T0
    )
    later = T0 + 10 * MINUTE
    registry.apply_stats_reply({}, answer_pairs(OTHER), now=later)  # solo is busy

    assert fleet_items(report_at(registry, later)) == []


def answer_pairs(node: str) -> dict[str, tuple[int | None, list[str] | None]]:
    """A ``{node: (pid, tasks)}`` reply as ``apply_stats_reply`` takes it."""
    return {node: (1 if node == SOLO else 2, ["app.known"])}


# --- Row 28c: the inspect completed and a worker stays silent -----------------------


@pytest.mark.parametrize("stats", [None, {}], ids=["no-reply", "empty-reply"])
@pytest.mark.parametrize(
    "ping", [None, OSError("down")], ids=["ping-empty", "ping-raises"]
)
@pytest.mark.parametrize("silent_min", [3.5, 10, 29])
def test_row28c_a_completed_inspect_nobody_answers_makes_the_silent_fleet_offline(
    silent_min: float, ping: object, stats: dict[str, object] | None
) -> None:
    registry = WorkerRegistry()
    prove_channel(registry)
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + silent_min * MINUTE

    refresh_at(registry, now, FakeInspect(stats=stats, ping=ping))  # type: ignore[arg-type]  # parametrized values are typed loosely
    report = report_at(registry, now, names=("app.known", "app.new"))

    assert fleet_items(report) == [(None, None, 2)]
    assert report.anomalies == {}


def refresh_at(registry: WorkerRegistry, now: datetime, inspect: FakeInspect) -> None:
    """Run the real refresh with the registry's clock at ``now``."""
    original = store_module._utcnow
    store_module._utcnow = lambda: now
    try:
        refresh(registry, inspect)
    finally:
        store_module._utcnow = original


def test_row28c_the_silent_worker_still_keeps_its_names_so_no_per_name_item() -> None:
    registry = WorkerRegistry()
    prove_channel(registry)
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + 6 * MINUTE
    refresh_at(registry, now, FakeInspect(stats=None))

    report = report_at(registry, now, names=("app.known",))

    assert [a.kind for a in report.items] == [FLEET]


def test_row28c_a_worker_that_answers_the_completed_inspect_is_online() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + 6 * MINUTE
    refresh_at(
        registry,
        now,
        FakeInspect(stats=answer_stats(SOLO), registered={SOLO: ["app.known"]}),
    )

    assert fleet_items(report_at(registry, now)) == []


def test_row28c_the_ping_fallback_answer_keeps_the_worker_online() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + 6 * MINUTE
    refresh_at(registry, now, FakeInspect(stats=None, ping={SOLO: {"ok": "pong"}}))

    assert fleet_items(report_at(registry, now)) == []


def test_row28c_per_worker_one_silent_and_one_answering_is_online() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.register_worker(OTHER, 2, ["app.known"], event_timestamp=T0)
    now = T0 + 6 * MINUTE
    refresh_at(
        registry,
        now,
        FakeInspect(
            stats=answer_stats(OTHER, pid=2), registered={OTHER: ["app.known"]}
        ),
    )

    assert fleet_items(report_at(registry, now)) == []


def test_row28c_a_worker_silent_for_less_than_the_stale_window_is_online() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + 2 * MINUTE
    refresh_at(registry, now, FakeInspect(stats=None))

    assert fleet_items(report_at(registry, now)) == []


def test_row28c_a_task_event_after_the_broadcast_keeps_the_worker_online() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + 6 * MINUTE
    refresh_at(registry, now, FakeInspect(stats=None))
    registry.mark_seen(SOLO, now + timedelta(seconds=1), now=now + timedelta(seconds=1))

    assert fleet_items(report_at(registry, now + MINUTE)) == []


def test_row28c_a_reply_a_worker_event_overtook_still_counts_as_the_worker_answering() -> (
    None
):
    registry = WorkerRegistry()
    seen = registry.event_generations()
    # The worker's clock is 10 minutes behind: its worker_ready is stamped long ago.
    registry.register_worker(SOLO, 2, ["app.known"], event_timestamp=T0 - 10 * MINUTE)

    # The reply was produced before that event, so it is skipped as stale, yet SOLO answered.
    registry.apply_stats_reply(seen, answer_pairs(SOLO), now=T0)

    assert fleet_items(report_at(registry, T0)) == []


def test_row28c_a_worker_clock_far_behind_looks_silent_after_a_completed_inspect() -> (
    None
):
    registry = WorkerRegistry()
    prove_channel(registry, T0 - MINUTE)
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 - 10 * MINUTE)

    registry.apply_stats_reply({}, {}, now=T0)  # a worker that is up but didn't answer

    assert fleet_items(report_at(registry, T0)) == [(None, None, 1)]


# --- Row 28j: workers we can't hear are not evidence (channel proven) ----------------


@pytest.mark.parametrize("stats", [None, {}], ids=["stats-none", "stats-empty"])
@pytest.mark.parametrize(
    "ping",
    [None, {}, OSError("down")],
    ids=["ping-none", "ping-empty", "ping-raises"],
)
def test_row28j_an_idle_fleet_that_never_answers_an_inspect_gives_no_item(
    stats: dict[str, object] | None, ping: object
) -> None:
    """Remote control off: every ``stats()`` and ``ping()`` returns None (round-1 PoC)."""
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + 9 * MINUTE
    refresh_at(registry, now, FakeInspect(stats=stats, ping=ping))  # type: ignore[arg-type]  # parametrized values are typed loosely

    assert fleet_items(report_at(registry, now + MINUTE)) == []


@pytest.mark.parametrize("answer", ["stats", "ping"])
def test_row28j_once_a_worker_answered_an_inspect_silence_is_evidence(
    answer: str,
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.register_worker(OTHER, 2, ["app.known"], event_timestamp=T0)
    first = T0 + MINUTE
    if answer == "stats":
        inspect = FakeInspect(stats=answer_stats(OTHER, pid=2))
    else:
        inspect = FakeInspect(stats=None, ping={OTHER: {"ok": "pong"}})
    refresh_at(registry, first, inspect)
    registry.mark_shutdown(OTHER, 2)
    later = T0 + 9 * MINUTE
    refresh_at(registry, later, FakeInspect(stats=None))

    assert fleet_items(report_at(registry, later)) == [(None, None, 1)]


def test_row28j_a_ping_answer_proves_the_channel_even_when_stats_raised() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.register_worker(OTHER, 2, ["app.known"], event_timestamp=T0)
    registry.mark_shutdown(OTHER, 2)
    refresh_at(
        registry,
        T0 + MINUTE,
        FakeInspect(stats=OSError("x"), ping={OTHER: {"ok": "pong"}}),
    )
    later = T0 + 9 * MINUTE
    refresh_at(registry, later, FakeInspect(stats=None))

    assert fleet_items(report_at(registry, later)) == [(None, None, 1)]


def test_row28j_an_empty_ping_reply_does_not_prove_the_channel() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.apply_ping_reply([], now=T0 + MINUTE)  # stats() raised, nobody pinged back
    registry.apply_stats_reply({}, {}, now=T0 + 9 * MINUTE)  # a completed, empty call

    assert fleet_items(report_at(registry, T0 + 9 * MINUTE)) == []


def test_row28j_a_stopped_fleet_is_still_offline_with_the_channel_unproven() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.mark_shutdown(SOLO, 1)
    refresh_at(registry, T0 + MINUTE, FakeInspect(stats=None))

    assert fleet_items(report_at(registry, T0 + 2 * MINUTE)) == [(None, None, 1)]


# --- Row 28k: an event stamped after a stop revives the nodename, listed or not ------


@pytest.mark.parametrize("broker", [True, False], ids=["broker", "no-broker"])
@pytest.mark.parametrize("listed", [True, False], ids=["listed", "unlisted"])
@pytest.mark.parametrize(
    ("delta_s", "revives"),
    [(-10, False), (0, False), (10, True)],
    ids=["before", "at", "after"],
)
def test_row28k_an_event_after_a_reported_stop_revives_the_worker_only_when_stamped_after(
    delta_s: int, revives: bool, listed: bool, broker: bool
) -> None:
    registry = WorkerRegistry()
    stop = T0 - 4 * MINUTE
    if listed:
        registry.register_worker(
            SOLO, 1, ["app.known"], event_timestamp=T0 - 5 * MINUTE
        )
    else:
        registry.mark_seen(SOLO, T0 - 5 * MINUTE, now=T0 - 5 * MINUTE)
    registry.mark_shutdown(SOLO, 1, stop)  # the restart's worker_ready is missed

    registry.mark_seen(SOLO, stop + timedelta(seconds=delta_s), now=T0)

    report = report_at(registry, T0, broker=broker)
    assert fleet_items(report) == ([] if revives else [(None, None, 1)])


def test_row28k_the_revived_worker_stays_online_until_it_stops_again() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 - 5 * MINUTE)
    registry.mark_shutdown(SOLO, 1, T0 - 4 * MINUTE)
    registry.mark_seen(SOLO, T0 - timedelta(seconds=10), now=T0)
    assert fleet_items(report_at(registry, T0)) == []

    registry.apply_ping_reply(
        [SOLO], now=T0 + MINUTE
    )  # a ping refreshes, never revives
    registry.mark_shutdown(SOLO, 2, T0 + MINUTE)

    assert fleet_items(report_at(registry, T0 + MINUTE)) == [(None, None, 1)]


def test_row28k_a_ping_answer_keeps_the_revived_worker_online_after_it_misses_the_next_one() -> (
    None
):
    registry = WorkerRegistry()
    prove_channel(registry, T0 - 11 * MINUTE)
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 - 12 * MINUTE)
    registry.mark_shutdown(SOLO, 1, T0 - 10 * MINUTE)
    registry.mark_seen(SOLO, T0 - 9 * MINUTE, now=T0 - 9 * MINUTE)  # revived
    refresh_at(registry, T0, FakeInspect(stats=None, ping={SOLO: {"ok": "pong"}}))

    refresh_at(registry, T0 + MINUTE, FakeInspect(stats=None))  # it misses this one

    assert fleet_items(report_at(registry, T0 + MINUTE)) == []


@pytest.mark.parametrize("forgotten_by", ["cleanup", "eviction"])
@pytest.mark.parametrize("minutes", [1, 29, 31, 60])
@pytest.mark.parametrize("listed", [True, False], ids=["listed", "unlisted"])
def test_row28k_a_revived_worker_that_keeps_sending_events_stays_online_after_the_stop_is_forgotten(
    listed: bool, minutes: int, forgotten_by: str
) -> None:
    """Round-2 PoC: the stop is forgotten (cleanup or eviction) while events keep coming."""
    registry = WorkerRegistry(max_unlisted=2 if forgotten_by == "eviction" else 1024)
    prove_channel(registry, T0 - MINUTE)
    stop = T0 + MINUTE
    if listed:
        registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    else:
        registry.mark_seen(SOLO, T0, now=T0)
    registry.mark_shutdown(SOLO, 1, stop, now=stop)
    registry.mark_seen(SOLO, stop + MINUTE, now=stop + MINUTE)  # revived
    for minute in range(2, minutes + 1):
        at = stop + minute * MINUTE
        registry.mark_seen(SOLO, at, now=at)
        if forgotten_by == "cleanup":
            registry.remove_stale_workers(now=at)
        elif minute == 2:
            registry.mark_shutdown(
                OTHER, 1, at, now=at
            )  # with PROBE's, evicts SOLO's stop (bound 2)
            registry.mark_shutdown("celery@c", 1, at, now=at)
    end = stop + minutes * MINUTE + MINUTE / 2
    refresh_at(registry, end, FakeInspect(stats=None))  # a busy worker misses it

    assert fleet_items(report_at(registry, end)) == []


def test_row28k_a_stop_is_forgotten_by_the_server_clock_not_the_workers() -> None:
    registry = WorkerRegistry()
    stop = T0 - 2 * timedelta(hours=1)  # a worker clock two hours behind
    registry.mark_shutdown(SOLO, 1, stop, now=T0)

    registry.remove_stale_workers(now=T0 + MINUTE)  # the server saw the stop 1 min ago
    registry.mark_seen(SOLO, stop - MINUTE, now=T0 + MINUTE)  # a late event

    facts = registry.registry_facts(broker_configured=True, now=T0 + MINUTE)
    assert facts.any_online is False


# --- Row 28d / 28e: silence is not evidence without a completed inspect -------------


@pytest.mark.parametrize("silent_min", [4, 29, 45, 600])
@pytest.mark.parametrize("how", ["no-broker", "inspect-raises"])
def test_row28d_an_idle_healthy_fleet_without_inspect_flags_the_new_name_per_name(
    how: str, silent_min: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + silent_min * MINUTE
    if how == "inspect-raises":
        refresh_at(registry, now, FakeInspect(stats=OSError("unreachable")))
    registry.remove_stale_workers(now=now)  # the consumer's periodic check

    report = report_at(registry, now, broker=how != "no-broker", names=("app.new",))

    assert fleet_items(report) == []
    gated_open = how == "no-broker" and silent_min < 30
    assert [a.name for a in report.items] == (["app.new"] if gated_open else [])


@pytest.mark.parametrize("silent_min", [4, 29, 45, 600])
@pytest.mark.parametrize("how", ["no-broker", "inspect-raises"])
def test_row28e_a_sigkilled_fleet_without_inspect_gives_no_item_before_or_after_cleanup(
    how: str, silent_min: int
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.register_worker(OTHER, 2, ["app.known"], event_timestamp=T0)
    now = T0 + silent_min * MINUTE
    if how == "inspect-raises":
        refresh_at(registry, now, FakeInspect(stats=OSError("unreachable")))
    registry.remove_stale_workers(now=now)

    report = report_at(registry, now, broker=how != "no-broker")

    assert fleet_items(report) == []


def test_row28e_the_cleanup_keeps_what_it_cannot_prove_and_later_evidence_closes_it() -> (
    None
):
    registry = WorkerRegistry()
    prove_channel(registry)
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    now = T0 + 45 * MINUTE
    registry.remove_stale_workers(now=now)
    assert fleet_items(report_at(registry, now, broker=False)) == []

    # The broker comes back and nobody answers the broadcast: now silence is evidence.
    refresh_at(registry, now + MINUTE, FakeInspect(stats=None))

    assert fleet_items(report_at(registry, now + MINUTE)) == [(None, None, 1)]


def test_row28e_a_clean_shutdown_of_every_worker_still_gives_the_item() -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    registry.mark_shutdown(SOLO, 1)

    assert fleet_items(report_at(registry, T0 + 20 * MINUTE, broker=False)) == [
        (None, None, 1)
    ]


# --- Row 28g: an evaluation while a refresh is in flight (round-3 PoC) ---------------


@pytest.mark.parametrize("stage", ["registered", "ping"])
def test_row28g_an_evaluation_during_a_refresh_does_not_see_the_workers_offline(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answered worker's last sign is 5 minutes old when the refresh starts.

    Stamping the broadcast as completed before its answers were applied made a
    concurrent evaluation see "completed after last_seen, not yet answered".
    """
    clock = Clock(monkeypatch, T0)
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {SOLO: (1, ["app.known"])}, now=T0)
    clock.advance(5 * MINUTE)
    seen_mid_refresh: list[list[tuple[str | None, str | None, int]]] = []

    def evaluate() -> None:
        seen_mid_refresh.append(fleet_items(report_at(registry, clock.now)))

    if stage == "registered":
        inspect = FakeInspect(
            stats=answer_stats(SOLO),
            registered={SOLO: ["app.known"]},
            during_registered=evaluate,
        )
    else:
        inspect = FakeInspect(
            stats=None, ping={SOLO: {"ok": "pong"}}, during_ping=evaluate
        )
    refresh(registry, inspect)

    assert seen_mid_refresh == [[]]
    assert fleet_items(report_at(registry, clock.now)) == []


def test_row28g_a_concurrent_evaluation_sees_a_fleet_that_goes_silent_only_once_the_refresh_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock(monkeypatch, T0)
    registry = WorkerRegistry()
    prove_channel(registry)
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0)
    clock.advance(5 * MINUTE)
    mid: list[list[tuple[str | None, str | None, int]]] = []

    refresh(
        registry,
        FakeInspect(
            stats=None,
            ping=None,
            during_ping=lambda: mid.append(fleet_items(report_at(registry, clock.now))),
        ),
    )

    assert mid == [[]]  # still in flight: not completed yet
    assert fleet_items(report_at(registry, clock.now)) == [(None, None, 1)]


# --- Row 28h: no refresh for 3-10 minutes after an answered one ----------------------


@pytest.mark.parametrize("minutes", [3.5, 5, 9.9, 30, 600])
def test_row28h_no_refresh_after_an_answered_one_keeps_the_fleet_online(
    minutes: float,
) -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, answer_pairs(SOLO), now=T0)

    assert fleet_items(report_at(registry, T0 + minutes * MINUTE)) == []


@pytest.mark.parametrize("via", ["stats", "ping-fallback"])
@pytest.mark.parametrize("minutes", [3.5, 5, 9.9])
def test_row28h_through_the_real_refresh_nothing_for_minutes_keeps_the_fleet_online(
    via: str, minutes: float
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(SOLO, 1, ["app.known"], event_timestamp=T0 - 20 * MINUTE)
    inspect = (
        FakeInspect(stats=answer_stats(SOLO), registered={SOLO: ["app.known"]})
        if via == "stats"
        else FakeInspect(stats=None, ping={SOLO: {"ok": "pong"}})
    )
    refresh_at(registry, T0, inspect)

    assert fleet_items(report_at(registry, T0 + minutes * MINUTE)) == []


def test_row28h_the_next_unanswered_broadcast_then_makes_it_offline() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, answer_pairs(SOLO), now=T0)
    later = T0 + 5 * MINUTE
    registry.apply_stats_reply({}, {}, now=later)

    assert fleet_items(report_at(registry, later)) == [(None, None, 1)]


# --- Row 28i and the unknown nodenames ------------------------------------------------


def test_row28i_a_task_event_only_worker_that_stops_cleanly_gives_the_item_at_once() -> (
    None
):
    registry = WorkerRegistry()
    registry.mark_seen(SOLO, T0, now=T0)
    assert fleet_items(report_at(registry, T0 + 10 * MINUTE, broker=False)) == []

    registry.mark_shutdown(SOLO, 1, T0 + 10 * MINUTE)

    assert fleet_items(report_at(registry, T0 + 10 * MINUTE)) == [(None, None, 1)]


def test_an_unknown_nodename_is_live_with_no_names_and_not_on_the_workers_page() -> (
    None
):
    registry = WorkerRegistry()
    registry.mark_seen(SOLO, T0, now=T0)

    facts = registry.registry_facts(broker_configured=False, now=T0)

    assert (facts.any_seen, facts.any_online) == (True, True)
    assert facts.live_names == frozenset()
    assert facts.any_live is False  # it doesn't open the no-broker gate clause
    assert registry.get_all_workers() == []


@pytest.mark.parametrize("late_s", [-30, -1, 0])
def test_row28i_a_late_event_stamped_before_the_stop_does_not_revive_it(
    late_s: int,
) -> None:
    registry = WorkerRegistry()
    registry.mark_seen(SOLO, T0, now=T0)
    stop = T0 + MINUTE
    registry.mark_shutdown(SOLO, 1, stop)

    registry.mark_seen(SOLO, stop + timedelta(seconds=late_s), now=stop + 5 * MINUTE)

    assert fleet_items(report_at(registry, stop + MINUTE)) == [(None, None, 1)]


def test_row28i_an_event_stamped_after_the_stop_makes_it_live_again() -> None:
    registry = WorkerRegistry()
    registry.mark_seen(SOLO, T0, now=T0)
    stop = T0 + MINUTE
    registry.mark_shutdown(SOLO, 1, stop)
    registry.mark_seen(SOLO, stop + timedelta(seconds=1), now=stop + MINUTE)

    assert fleet_items(report_at(registry, stop + MINUTE)) == []


def test_a_ping_reply_does_not_bring_a_stopped_unknown_nodename_back() -> None:
    registry = WorkerRegistry()
    registry.mark_seen(SOLO, T0, now=T0)
    registry.mark_shutdown(SOLO, 1, T0 + MINUTE)

    registry.apply_ping_reply([SOLO], now=T0 + 2 * MINUTE)

    assert fleet_items(report_at(registry, T0 + 2 * MINUTE)) == [(None, None, 1)]


def test_a_ping_reply_from_an_unknown_nodename_makes_it_known_and_online() -> None:
    registry = WorkerRegistry()
    registry.apply_ping_reply([SOLO], stats_completed=True, now=T0)

    facts = registry.registry_facts(broker_configured=True, now=T0 + 10 * MINUTE)

    assert (facts.any_seen, facts.any_online) == (True, True)  # it answered the latest


def test_worker_ready_and_a_stats_reply_make_an_unknown_nodename_a_listed_worker() -> (
    None
):
    registry = WorkerRegistry()
    registry.mark_seen(SOLO, T0, now=T0)
    registry.mark_seen(OTHER, T0, now=T0)
    registry.register_worker(SOLO, 1, ["app.a"], event_timestamp=T0)
    registry.apply_stats_reply({}, {OTHER: (2, ["app.b"])}, now=T0)

    assert sorted(w.hostname for w in registry.get_all_workers()) == [OTHER, SOLO]
    registry.mark_shutdown(SOLO, 1)
    registry.mark_shutdown(OTHER, 2)
    assert fleet_items(report_at(registry, T0 + MINUTE)) == [(None, None, 1)]


def test_the_cleanup_keeps_an_unknown_nodename_seen_recently() -> None:
    registry = WorkerRegistry()
    prove_channel(registry, T0 - MINUTE)
    registry.mark_seen(SOLO, T0, now=T0)
    registry.mark_seen(OTHER, T0, now=T0)
    registry.apply_stats_reply({}, {}, now=T0 + MINUTE)  # completed: both unanswered
    registry.mark_seen(OTHER, T0 + 40 * MINUTE, now=T0 + 40 * MINUTE)

    registry.remove_stale_workers(now=T0 + 45 * MINUTE)

    # SOLO (silent, evidence, past 30 min) is dropped; OTHER (recent) stays.
    facts = registry.registry_facts(broker_configured=True, now=T0 + 45 * MINUTE)
    assert facts.any_online is True
    registry.mark_shutdown(OTHER, 1, T0 + 46 * MINUTE)
    assert (
        registry.registry_facts(broker_configured=True, now=T0 + 46 * MINUTE).any_online
        is False
    )


def test_unknown_nodenames_are_capped_and_the_oldest_is_evicted() -> None:
    registry = WorkerRegistry(max_unlisted=2)
    for i, node in enumerate(("celery@a", "celery@b", "celery@c")):
        registry.mark_seen(node, T0 + i * timedelta(seconds=10), now=T0 + MINUTE)

    # "a" was the oldest: stopping "b" and "c" leaves nothing live only if "a" is gone.
    registry.mark_shutdown("celery@b", 1, T0 + MINUTE)
    registry.mark_shutdown("celery@c", 1, T0 + MINUTE)

    assert fleet_items(report_at(registry, T0 + MINUTE)) == [(None, None, 1)]
    assert (
        registry.registry_facts(broker_configured=True, now=T0 + MINUTE).any_online
        is False
    )


def test_the_stop_memory_is_capped_too() -> None:
    registry = WorkerRegistry(max_unlisted=2)
    for i, node in enumerate(("celery@a", "celery@b", "celery@c")):
        registry.mark_shutdown(node, 1, T0 + i * timedelta(seconds=10))

    # "a"'s stop was forgotten (the oldest), so an old event of it counts again.
    registry.mark_seen("celery@a", T0, now=T0 + MINUTE)

    assert (
        registry.registry_facts(broker_configured=True, now=T0 + MINUTE).any_online
        is True
    )


# --- The classifier rule alone (no registry) ------------------------------------------


def facts_of(**overrides: object) -> RegistryFacts:
    """Registry facts with a fleet that was seen and is offline unless overridden."""
    base: dict[str, object] = {
        "live_names": frozenset({"app.known"}),
        "any_live": True,
        "authority_at": None,
        "broker_configured": False,
        "any_seen": True,
        "any_online": False,
    }
    return RegistryFacts(**{**base, **overrides})  # type: ignore[arg-type]  # overrides are RegistryFacts fields


@pytest.mark.parametrize("authority_age", [None, 30, 3600])
@pytest.mark.parametrize("broker", [True, False])
@pytest.mark.parametrize("any_live", [True, False])
def test_the_fleet_item_needs_no_gate_and_no_authority_window(
    any_live: bool, broker: bool, authority_age: int | None
) -> None:
    now = T0 + 10 * MINUTE
    authority = (
        None if authority_age is None else now - timedelta(seconds=authority_age)
    )
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 0, name="app.new"))
    facts = classifier_facts(store.run_records(), {}, now)

    report = classify_anomalies(
        **facts._asdict(),
        now=now,
        settings=SETTINGS,
        registry=facts_of(
            any_live=any_live, broker_configured=broker, authority_at=authority
        ),
    )

    assert [(a.kind, a.task_count) for a in report.items] == [(FLEET, 1)]


@pytest.mark.parametrize(
    ("any_seen", "any_online", "expected"),
    [(True, False, 1), (True, True, 0), (False, False, 0), (False, True, 0)],
)
def test_the_fleet_item_needs_a_worker_seen_and_none_online(
    any_seen: bool, any_online: bool, expected: int
) -> None:
    now = T0 + 10 * MINUTE
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 0, name="app.new"))
    facts = classifier_facts(store.run_records(), {}, now)

    report = classify_anomalies(
        **facts._asdict(),
        now=now,
        settings=SETTINGS,
        registry=facts_of(
            any_seen=any_seen,
            any_online=any_online,
            authority_at=now,
            broker_configured=True,
        ),
    )

    assert report.counts[FLEET] == expected


def test_while_a_worker_is_online_the_per_name_items_are_unchanged() -> None:
    now = T0 + 10 * MINUTE
    store = GraphStore()
    store.add_event(task_event("t1", TaskState.PENDING, 0, name="app.new"))
    facts = classifier_facts(store.run_records(), {}, now)

    report = classify_anomalies(
        **facts._asdict(),
        now=now,
        settings=SETTINGS,
        registry=facts_of(any_online=True, authority_at=now, broker_configured=True),
    )

    assert [(a.kind, a.name) for a in report.items] == [(KIND_UNREGISTERED, "app.new")]
    assert report.anomalies["t1"].kind == KIND_UNREGISTERED
