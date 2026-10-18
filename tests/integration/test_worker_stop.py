"""Warm and cold worker stops against a real Celery worker.

A warm stop (SIGTERM) waits for running tasks before Celery sends
``worker_shutdown``; a cold stop (SIGQUIT) terminates them and sends no
``worker_shutdown``, which is what ``worker_terminating`` reports. Uses the
worker app of ``test_output_capture_worker.py``; requires Redis (``REDIS_URL``).
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import celery
import pytest
import redis
from celery import Celery

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
# Celery 5.5 added on_cold_shutdown, which cancels running tasks in the signal
# handler (each gets a RETRY "cancelled by Celery"); these tests pin its behavior.
CELERY_55 = tuple(int(p) for p in celery.__version__.split(".")[:2]) >= (5, 5)
needs_celery_55 = pytest.mark.skipif(
    not CELERY_55, reason="pins Celery 5.5+ cold-shutdown behavior"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 60


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


class StoppableWorker:
    """A worker subprocess whose stop signal the test chooses."""

    def __init__(self, pool: str, log_dir: Path) -> None:
        """Start the worker on its own queue and stemtrace stream."""
        suffix = uuid.uuid4().hex[:8]
        self.pool = pool
        self.queue = f"stop-test-{suffix}"
        self.stream_key = f"stop_test_{suffix}:events"
        self.nodename = f"stop{suffix}@localhost"
        self.client = redis.from_url(REDIS_URL)
        self.sender = Celery("stop_test_sender", broker=REDIS_URL)
        self.log_path = log_dir / "worker.log"
        env = {
            **os.environ,
            "REDIS_URL": REDIS_URL,
            "OUTPUT_TEST_QUEUE": self.queue,
            "OUTPUT_TEST_PREFIX": self.stream_key.removesuffix(":events"),
        }
        self._log = self.log_path.open("wb")
        self.proc = subprocess.Popen(
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
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def entries(self) -> list[TaskEvent | WorkerEvent]:
        """Every event published so far, in stream order."""
        out: list[TaskEvent | WorkerEvent] = []
        for _, fields in self.client.xrange(self.stream_key):
            payload = fields[b"data"].decode()
            if '"event_type"' in payload:
                out.append(WorkerEvent.model_validate_json(payload))
            else:
                out.append(TaskEvent.model_validate_json(payload))
        return out

    def wait(
        self, condition: str, check: Callable[[list[TaskEvent | WorkerEvent]], bool]
    ) -> list[TaskEvent | WorkerEvent]:
        """Wait until ``check(entries)`` is true; fail with the worker log."""
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            entries = self.entries()
            if check(entries):
                return entries
            time.sleep(0.2)
        log = self.log_path.read_text(errors="replace")[-4000:]
        raise AssertionError(f"{condition} not seen in time; worker log:\n{log}")

    def close(self) -> None:
        """Make sure the process is gone and clean the broker keys."""
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=20)
        self._log.close()
        self.client.delete(self.stream_key, self.queue, f"_kombu.binding.{self.queue}")


def _worker_events(entries: list[TaskEvent | WorkerEvent]) -> list[WorkerEvent]:
    return [e for e in entries if isinstance(e, WorkerEvent)]


def _task_states(
    entries: list[TaskEvent | WorkerEvent], task_id: str
) -> list[TaskState]:
    return [
        e.state for e in entries if isinstance(e, TaskEvent) and e.task_id == task_id
    ]


@pytest.fixture(params=["prefork", "threads"])
def worker(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StoppableWorker]:
    """A fresh worker per test: each test stops it."""
    run = StoppableWorker(request.param, tmp_path)
    try:
        run.wait(
            "worker_ready",
            lambda es: any(
                e.event_type == WorkerEventType.WORKER_READY for e in _worker_events(es)
            ),
        )
        yield run
    finally:
        run.close()


def _start_sleepy(
    worker: StoppableWorker, seconds: float, name: str = "output_test.sleepy"
) -> str:
    task_id = str(worker.sender.send_task(name, args=[seconds], queue=worker.queue).id)
    worker.wait("STARTED", lambda es: TaskState.STARTED in _task_states(es, task_id))
    return task_id


def test_warm_stop_finishes_running_task_before_worker_shutdown(
    worker: StoppableWorker,
) -> None:
    task_id = _start_sleepy(worker, 3)

    worker.proc.send_signal(signal.SIGTERM)
    worker.proc.wait(timeout=TIMEOUT_SECONDS)

    entries = worker.entries()
    kinds = [
        e.event_type.value
        if isinstance(e, WorkerEvent)
        else f"{e.state.value}:{e.task_id == task_id}"
        for e in entries
    ]
    assert "SUCCESS:True" in kinds
    assert kinds.index("SUCCESS:True") < kinds.index("worker_shutdown")
    assert "worker_terminating" not in kinds


def _kinds(entries: list[TaskEvent | WorkerEvent], task_id: str) -> list[str]:
    return [
        e.event_type.value
        if isinstance(e, WorkerEvent)
        else f"{e.state.value}:{e.task_id == task_id}"
        for e in entries
    ]


def _cancelled(entries: list[TaskEvent | WorkerEvent], task_id: str) -> bool:
    return any(
        isinstance(e, TaskEvent)
        and e.task_id == task_id
        and e.state == TaskState.RETRY
        and e.exception == "cancelled by Celery"
        for e in entries
    )


@needs_celery_55
def test_cold_stop_publishes_worker_terminating(worker: StoppableWorker) -> None:
    """Prefork: the running task is cancelled (RETRY "cancelled by Celery"),
    and worker_terminating marks the end the worker reports nothing else for.

    Threads: Celery 5.6.3 can't terminate a thread-pool job (its cold-shutdown
    handler, ``on_cold_shutdown`` -> ``Request.cancel``, gets
    ``NotImplementedError`` from ``TaskPool.terminate_job``), so the worker
    falls back to a warm stop: the task finishes, then worker_shutdown.
    """
    task_id = _start_sleepy(worker, 30 if worker.pool == "prefork" else 4)
    late_id = (
        _start_sleepy(worker, 30, "output_test.sleepy_late")
        if worker.pool == "prefork"
        else None
    )

    worker.proc.send_signal(signal.SIGQUIT)
    worker.proc.wait(timeout=TIMEOUT_SECONDS)

    entries = worker.entries()
    kinds = _kinds(entries, task_id)
    log = worker.log_path.read_text(errors="replace")[-3000:]
    if worker.pool == "threads":
        assert "worker_terminating" not in kinds, log
        assert kinds.index("SUCCESS:True") < kinds.index("worker_shutdown"), log
        return
    (terminating,) = [
        e
        for e in _worker_events(entries)
        if e.event_type == WorkerEventType.WORKER_TERMINATING
    ]
    assert terminating.hostname == worker.nodename
    assert _cancelled(entries, task_id), log
    assert "SUCCESS:True" not in kinds, log
    assert "worker_shutdown" not in kinds, log
    # Celery 5.6+ cancels an acks_late task without a RETRY (it is requeued):
    # only worker_terminating says it was cut short.
    assert late_id is not None
    if tuple(int(p) for p in celery.__version__.split(".")[:2]) >= (5, 6):
        late_states = set(_task_states(entries, late_id))
        assert TaskState.STARTED in late_states, log
        assert late_states <= {TaskState.RECEIVED, TaskState.STARTED}, log


@needs_celery_55
def test_cold_stop_after_warm_stop_ends_with_worker_shutdown(
    worker: StoppableWorker,
) -> None:
    """SIGQUIT during a warm stop: Celery cancels the running task, but the warm
    stop it interrupts still ends with worker_shutdown (no worker_terminating)."""
    if worker.pool != "prefork":
        pytest.skip("only prefork can cancel a running task")
    task_id = _start_sleepy(worker, 30)

    worker.proc.send_signal(signal.SIGTERM)
    # SIGTERM only sets should_stop; the warm stop starts when the event loop
    # next checks it (after its broker poll, about a second on Redis). A
    # SIGQUIT before that is a plain cold stop (worker_terminating).
    time.sleep(3)
    worker.proc.send_signal(signal.SIGQUIT)
    worker.proc.wait(timeout=TIMEOUT_SECONDS)

    entries = worker.entries()
    kinds = _kinds(entries, task_id)
    log = worker.log_path.read_text(errors="replace")[-3000:]
    assert _cancelled(entries, task_id), log
    assert "worker_shutdown" in kinds, log
    assert "worker_terminating" not in kinds, log
    assert "SUCCESS:True" not in kinds, log
