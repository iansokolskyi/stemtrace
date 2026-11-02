"""Real-worker checks M3, M3b, M4 and M5 of the anomaly classifier.

Each test runs a real Celery worker (``output_worker_app``) against Redis
(``REDIS_URL``), reads the events it published and classifies them. Set
``ANOMALY_RECORD=1`` to rewrite the recordings in ``tests/data/``.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO

import celery
import pytest
import redis
from celery import Celery

from stemtrace.core.anomalies import AnomalyEvidence, AnomalyKind, AnomalySettings
from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from tests.unit.anomaly_pipeline import classify, through_nth_ready

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "tests" / "data"
TIMEOUT_S = 90
SETTINGS = AnomalySettings(
    stuck_factor=3,
    stuck_floor=timedelta(seconds=1),
    stuck_ceiling=timedelta(seconds=5),
    stuck_min_samples=3,
)
Entry = TaskEvent | WorkerEvent


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


class Run:
    """One queue, stream and nodename; workers come and go under it."""

    def __init__(self, log_dir: Path) -> None:
        """Pick names; no worker runs yet."""
        suffix = uuid.uuid4().hex[:8]
        self.queue = f"anomaly-{suffix}"
        self.prefix = f"anomaly_{suffix}"
        self.stream_key = f"{self.prefix}:events"
        self.unacked_prefix = f"unacked_{suffix}"
        self.nodename = f"anomaly{suffix}@localhost"
        self.client = redis.from_url(REDIS_URL)
        self.sender = Celery("anomaly_sender", broker=REDIS_URL)
        self.log_dir = log_dir
        self.procs: list[subprocess.Popen[bytes]] = []

    def start(
        self, pool: str, *, visibility_timeout: int | None = None
    ) -> subprocess.Popen[bytes]:
        """Start a worker with this run's nodename in its own process group."""
        env = {
            **os.environ,
            "REDIS_URL": REDIS_URL,
            "OUTPUT_TEST_QUEUE": self.queue,
            "OUTPUT_TEST_PREFIX": self.prefix,
            "OUTPUT_TEST_UNACKED_PREFIX": self.unacked_prefix,
        }
        if visibility_timeout is not None:
            env["OUTPUT_TEST_VISIBILITY_TIMEOUT"] = str(visibility_timeout)
        with (self.log_dir / f"worker{len(self.procs)}.log").open("wb") as log:
            proc = self._spawn(pool, env, log)
        self.procs.append(proc)
        return proc

    def _spawn(
        self, pool: str, env: dict[str, str], log: IO[bytes]
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "tests.integration.output_worker_app",
                "worker",
                f"--pool={pool}",
                "--concurrency=2",
                f"--hostname={self.nodename}",
                "--loglevel=INFO",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def entries(self) -> list[Entry]:
        """Every event published so far, in stream order."""
        out: list[Entry] = []
        for _, fields in self.client.xrange(self.stream_key):
            payload = fields[b"data"].decode()
            if '"event_type"' in payload:
                out.append(WorkerEvent.model_validate_json(payload))
            else:
                out.append(TaskEvent.model_validate_json(payload))
        return out

    def raw_entries(self) -> list[str]:
        """The stream's payloads as published."""
        return [f[b"data"].decode() for _, f in self.client.xrange(self.stream_key)]

    def wait(self, what: str, check: Callable[[list[Entry]], bool]) -> list[Entry]:
        """Wait until ``check(entries)``; fail with the worker logs."""
        deadline = time.monotonic() + TIMEOUT_S
        while time.monotonic() < deadline:
            entries = self.entries()
            if check(entries):
                return entries
            time.sleep(0.2)
        logs = "\n".join(
            p.read_text(errors="replace")[-2000:] for p in self.log_dir.glob("*.log")
        )
        raise AssertionError(f"{what} not seen in time; worker logs:\n{logs}")

    def send(self, name: str, seconds: float) -> str:
        """Send a task to the run's queue."""
        return str(self.sender.send_task(name, args=[seconds], queue=self.queue).id)

    def sigkill_group(self, proc: subprocess.Popen[bytes]) -> None:
        """SIGKILL the worker's main process and its pool children together."""
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=20)

    def close(self) -> None:
        """Kill every worker left and delete the run's broker keys."""
        for proc in self.procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=20)
        self.client.delete(
            self.stream_key,
            self.queue,
            f"_kombu.binding.{self.queue}",
            self.unacked_prefix,
            f"{self.unacked_prefix}_index",
            f"{self.unacked_prefix}_mutex",
        )


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    """A fresh run; every worker it started is killed afterwards."""
    current = Run(tmp_path)
    try:
        yield current
    finally:
        current.close()


def _ready(entries: list[Entry]) -> int:
    return sum(
        isinstance(e, WorkerEvent) and e.event_type == WorkerEventType.WORKER_READY
        for e in entries
    )


def _count(entries: list[Entry], task_id: str, state: TaskState) -> int:
    return sum(
        isinstance(e, TaskEvent) and e.task_id == task_id and e.state == state
        for e in entries
    )


def _split(entries: list[Entry]) -> tuple[list[TaskEvent], list[WorkerEvent]]:
    return (
        [e for e in entries if isinstance(e, TaskEvent)],
        [e for e in entries if isinstance(e, WorkerEvent)],
    )


def _kinds(
    entries: list[Entry], now: datetime
) -> dict[str, tuple[AnomalyKind, object]]:
    tasks, workers = _split(entries)
    report = classify(tasks, workers, now=now, settings=SETTINGS)
    return {t: (a.kind, a.evidence) for t, a in report.anomalies.items()}


def _record(name: str, description: str, raw: list[str]) -> None:
    if not os.environ.get("ANOMALY_RECORD"):
        return
    (DATA / f"{name}.json").write_text(
        json.dumps(
            {
                "description": description,
                "celery": celery.__version__,
                "recorded": datetime.now(timezone.utc).date().isoformat(),
                "entries": [json.loads(p) for p in raw],
            },
            indent=1,
        )
        + "\n"
    )


def _after_last(entries: list[Entry], seconds: float) -> datetime:
    return max(e.timestamp for e in entries) + timedelta(seconds=seconds)


@pytest.mark.parametrize("pool", ["prefork", "threads", "solo"])
def test_m3_warm_stop_with_running_task_is_no_anomaly(run: Run, pool: str) -> None:
    proc = run.start(pool)
    run.wait("worker_ready", lambda es: _ready(es) == 1)
    task_id = run.send("output_test.sleepy", 3)
    run.wait("STARTED", lambda es: _count(es, task_id, TaskState.STARTED) == 1)

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=TIMEOUT_S)

    entries = run.entries()
    order = [
        e.event_type.value if isinstance(e, WorkerEvent) else e.state.value
        for e in entries
        if isinstance(e, WorkerEvent) or e.task_id == task_id
    ]
    assert order.index("SUCCESS") < order.index("worker_shutdown")
    assert _kinds(entries, _after_last(entries, 3600)) == {}


@pytest.mark.skipif(
    tuple(int(p) for p in celery.__version__.split(".")[:2]) < (5, 5),
    reason="the cancel RETRY exists on Celery 5.5+",
)
def test_m3b_cold_stop_prefork_is_interrupted_by_the_cancelled_retry(run: Run) -> None:
    proc = run.start("prefork")
    run.wait("worker_ready", lambda es: _ready(es) == 1)
    task_id = run.send("output_test.sleepy", 30)
    run.wait("STARTED", lambda es: _count(es, task_id, TaskState.STARTED) == 1)

    proc.send_signal(signal.SIGQUIT)
    proc.wait(timeout=TIMEOUT_S)

    entries = run.entries()
    terminating = next(
        e
        for e in entries
        if isinstance(e, WorkerEvent)
        and e.event_type == WorkerEventType.WORKER_TERMINATING
    )
    retry = next(
        e
        for e in entries
        if isinstance(e, TaskEvent)
        and e.task_id == task_id
        and e.state == TaskState.RETRY
    )
    print(
        "M3b order:",
        "RETRY before WORKER_TERMINATING"
        if retry.timestamp < terminating.timestamp
        else "RETRY after WORKER_TERMINATING"
        if retry.timestamp > terminating.timestamp
        else "RETRY and WORKER_TERMINATING at the same time",
        retry.timestamp.isoformat(),
        terminating.timestamp.isoformat(),
    )
    _record(
        "anomaly_m3b_cold_stop",
        "prefork worker, SIGQUIT while output_test.sleepy(30) runs (M3b)",
        run.raw_entries(),
    )
    assert retry.exception == "cancelled by Celery"
    got = _kinds(entries, terminating.timestamp + timedelta(seconds=1))
    assert got == {task_id: (AnomalyKind.INTERRUPTED, AnomalyEvidence.CANCELLED_RETRY)}


def test_m3b_cold_stop_threads_falls_back_to_a_warm_stop(run: Run) -> None:
    proc = run.start("threads")
    run.wait("worker_ready", lambda es: _ready(es) == 1)
    task_id = run.send("output_test.sleepy", 4)
    run.wait("STARTED", lambda es: _count(es, task_id, TaskState.STARTED) == 1)

    proc.send_signal(signal.SIGQUIT)
    proc.wait(timeout=TIMEOUT_S)

    entries = run.entries()
    assert _count(entries, task_id, TaskState.SUCCESS) == 1
    assert _kinds(entries, _after_last(entries, 3600)) == {}


def test_m4_acks_late_sigkill_restart_is_interrupted_then_cleared(run: Run) -> None:
    first = run.start("prefork", visibility_timeout=5)
    run.wait("worker_ready", lambda es: _ready(es) == 1)
    task_id = run.send("output_test.sleepy_late", 40)
    run.wait("STARTED", lambda es: _count(es, task_id, TaskState.STARTED) == 1)
    run.sigkill_group(first)
    # kombu 5.6.2 QoS.restore_visible runs on its first call and then every
    # 10th (100 s). The restarted worker's first call must come after the
    # message is older than visibility_timeout, or the redelivery waits ~100 s.
    time.sleep(6)

    run.start("prefork", visibility_timeout=5)
    # The redelivery lands within ~20-200 ms of the second worker_ready, so the
    # polled snapshot may already hold it: judge the stream up to that event.
    before = through_nth_ready(
        run.wait("second worker_ready", lambda es: _ready(es) == 2), 2
    )
    assert _count(before, task_id, TaskState.STARTED) == 1
    assert _kinds(before, _after_last(before, 0.5)) == {
        task_id: (AnomalyKind.INTERRUPTED, AnomalyEvidence.WORKER_RESTARTED)
    }

    after = run.wait(
        "redelivered STARTED",
        lambda es: _count(es, task_id, TaskState.STARTED) == 2,
    )
    _record(
        "anomaly_m4_acks_late_sigkill",
        "prefork acks_late output_test.sleepy_late(40), SIGKILL of the worker's process "
        "group, restart with the same nodename, visibility_timeout 5 s (M4)",
        run.raw_entries(),
    )
    assert _kinds(after, _after_last(after, 0.5)) == {}


def test_m5_sigkill_is_stuck_until_the_same_nodename_restarts(run: Run) -> None:
    first = run.start("prefork")
    run.wait("worker_ready", lambda es: _ready(es) == 1)
    task_id = run.send("output_test.sleepy", 60)
    started = run.wait(
        "STARTED", lambda es: _count(es, task_id, TaskState.STARTED) == 1
    )
    run.sigkill_group(first)
    started_at = next(
        e.timestamp
        for e in started
        if isinstance(e, TaskEvent) and e.state == TaskState.STARTED
    )

    # Before the restart: ceiling (5 s) not reached, then reached.
    assert _kinds(started, started_at + timedelta(seconds=4)) == {}
    assert _kinds(started, started_at + timedelta(seconds=6)) == {
        task_id: (AnomalyKind.STUCK, None)
    }

    run.start("prefork")
    restarted = through_nth_ready(
        run.wait("second worker_ready", lambda es: _ready(es) == 2), 2
    )
    _record(
        "anomaly_m5_sigkill_restart",
        "prefork output_test.sleepy(60), SIGKILL of the worker's process group, "
        "restart with the same nodename (M5)",
        run.raw_entries(),
    )
    assert _kinds(restarted, started_at + timedelta(seconds=6)) == {
        task_id: (AnomalyKind.INTERRUPTED, AnomalyEvidence.WORKER_RESTARTED)
    }
