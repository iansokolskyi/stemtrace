"""FAILURE events from a real prefork worker, by failure path and canvas shape.

A pool child reports an exception or a soft time limit itself; the worker's
main process reports a hard time limit and a killed child (WorkerLostError),
and there Celery's ``task_failure`` carries no request. Every shape is sent
once per failure path, and the main-process paths must publish the events the
child-side exception does: the same parent, root, group and chord fields, the
same worker, and the same FAILURE for each chain step left unrun.

Requires Redis (``REDIS_URL``, default ``redis://localhost:6379/15``): the
broker, the result backend and where stemtrace's events go.
"""

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import redis
from celery import Celery, chain, chord, group
from celery.canvas import Signature

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent
from stemtrace.core.graph import TaskGraph
from stemtrace.core.ports import ClosableTransport
from stemtrace.library.signals import connect_signals, disconnect_signals
from stemtrace.transports import get_transport

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]
APP_MODULE = "tests.integration.main_process_failure_worker_app"
TIMEOUT_SECONDS = 90
# How long the events a FAILURE leads to may trail it. The handler publishes
# them in the same call, so this only bounds a regression's wait.
TRAILING_SECONDS = 5


def _redis_available() -> bool:
    try:
        redis.from_url(REDIS_URL).ping()
    except redis.RedisError:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _redis_available(), reason="Redis not available"),
]

KINDS = {
    "exception": "mp.fail_exception",
    "soft_limit": "mp.fail_soft",
    "hard_limit": "mp.fail_hard",
    "killed": "mp.fail_killed",
}
MAIN_PROCESS_KINDS = ["hard_limit", "killed"]
EXCEPTION_TYPES = {
    "exception": f"{APP_MODULE}.Boom",
    "soft_limit": "billiard.exceptions.SoftTimeLimitExceeded",
    "hard_limit": "billiard.exceptions.TimeLimitExceeded",
    "killed": "billiard.exceptions.WorkerLostError",
}


class Shape(NamedTuple):
    """A canvas built around one failing task, and the roles that must fail."""

    build: Callable[[Callable[[str], Signature], Callable[[], Signature]], Signature]
    failed: frozenset[str]


# ``ok(role)`` and ``bad()`` make a task's signature; the failing task's role is "x".
SHAPES: dict[str, Shape] = {
    "plain": Shape(lambda ok, bad: bad(), frozenset({"x"})),
    "chain_first": Shape(
        lambda ok, bad: chain(bad(), ok("s1"), ok("s2")),
        frozenset({"x", "s1", "s2"}),
    ),
    "chain_middle": Shape(
        lambda ok, bad: chain(ok("s0"), bad(), ok("s2")),
        frozenset({"x", "s2"}),
    ),
    "chain_last": Shape(
        lambda ok, bad: chain(ok("s0"), ok("s1"), bad()),
        frozenset({"x"}),
    ),
    "group": Shape(
        lambda ok, bad: group(bad(), ok("m1")),
        frozenset({"x"}),
    ),
    "chord": Shape(
        lambda ok, bad: chord([bad(), ok("m1")], ok("cb")),
        frozenset({"x"}),
    ),
    # A chain as a chord header: the chain's last step is the header's member.
    "chord_chain_header": Shape(
        lambda ok, bad: chord([chain(ok("s0"), bad(), ok("s2")), ok("m1")], ok("cb")),
        frozenset({"x", "s2"}),
    ),
}


class LateChain(NamedTuple):
    """A chain whose first step is killed in ``on_success``, and when it is killed."""

    tasks: list[str]
    # The kill waits for this state from each of these steps, so the ordering a
    # test pins is guaranteed by the app, not by how fast the pool runs the steps.
    gate_state: TaskState
    gated_steps: tuple[str, ...]


# The first step dies in its ``on_success`` handler, after Celery sent the second step.
# In "ran" it dies once every later step succeeded; in "running" once the second step
# started (it then runs 8 s), so the third step is not yet sent.
LATE_CHAINS = {
    "ran": LateChain(
        ["mp.late_first", "mp.ok", "mp.ok", "mp.ok"],
        TaskState.SUCCESS,
        ("s2", "s3", "s4"),
    ),
    "running": LateChain(
        ["mp.late_first", "mp.slow", "mp.ok"], TaskState.STARTED, ("s2",)
    ),
}


class Scenario:
    """One canvas sent for one failure path, and the events its tasks produced."""

    def __init__(self, shape: str, kind: str) -> None:
        """Name the task IDs by role."""
        self.shape, self.kind = shape, kind
        self.roles = {
            role: str(uuid.uuid4()) for role in ("x", "s0", "s1", "s2", "m1", "cb")
        }
        self.events: list[TaskEvent] = []

    def token(self, value: str | None) -> str | None:
        """A task ID as its role; any other ID (a group's, a chord's) as ``<id>``."""
        if value is None:
            return None
        for role, task_id in self.roles.items():
            if value == task_id:
                return role
        return "<id>"

    def failures(self) -> dict[str, list[tuple[Any, ...]]]:
        """Each role's FAILUREs, as the fields every failure path must agree on.

        A list per role, so a task that gets two FAILUREs differs from one that gets one.
        """
        failures: dict[str, list[tuple[Any, ...]]] = {}
        for e in self.events:
            if e.state == TaskState.FAILURE:
                failures.setdefault(self.token(e.task_id) or "", []).append(
                    (
                        self.token(e.parent_id),
                        self.token(e.root_id),
                        self.token(e.group_id),
                        self.token(e.chord_id),
                        self.token(e.chord_callback_id),
                        e.hostname is not None,
                        e.queue is not None,
                        e.routing_key is not None,
                        e.retries,
                    )
                )
        return failures


class Run:
    """A worker, the scenarios sent to it and the events they produced."""

    def __init__(self, suffix: str, log_path: Path) -> None:
        """Name the run's queue and stream."""
        self.suffix = suffix
        self.log_path = log_path
        self.queue = f"mp-{suffix}"
        self.stream_key = f"mp_test_{suffix}:events"
        self.nodename = f"mp-{suffix}@localhost"
        self.client = redis.from_url(REDIS_URL)
        self.scenarios: dict[tuple[str, str], Scenario] = {}
        # Each LATE_CHAINS chain's task IDs by step, and its events.
        self.late_ids = {
            name: {f"s{i}": str(uuid.uuid4()) for i in range(1, len(late.tasks) + 1)}
            for name, late in LATE_CHAINS.items()
        }
        self.late_events: dict[str, list[TaskEvent]] = {
            name: [] for name in LATE_CHAINS
        }

    def events(self) -> list[TaskEvent | WorkerEvent]:
        """Every event published to the run's stream so far."""
        events: list[TaskEvent | WorkerEvent] = []
        for _, fields in self.client.xrange(self.stream_key):
            payload = fields[b"data"].decode()
            if '"event_type"' in payload:
                events.append(WorkerEvent.model_validate_json(payload))
            else:
                events.append(TaskEvent.model_validate_json(payload))
        return events

    def collect(self) -> None:
        """Sort the stream's task events into their scenarios."""
        by_id = {
            task_id: scenario
            for scenario in self.scenarios.values()
            for task_id in scenario.roles.values()
        }
        for scenario in self.scenarios.values():
            scenario.events = []
        self.late_events = {name: [] for name in LATE_CHAINS}
        late = {
            task_id: name
            for name, steps in self.late_ids.items()
            for task_id in steps.values()
        }
        for event in self.events():
            if isinstance(event, TaskEvent) and event.task_id in by_id:
                by_id[event.task_id].events.append(event)
            elif isinstance(event, TaskEvent) and event.task_id in late:
                self.late_events[late[event.task_id]].append(event)

    def wait(self, ready: Callable[[], bool], seconds: float) -> bool:
        """Poll until ``ready()``; False once ``seconds`` have passed."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if ready():
                return True
            time.sleep(0.2)
        return False

    def worker_log(self) -> str:
        """The tail of the worker's log, for a failed wait."""
        return self.log_path.read_text(errors="replace")[-4000:]

    def primary_failures_in(self) -> bool:
        """Whether every scenario's failing task has its FAILURE."""
        self.collect()
        return all("x" in s.failures() for s in self.scenarios.values())

    def late_chains_have(self, state: TaskState, steps: tuple[str, ...]) -> bool:
        """Whether, in every late chain, each of ``steps`` has an event in ``state``."""
        self.collect()
        return all(
            (task_id, state) in {(e.task_id, e.state) for e in self.late_events[name]}
            for name, ids in self.late_ids.items()
            for step, task_id in ids.items()
            if step in steps
        )

    def all_failures_in(self) -> bool:
        """Whether every scenario has the FAILURE of each task its shape fails."""
        self.collect()
        return all(
            set(s.failures()) >= SHAPES[s.shape].failed for s in self.scenarios.values()
        )


def _start_worker(run: Run) -> subprocess.Popen[bytes]:
    env = {**os.environ, "MP_BROKER_URL": REDIS_URL, "MP_SUFFIX": run.suffix}
    with run.log_path.open("wb") as log_file:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                APP_MODULE,
                "worker",
                "--pool=prefork",
                "--concurrency=4",
                f"--hostname={run.nodename}",
                f"--queues={run.queue}",
                "--loglevel=INFO",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )


def _stop_worker(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _send_late_chains(sender: Celery, run: Run) -> None:
    for name, late in LATE_CHAINS.items():
        ids = run.late_ids[name]
        gate = {
            "state": late.gate_state.value,
            "task_ids": [ids[step] for step in late.gated_steps],
        }
        chain(
            *(
                sender.signature(task, kwargs={"gate": gate} if i == 1 else None).set(
                    task_id=ids[f"s{i}"]
                )
                for i, task in enumerate(late.tasks, start=1)
            )
        ).apply_async()


def _send(sender: Celery, scenario: Scenario) -> None:
    ids = scenario.roles

    def ok(role: str) -> Signature:
        name = "mp.callback" if role == "cb" else "mp.ok"
        return sender.signature(name).set(task_id=ids[role])

    def bad() -> Signature:
        return sender.signature(KINDS[scenario.kind]).set(task_id=ids["x"])

    SHAPES[scenario.shape].build(ok, bad).apply_async()


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Run]:
    """Run a worker, send every shape once per failure path and collect the events."""
    suffix = uuid.uuid4().hex[:8]
    run = Run(suffix, tmp_path_factory.mktemp("mp") / "worker.log")
    sender = Celery("mp_sender", broker=REDIS_URL, backend=REDIS_URL)
    sender.conf.update(task_serializer="json", task_default_queue=run.queue)
    transport = get_transport(REDIS_URL, prefix=f"mp_test_{suffix}", ttl=300)
    proc = _start_worker(run)
    try:
        assert run.wait(
            lambda: any(isinstance(e, WorkerEvent) for e in run.events()),
            TIMEOUT_SECONDS,
        ), run.worker_log()
        connect_signals(transport)
        try:
            for shape in SHAPES:
                for kind in KINDS:
                    scenario = run.scenarios[shape, kind] = Scenario(shape, kind)
                    _send(sender, scenario)
        finally:
            disconnect_signals()
        assert run.wait(run.primary_failures_in, TIMEOUT_SECONDS), run.worker_log()
        # The unrun steps' FAILUREs trail the failing task's, in the same handler.
        run.wait(run.all_failures_in, TRAILING_SECONDS)
        # The late chains go once the earlier scenarios are done; each waits for its gate.
        connect_signals(transport)
        try:
            _send_late_chains(sender, run)
        finally:
            disconnect_signals()
        # Every later step runs whatever stemtrace publishes, and the first step's
        # FAILURE is the main process's; the later steps' FAILUREs trail it.
        assert run.wait(
            lambda: (
                run.late_chains_have(TaskState.FAILURE, ("s1",))
                and run.late_chains_have(TaskState.SUCCESS, ("s2", "s3", "s4"))
            ),
            TIMEOUT_SECONDS,
        ), run.worker_log()
        run.wait(
            lambda: run.late_chains_have(TaskState.FAILURE, ("s2", "s3", "s4")),
            TRAILING_SECONDS,
        )
        yield run
    finally:
        _stop_worker(proc)
        if isinstance(transport, ClosableTransport):
            transport.close()
        run.client.delete(run.stream_key, run.queue)


def _failure_of(scenario: Scenario, role: str) -> TaskEvent:
    [event] = [
        e
        for e in scenario.events
        if e.state == TaskState.FAILURE and scenario.token(e.task_id) == role
    ]
    return event


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("kind", KINDS)
def test_failing_task_fails_with_its_exception_type_on_its_worker(
    run: Run, shape: str, kind: str
) -> None:
    failure = _failure_of(run.scenarios[shape, kind], "x")

    assert failure.exception_type == EXCEPTION_TYPES[kind]
    assert failure.hostname == run.nodename


@pytest.mark.parametrize("shape", SHAPES)
def test_exception_failures_are_the_shapes_failed_tasks(run: Run, shape: str) -> None:
    # The oracle the other failure paths are held to is itself right.
    assert set(run.scenarios[shape, "exception"].failures()) == SHAPES[shape].failed


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("kind", ["soft_limit", *MAIN_PROCESS_KINDS])
def test_failures_match_the_exception_ones(run: Run, shape: str, kind: str) -> None:
    oracle = run.scenarios[shape, "exception"].failures()

    assert run.scenarios[shape, kind].failures() == oracle


def _late_events(run: Run, name: str, step: str, state: TaskState) -> list[TaskEvent]:
    task_id = run.late_ids[name][step]
    return [
        e for e in run.late_events[name] if e.task_id == task_id and e.state == state
    ]


def _late_failure_time(run: Run, name: str) -> datetime:
    """When the main process recorded the failure: its last FAILURE in the chain."""
    return max(
        e.timestamp for e in run.late_events[name] if e.state == TaskState.FAILURE
    )


def _late_states(
    run: Run, name: str, until: datetime | None = None
) -> dict[str, TaskState]:
    """The later steps' node states, from the events up to ``until`` (default: all)."""
    steps = {task_id: step for step, task_id in run.late_ids[name].items()}
    graph = TaskGraph()
    for event in run.late_events[name]:
        if until is None or event.timestamp <= until:
            graph.add_event(event)
    return {
        steps[task_id]: node.state
        for task_id, node in graph.nodes.items()
        if steps[task_id] != "s1"
    }


# The main process records a failure after the pool process already sent the next
# step, so its request.chain still lists every later step (stemtrace publishes a
# FAILURE for each, as celery's backend stores one: celery 5.6.3 backends/base.py
# mark_as_failure). The tests below pin what that shows; docs/graphs.md says it.
def test_late_failure_fails_the_later_steps_that_already_ran(run: Run) -> None:
    failed_at = _late_failure_time(run, "ran")

    for step in ("s2", "s3", "s4"):
        [success] = _late_events(run, "ran", step, TaskState.SUCCESS)
        [failure] = _late_events(run, "ran", step, TaskState.FAILURE)
        assert success.timestamp < failed_at
        assert failure.hostname is None
    # The later FAILURE wins over the earlier SUCCESS.
    assert _late_states(run, "ran") == dict.fromkeys(
        ("s2", "s3", "s4"), TaskState.FAILURE
    )


def test_late_failure_shows_running_and_unsent_steps_failed_until_they_finish(
    run: Run,
) -> None:
    failed_at = _late_failure_time(run, "running")
    [s2_started] = _late_events(run, "running", "s2", TaskState.STARTED)
    [s2_success] = _late_events(run, "running", "s2", TaskState.SUCCESS)
    [s3_pending] = _late_events(run, "running", "s3", TaskState.PENDING)
    [s3_failure] = _late_events(run, "running", "s3", TaskState.FAILURE)

    # When the first step dies s2 is running and s3 is not sent yet (its FAILURE
    # precedes its PENDING).
    assert s2_started.timestamp < failed_at < s2_success.timestamp
    assert s3_failure.timestamp < s3_pending.timestamp
    assert _late_states(run, "running", until=failed_at) == dict.fromkeys(
        ("s2", "s3"), TaskState.FAILURE
    )
    # Each step's own result then replaces its FAILURE.
    assert _late_states(run, "running") == dict.fromkeys(
        ("s2", "s3"), TaskState.SUCCESS
    )
