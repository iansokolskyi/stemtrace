"""Summary endpoints: overview KPIs, runs over time, failures by exception.

Each request copies the store's run records once (``GraphStore.run_records``,
references only) and aggregates outside the store lock: O(n) in the records
held (at most the store's ``max_nodes``), plus O(buckets) for time series.
The aggregation runs in slices that yield to the event loop between them.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import (
    datetime,  # noqa: TC003  FastAPI resolves Query annotations at runtime
)
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from stemtrace.server.api import params
from stemtrace.server.api.schemas import (
    FailureGroupListResponse,
    FailureGroupResponse,
    KpiCountResponse,
    KpiResponse,
    OverviewResponse,
    TimeSeriesResponse,
    TimeWindowResponse,
    WorkerCountsResponse,
    WorkerStatus,
)
from stemtrace.server.groups import group_lookup
from stemtrace.server.stats import (
    MAX_DURATION,
    TimeSeries,
    Window,
    WindowStats,
    auto_bucket,
    bucket_starts,
    collect_failures,
    count_runs,
    parse_duration,
    summarize_failures,
    time_series,
    window_stats,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from stemtrace.core.events import TaskState
    from stemtrace.core.groups import GroupConfig
    from stemtrace.server.stats import RunRecord
    from stemtrace.server.store import GraphStore, WorkerRegistry

# Records aggregated between yields to the event loop: about 1 ms of work, so
# a request over max_nodes records never holds the loop for longer.
_SLICE = 2048

_WITHIN_HELP = "Relative window ending now, e.g. 30s, 15m, 6h or 7d (at most 3650d)"


async def _slices(records: list[RunRecord]) -> AsyncIterator[list[RunRecord]]:
    """Yield ``records`` in slices, giving the event loop a turn after each."""
    # Concurrency model: ``records`` is the store's snapshot (a list copy of
    # immutable records, taken under the store lock), so the consumer thread
    # can ingest meanwhile without a race or a torn result. Slicing on the loop
    # rather than in a worker thread never waits for a free thread either.
    for start in range(0, len(records), _SLICE):
        yield records[start : start + _SLICE]
        await asyncio.sleep(0)


async def _oldest(records: list[RunRecord], now: datetime) -> datetime:
    """The oldest ``last_updated`` among ``records``, or ``now`` with none."""
    oldest = now
    async for part in _slices(records):
        oldest = min(oldest, min(r.last_updated for r in part))
    return oldest


def _window_response(window: Window) -> TimeWindowResponse:
    return TimeWindowResponse(start=window.start, end=window.end)


def _by_state_name(counts: dict[TaskState, list[int]]) -> dict[str, list[int]]:
    return {state.value: series for state, series in counts.items()}


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _recent_window(
    within: str | None, from_date: datetime | None, now: datetime, oldest: datetime
) -> Window:
    """The window ending now: from ``within``, ``from_date``, or ``oldest`` (the oldest run).

    It starts at most ``MAX_DURATION`` before now (a worker clock decades
    behind would otherwise need more than ``MAX_BUCKETS`` of the largest
    automatic bucket); the response's window shows the start used.
    """
    start = params.start_from(within, from_date, now)
    if start is not None and start > now:
        raise _unprocessable("from_date is after now")
    return Window(
        max(start if start is not None else oldest, now - MAX_DURATION),
        now,
        open_end=True,
    )


def add_summary_routes(
    router: APIRouter,
    *,
    store: GraphStore,
    worker_registry: WorkerRegistry | None,
    groups: GroupConfig,
) -> None:
    """Register ``/overview``, ``/stats/timeseries`` and ``/failures``."""
    _add_overview_route(router, store=store, worker_registry=worker_registry)
    _add_timeseries_route(router, store=store, groups=groups)
    _add_failures_route(router, store=store)


def _add_stats(total: WindowStats, part: WindowStats) -> WindowStats:
    return WindowStats(
        runs=total.runs + part.runs,
        failures=total.failures + part.failures,
        minutes=part.minutes,
    )


def _add_overview_route(
    router: APIRouter, *, store: GraphStore, worker_registry: WorkerRegistry | None
) -> None:
    @router.get("/overview", response_model=OverviewResponse)
    async def overview(
        within: Annotated[str, Query(description=_WITHIN_HELP)] = "1h",
        from_date: Annotated[
            datetime | None,
            Query(description="Not supported here (a 422): use within"),
        ] = None,
    ) -> OverviewResponse:
        """KPIs over the last ``within``, each with the window of equal length before it.

        Runs are tasks that ended SUCCESS, FAILURE, REVOKED or REJECTED (not
        IGNORED: those chose not to run), placed by their last event in
        lifecycle order. The failure rate is FAILURE runs over runs (0 with
        none). Workers are counted from worker events, without asking them.
        """
        if from_date is not None:
            # Rejected rather than ignored, so a script never gets the
            # default hour while thinking it set a start.
            raise _unprocessable("overview takes within only, not from_date")
        now = params.utcnow()
        window = Window(now - params.parse_within(within), now, open_end=True)
        baseline = window.previous()
        records = store.run_records()
        current, previous = WindowStats(0, 0, 0.0), WindowStats(0, 0, 0.0)
        async for part in _slices(records):
            current = _add_stats(current, window_stats(part, window))
            previous = _add_stats(previous, window_stats(part, baseline))
        workers = worker_registry.get_all_workers() if worker_registry else []
        return OverviewResponse(
            window=_window_response(window),
            baseline_window=_window_response(baseline),
            runs=KpiCountResponse(value=current.runs, baseline=previous.runs),
            failures=KpiCountResponse(
                value=current.failures, baseline=previous.failures
            ),
            runs_per_minute=KpiResponse(
                value=current.runs_per_minute, baseline=previous.runs_per_minute
            ),
            failure_rate=KpiResponse(
                value=current.failure_rate, baseline=previous.failure_rate
            ),
            workers=WorkerCountsResponse(
                online=sum(w.status == WorkerStatus.ONLINE for w in workers),
                total=len(workers),
            ),
        )


def _add_timeseries_route(
    router: APIRouter, *, store: GraphStore, groups: GroupConfig
) -> None:
    @router.get(
        "/stats/timeseries",
        response_model=TimeSeriesResponse,
        responses={422: {"description": "Bad bucket/window, or too many buckets"}},
    )
    async def stats_timeseries(
        bucket: Annotated[
            str | None,
            Query(
                description=(
                    "Bucket size, e.g. 30s, 15m, 6h or 7d (at most 3650d). Default: "
                    "the smallest of 1m, 5m, 15m, 1h, 6h, 1d, 7d that fits in 1440 "
                    "buckets"
                )
            ),
        ] = None,
        within: Annotated[str | None, Query(description=_WITHIN_HELP)] = None,
        from_date: Annotated[
            datetime | None, Query(description="Start (ISO format) instead of within")
        ] = None,
        group_by: Annotated[
            str | None, Query(description="Also split by task_group")
        ] = None,
    ) -> TimeSeriesResponse:
        """Runs by current state per bucket, from ``within`` or ``from_date`` to now.

        Without either, from the oldest run held, at most 3650 days back. Each
        run counts in the bucket of its last event; bucket edges are epoch
        multiples of the bucket size, and the last bucket also takes runs
        stamped after now. At most 1440 buckets.
        """
        if group_by not in (None, "task_group"):
            raise _unprocessable("group_by must be task_group")
        now = params.utcnow()
        records = store.run_records()
        window = _recent_window(
            within, from_date, now, min(await _oldest(records, now), now)
        )
        try:
            size = (
                parse_duration(bucket)
                if bucket is not None
                else auto_bucket(window.start, now)
            )
        except ValueError as exc:
            raise _unprocessable(f"bucket: {exc}") from exc
        key_of: Callable[[RunRecord], str] | None = None
        if group_by == "task_group":
            group_of = group_lookup(groups)

            def key_of(record: RunRecord) -> str:
                """Split the series by the run's task group (resolved once per name)."""
                return group_of(record.name).name

        try:
            if key_of is None:
                series = TimeSeries(bucket_starts(window.start, now, size), size)
                async for part in _slices(records):
                    count_runs(series, part)
            else:
                # Resolves every run's group: off the event loop, as the task
                # list's group filters do (see routes._group_task_names).
                series = await run_in_threadpool(
                    time_series,
                    records,
                    start=window.start,
                    end=now,
                    bucket=size,
                    key_of=key_of,
                )
        except ValueError as exc:
            raise _unprocessable(str(exc)) from exc
        # Buckets start on edges, so the series covers from the first edge.
        return TimeSeriesResponse(
            window=TimeWindowResponse(start=series.starts[0], end=now),
            bucket_seconds=int(size.total_seconds()),
            starts=series.starts,
            counts=_by_state_name(series.counts),
            by_task_group={
                key: _by_state_name(counts) for key, counts in series.by_key.items()
            }
            if group_by
            else None,
        )


def _add_failures_route(router: APIRouter, *, store: GraphStore) -> None:
    @router.get("/failures", response_model=FailureGroupListResponse)
    async def failures(
        group_by: Annotated[
            str, Query(description="How to group failures: exception")
        ] = "exception",
        within: Annotated[str | None, Query(description=_WITHIN_HELP)] = None,
        from_date: Annotated[
            datetime | None, Query(description="Start (ISO format) instead of within")
        ] = None,
    ) -> FailureGroupListResponse:
        """Tasks whose current state is FAILURE, grouped by exception class.

        Each group lists its count, first and last failure, task names and up
        to 5 of the latest task IDs; tasks without an exception class form one
        group with ``exception_type`` null. Without ``within`` or ``from_date``,
        from the oldest run held, at most 3650 days back.
        """
        if group_by != "exception":
            raise _unprocessable("group_by must be exception")
        now = params.utcnow()
        records = store.run_records()
        window = _recent_window(
            within, from_date, now, min(await _oldest(records, now), now)
        )
        by_type: dict[str | None, list[RunRecord]] = defaultdict(list)
        async for part in _slices(records):
            collect_failures(by_type, part, window)
        groups_ = summarize_failures(by_type)
        return FailureGroupListResponse(
            window=_window_response(window),
            group_by=group_by,
            total=sum(g.count for g in groups_),
            groups=[
                FailureGroupResponse(
                    exception_type=g.exception_type,
                    count=g.count,
                    first_seen=g.first_seen,
                    last_seen=g.last_seen,
                    task_names=list(g.task_names),
                    sample_task_ids=list(g.sample_task_ids),
                )
                for g in groups_
            ],
        )
