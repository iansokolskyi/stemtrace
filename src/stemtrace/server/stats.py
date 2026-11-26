"""Aggregates over the store's run records: KPIs, time series, failure groups, task stats.

Pure functions over :class:`RunRecord` lists. The store hands out a copy of its
records (see ``GraphStore.run_records``), so everything here runs outside the
store lock.

Every aggregate places a run at one time, its ``last_updated`` (its last event
in lifecycle order, which can be a later event that lost to its state, terminal
or not), and counts it under its current state.
"""

from __future__ import annotations

import heapq
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final

from stemtrace.core.events import CANCELLED_RETRY_REASON, TaskState
from stemtrace.core.graph import TERMINAL_STATES, NodeType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from stemtrace.core.graph import TaskNode


#: States that end a run with an outcome: the overview's runs, and what its
#: failure rate divides by. IGNORED is terminal but left out: ``Ignore()`` means
#: the task chose not to run (dedup, locks), and counting it would hide real
#: failures behind skipped work. Time series still count IGNORED by state.
OUTCOME_STATES: Final[frozenset[TaskState]] = TERMINAL_STATES - {TaskState.IGNORED}

#: Most buckets one time-series request may ask for.
MAX_BUCKETS: Final[int] = 1440

#: Most sample task IDs listed per failure group.
FAILURE_SAMPLES: Final[int] = 5

#: Longest failure message a run record keeps, after the cut to its first line.
MAX_MESSAGE_LENGTH: Final[int] = 300

#: Longest window or bucket (10 years). Summary windows start at most this far
#: back, so even the largest automatic bucket (7d) fits in ``MAX_BUCKETS`` and
#: bucket edges stay within the dates Python can represent.
MAX_DURATION: Final[timedelta] = timedelta(days=3650)

#: Bucket sizes tried, smallest first, when a time series doesn't name one.
AUTO_BUCKETS: Final[tuple[timedelta, ...]] = tuple(
    timedelta(seconds=s) for s in (60, 300, 900, 3600, 21600, 86400, 604800)
)

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_SECONDS: Final[dict[str, int]] = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True, slots=True)
class RunRecord:
    """What the aggregates need to know about one task run (a TASK node).

    Immutable, so the store can hand out references without copying.
    """

    task_id: str
    name: str
    state: TaskState
    last_updated: datetime
    # The exception class of a FAILURE run; None for other states.
    exception_type: str | None = None
    # Milliseconds of the last run of a terminal task; None before it ends or
    # when its STARTED is missing (see ``TaskNode.runtime_ms``).
    runtime_ms: int | None = None
    # Milliseconds the first attempt waited before starting; set on the same
    # terminal-only gate as ``runtime_ms`` (see ``TaskNode.queue_wait_ms``).
    queue_wait_ms: int | None = None
    # First line (at most ``MAX_MESSAGE_LENGTH`` characters) of the exception text
    # of the event that gave ``exception_type``; FAILURE runs only.
    exception_message: str | None = None
    # The state-deciding event's time and nodename (None for an eager task or
    # an old worker), whether it is a cold-stop cancelled RETRY, and the parent
    # the task's events reported.
    state_since: datetime | None = None
    state_host: str | None = None
    cancelled: bool = False
    reported_parent_id: str | None = None
    # Latest STARTED with the state event's ``retries`` (None if there is none;
    # a main-process terminal event has retries=0, so then it is attempt 0's),
    # and when a ``replace()`` replacement was due (None if none was sent).
    attempt_started: datetime | None = None
    replaced_progress: datetime | None = None

    @classmethod
    def from_node(cls, node: TaskNode) -> RunRecord | None:
        """Build the record of a TASK node; None for GROUP/CHORD or an empty node."""
        current, last_updated = node.state_event, node.last_updated
        if node.node_type != NodeType.TASK or current is None or last_updated is None:
            return None
        return cls(
            task_id=node.task_id,
            name=node.name,
            state=node.state,
            last_updated=last_updated,
            # Only failure groups read these. Reading them scans all of the node's
            # events, and this runs under the store lock on every event, so a
            # task retrying thousands of times would make ingest quadratic.
            exception_type=node.exception_type
            if node.state == TaskState.FAILURE
            else None,
            exception_message=_first_line(node.exception_text)
            if node.state == TaskState.FAILURE
            else None,
            # Same reason: run_times() scans and sorts every event, so only a
            # terminal task reads it: no calls while it runs, then one per event
            # that arrives after the terminal one (usually none).
            runtime_ms=node.runtime_ms if node.state in TERMINAL_STATES else None,
            # The wait scans the events too, and a late sent event moves it, so it
            # is read on every event of a terminal task and never carried forward
            # from STARTED.
            queue_wait_ms=node.queue_wait_ms if node.state in TERMINAL_STATES else None,
            # The rest is O(1): the node keeps the state event and per-attempt
            # and per-parent times as it ingests, so no event scan.
            state_since=current.timestamp,
            state_host=current.hostname,
            cancelled=current.state == TaskState.RETRY
            and current.exception == CANCELLED_RETRY_REASON,
            reported_parent_id=node.reported_parent_id,
            attempt_started=node.attempt_started,
            replaced_progress=node.replaced_progress,
        )


# A line that is only the class prefix, as in ``ValueError("\nreal cause")``.
_BARE_PREFIX = re.compile(r"[\w.]+:")


def _first_line(text: str | None) -> str | None:
    """The first line with content, cut to ``MAX_MESSAGE_LENGTH`` characters.

    Blank lines and a line holding only the ``Class:`` prefix are skipped;
    ``None`` when no line is left.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not _BARE_PREFIX.fullmatch(line):
            return line[:MAX_MESSAGE_LENGTH]
    return None


def parse_duration(text: str) -> timedelta:
    """Parse a relative window or bucket size: ``30s``, ``15m``, ``6h``, ``7d``.

    Raises:
        ValueError: Unless it is a positive whole number with one of the
            units s, m, h, d, and at most ``MAX_DURATION``.
    """
    match = _DURATION.match(text)
    if match is None or int(match.group(1)) <= 0:
        raise ValueError(
            f"expected a positive duration like 30s, 15m, 6h or 7d, got {text!r}"
        )
    seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    if seconds > MAX_DURATION.total_seconds():
        raise ValueError(f"at most {MAX_DURATION.days}d, got {text!r}")
    return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class Window:
    """A time range ``[start, end)``.

    An open-ended window (``open_end``) also takes runs stamped at or after
    ``end``: the latest window ends "now", and a worker whose clock is ahead
    stamps events in the future.
    """

    start: datetime
    end: datetime
    open_end: bool = False

    def contains(self, moment: datetime) -> bool:
        """Whether ``moment`` falls in the window."""
        return moment >= self.start and (self.open_end or moment < self.end)

    def previous(self) -> Window:
        """The window of the same length just before this one (closed at its end)."""
        return Window(self.start - (self.end - self.start), self.start)


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Runs with an outcome (``OUTCOME_STATES``) in one window."""

    runs: int
    failures: int
    minutes: float

    @property
    def runs_per_minute(self) -> float:
        """Runs with an outcome per minute of the window."""
        return self.runs / self.minutes if self.minutes > 0 else 0.0

    @property
    def failure_rate(self) -> float:
        """FAILURE runs over runs with an outcome; 0.0 when there are none."""
        return self.failures / self.runs if self.runs else 0.0


def window_stats(records: Iterable[RunRecord], window: Window) -> WindowStats:
    """Count the runs with an outcome, and the failures, in ``window``."""
    runs = failures = 0
    for record in records:
        if record.state in OUTCOME_STATES and window.contains(record.last_updated):
            runs += 1
            failures += record.state == TaskState.FAILURE
    minutes = (window.end - window.start).total_seconds() / 60
    return WindowStats(runs=runs, failures=failures, minutes=minutes)


@dataclass(slots=True)
class TimeSeries:
    """Runs by state per bucket, overall and optionally per key (task group).

    ``counts[state][i]`` is the number of runs in bucket ``i`` whose current
    state is ``state``; states with no run in range are left out.
    """

    starts: list[datetime]
    bucket: timedelta
    counts: dict[TaskState, list[int]] = field(default_factory=dict)
    by_key: dict[str, dict[TaskState, list[int]]] = field(default_factory=dict)


def auto_bucket(start: datetime, end: datetime) -> timedelta:
    """The smallest of ``AUTO_BUCKETS`` that covers ``[start, end]`` in ``MAX_BUCKETS``."""
    span = max(end.timestamp() - start.timestamp(), 0.0)
    return next(
        (b for b in AUTO_BUCKETS if span // b.total_seconds() + 2 <= MAX_BUCKETS),
        AUTO_BUCKETS[-1],
    )


def bucket_starts(start: datetime, end: datetime, bucket: timedelta) -> list[datetime]:
    """Bucket start times covering ``[start, end]``, aligned to multiples of ``bucket``.

    Alignment is to the Unix epoch, so two requests a few seconds apart get
    the same edges.

    Raises:
        ValueError: If ``start`` is after ``end``, or more than ``MAX_BUCKETS``
            buckets would be needed.
    """
    if start > end:
        raise ValueError("the window starts after now")
    size = bucket.total_seconds()
    first = int(start.timestamp() // size)
    last = int(end.timestamp() // size)
    count = last - first + 1
    if count > MAX_BUCKETS:
        raise ValueError(
            f"{count} buckets of {int(size)}s needed; at most {MAX_BUCKETS}: "
            "use a larger bucket or a shorter window"
        )
    return [
        datetime.fromtimestamp((first + i) * size, tz=timezone.utc)
        for i in range(count)
    ]


def time_series(
    records: Iterable[RunRecord],
    *,
    start: datetime,
    end: datetime,
    bucket: timedelta,
    key_of: Callable[[RunRecord], str] | None = None,
) -> TimeSeries:
    """Runs by current state per bucket from ``start`` to ``end``.

    The last bucket is open-ended (runs stamped after ``end`` by a clock
    ahead of the server's land there); runs before the first bucket are left
    out.

    Args:
        records: Run records.
        start: Start of the range (inclusive; rounded down to a bucket edge).
        end: End of the range, normally now.
        bucket: Bucket size.
        key_of: Also split counts by this key (e.g. the task group).

    Raises:
        ValueError: If more than ``MAX_BUCKETS`` buckets would be needed.
    """
    series = TimeSeries(starts=bucket_starts(start, end, bucket), bucket=bucket)
    count_runs(series, records, key_of=key_of)
    return series


def bucket_placer(series: TimeSeries) -> Callable[[RunRecord], int | None]:
    """A function giving a record's bucket in ``series``; None before the first bucket.

    The last bucket takes everything after it, so a run stamped in the future lands there.
    """
    first = series.starts[0].timestamp()
    size = series.bucket.total_seconds()
    last = len(series.starts) - 1

    def place(record: RunRecord) -> int | None:
        """The bucket of ``record``'s ``last_updated``."""
        offset = record.last_updated.timestamp() - first
        return None if offset < 0 else min(int(offset // size), last)

    return place


def count_runs(
    series: TimeSeries,
    records: Iterable[RunRecord],
    *,
    key_of: Callable[[RunRecord], str] | None = None,
) -> None:
    """Add ``records`` to ``series``, so a long list can be counted in slices."""
    place = bucket_placer(series)
    width = len(series.starts)
    for record in records:
        index = place(record)
        if index is None:
            continue
        series.counts.setdefault(record.state, [0] * width)[index] += 1
        if key_of is not None:
            per_key = series.by_key.setdefault(key_of(record), {})
            per_key.setdefault(record.state, [0] * width)[index] += 1


@dataclass(frozen=True, slots=True)
class FailureGroup:
    """FAILURE runs that raised the same exception class."""

    exception_type: str | None
    count: int
    first_seen: datetime
    last_seen: datetime
    task_names: tuple[str, ...]
    sample_task_ids: tuple[str, ...]
    # Message of the newest run by (last_updated, task_id), the order of the samples.
    latest_message: str | None = None


class FailureTally:
    """Running totals of one exception class's FAILURE runs, added to one by one.

    Keeps O(names + ``FAILURE_SAMPLES``) state, so reading a tally never sorts
    the runs: summarizing is O(types), whatever the number of failures.
    """

    __slots__ = (
        "_latest_entry",
        "_latest_message",
        "_names",
        "_newest",
        "count",
        "first_seen",
        "last_seen",
    )

    def __init__(self, first: RunRecord) -> None:
        """Start a tally with its first run."""
        self.count = 1
        self.first_seen = self.last_seen = first.last_updated
        self._names = {first.name}
        # Min-heap of the newest runs by (last_updated, task_id): the root is
        # the next to drop.
        self._newest = [(first.last_updated, first.task_id)]
        self._latest_entry = self._newest[0]
        self._latest_message = first.exception_message

    def add(self, record: RunRecord) -> None:
        """Count one more FAILURE run."""
        stamp = record.last_updated
        self.count += 1
        self.first_seen = min(self.first_seen, stamp)
        self.last_seen = max(self.last_seen, stamp)
        self._names.add(record.name)
        entry = (stamp, record.task_id)
        # The heap root is its smallest entry, so the newest is tracked apart.
        if entry > self._latest_entry:
            self._latest_entry = entry
            self._latest_message = record.exception_message
        if len(self._newest) < FAILURE_SAMPLES:
            heapq.heappush(self._newest, entry)
        elif entry > self._newest[0]:
            heapq.heapreplace(self._newest, entry)

    def group(self, exception_type: str | None) -> FailureGroup:
        """The tally as a group."""
        return FailureGroup(
            exception_type=exception_type,
            count=self.count,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            task_names=tuple(sorted(self._names)),
            sample_task_ids=tuple(
                task_id for _, task_id in sorted(self._newest, reverse=True)
            ),
            latest_message=self._latest_message,
        )


def collect_failures(
    by_type: dict[str | None, FailureTally],
    records: Iterable[RunRecord],
    window: Window,
) -> None:
    """Add the window's FAILURE ``records`` to ``by_type``, so slices can be collected.

    Runs without an exception class (events from workers older than the
    field) are tallied under ``None``.
    """
    for record in records:
        if record.state == TaskState.FAILURE and window.contains(record.last_updated):
            tally = by_type.get(record.exception_type)
            if tally is None:
                by_type[record.exception_type] = FailureTally(record)
            else:
                tally.add(record)


def summarize_failures(by_type: dict[str | None, FailureTally]) -> list[FailureGroup]:
    """Turn the tallies into groups, most runs first; ties by latest failure."""
    groups = [tally.group(exception_type) for exception_type, tally in by_type.items()]
    groups.sort(key=lambda g: (-g.count, -g.last_seen.timestamp()))
    return groups


#: Runs of task names with this prefix are left out of the task stats. Celery's
#: built-ins (``celery.chord_unlock``, ``celery.accumulate``) are never
#: registered by a worker, so they would show as unknown tasks (decision D9).
EXCLUDED_NAME_PREFIX: Final[str] = "celery."

#: States whose run supplies a runtime sample. A REVOKED run was cut short by an
#: operator, so its time says nothing about how long the task takes (D2).
RUNTIME_SAMPLE_STATES: Final[frozenset[TaskState]] = frozenset(
    {TaskState.SUCCESS, TaskState.FAILURE}
)

#: States that count as a failure. REJECTED is not one, as in the overview (D3).
FAILURE_STATES: Final[frozenset[TaskState]] = frozenset({TaskState.FAILURE})

#: Percentiles reported, in percent.
P50: Final[int] = 50
P95: Final[int] = 95


def percentile(sorted_samples: Sequence[int], percent: int) -> int | None:
    """The nearest-rank percentile of ``sorted_samples`` (ascending); None when empty.

    The value at rank ``ceil(percent * n / 100)``, counted from 1. The rank is
    computed in integers: ``0.95 * n`` is not exact in floats for every ``n``.

    Raises:
        ValueError: If ``percent`` is not in 1..100.
    """
    if not 1 <= percent <= 100:
        raise ValueError(f"percent must be in 1..100, got {percent}")
    if not sorted_samples:
        return None
    rank = -(-percent * len(sorted_samples) // 100)
    return sorted_samples[rank - 1]


def runtime_sample(record: RunRecord) -> int | None:
    """The record's runtime sample: SUCCESS or FAILURE runs with a paired start."""
    return record.runtime_ms if record.state in RUNTIME_SAMPLE_STATES else None


def queue_wait_sample(record: RunRecord) -> int | None:
    """The record's queue-wait sample: runs with an outcome that waited before starting.

    A run revoked after it started did wait, so REVOKED counts here and not above.
    """
    return record.queue_wait_ms if record.state in OUTCOME_STATES else None


def runtime_sort_value(record: RunRecord) -> int | None:
    """What a task list sorted by runtime ranks the run by: the p95 tile's sample, or None."""
    return (
        None if record.name.startswith(EXCLUDED_NAME_PREFIX) else runtime_sample(record)
    )


def queue_wait_sort_value(record: RunRecord) -> int | None:
    """What a task list sorted by queue wait ranks the run by: the p95 tile's sample, or None."""
    return (
        None
        if record.name.startswith(EXCLUDED_NAME_PREFIX)
        else queue_wait_sample(record)
    )


def p95(samples: list[int]) -> int | None:
    """Sort ``samples`` in place and return their nearest-rank p95; None when empty."""
    samples.sort()
    return percentile(samples, P95)


@dataclass(slots=True)
class WindowSamples:
    """The runtime and queue-wait samples of one window, in arrival order."""

    runtime: list[int] = field(default_factory=list)
    queue_wait: list[int] = field(default_factory=list)


def collect_samples(
    samples: WindowSamples, records: Iterable[RunRecord], window: Window
) -> None:
    """Add the samples of the ``window``'s runs to ``samples``, so slices can be collected.

    ``celery.*`` tasks give none, as in the task stats, so the runtime samples are
    the union of the Registry rows' samples.
    """
    for record in records:
        if record.name.startswith(EXCLUDED_NAME_PREFIX):
            continue
        if not window.contains(record.last_updated):
            continue
        runtime = runtime_sample(record)
        if runtime is not None:
            samples.runtime.append(runtime)
        wait = queue_wait_sample(record)
        if wait is not None:
            samples.queue_wait.append(wait)


def collect_bucket_samples(
    series: TimeSeries, buckets: list[WindowSamples], records: Iterable[RunRecord]
) -> None:
    """Add ``records``' samples to the ``series``' ``buckets``, placed as ``count_runs`` does.

    ``buckets`` has one entry per bucket; a long list can be added in slices.
    """
    place = bucket_placer(series)
    for record in records:
        if record.name.startswith(EXCLUDED_NAME_PREFIX):
            continue
        index = place(record)
        if index is None:
            continue
        runtime = runtime_sample(record)
        if runtime is not None:
            buckets[index].runtime.append(runtime)
        wait = queue_wait_sample(record)
        if wait is not None:
            buckets[index].queue_wait.append(wait)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStats:
    """Runs, failures and runtime percentiles of a set of task names."""

    runs: int
    failures: int
    runtime_samples: int
    p50_ms: int | None
    p95_ms: int | None
    last_run: datetime

    @property
    def failure_rate(self) -> float | None:
        """Failures over runs; None with no runs."""
        return self.failures / self.runs if self.runs else None


@dataclass(frozen=True, slots=True, kw_only=True)
class NameStats(RunStats):
    """The stats of one task name."""

    name: str
    task_group: str
    last_state: TaskState


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupStats(RunStats):
    """The stats of one task group: all runs of its task names together."""

    name: str
    task_count: int


class _NameTally:
    """Running totals of one task name's records."""

    __slots__ = ("failures", "last", "runs", "samples")

    def __init__(self) -> None:
        """Start with no records."""
        self.runs = self.failures = 0
        self.samples: list[int] = []
        # (last_updated, task_id) of the newest record, and its state; the
        # task ID breaks a tie so the answer doesn't depend on record order.
        self.last: tuple[datetime, str, TaskState] | None = None

    def add(self, record: RunRecord, *, in_window: bool) -> None:
        """Count one record; only ``last`` ignores the window."""
        if self.last is None or (record.last_updated, record.task_id) > self.last[:2]:
            self.last = (record.last_updated, record.task_id, record.state)
        if not in_window or record.state not in OUTCOME_STATES:
            return
        self.runs += 1
        self.failures += record.state in FAILURE_STATES
        runtime = runtime_sample(record)
        if runtime is not None:
            self.samples.append(runtime)


def task_stats(
    records: Iterable[RunRecord],
    window: Window,
    group_of: Callable[[str], str],
    *,
    name: str | None = None,
) -> tuple[list[NameStats], list[GroupStats]]:
    """Per-name stats and per-group rollups of the runs in ``window``, sorted by name.

    A name has a row whenever any of its records exists, so ``last_run`` and
    ``last_state`` (its newest record of any state) ignore the window. A group's
    percentiles come from all its names' samples together.

    Args:
        records: Run records (the store's snapshot).
        window: Which runs count, placed by ``last_updated``.
        group_of: Maps a task name to its group name; called once per name.
        name: Keep only the task with exactly this name.
    """
    tallies: dict[str, _NameTally] = {}
    for record in records:
        if record.name.startswith(EXCLUDED_NAME_PREFIX):
            continue
        if name is not None and record.name != name:
            continue
        tally = tallies.get(record.name)
        if tally is None:
            tally = tallies[record.name] = _NameTally()
        tally.add(record, in_window=window.contains(record.last_updated))

    rows: list[NameStats] = []
    members: dict[str, list[NameStats]] = defaultdict(list)
    samples_of: dict[str, list[int]] = defaultdict(list)
    for task_name in sorted(tallies):
        tally = tallies[task_name]
        assert tally.last is not None  # a tally exists only once it has a record
        tally.samples.sort()
        row = NameStats(
            name=task_name,
            task_group=group_of(task_name),
            runs=tally.runs,
            failures=tally.failures,
            runtime_samples=len(tally.samples),
            p50_ms=percentile(tally.samples, P50),
            p95_ms=percentile(tally.samples, P95),
            last_run=tally.last[0],
            last_state=tally.last[2],
        )
        rows.append(row)
        members[row.task_group].append(row)
        samples_of[row.task_group] += tally.samples

    groups: list[GroupStats] = []
    for group_name in sorted(members):
        # The union of the members' samples, never a mean of their percentiles.
        samples = sorted(samples_of[group_name])
        names = members[group_name]
        groups.append(
            GroupStats(
                name=group_name,
                task_count=len(names),
                runs=sum(r.runs for r in names),
                failures=sum(r.failures for r in names),
                runtime_samples=len(samples),
                p50_ms=percentile(samples, P50),
                p95_ms=percentile(samples, P95),
                last_run=max(r.last_run for r in names),
            )
        )
    return rows, groups
