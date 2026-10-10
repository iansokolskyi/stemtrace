"""Output capture against a real Celery worker (prefork/threads, default redirect).

Starts ``celery worker`` as a subprocess on ``tests/integration/output_worker_app.py``
and reads the events it publishes to Redis. This is the path unit tests can't
cover: a prefork child process, Celery's own logging setup (which clears root
handlers after ``init_worker``) and ``print()`` going through Celery's
``LoggingProxy``.

Requires Redis (``REDIS_URL``, default ``redis://localhost:6379/15``).
Pools default to prefork and threads; set ``OUTPUT_TEST_POOLS`` to try others,
e.g. ``OUTPUT_TEST_POOLS=gevent uv run --with gevent pytest <this file>``.
"""

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis
from celery import Celery

from stemtrace.core.events import TaskEvent, TaskState

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
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


class WorkerRun:
    """A running worker subprocess plus the Redis stream it publishes to."""

    def __init__(self, queue: str, prefix: str, log_path: Path) -> None:
        """Remember the queue/prefix/log file and connect to Redis."""
        self.queue = queue
        self.log_path = log_path
        self.stream_key = f"{prefix}:events"
        self.client = redis.from_url(REDIS_URL)
        self.sender = Celery("output_test_sender", broker=REDIS_URL)

    def send(self, task_name: str, *args: str) -> str:
        """Queue a task by name and return its id."""
        result = self.sender.send_task(task_name, args=args, queue=self.queue)
        return str(result.id)

    def events(self) -> list[TaskEvent]:
        """Read every task event published so far."""
        events: list[TaskEvent] = []
        for _, fields in self.client.xrange(self.stream_key):
            payload = fields[b"data"].decode()
            if '"task_id"' in payload and '"event_type"' not in payload:
                events.append(TaskEvent.model_validate_json(payload))
        return events

    def wait_for(self, task_id: str, state: TaskState) -> list[TaskEvent]:
        """Wait until ``task_id`` has a ``state`` event; return its events."""
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            mine = [e for e in self.events() if e.task_id == task_id]
            if any(e.state == state for e in mine):
                return mine
            time.sleep(0.2)
        log_tail = self.log_path.read_text(errors="replace")[-4000:]
        raise AssertionError(
            f"no {state} event for {task_id} within timeout; worker log:\n{log_tail}"
        )


@pytest.fixture(
    scope="module",
    params=os.environ.get("OUTPUT_TEST_POOLS", "prefork,threads").split(","),
)
def worker(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[WorkerRun]:
    """Start a worker (per pool type) for the test module and stop it afterwards."""
    suffix = uuid.uuid4().hex[:8]
    run = WorkerRun(
        queue=f"output-test-{suffix}",
        prefix=f"output_test_{suffix}",
        log_path=tmp_path_factory.mktemp("worker") / "worker.log",
    )
    env = {
        **os.environ,
        "REDIS_URL": REDIS_URL,
        "OUTPUT_TEST_QUEUE": run.queue,
        "OUTPUT_TEST_PREFIX": run.stream_key.removesuffix(":events"),
    }
    log_file = run.log_path.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "tests.integration.output_worker_app",
            "worker",
            f"--pool={request.param}",
            "--concurrency=2",
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
    try:
        yield run
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log_file.close()
        run.client.delete(run.stream_key)


def test_print_and_loggers_are_captured_on_success(worker: WorkerRun) -> None:
    """print() (stdout and stderr), get_task_logger and a module logger all land."""
    task_id = worker.send("output_test.noisy")

    events = worker.wait_for(task_id, TaskState.SUCCESS)

    success = next(e for e in events if e.state == TaskState.SUCCESS)
    assert success.output == (
        "printed line\n"
        "printed to stderr\n"
        "INFO tests.integration.output_worker_app: task logger line\n"
        "WARNING output_worker_lib: module logger line"
    )
    started = next(e for e in events if e.state == TaskState.STARTED)
    assert started.output is None


def test_each_attempt_has_its_own_output(worker: WorkerRun) -> None:
    """RETRY carries attempt 0's output, FAILURE attempt 1's."""
    task_id = worker.send("output_test.flaky")

    events = worker.wait_for(task_id, TaskState.FAILURE)

    outputs = {
        (e.state, e.retries): e.output
        for e in events
        if e.state in (TaskState.RETRY, TaskState.FAILURE)
    }
    assert outputs == {
        (TaskState.RETRY, 0): "attempt 0",
        (TaskState.FAILURE, 1): "attempt 1",
    }


def test_concurrent_tasks_keep_their_own_output(worker: WorkerRun) -> None:
    """Tasks running at the same time (threads pool) never mix output."""
    tags = [f"tag{i}" for i in range(4)]
    task_ids = {tag: worker.send("output_test.tagged", tag) for tag in tags}

    for tag, task_id in task_ids.items():
        events = worker.wait_for(task_id, TaskState.SUCCESS)
        success = next(e for e in events if e.state == TaskState.SUCCESS)
        assert success.output == "\n".join(f"{tag} {i}" for i in range(5))
