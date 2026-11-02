"""Matrix tests for the stuck and interrupted classifier (``core/anomalies.py``).

Each test names its row of the event-case matrix in
``.context/features/anomaly-detection.md``. Events go through a real
``GraphStore`` and ``WorkerTimeline`` and the server adapter. Times are
seconds after ``T0``; the thresholds are ``SMALL`` (k 3, floor 60 s,
ceiling 300 s, N 3).
"""

import json
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from stemtrace.core.anomalies import (
    DEFAULT_SETTINGS,
    Anomaly,
    AnomalyEvidence,
    AnomalyKind,
    AnomalySettings,
    NodeFacts,
    RuntimeStats,
    TimelineEntry,
    classify_anomalies,
)
from stemtrace.core.events import (
    CANCELLED_RETRY_REASON,
    TaskEvent,
    TaskState,
    WorkerEvent,
    WorkerEventType,
)
from stemtrace.server.store import WorkerTimeline
from tests.unit.anomaly_pipeline import (
    READY,
    SHUTDOWN,
    SMALL,
    TERMINATING,
    at,
    classify,
    completed_runs,
    sample_runs,
    task_event,
    worker_event,
)

S, R = TaskState.STARTED, TaskState.RECEIVED
FLOWS = Path(__file__).parents[1] / "data" / "celery_flows.json"


def kinds(
    task_events: list[TaskEvent],
    worker_events: list[WorkerEvent] | None = None,
    *,
    now: float,
    settings: AnomalySettings = SMALL,
    max_nodes: int = 10_000,
    timeline: WorkerTimeline | None = None,
) -> dict[str, AnomalyKind]:
    """Task ID -> kind of every flagged node."""
    report = classify(
        task_events,
        worker_events or [],
        now=now,
        settings=settings,
        max_nodes=max_nodes,
        timeline=timeline,
    )
    return {task_id: a.kind for task_id, a in report.anomalies.items()}


def only(
    task_events: list[TaskEvent],
    worker_events: list[WorkerEvent] | None = None,
    *,
    now: float,
) -> Anomaly:
    """The single anomaly of ``t1``."""
    report = classify(task_events, worker_events or [], now=now)
    assert set(report.anomalies) == {"t1"}
    return report.anomalies["t1"]


# --- Report shape ------------------------------------------------------------


def test_counts_has_every_kind_even_with_no_anomaly() -> None:
    report = classify([task_event("t1", TaskState.SUCCESS, 0)], now=10)

    assert dict(report.counts) == dict.fromkeys(AnomalyKind, 0)
    assert [kind.value for kind in AnomalyKind] == [
        "stuck",
        "interrupted",
        "unregistered",
        "no_workers_online",
    ]


def test_counts_tally_each_flagged_kind() -> None:
    events = [
        task_event("s1", S, 0, host="w@a"),
        task_event("i1", S, 0, host="w@b"),
        task_event("i2", S, 0, host="w@b"),
    ]

    report = classify(events, [worker_event(SHUTDOWN, 5, host="w@b")], now=1000)

    assert dict(report.counts) == {
        AnomalyKind.STUCK: 1,
        AnomalyKind.INTERRUPTED: 2,
        AnomalyKind.UNREGISTERED: 0,
        AnomalyKind.NO_WORKERS_ONLINE: 0,
    }


# --- Row 2: STARTED, worker alive; threshold T and its strict boundary --------

# (samples of name, runtime of each in s) -> T in s, per branch of T.
T_BRANCHES = [
    pytest.param(3, 30, 90, id="n>=N: k*p95 above the floor"),
    pytest.param(3, 5, 60, id="n>=N: floor wins over k*p95"),
    pytest.param(2, 30, 300, id="n<N: ceiling"),
    pytest.param(0, 0, 300, id="no samples: ceiling"),
]


@pytest.mark.parametrize(("count", "runtime", "threshold"), T_BRANCHES)
@pytest.mark.parametrize(("offset", "flagged"), [(-1, False), (0, False), (1, True)])
def test_stuck_fires_strictly_after_threshold(
    count: int, runtime: float, threshold: int, offset: int, flagged: bool
) -> None:
    events = completed_runs("app.work", count, runtime)
    events.append(task_event("t1", S, 0, host="w@a"))

    got = kinds(events, now=threshold + offset)

    assert got == ({"t1": AnomalyKind.STUCK} if flagged else {})


def test_stuck_reports_evidence_fields() -> None:
    events = completed_runs("app.work", 3, 30)
    events.append(task_event("t1", S, 5, host="w@a"))

    anomaly = only(events, now=125)

    assert anomaly == Anomaly(
        kind=AnomalyKind.STUCK,
        task_id="t1",
        name="app.work",
        since=at(5),
        hostname="w@a",
        elapsed_ms=120_000,
        threshold_ms=90_000,
        p95_ms=30_000,
        runtime_samples=3,
    )


def test_stuck_ceiling_below_floor_uses_the_floor() -> None:
    settings = AnomalySettings(
        stuck_floor=timedelta(seconds=60), stuck_ceiling=timedelta(seconds=30)
    )

    inside = kinds([task_event("t1", S, 0)], now=60, settings=settings)
    outside = kinds([task_event("t1", S, 0)], now=61, settings=settings)

    assert (inside, outside) == ({}, {"t1": AnomalyKind.STUCK})


def test_default_settings_are_the_specs() -> None:
    assert (
        AnomalySettings(
            stuck_factor=3,
            stuck_floor=timedelta(minutes=10),
            stuck_ceiling=timedelta(hours=1),
            stuck_min_samples=5,
        )
        == DEFAULT_SETTINGS
    )
    events = [task_event("t1", S, 0)]
    assert kinds(events, now=3600, settings=DEFAULT_SETTINGS) == {}
    assert kinds(events, now=3601, settings=DEFAULT_SETTINGS) == {
        "t1": AnomalyKind.STUCK
    }


# --- Row 1, 3, 15, 16: states that are never stuck ----------------------------


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: [task_event("t1", R, 0, host="w@a")], id="1 RECEIVED only"
        ),
        pytest.param(
            lambda: [task_event("t1", TaskState.PENDING, 0)], id="PENDING only"
        ),
        pytest.param(
            lambda: [
                task_event("t1", S, 0, host="w@a"),
                task_event(
                    "t1",
                    TaskState.RETRY,
                    1,
                    host="w@a",
                    exception="boom",
                    eta=2,
                ),
            ],
            id="15/16 RETRY with its ETA long past",
        ),
    ],
)
def test_state_other_than_started_is_never_stuck_or_interrupted(
    build: Callable[[], list[TaskEvent]],
) -> None:
    workers = [worker_event(SHUTDOWN, 5), worker_event(READY, 6)]

    assert kinds(build(), workers, now=100_000) == {}


def test_started_without_pending_follows_the_normal_rules() -> None:
    # Row 3.
    assert kinds([task_event("t1", S, 0, host="w@a")], now=301) == {
        "t1": AnomalyKind.STUCK
    }
    assert kinds(
        [task_event("t1", S, 0, host="w@a")],
        [worker_event(SHUTDOWN, 1)],
        now=301,
    ) == {"t1": AnomalyKind.INTERRUPTED}


# --- Rows 14, 20, 32: terminal states ----------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        TaskState.SUCCESS,
        TaskState.FAILURE,
        TaskState.REVOKED,
        TaskState.REJECTED,
        TaskState.IGNORED,
    ],
)
def test_terminal_state_is_never_flagged(state: TaskState) -> None:
    # Rows 14 (IGNORED, REJECTED), 20 (warm restart), 32 (REVOKED).
    events = [
        task_event("t1", S, 0, host="w@a"),
        task_event("t1", state, 3, host="w@a"),
    ]
    workers = [worker_event(SHUTDOWN, 5), worker_event(READY, 6)]

    assert kinds(events, workers, now=100_000) == {}


def test_revoked_before_start_is_never_flagged() -> None:
    # Row 32: revoked or expired before it started, no STARTED at all.
    events = [task_event("t1", TaskState.REVOKED, 3, host="w@a")]

    assert kinds(events, [worker_event(SHUTDOWN, 5)], now=100_000) == {}


# --- Row 4, 5: reordered events ----------------------------------------------


def test_success_after_worker_shutdown_clears_interrupted() -> None:
    # Row 4: interrupted until the SUCCESS lands, then nothing.
    started = task_event("t1", S, 0, host="w@a")
    success = task_event("t1", TaskState.SUCCESS, 9, host="w@a")
    shutdown = [worker_event(SHUTDOWN, 5)]

    assert kinds([started], shutdown, now=100) == {"t1": AnomalyKind.INTERRUPTED}
    assert kinds([started, success], shutdown, now=100) == {}


def test_arrival_order_does_not_change_the_answer() -> None:
    # Row 5: task and worker events in any order give one result.
    events = [
        *completed_runs("app.work", 3, 30),
        task_event("t1", S, 0, host="w@a"),
        task_event("t2", S, 0, host="w@b"),
        task_event("t2", TaskState.SUCCESS, 4, host="w@b"),
        task_event("t3", S, 0, host="w@b"),
    ]
    workers = [
        worker_event(SHUTDOWN, 2, host="w@b"),
        worker_event(READY, 8, host="w@c"),
    ]
    expected = classify(events, workers, now=1000).anomalies
    rng = random.Random(5)

    for _ in range(20):
        rng.shuffle(events)
        rng.shuffle(workers)
        assert classify(events, workers, now=1000).anomalies == expected
    assert {t: a.kind for t, a in expected.items()} == {
        "t1": AnomalyKind.STUCK,
        "t3": AnomalyKind.INTERRUPTED,
    }


# --- Rows 6, 7: clock skew ---------------------------------------------------


@pytest.mark.parametrize("skew", [-3600, 0, 3600])
def test_interrupted_ignores_skew_of_the_hosts_clock(skew: int) -> None:
    # Rows 6, 7: the worker's STARTED and its shutdown share a clock; the server
    # clock (``now``) is never compared with them.
    events = [task_event("t1", S, skew + 10, host="w@a")]
    workers = [worker_event(SHUTDOWN, skew + 20)]

    anomaly = only(events, workers, now=15)

    assert (anomaly.kind, anomaly.since) == (AnomalyKind.INTERRUPTED, at(skew + 20))


@pytest.mark.parametrize(
    ("skew", "now", "flagged"),
    [
        pytest.param(3600, 301, False, id="ahead: late, elapsed shrank"),
        pytest.param(3600, 3600 + 301, True, id="ahead: fires T after its clock"),
        pytest.param(-3600, 301, True, id="behind: early, elapsed grew"),
        pytest.param(0, 301, True, id="no skew"),
        pytest.param(0, 299, False, id="no skew, before T"),
    ],
)
def test_stuck_elapsed_carries_the_whole_skew(
    skew: int, now: float, flagged: bool
) -> None:
    got = kinds([task_event("t1", S, skew, host="w@a")], now=now)

    assert got == ({"t1": AnomalyKind.STUCK} if flagged else {})


# --- Rows 9, 10, 11: old workers ---------------------------------------------


def test_started_without_hostname_is_stuck_but_never_interrupted() -> None:
    # Row 9.
    events = [task_event("t1", S, 0)]
    workers = [worker_event(SHUTDOWN, 5)]

    assert kinds(events, workers, now=301) == {"t1": AnomalyKind.STUCK}


def test_ignored_by_old_worker_stays_started_and_is_stuck() -> None:
    # Row 10 (limitation): no IGNORED event, so it looks stuck.
    assert kinds([task_event("t1", S, 0, host="w@a")], now=301) == {
        "t1": AnomalyKind.STUCK
    }


def test_cold_stop_without_terminating_is_found_only_at_restart() -> None:
    # Row 11 (limitation).
    events = [task_event("t1", S, 0, host="w@a")]

    assert kinds(events, [], now=30) == {}
    assert kinds(events, [worker_event(READY, 40)], now=50) == {
        "t1": AnomalyKind.INTERRUPTED
    }


# --- Rows 12, 12a, 13: progress rules (a) and (b) ----------------------------


def _replacement_prefix() -> list[TaskEvent]:
    flow = json.loads(FLOWS.read_text())["replace_delayed_root"]
    return [TaskEvent.model_validate(e) for e in flow[:3]]


def test_replace_with_delayed_eta_is_not_stuck_before_the_eta() -> None:
    # Row 12, capture replace_delayed_root: STARTED 10:04:15.487, then the
    # replacement's PENDING with eta 10:04:16.500 and parent == task_id.
    events = _replacement_prefix()
    started = events[1].timestamp
    eta = events[2].eta
    assert eta is not None
    assert eta > started

    def classify_at(moment: datetime) -> dict[str, Anomaly]:
        return dict(classify(events, now=moment).anomalies)

    task_id = events[0].task_id
    assert classify_at(eta + timedelta(seconds=300)) == {}
    assert list(classify_at(eta + timedelta(seconds=301))) == [task_id]
    # Without rule (a) the node would be stuck 300 s after its STARTED.
    assert (eta - started) > timedelta(milliseconds=500)


def test_replacement_progress_moves_the_stuck_clock_to_the_eta() -> None:
    events = [
        task_event("t1", S, 0, host="w@a"),
        task_event("t1", TaskState.PENDING, 1, name="app.next", parent="t1", eta=100),
    ]

    assert kinds(events, now=400) == {}
    assert kinds(events, now=401) == {"t1": AnomalyKind.STUCK}


def test_replacement_that_ran_early_is_not_hidden_by_its_eta_field() -> None:
    # An ETA earlier than the timestamp counts the timestamp (max of the two).
    events = [
        task_event("t1", S, 0),
        task_event("t1", TaskState.PENDING, 90, parent="t1", eta=10),
    ]

    assert kinds(events, now=390) == {}
    assert kinds(events, now=391) == {"t1": AnomalyKind.STUCK}


def test_sender_pending_with_the_original_parent_does_not_hide_stuck() -> None:
    # A sender clock far ahead must not hide a stuck task.
    events = [
        task_event("t0", S, 0),
        task_event("t1", TaskState.PENDING, 5000, parent="t0"),
        task_event("t1", S, 10, parent="t0"),
    ]

    assert kinds(events, now=400)["t1"] == AnomalyKind.STUCK


def test_retry_received_from_a_skewed_host_delays_stuck_by_the_skew() -> None:
    # Row 12a (limitation, decided 2026-11-02): host b's clock is 1 h ahead of
    # host a's; its retry RECEIVED (parent = the task itself) moves progress.
    skew = 3600
    events = [
        task_event("t1", S, 0, host="w@a"),
        task_event("t1", TaskState.RETRY, 5, host="w@a", exception="boom", eta=10),
        task_event(
            "t1", R, 10 + skew, host="w@b", retries=1, parent="t1", eta=10 + skew
        ),
        task_event("t1", S, 10, host="w@a", retries=1),
    ]

    assert kinds(events, now=400) == {}
    assert kinds(events, now=skew + 10 + 301) == {"t1": AnomalyKind.STUCK}


def test_same_host_retry_received_cannot_hide_stuck() -> None:
    # Row 12a, same host: the RECEIVED and its eta precede the STARTED.
    events = [
        task_event("t1", S, 0, host="w@a"),
        task_event("t1", TaskState.RETRY, 5, host="w@a", exception="boom", eta=10),
        task_event("t1", R, 10, host="w@a", retries=1, parent="t1", eta=10),
        task_event("t1", S, 11, host="w@a", retries=1),
    ]

    assert kinds(events, now=11 + 301) == {"t1": AnomalyKind.STUCK}


def test_retry_started_is_the_state_event_and_sets_the_clock() -> None:
    # Retry STARTEDs carry parent == task_id and retries >= 1; they are not (a).
    events = [
        task_event("t1", S, 0, host="w@a"),
        task_event("t1", TaskState.RETRY, 5, host="w@a", exception="boom", eta=10),
        task_event("t1", S, 20, host="w@a", retries=1, parent="t1", eta=10),
    ]

    assert kinds(events, now=20 + 301) == {"t1": AnomalyKind.STUCK}
    assert kinds(events, now=20 + 299) == {}


def test_child_progress_hides_a_stuck_parent_until_it_goes_quiet() -> None:
    # Row 13 (limitation): rule (b), newest last_updated below the node.
    events = [
        task_event("p", S, 0, host="w@a"),
        task_event("c", S, 100, host="w@a", parent="p", name="app.child"),
    ]

    assert kinds(events, now=350) == {}
    assert kinds(events, now=401) == {"p": AnomalyKind.STUCK, "c": AnomalyKind.STUCK}


def test_grandchild_progress_hides_the_whole_chain() -> None:
    events = [
        task_event("a", S, 0),
        task_event("b", S, 10, parent="a"),
        task_event("c", S, 20, parent="b"),
        task_event("c", TaskState.SUCCESS, 120, parent="b"),
    ]

    assert kinds(events, now=420) == {}
    assert kinds(events, now=421) == {"a": AnomalyKind.STUCK, "b": AnomalyKind.STUCK}


def test_group_members_count_for_the_subtree_through_reported_parent() -> None:
    # Members sit under a synthetic group node; the walk uses reported parents.
    events = [
        task_event("p", S, 0),
        *(
            task_event(f"m{i}", TaskState.SUCCESS, 100, parent="p").model_copy(
                update={"group_id": "g1"}
            )
            for i in range(2)
        ),
    ]

    assert kinds(events, now=400) == {}
    assert kinds(events, now=401) == {"p": AnomalyKind.STUCK}


def _facts(task_id: str, parent: str | None, updated: float) -> NodeFacts:
    return NodeFacts(
        task_id=task_id,
        name="app.work",
        state=TaskState.STARTED,
        last_updated=at(updated),
        state_since=at(0),
        reported_parent_id=parent,
    )


def test_parent_cycle_gets_the_exact_subtree_answer() -> None:
    # Impossible from Celery IDs, but arbitrary events must not hang or lie.
    nodes = [_facts("a", "b", 10), _facts("b", "a", 50), _facts("c", "b", 30)]

    report = classify_anomalies(
        nodes, now=at(1000), runtime_stats={}, timeline={}, settings=SMALL
    )

    brute = {}
    parents = {n.task_id: n.reported_parent_id for n in nodes}
    for n in nodes:
        reach, frontier = set(), [k for k, p in parents.items() if p == n.task_id]
        while frontier:
            k = frontier.pop()
            if k not in reach and k != n.task_id:
                reach.add(k)
                frontier += [x for x, p in parents.items() if p == k]
        brute[n.task_id] = max(
            [at(0)] + [m.last_updated for m in nodes if m.task_id in reach]
        )
    expected = {
        t: int((at(1000) - progress).total_seconds() * 1000)
        for t, progress in brute.items()
        if at(1000) - progress > timedelta(seconds=300)
    }
    assert {t: a.elapsed_ms for t, a in report.anomalies.items()} == expected


# --- Rows 17, 21: cancelled RETRY and cold stop ------------------------------


def _cancelled(retry_at: float, *, host: str | None = "w@a") -> list[TaskEvent]:
    return [
        task_event("t1", S, 0, host=host),
        task_event(
            "t1", TaskState.RETRY, retry_at, host=host, exception=CANCELLED_RETRY_REASON
        ),
    ]


def test_cancelled_retry_without_host_evidence_is_not_flagged() -> None:
    # Row 17 (limitation): a lost broker connection sends the same RETRY.
    assert kinds(_cancelled(5), now=100_000) == {}
    assert (
        kinds(_cancelled(5), [worker_event(SHUTDOWN, 5, host="w@other")], now=9) == {}
    )


@pytest.mark.parametrize(
    ("retry_at", "terminating_at"),
    [
        pytest.param(11, 10, id="RETRY stamped after WORKER_TERMINATING"),
        pytest.param(9, 10, id="RETRY stamped before WORKER_TERMINATING"),
        pytest.param(10, 10, id="same timestamp"),
    ],
)
def test_cold_stop_cancelled_retry_is_interrupted_in_either_stamp_order(
    retry_at: float, terminating_at: float
) -> None:
    # Row 21: evidence is judged against the attempt's STARTED, not the RETRY.
    anomaly = only(
        _cancelled(retry_at), [worker_event(TERMINATING, terminating_at)], now=20
    )

    assert (anomaly.kind, anomaly.since, anomaly.evidence, anomaly.hostname) == (
        AnomalyKind.INTERRUPTED,
        at(terminating_at),
        AnomalyEvidence.CANCELLED_RETRY,
        "w@a",
    )


def test_cancelled_retry_without_its_started_is_not_eligible() -> None:
    events = [
        task_event(
            "t1",
            TaskState.RETRY,
            5,
            host="w@a",
            exception=CANCELLED_RETRY_REASON,
        )
    ]

    assert kinds(events, [worker_event(TERMINATING, 6)], now=100) == {}


def test_cancelled_retry_uses_the_started_of_its_own_attempt() -> None:
    # Attempt 1 started at 50, after the shutdown at 20: no evidence for it.
    events = [
        task_event("t1", S, 0, host="w@a"),
        task_event("t1", TaskState.RETRY, 5, host="w@a", exception="boom", eta=6),
        task_event("t1", S, 50, host="w@a", retries=1),
        task_event(
            "t1",
            TaskState.RETRY,
            55,
            host="w@a",
            retries=1,
            exception=CANCELLED_RETRY_REASON,
        ),
    ]

    assert kinds(events, [worker_event(SHUTDOWN, 20)], now=100) == {}
    assert kinds(events, [worker_event(SHUTDOWN, 52)], now=100) == {
        "t1": AnomalyKind.INTERRUPTED
    }


@pytest.mark.parametrize(
    ("event_type", "evidence"),
    [
        (SHUTDOWN, AnomalyEvidence.WORKER_SHUTDOWN),
        (TERMINATING, AnomalyEvidence.WORKER_TERMINATING),
        (READY, AnomalyEvidence.WORKER_RESTARTED),
    ],
)
def test_started_interrupted_names_the_worker_event(
    event_type: WorkerEventType, evidence: AnomalyEvidence
) -> None:
    anomaly = only(
        [task_event("t1", S, 0, host="w@a")], [worker_event(event_type, 7)], now=8
    )

    assert (anomaly.since, anomaly.evidence) == (at(7), evidence)


def test_interrupted_since_is_the_earliest_evidence_after_the_start() -> None:
    workers = [
        worker_event(READY, -50),
        worker_event(TERMINATING, 7),
        worker_event(SHUTDOWN, 9),
        worker_event(READY, 20),
    ]

    anomaly = only([task_event("t1", S, 0, host="w@a")], workers, now=30)

    assert (anomaly.since, anomaly.evidence) == (
        at(7),
        AnomalyEvidence.WORKER_TERMINATING,
    )


def test_evidence_must_be_strictly_after_the_start() -> None:
    events = [task_event("t1", S, 10, host="w@a")]

    assert kinds(events, [worker_event(SHUTDOWN, 10)], now=11) == {}
    assert kinds(events, [worker_event(SHUTDOWN, 10.001)], now=11) == {
        "t1": AnomalyKind.INTERRUPTED
    }


def test_evidence_of_another_nodename_does_not_count() -> None:
    events = [task_event("t1", S, 0, host="w@a")]

    assert kinds(events, [worker_event(SHUTDOWN, 5, host="w@b")], now=10) == {}


# --- Rows 18, 19: acks_late redelivery ---------------------------------------


def test_redelivery_started_clears_interrupted_and_restarts_the_stuck_clock() -> None:
    # Row 18: same retries, another host, later time: the new STARTED decides.
    events = [
        task_event("t1", S, 0, host="w@a"),
        task_event("t1", S, 100, host="w@b"),
    ]
    workers = [worker_event(SHUTDOWN, 50, host="w@a")]

    assert kinds(events[:1], workers, now=100) == {"t1": AnomalyKind.INTERRUPTED}
    assert kinds(events, workers, now=100 + 299) == {}
    assert kinds(events, workers, now=100 + 301) == {"t1": AnomalyKind.STUCK}


def test_redelivery_to_a_host_behind_the_gap_stays_interrupted() -> None:
    # Row 19 (limitation): the new STARTED is stamped before the old one, so the
    # old one stays the state event.
    events = [
        task_event("t1", S, 100, host="w@a"),
        task_event("t1", S, 40, host="w@b"),
    ]

    anomaly = only(events, [worker_event(SHUTDOWN, 110, host="w@a")], now=120)

    assert (anomaly.kind, anomaly.hostname) == (AnomalyKind.INTERRUPTED, "w@a")


# --- Rows 22, 23, 24: SIGKILL of the main process ----------------------------


def test_sigkill_then_restart_is_stuck_first_then_interrupted() -> None:
    # Row 22.
    events = [task_event("t1", S, 0, host="w@a")]
    restart = [worker_event(READY, 400)]

    assert kinds(events, [], now=301) == {"t1": AnomalyKind.STUCK}
    assert kinds(events, restart, now=401) == {"t1": AnomalyKind.INTERRUPTED}
    assert only(events, restart, now=401).evidence == AnomalyEvidence.WORKER_RESTARTED


def test_sigkill_without_restart_is_stuck_only_after_the_threshold() -> None:
    # Row 23 (limitation: never interrupted).
    events = [task_event("t1", S, 0, host="w@a")]

    assert kinds(events, [], now=299) == {}
    assert kinds(events, [], now=301) == {"t1": AnomalyKind.STUCK}


def test_same_nodename_on_another_machine_is_a_false_interrupted() -> None:
    # Row 24 (limitation): the nodename is all the evidence has.
    events = [task_event("t1", S, 0, host="celery@shared")]

    got = kinds(events, [worker_event(READY, 5, host="celery@shared", pid=9)], now=6)

    assert got == {"t1": AnomalyKind.INTERRUPTED}


# --- Priority ----------------------------------------------------------------


def test_interrupted_wins_over_stuck() -> None:
    events = [task_event("t1", S, 0, host="w@a")]

    anomaly = only(events, [worker_event(SHUTDOWN, 5)], now=100_000)

    assert anomaly.kind == AnomalyKind.INTERRUPTED
    assert anomaly.elapsed_ms is None
    assert anomaly.threshold_ms is None


def test_retry_is_never_stuck_however_long_it_waits() -> None:
    events = [
        task_event("t1", S, 0, host="w@a"),
        task_event("t1", TaskState.RETRY, 1, host="w@a", exception="boom", eta=2),
    ]

    assert kinds(events, now=1_000_000) == {}


# --- Rows 27, 29, 30 ---------------------------------------------------------


def test_evicted_node_has_no_anomaly_and_its_samples_leave_the_p95() -> None:
    # Row 27: eviction drops the old STARTED node and the 3 samples that set
    # T = 90 s, so the survivor falls back to the 300 s ceiling.
    events = [
        task_event("gone", S, -5),
        *completed_runs("app.work", 3, 30),
        task_event("t1", S, 50, host="w@a"),
        *(
            task_event(f"x{i}", TaskState.SUCCESS, 1 + i, name="app.other")
            for i in range(9)
        ),
    ]

    before = classify(events, now=50 + 301)
    after = classify(events, now=50 + 301, max_nodes=10)

    assert before.anomalies["t1"].threshold_ms == 90_000
    assert "gone" in before.anomalies
    assert "gone" not in after.anomalies
    assert (after.anomalies["t1"].threshold_ms, after.anomalies["t1"].p95_ms) == (
        300_000,
        None,
    )


def test_timeline_overflow_keeps_the_newest_events_and_since_moves_later() -> None:
    # Row 29 (limitation): 40 events, newest 32 kept; the first kept is #9.
    timeline = WorkerTimeline()
    workers = [worker_event(READY, 1 + i, pid=i) for i in range(40)]

    anomaly = classify(
        [task_event("t1", S, 0, host="w@a")], workers, now=100, timeline=timeline
    ).anomalies["t1"]

    assert (anomaly.kind, anomaly.since) == (AnomalyKind.INTERRUPTED, at(9))


def test_evicted_nodename_loses_its_evidence() -> None:
    # Row 29 (limitation): the timeline keeps 1 nodename; w@a's evidence is gone.
    timeline = WorkerTimeline(max_nodes=1)
    workers = [
        worker_event(SHUTDOWN, 5, host="w@a"),
        worker_event(READY, 9, host="w@b"),
    ]

    got = kinds(
        [task_event("t1", S, 0, host="w@a")], workers, now=10, timeline=timeline
    )

    assert got == {}


def test_celery_builtin_uses_the_ceiling_even_with_a_stats_row() -> None:
    # Row 30.
    node = NodeFacts(
        task_id="t1",
        name="celery.chord_unlock",
        state=S,
        last_updated=at(0),
        state_since=at(0),
        state_host="w@a",
    )
    stats = {"celery.chord_unlock": RuntimeStats(1000, 50)}

    def run(now: float) -> dict[str, Anomaly]:
        return dict(
            classify_anomalies(
                [node], now=at(now), runtime_stats=stats, timeline={}, settings=SMALL
            ).anomalies
        )

    assert run(300) == {}
    assert run(301)["t1"].threshold_ms == 300_000
    shutdown = {"w@a": [TimelineEntry(at(5), SHUTDOWN)]}
    assert (
        classify_anomalies(
            [node], now=at(6), runtime_stats=stats, timeline=shutdown, settings=SMALL
        )
        .anomalies["t1"]
        .kind
        == AnomalyKind.INTERRUPTED
    )


def test_runtime_stats_with_too_few_samples_use_the_ceiling() -> None:
    node = _facts("t1", None, 0)

    def run(samples: int) -> int:
        report = classify_anomalies(
            [node],
            now=at(10_000),
            runtime_stats={"app.work": RuntimeStats(1000, samples)},
            timeline={},
            settings=SMALL,
        )
        return report.anomalies["t1"].threshold_ms or 0

    assert (run(2), run(3)) == (300_000, 60_000)


# --- The statistic and its window --------------------------------------------

# k 1, so the threshold is the statistic itself: runtimes 1..20 s give
# p50 10 s, p95 19 s (nearest rank, ceil(0.95 x 20) = 19th), max 20 s, mean 10.5 s.
P95_SETTINGS = AnomalySettings(
    stuck_factor=1,
    stuck_floor=timedelta(seconds=1),
    stuck_ceiling=timedelta(seconds=1000),
    stuck_min_samples=3,
)


@pytest.mark.parametrize(
    ("now", "flagged"),
    [
        pytest.param(15, False, id="p50 (10 s) would flag here"),
        pytest.param(19, False, id="exactly p95"),
        pytest.param(19.001, True, id="just past p95, before max (20 s)"),
    ],
)
def test_stuck_threshold_is_the_p95_of_the_names_runtimes(
    now: float, flagged: bool
) -> None:
    events = [*sample_runs("app.work", range(1, 21)), task_event("t1", S, 0)]

    report = classify(events, now=now, settings=P95_SETTINGS)

    assert set(report.anomalies) == ({"t1"} if flagged else set())
    if flagged:
        anomaly = report.anomalies["t1"]
        assert (anomaly.threshold_ms, anomaly.p95_ms, anomaly.runtime_samples) == (
            19_000,
            19_000,
            20,
        )


def test_samples_stamped_after_now_still_count_toward_the_p95() -> None:
    # A worker whose clock is ahead stamps its finished runs after the server's
    # "now"; the window is open-ended, so they still count.
    ahead = sample_runs("app.work", range(1, 21), at_s=3600)
    events = [*ahead, task_event("t1", S, 0)]

    anomaly = classify(events, now=100, settings=P95_SETTINGS).anomalies["t1"]

    assert (anomaly.threshold_ms, anomaly.runtime_samples) == (19_000, 20)
