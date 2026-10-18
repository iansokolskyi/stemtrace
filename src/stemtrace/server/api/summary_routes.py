"""Summary endpoints: overview KPIs, runs over time, failures by exception.

Each request copies the store's run records once (``GraphStore.run_records``,
references only) and aggregates outside the store lock: O(n) in the records
held (at most the store's ``max_nodes``), plus O(buckets) for time series.
"""

from __future__ import annotations

from datetime import (
    datetime,  # noqa: TC003  FastAPI resolves Query annotations at runtime
)
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query

from stemtrace.server.api import params
from stemtrace.server.api.schemas import (
    FailureGroupListResponse,
    FailureGroupResponse,
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
    Window,
    auto_bucket,
    failure_groups,
    parse_duration,
    time_series,
    window_stats,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from stemtrace.core.events import TaskState
    from stemtrace.core.groups import GroupConfig
    from stemtrace.server.stats import RunRecord
    from stemtrace.server.store import GraphStore, WorkerRegistry

_WITHIN_HELP = "Relative window ending now, e.g. 30s, 15m, 6h or 7d (at most 3650d)"


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

        Runs are runs with an outcome (SUCCESS, FAILURE, REVOKED, REJECTED; not
        IGNORED, a task that chose not to run), placed by their last event in lifecycle order.
        The failure rate is FAILURE runs over those runs (0 with none). Workers are counted from worker events as the
        server last saw them, without asking the workers.
        """
        if from_date is not None:
            # Rejected rather than ignored, so a script never gets the
            # default hour while thinking it set a start.
            raise _unprocessable("overview takes within only, not from_date")
        now = params.utcnow()
        window = Window(now - params.parse_within(within), now, open_end=True)
        baseline = window.previous()
        records = store.run_records()
        current, previous = (
            window_stats(records, window),
            window_stats(records, baseline),
        )
        workers = worker_registry.get_all_workers() if worker_registry else []
        return OverviewResponse(
            window=_window_response(window),
            baseline_window=_window_response(baseline),
            runs=KpiResponse(value=current.runs, baseline=previous.runs),
            failures=KpiResponse(value=current.failures, baseline=previous.failures),
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
        oldest = min((r.last_updated for r in records), default=now)
        window = _recent_window(within, from_date, now, min(oldest, now))
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
            series = time_series(
                records, start=window.start, end=now, bucket=size, key_of=key_of
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
        oldest = min((r.last_updated for r in records), default=now)
        window = _recent_window(within, from_date, now, min(oldest, now))
        groups_ = failure_groups(records, window)
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
