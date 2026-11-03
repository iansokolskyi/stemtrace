"""Anomaly endpoint and the per-request facts pass that feeds every anomaly field.

A request copies the store's run records, the worker timeline and the worker
registry's facts, each under its own lock, and classifies the copies in a
worker thread: O(n log n) in the records held, never while holding a lock or
the event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import (
    datetime,
    timezone,
)
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from stemtrace.core.anomalies import (
    AnomalyEvidence,
    AnomalyKind,
    AnomalyReport,
    AnomalySettings,
    RegistryFacts,
    classify_anomalies,
)
from stemtrace.server.anomaly_facts import classifier_facts
from stemtrace.server.api import params
from stemtrace.server.api.schemas import AnomalyEvidence as EvidenceName
from stemtrace.server.api.schemas import AnomalyKind as KindName
from stemtrace.server.api.schemas import (
    AnomalyListResponse,
    AnomalyResponse,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stemtrace.core.anomalies import Anomaly
    from stemtrace.server.api.routes import _InspectRefresher
    from stemtrace.server.stats import RunRecord
    from stemtrace.server.store import (
        GraphStore,
        WorkerRegistry,
        WorkerTimeline,
        WorkerTimelineEntry,
    )

__all__ = [
    "AnomalyEvaluator",
    "Evaluation",
    "add_anomaly_routes",
    "anomaly_response",
    "kind_name",
]

_KINDS = [kind.value for kind in AnomalyKind]

# Spelled out so the types check: the core enums' values are the schema's literals.
_KIND_NAMES: dict[AnomalyKind, KindName] = {
    AnomalyKind.STUCK: "stuck",
    AnomalyKind.INTERRUPTED: "interrupted",
    AnomalyKind.UNREGISTERED: "unregistered",
    AnomalyKind.NO_WORKERS_ONLINE: "no_workers_online",
}
_EVIDENCE_NAMES: dict[AnomalyEvidence, EvidenceName] = {
    AnomalyEvidence.WORKER_SHUTDOWN: "worker_shutdown",
    AnomalyEvidence.WORKER_TERMINATING: "worker_terminating",
    AnomalyEvidence.WORKER_RESTARTED: "worker_restarted",
    AnomalyEvidence.CANCELLED_RETRY: "cancelled_retry",
}


def kind_name(anomaly: Anomaly) -> KindName:
    """The anomaly's kind as the API spells it."""
    return _KIND_NAMES[anomaly.kind]


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The anomalies of one request and the clock they were judged at."""

    report: AnomalyReport
    now: datetime

    @property
    def anomalies(self) -> Mapping[str, Anomaly]:
        """The flagged tasks by task ID."""
        return self.report.anomalies


class AnomalyEvaluator:
    """Classifies the store's tasks against the worker timeline and registry, once per call."""

    def __init__(
        self,
        store: GraphStore,
        timeline: WorkerTimeline | None,
        settings: AnomalySettings,
        registry: WorkerRegistry | None = None,
        broker_url: str | None = None,
    ) -> None:
        """Bind the store, the timeline (None: no worker events) and the thresholds.

        Without a ``registry`` no task is reported unregistered. ``broker_url``
        tells whether the server can inspect workers (the registry gate).
        """
        self._store = store
        self._timeline = timeline
        self._settings = settings
        self._registry = registry
        self._broker_configured = broker_url is not None

    async def evaluate(self) -> Evaluation:
        """Copy the facts under their locks, then classify them off both and the loop."""
        now = params.utcnow()
        # Copies under three locks, microseconds apart: a worker event that
        # lands between them only adds evidence, which the next poll sees.
        records = self._store.run_records()
        snapshot = self._timeline.snapshot() if self._timeline is not None else {}
        registry = (
            self._registry.registry_facts(broker_configured=self._broker_configured)
            if self._registry is not None
            else None
        )
        report = await run_in_threadpool(
            self._classify, records, snapshot, registry, now
        )
        return Evaluation(report, now)

    def _classify(
        self,
        records: list[RunRecord],
        snapshot: Mapping[str, Sequence[WorkerTimelineEntry]],
        registry: RegistryFacts | None,
        now: datetime,
    ) -> AnomalyReport:
        facts = classifier_facts(records, snapshot, now)
        return classify_anomalies(
            **facts._asdict(), now=now, settings=self._settings, registry=registry
        )


def anomaly_response(anomaly: Anomaly) -> AnomalyResponse:
    """The API form of an anomaly, with ``since`` in UTC."""
    return AnomalyResponse(
        kind=kind_name(anomaly),
        task_id=anomaly.task_id,
        name=anomaly.name,
        since=anomaly.since.astimezone(timezone.utc),
        hostname=anomaly.hostname,
        task_count=anomaly.task_count,
        elapsed_ms=anomaly.elapsed_ms,
        threshold_ms=anomaly.threshold_ms,
        p95_ms=anomaly.p95_ms,
        runtime_samples=anomaly.runtime_samples,
        evidence=_EVIDENCE_NAMES[anomaly.evidence] if anomaly.evidence else None,
    )


def _sort_key(anomaly: Anomaly) -> tuple[datetime, bool, str, bool, str]:
    """Oldest ``since`` first, then task ID, then name; an item without one comes last."""
    return (
        anomaly.since,
        anomaly.task_id is None,
        anomaly.task_id or "",
        anomaly.name is None,
        anomaly.name or "",
    )


def add_anomaly_routes(
    router: APIRouter,
    *,
    evaluator: AnomalyEvaluator,
    refresher: _InspectRefresher,
) -> None:
    """Register ``/anomalies``."""

    @router.get(
        "/anomalies",
        response_model=AnomalyListResponse,
        responses={422: {"description": "Unknown kind, or limit/offset out of range"}},
    )
    async def list_anomalies(
        kind: Annotated[
            str | None,
            Query(description=f"Only this kind: {', '.join(_KINDS)}"),
        ] = None,
        hostname: Annotated[
            str | None, Query(description="Only this exact worker nodename (name@host)")
        ] = None,
        name: Annotated[
            str | None, Query(description="Only this exact task name")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AnomalyListResponse:
        """Tasks that are stuck, interrupted or unregistered, oldest ``since`` first.

        An unregistered name is one item. ``counts`` has every kind (``no_workers_online``
        is always 0) and ignores the filters; ``total`` counts the items that pass
        them, before ``limit`` and ``offset``. Computed on every request.
        """
        if kind is not None and kind not in _KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown kind {kind}; valid kinds: {', '.join(_KINDS)}",
            )
        await refresher.maybe_refresh()
        evaluation = await evaluator.evaluate()
        found = [
            a
            for a in evaluation.report.items
            if (kind is None or a.kind.value == kind)
            and (hostname is None or a.hostname == hostname)
            and (name is None or a.name == name)
        ]
        found.sort(key=_sort_key)
        return AnomalyListResponse(
            anomalies=[anomaly_response(a) for a in found[offset : offset + limit]],
            counts={k.value: n for k, n in evaluation.report.counts.items()},
            total=len(found),
            limit=limit,
            offset=offset,
            evaluated_at=evaluation.now,
        )
