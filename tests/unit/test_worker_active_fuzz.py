"""``active`` against an oracle built from the raw events (B4b).

A seeded script of worker events (ready, stats naming a pid, stop, silence) and task
lifecycles (stuck, finished, retried, cancelled, no hostname, late delivery) is fed to a
real ``GraphStore`` and ``WorkerRegistry``. The oracle never reads a ``RunRecord``: it
keeps each nodename's live pid from the worker events, and counts a task when its own
events end at STARTED, on that nodename, at or after the live pid's ``worker_ready``.
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
from stemtrace.server.store import GraphStore, WorkerRegistry

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
NODES = ["celery@a", "celery@b"]
PIDS = [1, 2, 3]
_RANGE = os.environ.get("STEMTRACE_FUZZ_SEEDS", "0:40")
SEEDS = range(*map(int, _RANGE.split(":")))
TERMINAL = {TaskState.SUCCESS, TaskState.FAILURE, TaskState.RETRY, TaskState.REVOKED}


@dataclass
class Op:
    at: datetime
    kind: str  # ready | stats | stop | silent | task
    node: str = ""
    pid: int = 0
    events: list[TaskEvent] = field(default_factory=list)
    delay: int = 0  # task events: arrive this many script steps late


@dataclass
class Oracle:
    """Live pid and ready stamps per nodename, from the worker events alone."""

    newest: dict[str, int] = field(default_factory=dict)
    alive: dict[str, bool] = field(default_factory=dict)
    ready: dict[tuple[str, int], datetime] = field(default_factory=dict)

    def apply(self, op: Op) -> None:
        match op.kind:
            case "ready":
                self.newest[op.node] = op.pid
                self.alive[op.node] = True
                self.ready[op.node, op.pid] = op.at
            case "stats":
                self.newest[op.node] = op.pid
                self.alive[op.node] = True
            case "stop" | "silent":
                if self.newest.get(op.node) == op.pid:
                    self.alive[op.node] = False

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
    for i in range(rng.randint(8, 40)):
        now += timedelta(seconds=rng.randint(1, 30))
        node = rng.choice(NODES)
        roll = rng.random()
        if roll < 0.12:
            pid = rng.choice(PIDS)
            known[node].add(pid)
            script.append(Op(now, "ready", node, pid))
        elif roll < 0.20:
            pid = rng.choice(PIDS)
            known[node].add(pid)
            script.append(Op(now, "stats", node, pid))
        elif roll < 0.26 and known[node]:
            script.append(Op(now, "stop", node, rng.choice(sorted(known[node]))))
        elif roll < 0.30 and known[node]:
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

    ending = rng.choice(["stuck", "success", "failure", "retry", "cancelled"])
    events = [event(TaskState.STARTED, now)]
    later = now + timedelta(seconds=rng.randint(1, 90))
    if ending == "success":
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


def stuck_starts(delivered: list[Op]) -> list[tuple[str, datetime]]:
    """(nodename, STARTED stamp) of every task whose own events end at STARTED."""
    ended: dict[str, list[TaskEvent]] = {}
    for op in delivered:
        for event in op.events:
            ended.setdefault(event.task_id, []).append(event)
    started: list[tuple[str, datetime]] = []
    for events in ended.values():
        if any(e.state in TERMINAL for e in events):
            continue
        s = next(e for e in events if e.state == TaskState.STARTED)
        if s.hostname is not None:
            started.append((s.hostname, s.timestamp))
    return started


def expected_active(
    script: list[Op], delivered: list[Op]
) -> dict[tuple[str, int], int | None]:
    """The oracle's ``active`` for every (nodename, pid) the registry lists."""
    oracle = Oracle()
    for op in script:  # worker events in stamp order, task events do nothing
        oracle.apply(op)
    started = stuck_starts(delivered)
    result: dict[tuple[str, int], int | None] = {}
    for node, pid in {(o.node, o.pid) for o in script if o.pid}:
        live = oracle.live(node)
        if live is None or live[0] != pid:
            result[node, pid] = None
            continue
        bound = live[1]
        result[node, pid] = sum(
            1 for h, at in started if h == node and (bound is None or at >= bound)
        )
    return result


@functools.cache
def run_seed(
    seed: int,
) -> tuple[
    dict[tuple[str, int], object], dict[tuple[str, int], int | None], Counter[str]
]:
    script = make_script(seed)
    delivered = deliver_order(script)
    store, registry = GraphStore(), WorkerRegistry()
    running = Oracle()  # only to say who answers a stats broadcast
    for op in delivered:
        match op.kind:
            case "ready":
                registry.register_worker(
                    op.node, op.pid, ["app.t"], event_timestamp=op.at
                )
            case "stats":
                # A real reply names every worker that answers; a known node it
                # leaves out goes offline, so the live ones are named too.
                replies: dict[str, tuple[int | None, list[str] | None]] = {
                    node: (live[0], ["app.t"])
                    for node in NODES
                    if (live := running.live(node)) is not None
                }
                replies[op.node] = (op.pid, ["app.t"])
                registry.apply_stats_reply(
                    registry.event_generations(), replies, pools=None, now=op.at
                )
            case "stop":
                registry.mark_shutdown(op.node, op.pid, op.at, now=op.at)
            case "silent":
                registry.mark_offline(op.node, op.pid)
            case "task":
                for event in op.events:
                    store.add_event(event)
        running.apply(op)
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=registry))
    body = TestClient(app).get("/api/workers?refresh=false").json()
    actual = {
        (w["hostname"], w["pid"]): w.get("active", "absent") for w in body["workers"]
    }
    expected = expected_active(script, delivered)
    return actual, expected, hit_cells(script, delivered, expected)


def hit_cells(
    script: list[Op], delivered: list[Op], expected: dict[tuple[str, int], int | None]
) -> Counter[str]:
    hits: Counter[str] = Counter()
    oracle = Oracle()
    for op in script:
        oracle.apply(op)
    starts = stuck_starts(delivered)
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
    hits["active>0"] += any(v for v in expected.values() if v)
    hits["live-zero"] += any(v == 0 for v in expected.values())
    hits["null-row"] += any(v is None for v in expected.values())
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
    return hits


@pytest.mark.parametrize("seed", SEEDS)
def test_active_matches_the_event_oracle(seed: int) -> None:
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
        "bound-keeps-newer-run", "other-node-runs-present",
    ]  # fmt: skip
    assert {cell: total[cell] for cell in wanted if total[cell] == 0} == {}
