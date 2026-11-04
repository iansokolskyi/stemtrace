"""A simulated fleet driving a real ``WorkerRegistry`` for the fleet-alert property test.

Three things run side by side: the **truth** (what each worker really is), the
real registry and refresh function fed through a fake Celery ``Inspect``, and an
**oracle** that tracks only what the server could have observed, written from
the spec ("D4b fleet item"). ``World.evaluate`` checks the invariant both ways.
"""

import random
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.anomalies import AnomalyKind, AnomalySettings, classify_anomalies
from stemtrace.server import store
from stemtrace.server.api.routes import _refresh_worker_registry_from_inspect
from stemtrace.server.store import WORKER_STALE_TIMEOUT_MINUTES, WorkerRegistry

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
STALE = timedelta(minutes=WORKER_STALE_TIMEOUT_MINUTES)
CLEANUP = timedelta(minutes=30)
MISS_GAP = timedelta(seconds=2)  # D4d: the shared inspect interval two misses need
LATE = timedelta(minutes=10)  # how late a stopped worker's last events may arrive
NODES = ("celery@a", "celery@b", "celery@c")
ROWS = (
    "28",
    "28_after_cleanup",
    "28a",
    "28b",
    "28c",
    "28d",
    "28e",
    "28g",
    "28h",
    "28i",
    "28j",
    "28k",
    "28k_long",
    "28l",
    "28m",
    "28n",
    "28o",
)
ADVANCES = (1, 20, 100, 200, 300, 700, 2000)  # seconds: inside and past 3 and 10 min


class Clock:
    """The registry's clock, moved by the simulation."""

    def __init__(self) -> None:
        self.now = T0

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class ONode:
    """What the oracle knows of a nodename: stopped by a reported event, last sign of life."""

    stopped: bool
    last_seen: datetime
    listed: bool
    misses: int = 0  # D4d: counted unanswered broadcasts since its last sign of life
    first_miss: datetime | None = None
    last_miss: datetime | None = None


@dataclass(frozen=True)
class Pending:
    """One eligible PENDING task (the fleet item needs at least one)."""

    task_id: str
    name: str
    state_since: datetime


class FakeInspect:
    """A Celery ``Inspect`` whose replies come from the truth, running hooks as it is called."""

    def __init__(self, world: "World", plan: "Plan") -> None:
        self._world, self._plan = world, plan

    def stats(self) -> dict[str, object] | None:
        self._world.hook("stats", self._plan)
        if self._plan.stats_raises:
            raise OSError("broker unreachable")
        plan = self._plan
        plan.stats_answered = {
            n: ({"pid": p} if plan.pids else {}) for n, p in plan.answers.items()
        }
        return dict(plan.stats_answered) or None

    def registered(self) -> dict[str, object] | None:
        self._world.hook("registered", self._plan)
        if self._plan.registered_raises:
            raise OSError("registered failed")
        return {n: ["app.t"] for n in self._plan.answers} or None

    def ping(self) -> dict[str, object] | None:
        self._world.hook("ping", self._plan)
        if self._plan.ping_raises:
            raise OSError("ping failed")
        return {n: {"ok": "pong"} for n in self._plan.ping_answers} or None


@dataclass
class Plan:
    """One refresh: what is asked, who answers (snapshotted when asked), what happens meanwhile."""

    stats_raises: bool
    registered_raises: bool
    ping_raises: bool
    pids: bool
    answers: dict[str, int]
    ping_answers: list[str]
    meanwhile: dict[str, list[str]]
    seen_generation: dict[str, int]
    stats_answered: dict[str, dict[str, int]] | None = None


class World:
    """Truth, registry, oracle and the reach counters for one seeded run."""

    def __init__(
        self, seed: int, reached: Counter[str], *, broker: bool, deaf: bool = False
    ) -> None:
        self.rng = random.Random(seed)
        self.reached = reached
        self.broker = broker
        self.deaf = deaf  # remote control off: no worker ever answers an inspect
        self.clock = Clock()
        self.registry = WorkerRegistry()
        self.truth: dict[str, str] = {}  # absent|alive|busy|killed|stopped
        self.pid: dict[str, int] = {}
        for n in NODES:
            self.truth[n] = self.rng.choice(["absent", "alive", "killed"])
            self.pid[n] = 1
        # Oracle.
        self.ever_seen = False
        self.nodes: dict[str, ONode] = {}
        self.completed: datetime | None = None
        self.channel_proven = False
        self.rev_listed: set[str] = set()
        # Names restarted without a worker_ready since their last announced start.
        self.unannounced: set[str] = set()
        self.answered_last: frozenset[str] = frozenset()
        # Who answered each of the last two completed broadcasts (D4d invariant).
        self.answered_window: list[frozenset[str]] = []
        self.had_miss_reset: set[str] = set()  # answered after a miss (row 28n)
        self.generation: dict[str, int] = dict.fromkeys(NODES, 0)
        # Truth-side logs for the two direction checks.
        self.reported_stopped: set[str] = set()
        self.last_event: dict[str, datetime] = {}
        self.answered_latest: set[str] = set()
        self.last_late_event: datetime | None = None
        self.last_stopped_unlisted: bool = False
        self.stop_ts: dict[str, datetime] = {}
        self.stopped_at: dict[str, datetime] = {}  # oracle: when a stop was reported
        self.in_flight: Plan | None = None
        self.stage = ""
        self.killed_since_completion = False

    # --- the registry's view ------------------------------------------------

    def offline(self) -> bool:
        """What the registry and classifier report now: the fleet item present."""
        now = self.clock.now
        facts = self.registry.registry_facts(broker_configured=self.broker, now=now)
        pending = [Pending("t1", "app.t", now - timedelta(hours=1))]
        report = classify_anomalies(
            [],
            now=now,
            runtime_stats={},
            timeline={},
            settings=AnomalySettings(),
            registry=facts,
            pending=pending,
        )
        return report.counts[AnomalyKind.NO_WORKERS_ONLINE] == 1

    # --- the oracle, from the spec -----------------------------------------

    def silent(self, name: str, node: ONode) -> bool:
        """Silent with evidence: two counted misses after the last sign, past the window."""
        return (
            self.channel_proven
            and node.misses >= 2
            and node.first_miss is not None
            and node.first_miss > node.last_seen
            and node.last_seen < self.clock.now - STALE
        )

    def expected_offline(self) -> bool:
        """Offline: seen since start, and every known worker stopped or silent with evidence."""
        return self.ever_seen and all(
            n.stopped or self.silent(name, n) for name, n in self.nodes.items()
        )

    def _answer(self, name: str, *, with_pid: bool) -> None:
        now = self.clock.now
        node = self.nodes.get(name)
        if with_pid:
            self.nodes[name] = ONode(False, now, True)
            self.stopped_at.pop(name, None)
            self.reported_stopped.discard(name)
            self.rev_listed.discard(name)
            self.ever_seen = True
        elif node is not None:
            if not node.stopped:  # a ping never revives a stopped worker
                node.last_seen = now
                node.misses, node.first_miss, node.last_miss = 0, None, None
        elif (
            name not in self.stopped_at
        ):  # a stopped nodename can't be revived by a ping
            self.nodes[name] = ONode(False, now, False)
            self.reported_stopped.discard(
                name
            )  # its stop memory expired; it is running
            self.ever_seen = True
        self.last_stopped_unlisted = False

    # --- operations (each updates truth, registry and oracle) ---------------

    def start(self, name: str, *, announce: bool | None = None) -> None:
        """A worker process starts; its ``worker_ready`` is seen unless it is the first start."""
        self.pid[name] += 1
        self.truth[name] = "alive"
        node = self.nodes.get(name)
        # A restart the server listed under another pid always sends worker_ready
        # (a shutdown for an unlisted pid would not stop the listed one).
        announced = (
            announce
            if announce is not None
            else (node is not None and node.listed and not node.stopped)
            or self.rng.random() < 0.8
        )
        if announced:
            self.unannounced.discard(name)
        else:
            self.unannounced.add(name)
        if name in self.reported_stopped and not announced:
            self.clock.advance(1)  # its events are then stamped after the stop
        if announced:
            self.registry.register_worker(
                name, self.pid[name], ["app.t"], event_timestamp=self.clock.now
            )
            self.generation[name] += 1
            self.reported_stopped.discard(name)
            self.rev_listed.discard(name)
            self.stopped_at.pop(name, None)
            self.nodes[name] = ONode(False, self.clock.now, True)
            self.ever_seen = True
            self.last_event.pop(name, None)

    def stop(self, name: str) -> None:
        """A clean stop: ``worker_shutdown`` (or ``worker_terminating``) is reported."""
        if self.truth[name] not in ("alive", "busy") or self.rng.random() < 0.15:
            if self.truth[name] in ("alive", "busy"):
                self.truth[name] = "killed"  # the event was lost: the server can't tell
            return
        self.truth[name] = "stopped"
        self.registry.mark_shutdown(name, self.pid[name], self.clock.now)
        self.stopped_at[name] = self.stop_ts[name] = self.clock.now
        self.generation[name] += 1
        self.reported_stopped.add(name)
        self.rev_listed.discard(name)
        node = self.nodes.get(name)
        if node is not None and node.listed:
            node.stopped = True
        elif node is not None:
            del self.nodes[name]
            self.last_stopped_unlisted = True

    def kill(self, name: str) -> None:
        """SIGKILL: no event."""
        if self.truth[name] in ("alive", "busy"):
            self.truth[name] = "killed"
            self.killed_since_completion = True

    def toggle_busy(self, name: str) -> None:
        """A worker stops answering broadcasts (or resumes)."""
        if self.truth[name] == "alive":
            self.truth[name] = "busy"
        elif self.truth[name] == "busy":
            self.truth[name] = "alive"

    def task(self, name: str) -> None:
        """A task event from a worker that is running."""
        if self.truth[name] not in ("alive", "busy"):
            return
        now = self.clock.now
        self.registry.mark_seen(name, now)
        self.ever_seen = True
        self.last_event[name] = now
        node = self.nodes.get(name)
        if node is None:
            self.nodes[name] = ONode(False, now, False)
            self.reported_stopped.discard(name)
        elif node.stopped:  # stamped after the stop: a restart whose ready was missed
            self.nodes[name] = ONode(False, now, False)
            self.reported_stopped.discard(name)
            self.rev_listed.add(name)
        else:
            node.last_seen = max(node.last_seen, now)
            node.misses, node.first_miss, node.last_miss = 0, None, None
        self.last_stopped_unlisted = False

    def late_task(self, name: str) -> None:
        """A task's last event, stamped before its worker stopped, reaches the server after."""
        if self.truth[name] != "stopped" or self.clock.now - self.stop_ts[name] > LATE:
            return
        stamp = self.stop_ts[name] - timedelta(seconds=1)
        self.registry.mark_seen(name, stamp)
        self.ever_seen = True
        self.last_late_event = self.clock.now
        if name not in self.nodes and name not in self.stopped_at:
            self.nodes[name] = ONode(False, stamp, False)

    def revive_run(self, name: str) -> None:
        """A worker stops cleanly, restarts with its ``worker_ready`` missed, then runs busy for 35 minutes."""
        if self.truth[name] not in ("alive", "busy"):
            return
        self.truth[name] = "alive"
        self.stop(name)
        if self.truth[name] != "stopped":
            return
        self.clock.advance(1)
        self.start(name, announce=False)
        for _ in range(35):
            self.clock.advance(60)
            self.task(name)
            self.cleanup()
            self.evaluate()
        self.toggle_busy(name)  # too busy to answer the next broadcast
        if self.broker:
            self.refresh()
        self.evaluate()

    def advance(self) -> None:
        """Move the clock, sometimes past the 30-minute cleanup."""
        self.clock.advance(self.rng.choice(ADVANCES))

    def cleanup(self) -> None:
        """The consumer's periodic stale check: remove very old workers."""
        self.registry.remove_stale_workers(now=self.clock.now)
        cutoff = self.clock.now - CLEANUP
        self.stopped_at = {n: t for n, t in self.stopped_at.items() if t >= cutoff}
        for name, node in list(self.nodes.items()):
            if node.listed and node.last_seen < cutoff:
                if node.stopped or self.silent(name, node):
                    del self.nodes[name]
                else:
                    self.nodes[name] = replace(node, stopped=False, listed=False)
            elif (
                not node.listed and node.last_seen < cutoff and self.silent(name, node)
            ):
                del self.nodes[name]

    # --- refresh and evaluation ---------------------------------------------

    def plan(self) -> Plan:
        """Decide what the next refresh asks and who answers it, as of now."""
        rng = self.rng
        up = rng.random() < 0.85
        # Each alive worker independently misses a broadcast now and then (D4d:
        # isolated misses), on top of whole-fleet misses and busy runs of them.
        answering = [
            n
            for n in NODES
            if self.truth[n] == "alive" and not self.deaf and rng.random() > 0.12
        ]
        ops = ["evaluate", "task", "advance", "start", "stop", "kill", "cleanup"]
        meanwhile = {
            stage: [rng.choice(ops) for _ in range(rng.choice([0, 0, 1, 2]))]
            for stage in ("stats", "registered", "ping")
        }
        # stats() completes empty although workers are alive (a slow link): the ping
        # fallback may still answer, and registered() may still fail (D7a readings 4, 5, 8).
        # Not while a name was restarted unannounced: a later worker_shutdown for its new
        # pid leaves the old pid live in the registry but removes it from this oracle (a
        # pre-existing model gap, parked: it is not an inspect rule).
        stats_silent = up and not self.unannounced and rng.random() < 0.2
        return Plan(
            stats_raises=not up or rng.random() < 0.05,
            registered_raises=rng.random() < 0.1,
            ping_raises=not up or rng.random() < 0.3,
            pids=rng.random() < 0.9,
            answers={n: self.pid[n] for n in answering}
            if up and not stats_silent
            else {},
            ping_answers=[n for n in answering if rng.random() < 0.9] if up else [],
            meanwhile=meanwhile,
            seen_generation=dict(self.generation),
        )

    def hook(self, stage: str, plan: Plan) -> None:
        """Run what happens while the refresh waits (events, evaluations, time)."""
        self.stage = stage
        for op in plan.meanwhile[stage]:
            self.run_op(op)

    def refresh(self) -> None:
        """Run the real refresh function against a fake inspect, then update the oracle."""
        plan = self.plan()
        self.in_flight = plan
        _refresh_worker_registry_from_inspect(self.registry, FakeInspect(self, plan))
        self.in_flight = None
        self.stage = ""
        self._oracle_refresh(plan)

    def _oracle_refresh(self, plan: Plan) -> None:
        now = self.clock.now
        before = {n: o.misses for n, o in self.nodes.items()}
        answered: set[str] = set()
        completed = not plan.stats_raises
        if plan.stats_answered:
            self.channel_proven = True
            for name in plan.stats_answered:
                answered.add(name)
                if self.generation[name] == plan.seen_generation[name]:
                    self._answer(name, with_pid=plan.pids)
        else:
            # Written from the D7a spec: a stats() error ends the attempt (reading 4); empty
            # stats whose registered() fails ends it too, broadcast stamped (readings 5, 8);
            # otherwise empty stats falls back to ping().
            ping_runs = not plan.stats_raises and not plan.registered_raises
            for name in [] if plan.ping_raises or not ping_runs else plan.ping_answers:
                answered.add(name)
                self.channel_proven = True
                self._answer(name, with_pid=False)
        if completed:
            self._count_misses(now, answered, before)
            self.completed = now
            self.answered_last = frozenset(answered)
            self.answered_window = [*self.answered_window[-1:], frozenset(answered)]
            self.answered_latest = {
                n for n in answered if n not in self.reported_stopped
            }
            self.killed_since_completion = False

    def _count_misses(
        self, now: datetime, answered: set[str], before: dict[str, int]
    ) -> None:
        """D4d: a completed broadcast an unstopped node didn't answer is a miss, if 2 s after the last."""
        self.had_miss_reset |= {n for n in answered if before.get(n)}
        for name, node in self.nodes.items():
            if node.stopped:
                continue
            if name in answered:
                node.misses, node.first_miss, node.last_miss = 0, None, None
            elif not self.channel_proven:
                continue
            elif node.misses == 0:
                if now > node.last_seen:
                    node.misses, node.first_miss, node.last_miss = 1, now, now
                    if name in self.had_miss_reset:
                        self.reached["28n"] += 1
                        self.had_miss_reset.discard(name)
            elif node.last_miss is not None and now - node.last_miss >= MISS_GAP:
                node.misses, node.last_miss = 2, now
            else:
                self.reached["28o"] += 1

    def evaluate(self) -> None:
        """Compare the registry with the oracle and the truth, and record what was reached."""
        got = self.offline()
        want = self.expected_offline()
        assert got == want, self.describe(got, want)
        now = self.clock.now
        for answered in self.answered_window:
            if answered - self.reported_stopped:
                assert not got, "offline while a worker answered a recent broadcast"
        for name, at in self.last_event.items():
            stop = self.stop_ts.get(name)
            if (stop is None or at > stop) and at >= now - STALE:
                assert not got, "offline while a worker sent an event in the window"
        late = self.last_late_event
        recently_late = late is not None and late >= now - STALE
        learned = list(self.nodes)
        if (
            self.ever_seen
            and not recently_late
            and all(n in self.reported_stopped for n in learned)
        ):
            assert got, "stopped fleet not offline: " + self.describe(got, want)
        self.tag(got)

    def describe(self, got: bool, want: bool) -> str:
        """The state at a mismatch."""
        return (
            f"registry={got} oracle={want} now={self.clock.now} nodes={self.nodes} "
            f"completed={self.completed} answered={set(self.answered_last)} "
            f"truth={self.truth}"
        )

    def tag(self, got: bool) -> None:
        """Count which spec rows this evaluation reached."""
        live = {n: o for n, o in self.nodes.items() if not o.stopped}
        for row in (
            self._rows_stopped(got, live),
            self._rows_silent(got, live),
            self._rows_no_evidence(got, live),
            self._rows_concurrent(got, live),
        ):
            self.reached.update(row)

    def _rows_stopped(self, got: bool, live: dict[str, ONode]) -> list[str]:
        rows = []
        if got and self.ever_seen and not live:
            rows.append("28")
            if not self.nodes:
                rows.append("28_after_cleanup")
        if not self.ever_seen and "killed" in self.truth.values():
            assert not got
            rows.append("28a")
        if got and self.last_stopped_unlisted and not live:
            rows.append("28i")
        if not got and any(n in live and not live[n].listed for n in self.rev_listed):
            rows.append("28k")
        if not got and self._busy_revived_for_long(live):
            rows.append("28k_long")
        return rows

    def _busy_revived_for_long(self, live: dict[str, ONode]) -> bool:
        """A revived worker still sending events more than 30 minutes after its stop."""
        now = self.clock.now
        return any(
            n in live
            and now - self.stop_ts[n] > timedelta(minutes=31)
            and self.last_event.get(n, T0) >= now - STALE
            for n in self.rev_listed
        )

    def _rows_silent(self, got: bool, live: dict[str, ONode]) -> list[str]:
        """28b (a missed broadcast inside the window), 28c (silent with evidence), 28h."""
        rows = []
        now = self.clock.now
        if got and live and self.completed is not None:
            rows.append("28c")
        if got and live and all(o.misses >= 2 for o in live.values()):
            rows.append("28m")
        if (
            not got
            and live
            and self.channel_proven
            and all(o.misses == 1 for o in live.values())
            and all(o.last_seen < now - STALE for o in live.values())
        ):
            rows.append("28l")
        unproven = not got and not self.channel_proven and self.completed is not None
        if unproven and any(
            self.completed > o.last_seen and o.last_seen < now - STALE
            for n, o in live.items()
            if n not in self.answered_last
        ):
            rows.append("28j")
        if not live or got or self.completed is None:
            return rows
        held = [
            n
            for n, o in live.items()
            if n not in self.answered_last and self.completed > o.last_seen
        ]
        if len(held) == len(live) and any(
            live[n].last_seen >= now - STALE for n in held
        ):
            rows.append("28b")
        gap = now - self.completed
        if (
            self.answered_last
            and timedelta(minutes=3) <= gap <= timedelta(minutes=10)
            and any(n in live for n in self.answered_last)
        ):
            rows.append("28h")
        return rows

    def _rows_no_evidence(self, got: bool, live: dict[str, ONode]) -> list[str]:
        """28d (idle fleet, no broker) and 28e (killed fleet, no completed inspect since)."""
        rows = []
        if not live or got:
            return rows
        if (
            not self.broker
            and all(self.truth[n] in ("alive", "busy") for n in live)
            and any(o.last_seen < self.clock.now - STALE for o in live.values())
        ):
            rows.append("28d")
        if all(self.truth[n] == "killed" for n in live) and (
            self.completed is None
            or all(self.completed <= o.last_seen for o in live.values())
        ):
            rows.append("28e")
        return rows

    def _rows_concurrent(self, got: bool, live: dict[str, ONode]) -> list[str]:
        """28g: evaluated while a refresh whose answerers look silent is in flight."""
        plan = self.in_flight
        old = self.clock.now - STALE
        if (
            plan is not None
            and self.stage in ("registered", "ping")
            and not got
            and any(n in live and live[n].last_seen < old for n in plan.answers)
        ):
            return ["28g"]
        return []

    def run_op(self, op: str) -> None:
        """Run one named operation on a random node."""
        name = self.rng.choice(NODES)
        per_node = {
            "start": self.start,
            "stop": self.stop,
            "kill": self.kill,
            "busy": self.toggle_busy,
            "task": self.task,
            "late": self.late_task,
            "revive_run": self.revive_run,
        }
        if op in per_node:
            per_node[op](name)
        elif op == "advance":
            self.advance()
        elif op == "cleanup":
            self.cleanup()
        elif op == "refresh":
            if self.broker:
                self.refresh()
        elif op == "refresh_again":  # back-to-back requests, 0-3 s apart (D4d)
            if self.broker:
                self.refresh()
                self.clock.advance(self.rng.choice([0, 1, 2, 3]))
                self.refresh()
        else:
            self.evaluate()

    def run(self, steps: int) -> None:
        """Run ``steps`` random operations, evaluating after each one."""
        ops = [
            "start",
            "stop",
            "kill",
            "busy",
            "task",
            "task",
            "late",
            "advance",
            "advance",
            "cleanup",
            "refresh",
            "refresh",
            "refresh_again",
            "revive_run",
            "evaluate",
        ]
        for _ in range(steps):
            self.run_op(self.rng.choice(ops))
            self.evaluate()


def patched_clock(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    """Make the registry read the world's clock wherever it isn't passed a time."""
    monkeypatch.setattr(store, "_utcnow", lambda: world.clock.now)
