"""A hard time limit that hits in ``on_success``, from a real prefork worker.

The pool child has stored SUCCESS in the result backend by the time ``on_success``
runs, so Celery keeps it (``backends/base.py`` ``_store_result``, 5.6.3) while the
main process still records a ``TimeLimitExceeded`` failure. The child then gets
SIGTERM, which unwinds ``on_success`` and still runs ``task_postrun``.

Requires Redis (``REDIS_URL``, default ``redis://localhost:6379/15``).
"""

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import redis
from celery import Celery

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent
from stemtrace.core.graph import TaskGraph

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]
APP_MODULE = "tests.integration.on_success_limit_worker_app"
WAIT_SECONDS = 60
# The child's SUCCESS follows the main process's FAILURE by milliseconds.
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


def _wait(ready: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.2)
    return False


class Run:
    """A worker, the task sent to it and where its events and result live."""

    def __init__(self, suffix: str) -> None:
        """Name the run's queue and stream."""
        self.suffix = suffix
        self.queue = f"os-{suffix}"
        self.stream_key = f"os_test_{suffix}:events"
        self.client = redis.from_url(REDIS_URL)
        self.task_id = str(uuid.uuid4())
        self.result_state: str | None = None

    def events(self) -> list[TaskEvent | WorkerEvent]:
        """Every event on the run's stream so far."""
        events: list[TaskEvent | WorkerEvent] = []
        for _, fields in self.client.xrange(self.stream_key):
            payload = fields[b"data"].decode()
            if '"event_type"' in payload:
                events.append(WorkerEvent.model_validate_json(payload))
            else:
                events.append(TaskEvent.model_validate_json(payload))
        return events

    def task_events(self) -> list[TaskEvent]:
        """The task's events, in stream order."""
        return [
            e
            for e in self.events()
            if isinstance(e, TaskEvent) and e.task_id == self.task_id
        ]

    def states(self) -> list[TaskState]:
        """The task's event states, in stream order."""
        return [e.state for e in self.task_events()]


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Run]:
    """Run a worker, send the task and wait for its FAILURE and SUCCESS."""
    suffix = uuid.uuid4().hex[:8]
    run = Run(suffix)
    log_path = tmp_path_factory.mktemp("os") / "worker.log"
    env = {**os.environ, "OS_BROKER_URL": REDIS_URL, "OS_SUFFIX": suffix}
    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                APP_MODULE,
                "worker",
                "--pool=prefork",
                "--concurrency=1",
                f"--hostname=os-{suffix}@localhost",
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
    try:
        assert _wait(
            lambda: any(isinstance(e, WorkerEvent) for e in run.events()),
            WAIT_SECONDS,
        ), log_path.read_text(errors="replace")[-4000:]
        sender = Celery("os_sender", broker=REDIS_URL, backend=REDIS_URL)
        sender.conf.update(task_serializer="json", task_default_queue=run.queue)
        result = sender.send_task("os.limited", task_id=run.task_id)
        assert _wait(lambda: TaskState.FAILURE in run.states(), WAIT_SECONDS), (
            log_path.read_text(errors="replace")[-4000:]
        )
        _wait(lambda: TaskState.SUCCESS in run.states(), TRAILING_SECONDS)
        run.result_state = result.state
        yield run
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        run.client.delete(run.stream_key, run.queue, f"celery-task-meta-{run.task_id}")


def test_hard_limit_in_on_success_publishes_failure_then_success(run: Run) -> None:
    events = [e for e in run.task_events() if e.state != TaskState.PENDING]

    assert [e.state for e in events] == [
        TaskState.STARTED,
        TaskState.FAILURE,
        TaskState.SUCCESS,
    ]
    assert events[1].exception_type == "billiard.exceptions.TimeLimitExceeded"
    assert events[1].timestamp <= events[2].timestamp


def test_hard_limit_in_on_success_graph_node_ends_success(run: Run) -> None:
    graph = TaskGraph()
    for event in run.task_events():
        graph.add_event(event)

    assert graph.nodes[run.task_id].state == TaskState.SUCCESS


def test_hard_limit_in_on_success_backend_keeps_success(run: Run) -> None:
    assert run.result_state == "SUCCESS"
