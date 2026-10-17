"""Tests for the summary aggregates (server/stats.py)."""

from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.events import TaskState
from stemtrace.server.stats import (
    FAILURE_SAMPLES,
    MAX_BUCKETS,
    RunRecord,
    Window,
    bucket_starts,
    failure_groups,
    parse_duration,
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


def test_window_stats_counts_finished_runs_and_failures_only() -> None:
    window = Window(T0, T0 + timedelta(minutes=10))
    records = [
        _run("a", TaskState.SUCCESS, T0),
        _run("b", TaskState.FAILURE, T0 + timedelta(minutes=1)),
        _run("c", TaskState.REVOKED, T0 + timedelta(minutes=2)),
        _run("d", TaskState.REJECTED, T0 + timedelta(minutes=3)),
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


# --- failure_groups ---------------------------------------------------------


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
