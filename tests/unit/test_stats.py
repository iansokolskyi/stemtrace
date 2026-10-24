"""Tests for the summary aggregates (server/stats.py)."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.events import TaskState
from stemtrace.core.graph import TERMINAL_STATES, NodeType, TaskNode
from stemtrace.server.stats import (
    FAILURE_SAMPLES,
    MAX_BUCKETS,
    OUTCOME_STATES,
    FailureGroup,
    FailureTally,
    RunRecord,
    Window,
    auto_bucket,
    bucket_starts,
    collect_failures,
    parse_duration,
    summarize_failures,
    time_series,
    window_stats,
)

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _run(
    task_id: str,
    state: TaskState,
    at: datetime,
    name: str = "app.work",
    exception_type: str | None = None,
) -> RunRecord:
    return RunRecord(
        task_id=task_id,
        name=name,
        state=state,
        last_updated=at,
        exception_type=exception_type,
    )


# --- parse_duration ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30s", 30),
        ("1m", 60),
        ("15m", 900),
        ("6h", 21600),
        ("7d", 604800),
        (" 2h ", 7200),
    ],
)
def test_parse_duration_accepts_units(text: str, seconds: int) -> None:
    assert parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "text", ["", "0m", "-5m", "5", "m", "1.5h", "5w", "1h30m", "5 minutes"]
)
def test_parse_duration_rejects_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="positive duration"):
        parse_duration(text)


# --- Window -----------------------------------------------------------------


def test_window_includes_start_and_excludes_end() -> None:
    window = Window(T0, T0 + timedelta(hours=1))

    assert window.contains(T0) is True
    assert window.contains(T0 + timedelta(minutes=59, seconds=59)) is True
    assert window.contains(T0 + timedelta(hours=1)) is False
    assert window.contains(T0 - timedelta(microseconds=1)) is False


def test_open_ended_window_takes_runs_after_its_end() -> None:
    window = Window(T0, T0 + timedelta(hours=1), open_end=True)

    assert window.contains(T0 + timedelta(hours=5)) is True
    assert window.contains(T0 - timedelta(seconds=1)) is False


def test_previous_window_is_equal_length_just_before_and_closed() -> None:
    window = Window(T0, T0 + timedelta(hours=1), open_end=True)

    assert window.previous() == Window(T0 - timedelta(hours=1), T0, open_end=False)


# --- window_stats -----------------------------------------------------------


def test_window_stats_counts_runs_with_an_outcome_and_failures_only() -> None:
    window = Window(T0, T0 + timedelta(minutes=10))
    records = [
        _run("a", TaskState.SUCCESS, T0),
        _run("b", TaskState.FAILURE, T0 + timedelta(minutes=1)),
        _run("c", TaskState.REVOKED, T0 + timedelta(minutes=2)),
        _run("d", TaskState.REJECTED, T0 + timedelta(minutes=3)),
        _run("d2", TaskState.IGNORED, T0 + timedelta(minutes=3)),
        _run("e", TaskState.STARTED, T0 + timedelta(minutes=4)),
        _run("f", TaskState.RETRY, T0 + timedelta(minutes=5)),
        _run("g", TaskState.PENDING, T0 + timedelta(minutes=6)),
        _run("h", TaskState.SUCCESS, T0 + timedelta(minutes=10)),  # at end: out
        _run("i", TaskState.FAILURE, T0 - timedelta(seconds=1)),  # before: out
    ]

    stats = window_stats(records, window)

    assert (stats.runs, stats.failures) == (4, 1)
    assert stats.runs_per_minute == pytest.approx(0.4)
    assert stats.failure_rate == pytest.approx(0.25)


def test_failure_rate_ignores_skipped_tasks_in_a_dedup_heavy_window() -> None:
    """Ignore() means the task chose not to run: 90 of them mustn't dilute 5 failures."""
    window = Window(T0, T0 + timedelta(minutes=10))
    states = (
        [TaskState.IGNORED] * 90 + [TaskState.FAILURE] * 5 + [TaskState.SUCCESS] * 5
    )
    records = [
        _run(f"t{i}", state, T0 + timedelta(seconds=i))
        for i, state in enumerate(states)
    ]

    stats = window_stats(records, window)

    assert (stats.runs, stats.failures) == (10, 5)
    assert stats.failure_rate == pytest.approx(0.5)


def test_outcome_states_are_the_terminal_states_but_ignored() -> None:
    assert {
        TaskState.SUCCESS,
        TaskState.FAILURE,
        TaskState.REVOKED,
        TaskState.REJECTED,
    } == OUTCOME_STATES
    assert TaskState.IGNORED in TERMINAL_STATES


def test_window_stats_with_no_runs_has_zero_rates() -> None:
    stats = window_stats([], Window(T0, T0 + timedelta(hours=1)))

    assert (stats.runs, stats.runs_per_minute, stats.failure_rate) == (0, 0.0, 0.0)


# --- bucket_starts / time_series --------------------------------------------


def test_bucket_starts_align_to_epoch_multiples() -> None:
    start = T0 + timedelta(minutes=7, seconds=13)
    starts = bucket_starts(start, start + timedelta(minutes=12), timedelta(minutes=5))

    assert starts == [
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=10),
        T0 + timedelta(minutes=15),
    ]


def test_bucket_starts_are_stable_between_requests() -> None:
    a = bucket_starts(
        T0 + timedelta(seconds=1), T0 + timedelta(hours=1), timedelta(minutes=5)
    )
    b = bucket_starts(
        T0 + timedelta(seconds=40),
        T0 + timedelta(hours=1, seconds=40),
        timedelta(minutes=5),
    )

    assert a == b


def test_bucket_starts_reject_too_many() -> None:
    bucket = timedelta(minutes=1)
    ok = bucket_starts(T0, T0 + bucket * (MAX_BUCKETS - 1), bucket)

    assert len(ok) == MAX_BUCKETS
    with pytest.raises(ValueError, match=f"at most {MAX_BUCKETS}"):
        bucket_starts(T0, T0 + bucket * MAX_BUCKETS, bucket)


def test_time_series_places_runs_by_last_update_and_state() -> None:
    bucket = timedelta(minutes=5)
    records = [
        _run("a", TaskState.SUCCESS, T0),  # first edge: bucket 0
        _run("b", TaskState.SUCCESS, T0 + timedelta(minutes=4, seconds=59)),  # bucket 0
        _run("c", TaskState.FAILURE, T0 + timedelta(minutes=5)),  # next edge: bucket 1
        _run("d", TaskState.RETRY, T0 + timedelta(minutes=9)),  # bucket 1
        _run("e", TaskState.SUCCESS, T0 + timedelta(hours=3)),  # future: last bucket
        _run("f", TaskState.FAILURE, T0 - timedelta(seconds=1)),  # before start: out
    ]

    series = time_series(
        records, start=T0, end=T0 + timedelta(minutes=10), bucket=bucket
    )

    assert series.starts == [T0, T0 + bucket, T0 + 2 * bucket]
    assert series.counts == {
        TaskState.SUCCESS: [2, 0, 1],
        TaskState.FAILURE: [0, 1, 0],
        TaskState.RETRY: [0, 1, 0],
    }
    assert series.by_key == {}


def test_time_series_splits_by_key() -> None:
    records = [
        _run("a", TaskState.SUCCESS, T0, name="billing.charge"),
        _run("b", TaskState.FAILURE, T0, name="search.reindex"),
        _run("c", TaskState.FAILURE, T0 + timedelta(minutes=5), name="billing.refund"),
    ]

    series = time_series(
        records,
        start=T0,
        end=T0 + timedelta(minutes=5),
        bucket=timedelta(minutes=5),
        key_of=lambda r: r.name.split(".")[0],
    )

    assert series.by_key == {
        "billing": {TaskState.SUCCESS: [1, 0], TaskState.FAILURE: [0, 1]},
        "search": {TaskState.FAILURE: [1, 0]},
    }
    assert series.counts == {TaskState.SUCCESS: [1, 0], TaskState.FAILURE: [1, 1]}


# --- failure groups -----------------------------------------------------------


def failure_groups(
    records: list[RunRecord], window: Window, *, slice_size: int = 10**9
) -> list[FailureGroup]:
    """Collect ``records`` in slices of ``slice_size``, as the route does."""
    by_type: dict[str | None, FailureTally] = {}
    for start in range(0, len(records), slice_size):
        collect_failures(by_type, records[start : start + slice_size], window)
    return summarize_failures(by_type)


def _reference_groups(records: list[RunRecord], window: Window) -> list[FailureGroup]:
    """The spec: sort every failure of a type (what the tallies must equal)."""
    by_type: dict[str | None, list[RunRecord]] = {}
    for r in records:
        if r.state == TaskState.FAILURE and window.contains(r.last_updated):
            by_type.setdefault(r.exception_type, []).append(r)
    groups = []
    for exception_type, failed in by_type.items():
        failed.sort(key=lambda r: (r.last_updated, r.task_id), reverse=True)
        groups.append(
            FailureGroup(
                exception_type=exception_type,
                count=len(failed),
                first_seen=failed[-1].last_updated,
                last_seen=failed[0].last_updated,
                task_names=tuple(sorted({r.name for r in failed})),
                sample_task_ids=tuple(r.task_id for r in failed[:FAILURE_SAMPLES]),
            )
        )
    groups.sort(key=lambda g: (-g.count, -g.last_seen.timestamp()))
    return groups


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("slice_size", [1, 5, 10**9])
def test_failure_groups_equal_the_sorting_reference_on_random_runs(
    seed: int, slice_size: int
) -> None:
    """Ties on time (task ID breaks them), few and many types, names and states."""
    rng = random.Random(seed)
    states = [TaskState.FAILURE] * 3 + [TaskState.SUCCESS, TaskState.RETRY]
    records = [
        _run(
            f"t{rng.randrange(10**6)}-{i}",
            rng.choice(states),
            T0 + timedelta(minutes=rng.randrange(-5, 12)),
            f"app.n{rng.randrange(6)}",
            rng.choice(["A", "B", "C", None][: rng.randrange(1, 5)]),
        )
        for i in range(rng.randrange(0, 60))
    ]
    window = Window(T0, T0 + timedelta(minutes=8), open_end=bool(seed % 2))

    assert failure_groups(records, window, slice_size=slice_size) == _reference_groups(
        records, window
    )


def test_failure_groups_group_by_exception_most_first() -> None:
    window = Window(T0, T0 + timedelta(hours=1), open_end=True)
    records = [
        _run("v1", TaskState.FAILURE, T0 + timedelta(minutes=1), "a.x", "ValueError"),
        _run("v2", TaskState.FAILURE, T0 + timedelta(minutes=9), "b.y", "ValueError"),
        _run("k1", TaskState.FAILURE, T0 + timedelta(minutes=5), "a.x", "KeyError"),
        _run("n1", TaskState.FAILURE, T0 + timedelta(minutes=2), "a.x", None),
        _run("ok", TaskState.SUCCESS, T0 + timedelta(minutes=3), "a.x", "ValueError"),
        _run("r1", TaskState.RETRY, T0 + timedelta(minutes=3), "a.x", "ValueError"),
        _run("old", TaskState.FAILURE, T0 - timedelta(minutes=1), "a.x", "KeyError"),
    ]

    groups = failure_groups(records, window)

    assert [(g.exception_type, g.count) for g in groups] == [
        ("ValueError", 2),
        ("KeyError", 1),  # ties broken by latest failure: KeyError at :05
        (None, 1),
    ]
    first = groups[0]
    assert first.first_seen == T0 + timedelta(minutes=1)
    assert first.last_seen == T0 + timedelta(minutes=9)
    assert first.task_names == ("a.x", "b.y")
    assert first.sample_task_ids == ("v2", "v1")


def test_failure_groups_keep_the_latest_samples() -> None:
    window = Window(T0, T0 + timedelta(hours=1), open_end=True)
    records = [
        _run(f"t{i}", TaskState.FAILURE, T0 + timedelta(minutes=i), "a.x", "E")
        for i in range(FAILURE_SAMPLES + 3)
    ]

    (group,) = failure_groups(records, window)

    assert group.count == FAILURE_SAMPLES + 3
    assert group.sample_task_ids == tuple(
        f"t{i}" for i in range(FAILURE_SAMPLES + 2, 2, -1)
    )


def test_parse_duration_caps_at_ten_years() -> None:
    assert parse_duration("3650d") == timedelta(days=3650)
    with pytest.raises(ValueError, match="at most 3650d"):
        parse_duration("3651d")
    with pytest.raises(ValueError, match="at most 3650d"):
        parse_duration("9999999999d")


@pytest.mark.parametrize(
    ("span", "bucket"),
    [
        (timedelta(0), timedelta(minutes=1)),
        (timedelta(hours=23), timedelta(minutes=1)),
        (timedelta(days=2), timedelta(minutes=5)),
        (timedelta(days=6), timedelta(minutes=15)),
        (timedelta(days=30), timedelta(hours=1)),
        (timedelta(days=3650), timedelta(days=7)),
    ],
)
def test_auto_bucket_picks_the_smallest_that_fits(
    span: timedelta, bucket: timedelta
) -> None:
    start = T0 - span
    chosen = auto_bucket(start, T0)

    assert chosen == bucket
    assert len(bucket_starts(start, T0, chosen)) <= MAX_BUCKETS


def test_bucket_starts_reject_a_start_after_the_end() -> None:
    with pytest.raises(ValueError, match="after now"):
        bucket_starts(T0 + timedelta(seconds=1), T0, timedelta(minutes=5))


def test_run_record_only_for_task_nodes_with_events() -> None:
    task = TaskNode(task_id="t", name="app.work", state=TaskState.PENDING)
    group = TaskNode(
        task_id="group:g",
        name="group",
        state=TaskState.SUCCESS,
        node_type=NodeType.GROUP,
    )

    assert RunRecord.from_node(task) is None  # no events yet
    assert RunRecord.from_node(group) is None
