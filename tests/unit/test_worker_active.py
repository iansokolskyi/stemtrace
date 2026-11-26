"""Busy slots per worker (B4b): ``active`` counts STARTED run records on the live pid.

Every row goes through ``GET /api/workers`` (or ``/api/workers/{hostname}``) so a
test fails on a wrong value, not on a missing helper. The stamps are the worker's
own clock on both sides (``worker_ready`` and STARTED are both
``datetime.now(timezone.utc)`` on the worker host in ``library/signals.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry
from stemtrace.server.worker_pool import PoolInfo
from tests.unit.test_summary_routes_off_loop import (
    MAX_LOOP_WORK,
    _get,
    _longest_loop_hold,
)

if TYPE_CHECKING:
    from stemtrace.server.stats import RunRecord

T = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
NODE = "celery@a"
ABSENT = "absent"


class Rig:
    """A store, a registry and a client over them."""

    def __init__(self) -> None:
        self.store = GraphStore()
        self.registry = WorkerRegistry()
        app = FastAPI()
        app.include_router(create_api_router(self.store, worker_registry=self.registry))
        self.client = TestClient(app)
        self._n = 0

    def ready(self, pid: int, at: datetime = T, node: str = NODE) -> None:
        self.registry.register_worker(node, pid, ["app.t"], event_timestamp=at)

    def run(
        self,
        state: TaskState,
        at: datetime,
        *,
        node: str | None = NODE,
        name: str = "app.t",
        exception: str | None = None,
    ) -> str:
        """Add one task whose newest event is ``state`` at ``at`` on ``node``."""
        self._n += 1
        task_id = f"t{self._n}"
        events = [TaskState.PENDING] if state != TaskState.PENDING else []
        for earlier in events:
            self.store.add_event(
                TaskEvent(
                    task_id=task_id,
                    name=name,
                    state=earlier,
                    timestamp=at - timedelta(seconds=2),
                )
            )
        self.store.add_event(
            TaskEvent(
                task_id=task_id,
                name=name,
                state=state,
                timestamp=at,
                hostname=node,
                exception=exception,
            )
        )
        return task_id

    def started(self, n: int, at: datetime, *, node: str | None = NODE) -> None:
        for _ in range(n):
            self.run(TaskState.STARTED, at, node=node)

    def rows(self) -> dict[int, object]:
        """``active`` by pid for ``/api/workers`` (``absent`` if the field is missing)."""
        body = self.client.get("/api/workers?refresh=false").json()
        return {w["pid"]: w.get("active", ABSENT) for w in body["workers"]}

    def active(self, pid: int) -> object:
        return self.rows()[pid]


def test_three_started_after_ready_count_three() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(3, T + timedelta(seconds=5))
    assert rig.active(10) == 3


def test_live_worker_with_no_started_runs_reads_zero_not_null() -> None:
    rig = Rig()
    rig.ready(10)
    assert rig.active(10) == 0


def test_started_from_the_previous_pid_is_not_counted_after_a_restart() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(2, T + timedelta(seconds=5))  # killed with -9: never terminal
    rig.ready(11, T + timedelta(seconds=60))
    rig.started(1, T + timedelta(seconds=70))
    assert rig.rows() == {10: None, 11: 1}


def test_started_stamped_exactly_at_ready_counts() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(1, T)
    assert rig.active(10) == 1


def test_started_one_microsecond_before_ready_does_not_count() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(1, T - timedelta(microseconds=1))
    assert rig.active(10) == 0


def test_lost_process_keeps_its_started_runs_counting_until_a_new_pid_or_stop() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(2, T + timedelta(seconds=5))
    # Hours of silence on the task side change nothing: the rule reads no anomalies.
    rig.started(1, T + timedelta(hours=3))
    assert rig.active(10) == 3


def test_pid_known_only_from_a_stats_reply_has_no_lower_bound() -> None:
    rig = Rig()
    rig.started(2, T - timedelta(hours=5))  # the older process's strays: unavoidable
    rig.started(1, T + timedelta(seconds=5))
    rig.registry.apply_stats_reply(
        rig.registry.event_generations(),
        {NODE: (10, ["app.t"])},
        pools=None,
        now=T,
    )
    assert rig.active(10) == 3


def test_started_with_no_hostname_is_counted_for_no_worker() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(2, T + timedelta(seconds=5), node=None)
    assert rig.active(10) == 0


def test_more_started_than_slots_reports_the_real_count() -> None:
    rig = Rig()
    rig.ready(10)
    rig.registry.apply_stats_reply(
        rig.registry.event_generations(),
        {NODE: (10, ["app.t"])},
        pools={NODE: PoolInfo("prefork", 8)},
        now=T + timedelta(seconds=1),
    )
    rig.started(9, T + timedelta(seconds=5))
    body = rig.client.get("/api/workers?refresh=false").json()["workers"][0]
    assert (body["concurrency"], body.get("active", ABSENT)) == (8, 9)


@pytest.mark.parametrize(
    "state",
    [s for s in TaskState if s != TaskState.STARTED],
)
def test_only_started_runs_count_whatever_the_other_state(state: TaskState) -> None:
    rig = Rig()
    rig.ready(10)
    rig.run(state, T + timedelta(seconds=5))
    assert rig.active(10) == 0


def test_cancelled_retry_from_a_cold_stop_is_not_counted() -> None:
    rig = Rig()
    rig.ready(10)
    rig.run(
        TaskState.RETRY,
        T + timedelta(seconds=5),
        exception="cancelled by Celery",
    )
    rig.started(1, T + timedelta(seconds=6))
    assert rig.active(10) == 1


def test_run_that_started_and_finished_is_not_counted() -> None:
    rig = Rig()
    rig.ready(10)
    task_id = rig.run(TaskState.STARTED, T + timedelta(seconds=5))
    rig.store.add_event(
        TaskEvent(
            task_id=task_id,
            name="app.t",
            state=TaskState.SUCCESS,
            timestamp=T + timedelta(seconds=9),
            hostname=NODE,
        )
    )
    assert rig.active(10) == 0


def test_internal_celery_tasks_hold_a_slot_and_count() -> None:
    rig = Rig()
    rig.ready(10)
    rig.run(TaskState.STARTED, T + timedelta(seconds=5), name="celery.chord_unlock")
    assert rig.active(10) == 1


def test_stopped_worker_reads_null() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(2, T + timedelta(seconds=5))
    rig.registry.mark_shutdown(NODE, 10, T + timedelta(seconds=30))
    assert rig.active(10) is None


def test_worker_marked_offline_by_silence_reads_null() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(2, T + timedelta(seconds=5))
    rig.registry.mark_offline(NODE, 10)
    assert rig.active(10) is None


def test_worker_the_stale_cleanup_marked_offline_reads_null() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(2, T + timedelta(seconds=5))
    rig.registry.remove_stale_workers(now=T + timedelta(minutes=10))
    assert rig.active(10) is None


def test_stopped_nodename_revived_by_task_events_reads_null_for_the_listed_pid() -> (
    None
):
    rig = Rig()
    rig.ready(10)
    rig.registry.mark_shutdown(NODE, 10, T + timedelta(seconds=30))
    rig.started(1, T + timedelta(seconds=40))
    rig.registry.mark_seen(
        NODE, T + timedelta(seconds=40), now=T + timedelta(seconds=41)
    )
    assert rig.active(10) is None


def test_superseded_pid_reads_null_even_if_an_event_marked_it_online() -> None:
    rig = Rig()
    rig.ready(10)
    rig.registry.mark_node_alive(NODE, 99, now=T + timedelta(seconds=5))  # unlisted pid
    rig.started(1, T + timedelta(seconds=6))
    rig.registry.mark_seen(NODE, T + timedelta(seconds=6), now=T + timedelta(seconds=7))
    assert rig.active(10) is None


def test_counts_do_not_cross_nodenames() -> None:
    rig = Rig()
    rig.ready(10, node="celery@a")
    rig.ready(20, node="celery@b")
    rig.started(2, T + timedelta(seconds=5), node="celery@a")
    rig.started(5, T + timedelta(seconds=5), node="celery@b")
    assert rig.rows() == {10: 2, 20: 5}


def test_same_pid_number_on_two_nodenames_counts_each_own_runs() -> None:
    rig = Rig()
    rig.ready(7, node="celery@a")
    rig.ready(7, at=T + timedelta(seconds=30), node="celery@b")
    rig.started(1, T + timedelta(seconds=5), node="celery@a")
    rig.started(3, T + timedelta(seconds=40), node="celery@b")
    body = rig.client.get("/api/workers?refresh=false").json()["workers"]
    assert {w["hostname"]: w.get("active", ABSENT) for w in body} == {
        "celery@a": 1,
        "celery@b": 3,
    }


def test_same_pid_number_after_a_restart_counts_only_runs_since_the_new_ready() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(2, T + timedelta(seconds=5))
    rig.ready(10, T + timedelta(seconds=60))  # pid reused (container restart)
    rig.started(1, T + timedelta(seconds=70))
    assert rig.active(10) == 1


def test_naive_ready_stamp_is_read_as_utc_without_an_error() -> None:
    rig = Rig()
    rig.ready(10, T.replace(tzinfo=None))
    rig.started(1, T + timedelta(seconds=5))
    assert rig.active(10) == 1


def test_hostname_route_returns_active_too() -> None:
    rig = Rig()
    rig.ready(10)
    rig.started(2, T + timedelta(seconds=5))
    body = rig.client.get(f"/api/workers/{NODE}").json()["workers"]
    assert [(w["pid"], w.get("active", ABSENT)) for w in body] == [(10, 2)]


def test_no_worker_registry_returns_no_workers() -> None:
    app = FastAPI()
    app.include_router(create_api_router(GraphStore()))
    body = TestClient(app).get("/api/workers").json()
    assert body == {"workers": [], "total": 0}


# --- reading 1: worker_ready clears pool values on a provable restart ----------------


def pool_row(registry: WorkerRegistry, pid: int) -> tuple[str | None, int | None]:
    (w,) = [w for w in registry.get_all_workers() if w.pid == pid]
    return w.pool, w.concurrency


def stats(registry: WorkerRegistry, pid: int, at: datetime) -> None:
    registry.apply_stats_reply(
        registry.event_generations(),
        {NODE: (pid, ["app.t"])},
        pools={NODE: PoolInfo("prefork", 8)},
        now=at,
    )


def test_ready_newer_than_the_stored_one_clears_pool_values_of_that_pid() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE, 10, ["app.t"], event_timestamp=T)
    stats(registry, 10, T + timedelta(seconds=1))
    registry.register_worker(
        NODE, 10, ["app.t"], event_timestamp=T + timedelta(minutes=5)
    )
    assert pool_row(registry, 10) == (None, None)


def test_replayed_ready_with_the_same_stamp_keeps_pool_values() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE, 10, ["app.t"], event_timestamp=T)
    stats(registry, 10, T + timedelta(seconds=1))
    registry.register_worker(NODE, 10, ["app.t"], event_timestamp=T)
    assert pool_row(registry, 10) == ("prefork", 8)


def test_replayed_older_ready_keeps_pool_values() -> None:
    registry = WorkerRegistry()
    registry.register_worker(
        NODE, 10, ["app.t"], event_timestamp=T + timedelta(minutes=5)
    )
    stats(registry, 10, T + timedelta(minutes=6))
    registry.register_worker(NODE, 10, ["app.t"], event_timestamp=T)
    assert pool_row(registry, 10) == ("prefork", 8)


def test_first_ready_for_a_pid_known_only_from_stats_keeps_pool_values() -> None:
    registry = WorkerRegistry()
    stats(registry, 10, T)
    registry.register_worker(
        NODE, 10, ["app.t"], event_timestamp=T + timedelta(minutes=5)
    )
    assert pool_row(registry, 10) == ("prefork", 8)


def test_stats_reply_taken_before_the_restart_ready_cannot_bring_old_values_back() -> (
    None
):
    registry = WorkerRegistry()
    registry.register_worker(NODE, 10, ["app.t"], event_timestamp=T)
    seen = registry.event_generations()
    registry.register_worker(
        NODE, 10, ["app.t"], event_timestamp=T + timedelta(minutes=5)
    )
    registry.apply_stats_reply(
        seen,
        {NODE: (10, ["app.t"])},
        pools={NODE: PoolInfo("prefork", 8)},
        now=T + timedelta(minutes=6),
    )
    assert pool_row(registry, 10) == (None, None)


def test_next_reply_after_the_restart_ready_refills_pool_values() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE, 10, ["app.t"], event_timestamp=T)
    stats(registry, 10, T + timedelta(seconds=1))
    registry.register_worker(
        NODE, 10, ["app.t"], event_timestamp=T + timedelta(minutes=5)
    )
    stats(registry, 10, T + timedelta(minutes=6))
    assert pool_row(registry, 10) == ("prefork", 8)


# --- cost -------------------------------------------------------------------------


def test_workers_without_a_live_row_do_not_scan_the_run_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig()
    rig.ready(10)
    rig.registry.mark_shutdown(NODE, 10, T + timedelta(seconds=1))

    def scanned() -> list[object]:
        raise AssertionError("run_records() was called with no live worker")

    monkeypatch.setattr(rig.store, "run_records", scanned)
    assert rig.rows() == {10: None}


def test_many_live_workers_share_one_scan_of_the_run_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig()
    for n in range(25):
        rig.ready(100 + n, node=f"celery@w{n}")
    scans = []
    real = rig.store.run_records

    def counting() -> list[RunRecord]:
        scans.append(1)
        return real()

    monkeypatch.setattr(rig.store, "run_records", counting)
    assert len(rig.rows()) == 25
    assert len(scans) == 1


@pytest.mark.slow
async def test_workers_request_at_30k_started_runs_keeps_loop_work_within_budget() -> (
    None
):
    store = GraphStore(max_nodes=35_000)
    registry = WorkerRegistry()
    for n in range(2):
        registry.register_worker(f"celery@w{n}", 100 + n, ["app.t"], event_timestamp=T)
    for i in range(30_000):
        store.add_event(
            TaskEvent(
                task_id=f"t{i}",
                name="app.t",
                state=TaskState.STARTED,
                timestamp=T + timedelta(seconds=1 + i % 1000),
                hostname=f"celery@w{i % 2}",
            )
        )
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=registry))
    assert (await _get(app, "/api/health")).status_code == 200
    longest = await _longest_loop_hold(app, "/api/workers?refresh=false")
    assert longest <= MAX_LOOP_WORK
