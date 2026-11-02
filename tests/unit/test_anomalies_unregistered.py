"""Matrix tests for unregistered (``core/anomalies.py``, D4).

Each test names its row of the event-case matrix in
``.context/features/anomaly-detection.md``. Events go through a real
``GraphStore`` and the server adapter. Times are seconds after ``T0``;
"now" is ``NOW`` and the settings are ``SETTINGS`` (grace 60 s, authority
window 120 s, on top of SMALL).
"""

import itertools
from dataclasses import replace
from datetime import timedelta

import pytest

from stemtrace.core.anomalies import (
    Anomaly,
    AnomalyKind,
    AnomalySettings,
    NodeFacts,
    RegistryFacts,
    classify_anomalies,
)
from stemtrace.core.events import TaskEvent, TaskState
from tests.unit.anomaly_pipeline import SMALL, at, classify, task_event

NOW = 1000.0
GRACE = 60.0
WINDOW = 120.0
SETTINGS = replace(
    SMALL,
    pending_grace=timedelta(seconds=GRACE),
    authority_window=timedelta(seconds=WINDOW),
)
FRESH = NOW - 10
P, R, S = TaskState.PENDING, TaskState.RECEIVED, TaskState.STARTED
OLD = NOW - GRACE - 40  # a PENDING stamp well past the grace


def registry(
    names: tuple[str, ...] = ("app.known",),
    *,
    authority: float | None = FRESH,
    broker: bool = True,
) -> RegistryFacts:
    """Registry facts; ``authority`` is seconds after T0 (None: never confirmed)."""
    return RegistryFacts(
        live_names=frozenset(names),
        authority_at=None if authority is None else at(authority),
        broker_configured=broker,
    )


def pending(task_id: str, seconds: float, name: str = "app.new") -> TaskEvent:
    """A sender-side PENDING stamped ``seconds`` after T0."""
    return task_event(task_id, P, seconds, name=name)


def run(
    events: list[TaskEvent],
    facts: RegistryFacts | None,
    *,
    now: float = NOW,
    settings: AnomalySettings = SETTINGS,
    max_nodes: int = 10_000,
) -> tuple[list[Anomaly], dict[str, Anomaly], dict[AnomalyKind, int]]:
    """The report's list items, node map and counts."""
    report = classify(
        events, now=now, settings=settings, registry=facts, max_nodes=max_nodes
    )
    return list(report.items), dict(report.anomalies), dict(report.counts)


def unregistered(items: list[Anomaly]) -> list[Anomaly]:
    """Only the per-name items."""
    return [a for a in items if a.kind == AnomalyKind.UNREGISTERED]


# --- The rule: eligible node, online registration, one item per name ----------


def test_unregistered_pending_task_past_grace_gets_a_per_name_item_and_a_node_entry() -> (
    None
):
    items, nodes, counts = run([pending("t1", OLD)], registry())

    assert items == [
        Anomaly(
            kind=AnomalyKind.UNREGISTERED,
            task_id=None,
            name="app.new",
            since=at(OLD),
            task_count=1,
        )
    ]
    assert set(nodes) == {"t1"}
    assert nodes["t1"].kind == AnomalyKind.UNREGISTERED
    assert nodes["t1"].name == "app.new"
    assert counts[AnomalyKind.UNREGISTERED] == 1


def test_unregistered_groups_by_name_with_count_and_oldest_since() -> None:
    events = [
        pending("a2", OLD + 10, "app.a"),
        pending("a1", OLD, "app.a"),
        pending("b1", OLD + 5, "app.b"),
    ]

    items, nodes, counts = run(events, registry())

    assert [(a.name, a.task_count, a.since) for a in items] == [
        ("app.a", 2, at(OLD)),
        ("app.b", 1, at(OLD + 5)),
    ]
    assert {a.task_id for a in items} == {None}
    assert set(nodes) == {"a1", "a2", "b1"}
    assert counts[AnomalyKind.UNREGISTERED] == 2  # names, not tasks


def test_unregistered_counts_only_nodes_past_grace() -> None:
    events = [pending("old", OLD), pending("young", NOW - GRACE + 20)]

    items, nodes, _ = run(events, registry())

    assert [(a.task_count, a.since) for a in items] == [(1, at(OLD))]
    assert set(nodes) == {"old"}


def test_unregistered_name_registered_by_an_online_worker_is_not_flagged() -> None:
    items, nodes, counts = run(
        [pending("t1", OLD, "app.known")], registry(("app.known",))
    )

    assert (items, nodes) == ([], {})
    assert counts[AnomalyKind.UNREGISTERED] == 0


# --- Row 25/26/27/28/33/34, 30, 31 and the grace boundary -----------------


@pytest.mark.parametrize(
    ("age", "flagged"),
    [
        (GRACE - 1, False),
        (GRACE, False),  # strict: exactly grace is not past it
        (GRACE + 1, True),
    ],
)
def test_unregistered_grace_boundary_is_strict_within_one_second(
    age: float, flagged: bool
) -> None:
    items, _, _ = run([pending("t1", NOW - age)], registry())

    assert [a.kind for a in items] == ([AnomalyKind.UNREGISTERED] if flagged else [])


def test_unregistered_follows_the_grace_setting() -> None:
    events = [pending("t1", NOW - 100)]

    short, _, _ = run(
        events,
        registry(),
        settings=replace(SETTINGS, pending_grace=timedelta(seconds=99)),
    )
    long, _, _ = run(
        events,
        registry(),
        settings=replace(SETTINGS, pending_grace=timedelta(seconds=101)),
    )

    assert (len(short), len(long)) == (1, 0)


def test_row33_name_registered_only_by_an_offline_worker_is_unregistered() -> None:
    # The registry facts hold online registrations only: an offline worker's
    # "app.new" is absent while another worker is online.
    items, _, _ = run([pending("t1", OLD)], registry(("app.other",)))

    assert [a.name for a in items] == ["app.new"]


def test_row34_rolling_deploy_new_worker_registers_the_name_within_grace() -> None:
    young = NOW - GRACE + 30
    during = run([pending("t1", young)], registry(("app.other",)))[0]
    after_registered = run([pending("t1", OLD)], registry(("app.other", "app.new")))[0]

    assert (during, after_registered) == ([], [])


def test_row31_producer_ships_a_new_name_first_is_flagged() -> None:
    items, _, _ = run([pending("t1", OLD, "app.brand_new")], registry())

    assert [a.name for a in items] == ["app.brand_new"]


@pytest.mark.parametrize("name", ["celery.chord_unlock", "celery.accumulate"])
def test_row30_celery_builtins_are_never_unregistered(name: str) -> None:
    items, nodes, counts = run([pending("t1", OLD, name)], registry())

    assert (items, nodes) == ([], {})
    assert counts[AnomalyKind.UNREGISTERED] == 0


@pytest.mark.parametrize(
    "state",
    [
        TaskState.RECEIVED,
        TaskState.STARTED,
        TaskState.RETRY,
        TaskState.SUCCESS,
        TaskState.FAILURE,
        TaskState.REVOKED,
        TaskState.REJECTED,
        TaskState.IGNORED,
    ],
)
def test_rows1_3_14_15_32_only_a_pending_state_is_eligible(state: TaskState) -> None:
    events = [task_event("t1", state, OLD, name="app.new", host="w@a")]

    items, nodes, _ = run(events, registry(), now=OLD + 20)
    items_late, _, _ = run(events, registry(), now=OLD + 100)

    assert (items, nodes) == ([], {})
    assert unregistered(items_late) == []


def test_row3_received_after_pending_is_not_counted_as_pending() -> None:
    events = [pending("t1", OLD), task_event("t1", R, OLD + 1, name="app.new")]

    items, nodes, _ = run(events, registry())

    assert (items, nodes) == ([], {})


def test_row5_arrival_order_does_not_change_the_answer() -> None:
    events = [
        pending("t1", OLD),
        pending("t2", OLD + 5),
        pending("t3", OLD + 9, "app.k"),
    ]
    answers = {
        tuple(run(list(order), registry(("app.k",)))[0])
        for order in itertools.permutations(events)
    }

    assert len(answers) == 1
    assert [a.name for a in next(iter(answers))] == ["app.new"]


@pytest.mark.parametrize("skew", [-3600.0, 0.0, 3600.0])
def test_row8_sender_clock_skew_shifts_the_grace_by_the_skew(skew: float) -> None:
    items, _, _ = run([pending("t1", OLD + skew)], registry())

    # A sender 1 h ahead stamps the future: not yet past grace. 1 h behind: flagged.
    assert bool(items) == (skew <= 0)


def test_row27_an_evicted_node_is_not_counted() -> None:
    # The store evicts down to its bound when a third node arrives (it keeps t3).
    events = [pending("t1", OLD), pending("t2", OLD + 1), pending("t3", OLD + 2)]

    items, nodes, _ = run(events, registry(), max_nodes=2)

    assert [(a.task_count, a.since) for a in items] == [(1, at(OLD + 2))]
    assert set(nodes) == {"t3"}


def test_unregistered_leaves_stuck_and_interrupted_unchanged() -> None:
    events = [
        task_event("s1", S, 0, name="app.slow", host="w@a"),
        pending("u1", OLD),
    ]

    items, nodes, counts = run(events, registry())

    assert {a.kind for a in items} == {AnomalyKind.STUCK, AnomalyKind.UNREGISTERED}
    assert nodes["s1"].kind == AnomalyKind.STUCK
    assert counts[AnomalyKind.STUCK] == 1


# --- The D6 authority gate ----------------------------------------------------


@pytest.mark.parametrize(
    ("facts", "fires"),
    [
        pytest.param(registry(authority=NOW - WINDOW), True, id="authority-at-window"),
        pytest.param(registry(authority=NOW - WINDOW - 1), False, id="window-plus-1s"),
        pytest.param(registry(authority=NOW - WINDOW + 1), True, id="window-minus-1s"),
        pytest.param(registry(authority=None), False, id="inspect-never-confirmed"),
        pytest.param(
            registry(authority=None, broker=False), True, id="no-broker-with-workers"
        ),
        pytest.param(
            registry((), authority=None, broker=False),
            False,
            id="no-broker-no-registered-workers",
        ),
        pytest.param(
            registry((), authority=None, broker=False),
            False,
            id="no-broker-online-worker-with-no-tasks",
        ),
        pytest.param(
            registry(authority=NOW - WINDOW - 1, broker=False),
            True,
            id="no-broker-ignores-stale-authority",
        ),
        pytest.param(None, False, id="no-registry"),
    ],
)
def test_gate_decides_whether_unregistered_can_fire(
    facts: RegistryFacts | None, fires: bool
) -> None:
    # Row 25/26: a server restarted after its workers, no authority yet: gated.
    items, nodes, counts = run([pending("t1", OLD)], facts)

    assert bool(unregistered(items)) is fires
    assert bool(nodes) is fires
    assert counts[AnomalyKind.UNREGISTERED] == int(fires)


def test_gate_follows_the_authority_window_setting() -> None:
    facts = registry(authority=NOW - 100)

    wide, _, _ = run(
        [pending("t1", OLD)],
        facts,
        settings=replace(SETTINGS, authority_window=timedelta(seconds=101)),
    )
    narrow, _, _ = run(
        [pending("t1", OLD)],
        facts,
        settings=replace(SETTINGS, authority_window=timedelta(seconds=99)),
    )

    assert (len(wide), len(narrow)) == (1, 0)


def test_classifier_accepts_node_facts_as_pending_tasks() -> None:
    waiting = NodeFacts(
        task_id="n1",
        name="app.new",
        state=P,
        last_updated=at(OLD),
        state_since=at(OLD),
    )

    report = classify_anomalies(
        [],
        now=at(NOW),
        runtime_stats={},
        timeline={},
        settings=SETTINGS,
        pending=[waiting],
        registry=registry(),
    )

    assert [(a.name, a.task_count) for a in report.items] == [("app.new", 1)]
    assert set(report.anomalies) == {"n1"}


def test_classifier_ignores_a_pending_task_with_no_stamp() -> None:
    unstamped = NodeFacts(task_id="n1", name="app.new", state=P, last_updated=at(OLD))

    report = classify_anomalies(
        [],
        now=at(NOW),
        runtime_stats={},
        timeline={},
        settings=SETTINGS,
        pending=[unstamped],
        registry=registry(),
    )

    assert list(report.items) == []


def test_one_online_worker_with_no_tasks_keeps_per_name_items() -> None:
    items, _, _ = run([pending("t1", OLD)], registry(()))

    assert [a.kind for a in items] == [AnomalyKind.UNREGISTERED]


# --- While every worker is offline, the per-name rules apply unchanged ----------


def test_row28_a_sigkilled_fleet_keeps_its_names_so_nothing_fires() -> None:
    # No stop was reported: the dead workers stay live until the 30-minute cleanup.
    items, nodes, _ = run([pending("t1", OLD, "app.known")], registry(("app.known",)))

    assert (items, nodes) == ([], {})


def test_row28_after_the_cleanup_the_gate_is_closed_and_nothing_fires() -> None:
    gone = registry((), authority=NOW - 1800, broker=False)
    with_broker = registry((), authority=NOW - 1800)

    assert run([pending("t1", OLD)], gone)[:2] == ([], {})
    assert run([pending("t1", OLD)], with_broker)[:2] == ([], {})


def test_row28_with_every_worker_stopped_and_a_fresh_reply_names_are_unregistered() -> (
    None
):
    # A clean stop of every worker, an inspect reply seconds old: no names left.
    items, _, _ = run([pending("t1", OLD, "app.known")], registry(()))

    assert [(a.kind, a.name) for a in items] == [
        (AnomalyKind.UNREGISTERED, "app.known")
    ]


@pytest.mark.parametrize("broker", [True, False])
@pytest.mark.parametrize("authority", [None, FRESH, NOW - WINDOW - 1, NOW - 7200])
@pytest.mark.parametrize("names", [(), ("app.known",)])
def test_no_workers_online_is_never_emitted(
    names: tuple[str, ...], authority: float | None, broker: bool
) -> None:
    events = [pending("t1", OLD, "app.new"), pending("t2", OLD, "app.known")]

    items, _, counts = run(events, registry(names, authority=authority, broker=broker))

    assert AnomalyKind.NO_WORKERS_ONLINE not in {a.kind for a in items}
    assert counts[AnomalyKind.NO_WORKERS_ONLINE] == 0
    assert list(counts) == list(AnomalyKind)
