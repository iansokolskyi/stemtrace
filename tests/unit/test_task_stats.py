"""Tests for the per-name and per-group task stats (``stats.task_stats``).

Spec: the "T6 spec" section of the backend plan (which runs count, the window,
nearest-rank percentiles). Each owner decision lives in one constant or function
of ``stemtrace.server.stats``; the tests below name the decision they pin.
"""

import math
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

from stemtrace.core.events import TaskState
from stemtrace.server.stats import (
    GroupStats,
    NameStats,
    RunRecord,
    Window,
    percentile,
    task_stats,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
EVERYTHING = Window(NOW - timedelta(days=365), NOW, open_end=True)


def _group_of(name: str) -> str:
    return name.split(".")[0]


def _run(
    name: str,
    state: TaskState = TaskState.SUCCESS,
    runtime_ms: int | None = None,
    *,
    age_s: int = 0,
    task_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        task_id=task_id or f"{name}-{state.value}-{runtime_ms}-{age_s}",
        name=name,
        state=state,
        last_updated=NOW - timedelta(seconds=age_s),
        runtime_ms=runtime_ms,
    )


def _stats(
    records: list[RunRecord],
    window: Window = EVERYTHING,
    name: str | None = None,
) -> tuple[dict[str, NameStats], dict[str, GroupStats]]:
    tasks, groups = task_stats(records, window, _group_of, name=name)
    return {t.name: t for t in tasks}, {g.name: g for g in groups}


def _oracle_nearest_rank(samples: list[int], percent: int) -> int:
    """The smallest sample with at least ``percent`` % of the samples at or below it.

    That is the definition of the nearest-rank percentile, written without the
    ``ceil(p*n) - 1`` index the implementation uses.
    """
    need = Fraction(percent, 100)
    n = len(samples)
    at_or_below = 0
    for value, count in sorted(Counter(samples).items()):
        at_or_below += count
        if Fraction(at_or_below, n) >= need:
            return value
    raise AssertionError("unreachable: all samples are at or below the maximum")


# --- percentiles ---------------------------------------------------------


def test_percentile_property_matches_the_nearest_rank_definition() -> None:
    """10k seeded cases, n from 1 to 500, with many ties, vs a naive oracle."""
    rng = random.Random(20261101)
    for _ in range(10_000):
        n = rng.randint(1, 500)
        spread = rng.choice([3, 50, 10_000])  # few distinct values means many ties
        samples = [rng.randint(0, spread) for _ in range(n)]
        for percent in (50, 95):
            got = percentile(sorted(samples), percent)
            assert got == _oracle_nearest_rank(samples, percent), (n, percent)


def test_percentile_of_no_samples_is_none() -> None:
    assert percentile([], 95) is None


def test_percentile_of_one_sample_is_that_sample() -> None:
    assert percentile([7], 50) == percentile([7], 95) == 7


def test_percentile_p95_of_20_samples_is_the_19th_not_the_20th() -> None:
    """Nearest rank is ceil(0.95 * 20) = 19; a floor rank would give the 20th."""
    assert percentile(list(range(1, 21)), 95) == 19
    assert percentile(list(range(1, 21)), 50) == 10


@pytest.mark.parametrize("n", range(1, 201))
def test_percentile_never_rounds_through_floats(n: int) -> None:
    """``0.95 * n`` is not exact for every n; the rank must come from integers."""
    samples = list(range(1, n + 1))
    assert percentile(samples, 95) == math.ceil(Fraction(95 * n, 100))
    assert percentile(samples, 50) == math.ceil(Fraction(n, 2))


# --- which runs count (D2, D3, D9) ---------------------------------------


def test_success_and_failure_runs_count_and_give_samples() -> None:
    tasks, _ = _stats(
        [
            _run("app.work", TaskState.SUCCESS, 100),
            _run("app.work", TaskState.SUCCESS, 300),
            _run("app.work", TaskState.FAILURE, 200),
        ]
    )

    row = tasks["app.work"]
    assert (row.runs, row.failures, row.runtime_samples) == (3, 1, 3)
    assert (row.p50_ms, row.p95_ms) == (200, 300)
    assert row.failure_rate == pytest.approx(1 / 3)


def test_ignored_runs_are_not_runs_but_still_set_last_run() -> None:
    tasks, _ = _stats(
        [
            _run("app.work", TaskState.SUCCESS, 10, age_s=60),
            _run("app.work", TaskState.IGNORED, age_s=5),
        ]
    )

    row = tasks["app.work"]
    assert (row.runs, row.failures, row.runtime_samples) == (1, 0, 1)
    assert row.last_run == NOW - timedelta(seconds=5)
    assert row.last_state == TaskState.IGNORED


def test_revoked_run_counts_but_gives_no_runtime_sample() -> None:
    """D2: an operator cut it short, so its runtime is not how long the task takes."""
    tasks, _ = _stats(
        [_run("app.work", TaskState.REVOKED, 5_000), _run("app.work", runtime_ms=40)]
    )

    row = tasks["app.work"]
    assert (row.runs, row.failures, row.runtime_samples) == (2, 0, 1)
    assert row.p95_ms == 40


def test_rejected_run_counts_but_is_not_a_failure() -> None:
    """D3: the same definition as ``/api/overview``."""
    tasks, _ = _stats([_run("app.work", TaskState.REJECTED, 9)])

    row = tasks["app.work"]
    assert (row.runs, row.failures, row.runtime_samples) == (1, 0, 0)
    assert (row.p50_ms, row.p95_ms) == (None, None)


@pytest.mark.parametrize(
    "state",
    [
        TaskState.PENDING,
        TaskState.RECEIVED,
        TaskState.STARTED,
        TaskState.RETRY,
    ],
)
def test_unfinished_run_is_not_a_run_but_sets_last_run(state: TaskState) -> None:
    tasks, _ = _stats([_run("app.work", state, age_s=3)])

    row = tasks["app.work"]
    assert (row.runs, row.failures, row.runtime_samples) == (0, 0, 0)
    assert row.failure_rate is None
    assert (row.last_run, row.last_state) == (NOW - timedelta(seconds=3), state)


def test_success_without_a_runtime_counts_as_a_run_without_a_sample() -> None:
    tasks, _ = _stats([_run("app.work", TaskState.SUCCESS, None)])

    row = tasks["app.work"]
    assert (row.runs, row.runtime_samples, row.p50_ms) == (1, 0, None)


def test_celery_builtins_are_left_out_of_rows_and_rollups() -> None:
    """D9: ``celery.*`` is never registered by a worker, so it isn't a task row."""
    tasks, groups = _stats(
        [
            _run("celery.chord_unlock", TaskState.FAILURE, 1),
            _run("celery.accumulate", TaskState.SUCCESS, 1),
            _run("app.work", TaskState.SUCCESS, 5),
        ]
    )

    assert set(tasks) == {"app.work"}
    assert set(groups) == {"app"}
    assert groups["app"].runs == 1


# --- the window (D4) -----------------------------------------------------


def test_run_is_placed_in_the_window_by_last_updated() -> None:
    window = Window(NOW - timedelta(hours=1), NOW, open_end=True)
    tasks, _ = _stats(
        [
            _run("app.work", runtime_ms=1, age_s=30),
            _run("app.work", runtime_ms=2, age_s=7200),
        ],
        window,
    )

    row = tasks["app.work"]
    assert (row.runs, row.runtime_samples) == (1, 1)
    assert row.p95_ms == 1


def test_run_stamped_after_now_lands_in_an_open_ended_window() -> None:
    """A worker clock ahead of the server's stamps events in the future."""
    window = Window(NOW - timedelta(hours=1), NOW, open_end=True)
    tasks, _ = _stats([_run("app.work", runtime_ms=1, age_s=-3600)], window)

    assert tasks["app.work"].runs == 1


def test_last_run_ignores_the_window_and_the_name_keeps_its_row() -> None:
    window = Window(NOW - timedelta(minutes=1), NOW, open_end=True)
    tasks, groups = _stats([_run("app.work", runtime_ms=1, age_s=3 * 3600)], window)

    row = tasks["app.work"]
    assert (row.runs, row.failures, row.failure_rate) == (0, 0, None)
    assert (row.p50_ms, row.p95_ms) == (None, None)
    assert row.last_run == NOW - timedelta(hours=3)
    assert row.last_state == TaskState.SUCCESS
    assert (groups["app"].runs, groups["app"].last_run) == (0, row.last_run)


def test_last_run_is_the_newest_run_of_any_state() -> None:
    tasks, _ = _stats(
        [
            _run("app.work", TaskState.SUCCESS, 1, age_s=100),
            _run("app.work", TaskState.FAILURE, 1, age_s=50),
            _run("app.work", TaskState.STARTED, age_s=10),
        ]
    )

    assert tasks["app.work"].last_state == TaskState.STARTED
    assert tasks["app.work"].last_run == NOW - timedelta(seconds=10)


# --- groups --------------------------------------------------------------


def test_group_percentiles_come_from_the_union_of_samples_not_a_mean() -> None:
    """One slow name (1 run) and one fast name (99 runs): the group p95 is fast."""
    records = [_run("app.slow", runtime_ms=1000, task_id="s")] + [
        _run("app.fast", runtime_ms=10, task_id=f"f{i}") for i in range(99)
    ]

    tasks, groups = _stats(records)

    assert (tasks["app.slow"].p95_ms, tasks["app.fast"].p95_ms) == (1000, 10)
    # A mean of the two names' p95s would be 505.
    assert groups["app"].p95_ms == 10
    assert groups["app"].runtime_samples == 100


def test_group_rollup_sums_runs_and_failures_and_takes_the_newest_last_run() -> None:
    tasks, groups = _stats(
        [
            _run("app.a", TaskState.SUCCESS, 5, age_s=100),
            _run("app.a", TaskState.FAILURE, 7, age_s=90),
            _run("app.b", TaskState.FAILURE, 9, age_s=10),
            _run("other.c", TaskState.SUCCESS, 1, age_s=1),
        ]
    )

    group = groups["app"]
    assert (group.runs, group.failures, group.task_count) == (3, 2, 2)
    assert group.failure_rate == pytest.approx(2 / 3)
    assert group.last_run == NOW - timedelta(seconds=10)
    assert tasks["app.a"].task_group == "app"
    assert groups["other"].task_count == 1


def test_group_failure_rate_is_none_without_runs() -> None:
    _, groups = _stats([_run("app.work", TaskState.STARTED)])

    assert (groups["app"].runs, groups["app"].failure_rate) == (0, None)


def test_name_filter_keeps_that_name_and_its_group_rolls_up_only_it() -> None:
    tasks, groups = _stats(
        [_run("app.a", runtime_ms=5), _run("app.b", runtime_ms=9)], name="app.b"
    )

    assert set(tasks) == {"app.b"}
    assert (groups["app"].runs, groups["app"].task_count) == (1, 1)


def test_name_filter_matches_the_whole_name_case_sensitively() -> None:
    tasks, groups = _stats([_run("app.add", runtime_ms=5)], name="app.ad")

    assert (tasks, groups) == ({}, {})


def test_no_records_gives_no_rows() -> None:
    assert task_stats([], EVERYTHING, _group_of) == ([], [])


def test_rows_and_groups_are_sorted_by_name() -> None:
    tasks, groups = task_stats(
        [_run("z.b"), _run("a.c"), _run("z.a")], EVERYTHING, _group_of
    )

    assert [t.name for t in tasks] == ["a.c", "z.a", "z.b"]
    assert [g.name for g in groups] == ["a", "z"]


def test_group_is_resolved_once_per_name() -> None:
    calls: list[str] = []

    def group_of(name: str) -> str:
        calls.append(name)
        return "g"

    task_stats(
        [_run("app.a", task_id=f"t{i}") for i in range(50)], EVERYTHING, group_of
    )

    assert calls == ["app.a"]
