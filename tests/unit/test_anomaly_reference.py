"""Sanity suite for the anomaly reference: one hand-built case per matrix cell.

Tests the reading of ``.context/features/anomaly-detection.md``, not production code.
"""

import itertools
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.events import (
    CANCELLED_RETRY_REASON,
    TaskEvent,
    TaskState,
    WorkerEvent,
    WorkerEventType,
)
from tests.unit.anomaly_reference import RefAnomaly, reference_anomalies

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
FLOOR = timedelta(seconds=10)
CEILING = timedelta(seconds=60)
FACTOR = 2.0
MIN_SAMPLES = 3
HOST = "celery@a"
OTHER = "celery@b"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def task(
    state: TaskState,
    seconds: float,
    task_id: str = "t",
    name: str = "app.work",
    host: str | None = HOST,
    **fields: object,
) -> TaskEvent:
    return TaskEvent.model_validate(
        {
            "task_id": task_id,
            "name": name,
            "state": state,
            "timestamp": at(seconds),
            "hostname": host,
            **fields,
        }
    )


def worker(
    kind: WorkerEventType, seconds: float, host: str = HOST, pid: int = 1
) -> WorkerEvent:
    return WorkerEvent(event_type=kind, hostname=host, pid=pid, timestamp=at(seconds))


SHUTDOWN = WorkerEventType.WORKER_SHUTDOWN
TERMINATING = WorkerEventType.WORKER_TERMINATING
READY = WorkerEventType.WORKER_READY


def classify(
    events: list[TaskEvent],
    workers: list[WorkerEvent] | None = None,
    now: float = 0,
    stats: Mapping[str, tuple[int | None, int]] | None = None,
    parents: dict[str, str | None] | None = None,
) -> dict[str, RefAnomaly]:
    return reference_anomalies(
        events,
        workers or [],
        stats or {},
        parents or {},
        at(now),
        stuck_factor=FACTOR,
        stuck_floor=FLOOR,
        stuck_ceiling=CEILING,
        stuck_min_samples=MIN_SAMPLES,
    )


def kind(result: dict[str, RefAnomaly], task_id: str = "t") -> str | None:
    return result[task_id].kind if task_id in result else None


# Row 1
def test_classify_received_only_far_past_threshold_flags_nothing() -> None:
    events = [task(TaskState.RECEIVED, 0)]
    assert classify(events, [worker(SHUTDOWN, 5)], now=10_000) == {}


# Row 2 and the strict boundary (T = ceiling = 60 s when n < N)
@pytest.mark.parametrize(
    ("now", "expected"),
    [(59, None), (60, None), (61, "stuck")],
    ids=["T-1s", "T-exact", "T+1s"],
)
def test_classify_started_around_ceiling_uses_strict_greater_than(
    now: float, expected: str | None
) -> None:
    result = classify([task(TaskState.STARTED, 0)], now=now)
    assert kind(result) == expected
    if expected:
        assert result["t"] == RefAnomaly("stuck", at(0), None, 60_000)


@pytest.mark.parametrize(
    ("now", "expected"),
    [(39, None), (40, None), (41, "stuck")],
    ids=["T-1s", "T-exact", "T+1s"],
)
def test_classify_started_with_enough_samples_uses_k_times_p95(
    now: float, expected: str | None
) -> None:
    stats = {"app.work": (20_000, 3)}  # T = 2 x 20 s = 40 s
    result = classify([task(TaskState.STARTED, 0)], now=now, stats=stats)
    assert kind(result) == expected
    if expected:
        assert result["t"].threshold_ms == 40_000


def test_classify_started_with_too_few_samples_uses_ceiling() -> None:
    stats = {"app.work": (1_000, 2)}  # n < N: T = ceiling, not max(2 s, floor)
    assert classify([task(TaskState.STARTED, 0)], now=30, stats=stats) == {}


def test_classify_small_p95_is_raised_to_floor() -> None:
    stats = {"app.work": (1_000, 5)}  # k x p95 = 2 s < floor 10 s
    assert kind(classify([task(TaskState.STARTED, 0)], now=10, stats=stats)) is None
    result = classify([task(TaskState.STARTED, 0)], now=11, stats=stats)
    assert result["t"].threshold_ms == 10_000


# Row 3
def test_classify_started_without_pending_follows_normal_rules() -> None:
    events = [task(TaskState.RECEIVED, 0), task(TaskState.STARTED, 1)]
    assert kind(classify(events, now=62)) == "stuck"
    assert kind(classify(events, [worker(SHUTDOWN, 5)], now=3)) == "interrupted"


# Row 4
def test_classify_success_after_worker_shutdown_clears_interrupted() -> None:
    started = task(TaskState.STARTED, 0)
    shutdown = worker(SHUTDOWN, 5)
    assert kind(classify([started], [shutdown], now=6)) == "interrupted"
    done = [started, task(TaskState.SUCCESS, 6)]
    assert classify(done, [shutdown], now=10_000) == {}


# Row 5
def test_classify_any_event_order_gives_same_answer() -> None:
    events = [
        task(TaskState.PENDING, 0, host=None),
        task(TaskState.RECEIVED, 1),
        task(TaskState.STARTED, 2),
    ]
    workers = [worker(READY, 0), worker(TERMINATING, 5)]
    expected = classify(events, workers, now=100)
    assert kind(expected) == "interrupted"
    for order in itertools.permutations(events):
        assert classify(list(order), list(reversed(workers)), now=100) == expected


# Rows 6 and 7
def test_classify_worker_clock_one_hour_ahead_stuck_fires_an_hour_late() -> None:
    events = [task(TaskState.STARTED, 3600)]  # server clock reads 0
    assert classify(events, now=3600 + 60) == {}
    assert kind(classify(events, now=3600 + 61)) == "stuck"
    assert classify(events, now=61) == {}  # one real minute in: not flagged


def test_classify_worker_clock_one_hour_behind_stuck_fires_an_hour_early() -> None:
    events = [task(TaskState.STARTED, -3600)]
    assert kind(classify(events, now=1)) == "stuck"  # real elapsed: 1 s


@pytest.mark.parametrize("skew", [-3600, 0, 3600])
def test_classify_interrupted_is_skew_free_on_one_host(skew: int) -> None:
    events = [task(TaskState.STARTED, skew)]
    after = classify(events, [worker(SHUTDOWN, skew + 5)], now=skew + 6)
    before = classify(events, [worker(SHUTDOWN, skew - 5)], now=skew + 6)
    assert kind(after) == "interrupted"
    assert before == {}


# Row 8 is unregistered only: not in scope. Row 9
def test_classify_no_hostname_is_stuck_but_never_interrupted() -> None:
    events = [task(TaskState.STARTED, 0, host=None)]
    workers = [worker(SHUTDOWN, 5)]
    assert classify(events, workers, now=10) == {}
    assert kind(classify(events, workers, now=100)) == "stuck"


# Row 10
def test_classify_started_with_no_terminal_from_old_worker_is_stuck() -> None:
    assert kind(classify([task(TaskState.STARTED, 0)], now=100)) == "stuck"


# Row 11
def test_classify_no_terminating_event_only_restart_gives_interrupted() -> None:
    events = [task(TaskState.STARTED, 0)]
    assert classify(events, [], now=10) == {}
    result = classify(events, [worker(READY, 20)], now=30)
    assert result["t"] == RefAnomaly("interrupted", at(20), "worker_restarted", None)


# Row 12
def _replace_delayed_root() -> list[TaskEvent]:
    return [
        task(TaskState.PENDING, 0, host=None),
        task(TaskState.STARTED, 1),
        # replacement PENDING: self-parented, ETA 100 s after the replaced STARTED
        task(
            TaskState.PENDING,
            1.1,
            host=None,
            parent_id="t",
            eta=at(101),
        ),
    ]


def test_classify_replace_delayed_root_rule_a_moves_progress_to_eta() -> None:
    events = _replace_delayed_root()
    assert classify(events, now=101 + 60) == {}
    result = classify(events, now=101 + 61)
    assert result["t"] == RefAnomaly("stuck", at(1), None, 60_000)


def test_classify_replace_evaluates_interrupted_on_replacement_started() -> None:
    events = [*_replace_delayed_root(), task(TaskState.STARTED, 102)]
    assert classify(events, [worker(SHUTDOWN, 50)], now=103) == {}
    assert kind(classify(events, [worker(SHUTDOWN, 103)], now=104)) == "interrupted"


def test_classify_sender_pending_with_original_parent_does_not_hide_stuck() -> None:
    events = [
        task(TaskState.PENDING, 5000, host=None, parent_id="p", eta=at(9000)),
        task(TaskState.STARTED, 0, parent_id="p"),
    ]
    assert kind(classify(events, now=61, parents={"t": "p"})) == "stuck"


# Row 12a
def test_classify_retry_received_on_host_ahead_delays_stuck_by_the_skew() -> None:
    events = [
        task(TaskState.STARTED, 0, retries=1, host=HOST),
        task(TaskState.RECEIVED, 3600, retries=1, host=OTHER, parent_id="t"),
    ]
    assert classify(events, now=3600 + 60) == {}
    assert kind(classify(events, now=3600 + 61)) == "stuck"


def test_classify_retry_received_on_same_host_cannot_hide_stuck() -> None:
    events = [
        task(TaskState.RECEIVED, -1, retries=1, parent_id="t", eta=at(0)),
        task(TaskState.STARTED, 0, retries=1),
    ]
    assert kind(classify(events, now=61)) == "stuck"


# Row 13
def test_classify_busy_reported_child_subtree_hides_stuck_parent() -> None:
    events = [
        task(TaskState.STARTED, 0, task_id="p"),
        task(TaskState.SUCCESS, 100, task_id="c", host=OTHER),
        task(TaskState.STARTED, 150, task_id="g", host=OTHER),
    ]
    parents: dict[str, str | None] = {"p": None, "c": "p", "g": "c"}
    assert "p" in classify(events[:1], now=61, parents=parents)
    # child c progressed at 100: within T at now = 160
    assert "p" not in classify(events[:2], now=160, parents=parents)
    assert "p" in classify(events[:2], now=161, parents=parents)
    # grandchild g progressed at 150: hides p until 210
    assert "p" not in classify(events, now=210, parents=parents)
    assert "p" in classify(events, now=211, parents=parents)


def test_classify_late_stamped_attempt0_pending_does_not_count_as_child_progress() -> (
    None
):
    events = [
        task(TaskState.STARTED, 0, task_id="p"),
        task(TaskState.STARTED, 10, task_id="c", host=OTHER),
        task(TaskState.PENDING, 500, task_id="c", host=None),  # lifecycle: first
    ]
    result = classify(events, now=71, parents={"c": "p"})
    assert "p" in result  # c's last_updated is 10, not 500


# Row 14 and 32
@pytest.mark.parametrize(
    "terminal",
    [TaskState.IGNORED, TaskState.REJECTED, TaskState.REVOKED, TaskState.FAILURE],
)
def test_classify_terminal_state_flags_nothing(terminal: TaskState) -> None:
    events = [task(TaskState.STARTED, 0), task(terminal, 1, retries=0)]
    assert classify(events, [worker(SHUTDOWN, 5)], now=10_000) == {}


def test_classify_revoked_before_start_flags_nothing() -> None:
    events = [task(TaskState.PENDING, 0, host=None), task(TaskState.REVOKED, 1)]
    assert classify(events, [worker(SHUTDOWN, 5)], now=10_000) == {}


# Row 15
def test_classify_retry_countdown_flags_nothing() -> None:
    events = [
        task(TaskState.STARTED, 0),
        task(TaskState.RETRY, 1, exception="boom", eta=at(30)),
    ]
    assert classify(events, [worker(SHUTDOWN, 5)], now=10_000) == {}


def test_classify_cancelled_retry_with_host_evidence_is_interrupted() -> None:
    events = [
        task(TaskState.STARTED, 0),
        task(TaskState.RETRY, 1, exception=CANCELLED_RETRY_REASON),
    ]
    result = classify(events, [worker(SHUTDOWN, 5)], now=10)
    assert result["t"] == RefAnomaly("interrupted", at(5), "cancelled_retry", None)


# Row 16
def test_classify_retry_with_long_past_eta_and_no_next_started_flags_nothing() -> None:
    events = [
        task(TaskState.STARTED, 0),
        task(TaskState.RETRY, 1, exception="boom", eta=at(2)),
    ]
    assert classify(events, now=100_000) == {}


# Row 17
def test_classify_cancelled_retry_without_host_evidence_flags_nothing() -> None:
    events = [
        task(TaskState.STARTED, 0),
        task(TaskState.RETRY, 1, exception=CANCELLED_RETRY_REASON),
    ]
    assert classify(events, [worker(SHUTDOWN, 5, host=OTHER)], now=100_000) == {}


def test_classify_cancelled_retry_without_its_started_is_not_eligible() -> None:
    events = [task(TaskState.RETRY, 1, retries=2, exception=CANCELLED_RETRY_REASON)]
    other_attempt = [task(TaskState.STARTED, 0, retries=1), *events]
    assert classify(events, [worker(SHUTDOWN, 5)], now=10) == {}
    assert classify(other_attempt, [worker(SHUTDOWN, 5)], now=10) == {}


# Row 18
def test_classify_acks_late_redelivery_clears_interrupted_and_restarts_clock() -> None:
    first = task(TaskState.STARTED, 0, host=HOST)
    shutdown = worker(SHUTDOWN, 5)
    assert kind(classify([first], [shutdown], now=10)) == "interrupted"
    redelivered = task(TaskState.STARTED, 100, host=OTHER)
    result = classify([first, redelivered], [shutdown], now=100 + 60)
    assert result == {}
    result = classify([first, redelivered], [shutdown], now=100 + 61)
    assert result["t"] == RefAnomaly("stuck", at(100), None, 60_000)


# Row 19
def test_classify_redelivery_on_host_behind_leaves_old_started_as_state() -> None:
    first = task(TaskState.STARTED, 0, host=HOST)
    behind = task(TaskState.STARTED, -3000, host=OTHER)
    result = classify([first, behind], [worker(SHUTDOWN, 5)], now=10)
    assert result["t"].kind == "interrupted"


# Row 20
def test_classify_warm_restart_flags_nothing() -> None:
    events = [task(TaskState.STARTED, 0), task(TaskState.SUCCESS, 4)]
    assert classify(events, [worker(SHUTDOWN, 5), worker(READY, 8)], now=100_000) == {}


# Row 21
@pytest.mark.parametrize("retry_at", [4.0, 6.0], ids=["retry-before", "retry-after"])
def test_classify_cold_stop_is_interrupted_whatever_the_retry_stamp(
    retry_at: float,
) -> None:
    events = [
        task(TaskState.STARTED, 0),
        task(TaskState.RETRY, retry_at, exception=CANCELLED_RETRY_REASON),
    ]
    result = classify(events, [worker(TERMINATING, 5)], now=10)
    assert result["t"] == RefAnomaly("interrupted", at(5), "cancelled_retry", None)


def test_classify_cold_stop_without_cancel_retry_names_worker_terminating() -> None:
    result = classify([task(TaskState.STARTED, 0)], [worker(TERMINATING, 5)], now=10)
    assert result["t"] == RefAnomaly("interrupted", at(5), "worker_terminating", None)


# Row 22 and 23
def test_classify_sigkill_then_restart_is_stuck_before_and_interrupted_after() -> None:
    events = [task(TaskState.STARTED, 0)]
    assert kind(classify(events, now=61)) == "stuck"
    restart = [worker(READY, 100)]
    result = classify(events, restart, now=101)
    assert result["t"] == RefAnomaly("interrupted", at(100), "worker_restarted", None)


def test_classify_sigkill_without_restart_is_stuck_only_after_threshold() -> None:
    events = [task(TaskState.STARTED, 0)]
    assert classify(events, now=60) == {}
    assert kind(classify(events, now=61)) == "stuck"


# Row 24
def test_classify_same_nodename_on_two_machines_gives_false_interrupted() -> None:
    events = [task(TaskState.STARTED, 0)]
    result = classify(events, [worker(READY, 3, pid=999)], now=4)
    assert kind(result) == "interrupted"


# Row 29: the reference keeps every worker event, so `since` is the earliest later one.
def test_classify_many_worker_events_since_is_the_earliest_after_started() -> None:
    events = [task(TaskState.STARTED, 10)]
    workers = [worker(READY, s) for s in range(0, 60, 2)]
    result = classify(events, workers, now=70)
    assert result["t"].since == at(12)


def test_classify_equal_timestamp_worker_events_pick_ready_type_first() -> None:
    events = [task(TaskState.STARTED, 0)]
    workers = [worker(SHUTDOWN, 5), worker(READY, 5)]
    assert classify(events, workers, now=6)["t"].evidence == "worker_restarted"


# Row 30
def test_classify_celery_builtin_uses_ceiling_even_with_stats() -> None:
    events = [task(TaskState.STARTED, 0, name="celery.chord_unlock")]
    stats = {"celery.chord_unlock": (1_000, 99)}
    assert classify(events, now=60, stats=stats) == {}
    assert classify(events, now=61, stats=stats)["t"].threshold_ms == 60_000
    result = classify(events, [worker(SHUTDOWN, 5)], now=6, stats=stats)
    assert result["t"].kind == "interrupted"


# Priority and common rules
def test_classify_interrupted_wins_over_stuck() -> None:
    result = classify([task(TaskState.STARTED, 0)], [worker(SHUTDOWN, 5)], now=1000)
    assert result["t"].kind == "interrupted"


def test_classify_naive_timestamps_are_read_as_utc() -> None:
    naive = TaskEvent(
        task_id="t",
        name="app.work",
        state=TaskState.STARTED,
        timestamp=datetime(2026, 1, 1, 12, 0),
        hostname=HOST,
    )
    assert classify([naive], now=61)["t"].since == at(0)


def test_classify_higher_retry_attempt_decides_state() -> None:
    events = [
        task(TaskState.STARTED, 0, retries=0),
        task(TaskState.RETRY, 1, retries=0, exception="boom"),
        task(TaskState.STARTED, 30, retries=1),
    ]
    assert classify(events, now=30 + 61)["t"].since == at(30)
