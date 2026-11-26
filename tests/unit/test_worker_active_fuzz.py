"""``active`` against an oracle built from the raw events (B4b).

A seeded script of worker events (ready, a replayed ready with an older stamp, stats
naming a pid and its pool, stop, silence) and task lifecycles (stuck, finished, retried,
retried and started again, cancelled, no hostname, late delivery) is fed to a real
``GraphStore`` (small ``max_nodes`` on some seeds) and ``WorkerRegistry`` (task events
call ``mark_seen``). The oracle never reads a ``RunRecord``: it keeps each nodename's
live pid, ``worker_ready`` stamp (the newest one) and pool from the worker events, and
counts a task when the events the store still holds end at STARTED, on that nodename, at
or after the live pid's ``worker_ready``.
``STEMTRACE_FUZZ_SEEDS=a:b`` widens the range for a sweep.
"""

from __future__ import annotations

import functools
import os
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import CANCELLED_RETRY_REASON, TaskEvent, TaskState
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import (
    WORKER_STALE_TIMEOUT_MINUTES,
    GraphStore,
    WorkerRegistry,
)
from stemtrace.server.worker_pool import NO_POOL_INFO, PoolInfo

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
NODES = ["celery@a", "celery@b"]
PIDS = [1, 2, 3]
_RANGE = os.environ.get("STEMTRACE_FUZZ_SEEDS", "0:60")
SEEDS = range(*map(int, _RANGE.split(":")))
POOLS = [
    NO_POOL_INFO,
    PoolInfo("solo", 1),
    PoolInfo("prefork", 4),
    PoolInfo("threads", 8),
    PoolInfo("prefork", None),
]
SMALL_STORE = 12


@dataclass
class Op:
    at: datetime
    kind: str  # ready | stats | stop | silent | task
    node: str = ""
    pid: int = 0
    events: list[TaskEvent] = field(default_factory=list)
    delay: int = 0  # task events: arrive this many script steps late
    pool: PoolInfo = NO_POOL_INFO  # stats: what the reply says about this pid


@dataclass
class Oracle:
    """Live pid and ready stamps per nodename, from the worker events alone."""

    newest: dict[str, int] = field(default_factory=dict)
    alive: dict[str, bool] = field(default_factory=dict)
    ready: dict[tuple[str, int], datetime] = field(default_factory=dict)
    pool: dict[tuple[str, int], PoolInfo] = field(default_factory=dict)
    stopped: dict[str, bool] = field(default_factory=dict)
    events: Counter[str] = field(default_factory=Counter)

    def apply(self, op: Op) -> None:
        key = (op.node, op.pid)
        match op.kind:
            case "ready":
                earlier = self.ready.get(key)
                if earlier is not None and op.at > earlier:
                    self.events["pool-cleared-by-newer-ready"] += (
                        self.pool.get(key, NO_POOL_INFO) != NO_POOL_INFO
                    )
                    self.pool[key] = NO_POOL_INFO
                if earlier is not None and op.at < earlier:
                    self.events["replayed-ready-keeps-newest-stamp"] += 1
                    self.events["replayed-ready-keeps-pool"] += (
                        self.pool.get(key, NO_POOL_INFO) != NO_POOL_INFO
                    )
                self.newest[op.node] = op.pid
                self.alive[op.node] = True
                self.stopped[op.node] = False
                self.ready[key] = max(earlier or op.at, op.at)
            case "stats":
                self.newest[op.node] = op.pid
                self.alive[op.node] = True
                self.stopped[op.node] = False
                self.pool[key] = op.pool
            case "stop" | "silent":
                if self.newest.get(op.node) == op.pid:
                    self.alive[op.node] = False
                    self.stopped[op.node] = op.kind == "stop"

    def seen(self, node: str, at: datetime, now: datetime) -> None:
        """``mark_seen``: a recent task event revives a node that only went silent."""
        recent = min(at, now) >= now - timedelta(minutes=WORKER_STALE_TIMEOUT_MINUTES)
        if node in self.newest and recent and not self.stopped[node]:
            self.events["revived-by-task-event"] += not self.alive[node]
            self.alive[node] = True

    def live(self, node: str) -> tuple[int, datetime | None] | None:
        pid = self.newest.get(node)
        if pid is None or not self.alive[node]:
            return None
        return pid, self.ready.get((node, pid))


def make_script(seed: int) -> list[Op]:
    rng = random.Random(seed)
    now = T0
    script: list[Op] = []
    known: dict[str, set[int]] = {n: set() for n in NODES}
    readied: dict[tuple[str, int], datetime] = {}
    for i in range(rng.randint(8, 40)):
        now += timedelta(seconds=rng.randint(1, 30))
        node = rng.choice(NODES)
        roll = rng.random()
        if roll < 0.16:
            # Mostly a pid already seen: a restart that reuses the number, or a replay.
            pid = rng.choice(
                sorted(known[node]) if known[node] and rng.random() < 0.7 else PIDS
            )
            known[node].add(pid)
            at = now
            if (node, pid) in readied and rng.random() < 0.5:
                # A replayed ready (Redis stream) is old news: stamped before the last.
                at = readied[node, pid] - timedelta(seconds=rng.randint(1, 60))
            readied[node, pid] = max(at, readied.get((node, pid), at))
            script.append(Op(at, "ready", node, pid))
        elif roll < 0.26:
            pid = rng.choice(
                sorted(known[node]) if known[node] and rng.random() < 0.6 else PIDS
            )
            known[node].add(pid)
            script.append(Op(now, "stats", node, pid, pool=rng.choice(POOLS)))
        elif roll < 0.31 and known[node]:
            script.append(Op(now, "stop", node, rng.choice(sorted(known[node]))))
        elif roll < 0.35 and known[node]:
            script.append(Op(now, "silent", node, rng.choice(sorted(known[node]))))
        else:
            script.append(task_op(rng, now, node, i))
    return script


def task_op(rng: random.Random, now: datetime, node: str, i: int) -> Op:
    host = None if rng.random() < 0.1 else node
    task_id = f"t{i}"

    def event(state: TaskState, at: datetime, **extra: object) -> TaskEvent:
        return TaskEvent(
            task_id=task_id,
            name=rng.choice(["app.t", "celery.chord_unlock"]),
            state=state,
            timestamp=at,
            hostname=None if state == TaskState.PENDING else host,
            **extra,  # type: ignore[arg-type]  # test helper: only str fields
        )

    ending = rng.choice(
        ["stuck", "success", "failure", "retry", "cancelled", "retry_started"]
    )
    events = [event(TaskState.STARTED, now)]
    later = now + timedelta(seconds=rng.randint(1, 90))
    if ending == "retry_started":
        events.append(event(TaskState.RETRY, later, exception="boom"))
        again = later + timedelta(seconds=rng.randint(1, 30))
        events.append(event(TaskState.STARTED, again, retries=1))
    elif ending == "success":
        events.append(event(TaskState.SUCCESS, later))
    elif ending == "failure":
        events.append(event(TaskState.FAILURE, later, exception_type="E"))
    elif ending == "retry":
        events.append(event(TaskState.RETRY, later, exception="boom"))
    elif ending == "cancelled":
        events.append(event(TaskState.RETRY, later, exception=CANCELLED_RETRY_REASON))
    if rng.random() < 0.5:
        events.insert(0, event(TaskState.PENDING, now - timedelta(seconds=1)))
    return Op(now, "task", node, 0, events, delay=rng.choice([0, 0, 0, 1, 3, 9]))


def deliver_order(script: list[Op]) -> list[Op]:
    """Worker events in stamp order; each task's events land ``delay`` steps late."""
    keyed = [(i + op.delay, i, op) for i, op in enumerate(script)]
    return [op for _, _, op in sorted(keyed, key=lambda k: k[:2])]


def stuck_starts(
    delivered: list[Op], held: dict[str, list[TaskEvent]]
) -> list[tuple[str, datetime]]:
    """(nodename, STARTED stamp) of every task whose newest held event is STARTED.

    ``held`` is what the store still has per task: eviction can drop a whole
    flow, or the first events of one that was refilled.
    """
    started: list[tuple[str, datetime]] = []
    for op in delivered:
        for task_id in {e.task_id for e in op.events}:
            events = held.get(task_id)
            if not events:
                continue
            newest = max(events, key=lambda e: (e.timestamp, STATE_ORDER[e.state]))
            if newest.state == TaskState.STARTED and newest.hostname is not None:
                started.append((newest.hostname, newest.timestamp))
    return started


STATE_ORDER = {
    s: i
    for i, s in enumerate(
        [
            TaskState.PENDING,
            TaskState.RECEIVED,
            TaskState.STARTED,
            TaskState.RETRY,
            TaskState.SUCCESS,
            TaskState.FAILURE,
            TaskState.REJECTED,
            TaskState.REVOKED,
            TaskState.IGNORED,
        ]
    )
}

Row = tuple[int | None, str | None, int | None]  # active, pool, concurrency


def expected_rows(
    oracle: Oracle,
    script: list[Op],
    delivered: list[Op],
    held: dict[str, list[TaskEvent]],
) -> dict[tuple[str, int], Row]:
    """The oracle's (active, pool, concurrency) for every (nodename, pid) listed."""
    started = stuck_starts(delivered, held)
    result: dict[tuple[str, int], Row] = {}
    for node, pid in {(o.node, o.pid) for o in script if o.pid}:
        pool = oracle.pool.get((node, pid), NO_POOL_INFO)
        live = oracle.live(node)
        if live is None or live[0] != pid:
            result[node, pid] = (None, *pool)
            continue
        bound = live[1]
        active = sum(
            1 for h, at in started if h == node and (bound is None or at >= bound)
        )
        result[node, pid] = (active, *pool)
    return result


def stats_reply(
    oracle: Oracle, op: Op
) -> tuple[dict[str, tuple[int | None, list[str] | None]], dict[str, PoolInfo]]:
    """A real reply names every worker that answers; a known node it leaves out goes
    offline, so the live ones are named too, with the pool they already had."""
    replies: dict[str, tuple[int | None, list[str] | None]] = {}
    pools: dict[str, PoolInfo] = {}
    for node in NODES:
        if (live := oracle.live(node)) is not None:
            replies[node] = (live[0], ["app.t"])
            pools[node] = oracle.pool.get((node, live[0]), NO_POOL_INFO)
    replies[op.node] = (op.pid, ["app.t"])
    pools[op.node] = op.pool
    return replies, pools


def read_rows(
    store: GraphStore, registry: WorkerRegistry
) -> dict[tuple[str, int], Row]:
    """(active, pool, concurrency) per (nodename, pid) from ``/api/workers``."""
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=registry))
    body = TestClient(app).get("/api/workers?refresh=false").json()
    return {
        (w["hostname"], w["pid"]): (
            w.get("active", "absent"),
            w.get("pool", "absent"),
            w.get("concurrency", "absent"),
        )
        for w in body["workers"]
    }


def held_events(store: GraphStore, delivered: list[Op]) -> dict[str, list[TaskEvent]]:
    """Per task, the events the store still has (none once it was evicted)."""
    held: dict[str, list[TaskEvent]] = {}
    for op in delivered:
        for task_id in {e.task_id for e in op.events}:
            node = store.get_node(task_id)
            held[task_id] = [] if node is None else list(node.events)
    return held


def replay(
    delivered: list[Op], max_nodes: int
) -> tuple[Oracle, dict[tuple[str, int], Row], dict[str, list[TaskEvent]]]:
    """Feed the delivered ops to a real store and registry; the oracle follows."""
    store, registry = GraphStore(max_nodes=max_nodes), WorkerRegistry()
    oracle = Oracle()
    clock = T0
    for op in delivered:
        clock = max(clock, op.at)
        match op.kind:
            case "ready":
                registry.register_worker(
                    op.node, op.pid, ["app.t"], event_timestamp=op.at
                )
            case "stats":
                replies, pools = stats_reply(oracle, op)
                registry.apply_stats_reply(
                    registry.event_generations(), replies, pools=pools, now=op.at
                )
            case "stop":
                registry.mark_shutdown(op.node, op.pid, op.at, now=op.at)
            case "silent":
                registry.mark_offline(op.node, op.pid)
            case "task":
                for event in op.events:
                    store.add_event(event)
                    if (host := event.hostname) is not None:
                        registry.mark_seen(host, event.timestamp, now=clock)
                        oracle.seen(host, event.timestamp, clock)
        if op.kind != "task":
            oracle.apply(op)
    return oracle, read_rows(store, registry), held_events(store, delivered)


@functools.cache
def run_seed(
    seed: int,
) -> tuple[dict[tuple[str, int], Row], dict[tuple[str, int], Row], Counter[str]]:
    script = make_script(seed)
    delivered = deliver_order(script)
    max_nodes = SMALL_STORE if seed % 3 == 0 else 10_000
    oracle, actual, held = replay(delivered, max_nodes)
    expected = expected_rows(oracle, script, delivered, held)
    return actual, expected, hit_cells(script, delivered, expected, oracle, held)


def hit_cells(
    script: list[Op],
    delivered: list[Op],
    expected: dict[tuple[str, int], Row],
    oracle: Oracle,
    held: dict[str, list[TaskEvent]],
) -> Counter[str]:
    hits: Counter[str] = Counter()
    hits.update(oracle.events)
    starts = stuck_starts(delivered, held)
    for node in NODES:
        live = oracle.live(node)
        mine = [at for host, at in starts if host == node]
        other = [at for host, at in starts if host != node]
        hits["stuck-on-offline-node"] += live is None and bool(mine)
        hits["live-no-bound-counts"] += (
            live is not None and live[1] is None and bool(mine)
        )
        hits["bound-excludes-older-run"] += (
            live is not None and live[1] is not None and any(a < live[1] for a in mine)
        )
        hits["bound-keeps-newer-run"] += (
            live is not None and live[1] is not None and any(a >= live[1] for a in mine)
        )
        hits["other-node-runs-present"] += (
            live is not None and bool(mine) and bool(other)
        )
    kinds = {op.kind for op in script}
    hits.update(f"op:{k}" for k in kinds)
    hits["active>0"] += any(v[0] for v in expected.values() if v[0])
    hits["live-zero"] += any(v[0] == 0 for v in expected.values())
    hits["null-row"] += any(v[0] is None for v in expected.values())
    hits["pool-in-row"] += any(v[1] is not None for v in expected.values())
    hits["host-none"] += any(
        e.hostname is None
        for op in script
        for e in op.events
        if e.state == TaskState.STARTED
    )
    hits["late"] += any(op.delay for op in script)
    hits["pid-reuse"] += sum(1 for op in script if op.kind == "ready") > len(
        {(o.node, o.pid) for o in script if o.kind == "ready"}
    )
    hits["retried-started-counts"] += any(
        e.state == TaskState.STARTED and e.retries == 1
        for evs in held.values()
        for e in evs
        if e == max(evs, key=lambda x: (x.timestamp, STATE_ORDER[x.state]))
    )
    # A task whose scripted events end at STARTED but the store no longer holds.
    scripted = stuck_starts(
        delivered, {e.task_id: op.events for op in delivered for e in op.events}
    )
    hits["eviction-dropped-a-stuck-run"] += len(scripted) > len(starts)
    return hits


@pytest.mark.parametrize("seed", SEEDS)
def test_active_pool_and_concurrency_match_the_event_oracle(seed: int) -> None:
    actual, expected, _ = run_seed(seed)
    assert actual == expected


def test_generator_reaches_every_cell_over_the_default_seeds() -> None:
    total: Counter[str] = Counter()
    for seed in SEEDS:
        total.update(run_seed(seed)[2])
    wanted = [
        "op:ready", "op:stats", "op:stop", "op:silent", "op:task",
        "active>0", "live-zero", "null-row", "host-none", "late", "pid-reuse",
        "stuck-on-offline-node", "live-no-bound-counts", "bound-excludes-older-run",
        "bound-keeps-newer-run", "other-node-runs-present", "pool-in-row",
        "replayed-ready-keeps-newest-stamp", "replayed-ready-keeps-pool",
        "pool-cleared-by-newer-ready", "revived-by-task-event",
        "retried-started-counts", "eviction-dropped-a-stuck-run",
    ]  # fmt: skip
    assert {cell: total[cell] for cell in wanted if total[cell] == 0} == {}
