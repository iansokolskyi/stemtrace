"""Aggregates over the store's run records: KPIs, time series, failure groups.

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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final

from stemtrace.core.events import TaskState
from stemtrace.core.graph import TERMINAL_STATES, NodeType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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

    @classmethod
    def from_node(cls, node: TaskNode) -> RunRecord | None:
        """Build the record of a TASK node; None for GROUP/CHORD or an empty node."""
        if node.node_type != NodeType.TASK or node.last_updated is None:
            return None
        return cls(
            task_id=node.task_id,
            name=node.name,
            state=node.state,
            last_updated=node.last_updated,
            # Only failure groups read it. Reading it scans all of the node's
            # events, and this runs under the store lock on every event, so a
            # task retrying thousands of times would make ingest quadratic.
            exception_type=node.exception_type
            if node.state == TaskState.FAILURE
            else None,
            # Same reason: run_times() scans and sorts every event, so only a
            # terminal task reads it: no calls while it runs, then one per event
            # that arrives after the terminal one (usually none).
            runtime_ms=node.runtime_ms if node.state in TERMINAL_STATES else None,
        )


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


def count_runs(
    series: TimeSeries,
    records: Iterable[RunRecord],
    *,
    key_of: Callable[[RunRecord], str] | None = None,
) -> None:
    """Add ``records`` to ``series``, so a long list can be counted in slices."""
    size = series.bucket.total_seconds()
    first = series.starts[0].timestamp()
    width = len(series.starts)
    for record in records:
        offset = record.last_updated.timestamp() - first
        if offset < 0:
            continue
        index = min(int(offset // size), width - 1)
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


class FailureTally:
    """Running totals of one exception class's FAILURE runs, added to one by one.

    Keeps O(names + ``FAILURE_SAMPLES``) state, so reading a tally never sorts
    the runs: summarizing is O(types), whatever the number of failures.
    """

    __slots__ = ("_names", "_newest", "count", "first_seen", "last_seen")

    def __init__(self, first: RunRecord) -> None:
        """Start a tally with its first run."""
        self.count = 1
        self.first_seen = self.last_seen = first.last_updated
        self._names = {first.name}
        # Min-heap of the newest runs by (last_updated, task_id): the root is
        # the next to drop.
        self._newest = [(first.last_updated, first.task_id)]

    def add(self, record: RunRecord) -> None:
        """Count one more FAILURE run."""
        stamp = record.last_updated
        self.count += 1
        self.first_seen = min(self.first_seen, stamp)
        self.last_seen = max(self.last_seen, stamp)
        self._names.add(record.name)
        entry = (stamp, record.task_id)
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
