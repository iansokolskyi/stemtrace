"""Output capture against a real Celery worker.

Starts ``celery worker`` as a subprocess on ``tests/integration/output_worker_app.py``
and reads the events it publishes to Redis. This is the path unit tests can't
cover: a prefork child process, Celery's own logging setup (which clears root
handlers after ``init_worker``), ``print()`` going through Celery's
``LoggingProxy`` (or not, with the redirect off), and a worker with no logging
handlers at all. The same workers also check the worker-side RECEIVED event.

Requires Redis (``REDIS_URL``, default ``redis://localhost:6379/15``).
Worker modes default to prefork and threads with Celery's redirect, plus prefork
without it; set ``OUTPUT_TEST_POOLS`` to try other pools (with the redirect),
e.g. ``OUTPUT_TEST_POOLS=gevent uv run --with gevent pytest <this file>``.
"""

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import redis
from celery import Celery

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import TaskGraph

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


_DEFAULT_MODES = ["prefork", "threads", "prefork-no-redirect"]
_MODES = os.environ.get("OUTPUT_TEST_POOLS", ",".join(_DEFAULT_MODES)).split(",")


def _start_worker(
    pool: str, extra_env: Mapping[str, str], log_dir: Path
) -> Iterator[WorkerRun]:
    """Run a worker subprocess until the generator is closed."""
    suffix = uuid.uuid4().hex[:8]
    run = WorkerRun(
        queue=f"output-test-{suffix}",
        prefix=f"output_test_{suffix}",
        log_path=log_dir / "worker.log",
    )
    env = {
        **os.environ,
        "REDIS_URL": REDIS_URL,
        "OUTPUT_TEST_QUEUE": run.queue,
        "OUTPUT_TEST_PREFIX": run.stream_key.removesuffix(":events"),
        **extra_env,
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
            f"--pool={pool}",
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
        # The event stream plus the queue list and kombu's binding set the
        # worker left in the broker database.
        run.client.delete(run.stream_key, run.queue, f"_kombu.binding.{run.queue}")


@pytest.fixture(scope="module", params=_MODES)
def worker(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[WorkerRun]:
    """Start a worker per mode (pool, redirect on/off) and stop it afterwards."""
    mode: str = request.param
    pool, _, variant = mode.partition("-")
    extra_env = {"OUTPUT_TEST_REDIRECT": "0"} if variant == "no-redirect" else {}
    yield from _start_worker(pool, extra_env, tmp_path_factory.mktemp("worker"))


@pytest.fixture(scope="module")
def prefork_worker(tmp_path_factory: pytest.TempPathFactory) -> Iterator[WorkerRun]:
    """A prefork worker, for tests that kill a pool child."""
    yield from _start_worker("prefork", {}, tmp_path_factory.mktemp("worker-prefork"))


@pytest.fixture(scope="module")
def received_off_worker(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[WorkerRun]:
    """A prefork worker that leaves the opt-in RECEIVED event off."""
    yield from _start_worker(
        "prefork",
        {"OUTPUT_TEST_PUBLISH_RECEIVED": "0"},
        tmp_path_factory.mktemp("worker-received-off"),
    )


@pytest.fixture(scope="module")
def unconfigured_logging_worker(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[WorkerRun]:
    """A prefork worker whose setup_logging receiver configures no handlers."""
    yield from _start_worker(
        "prefork",
        {"OUTPUT_TEST_NOOP_LOGGING": "1"},
        tmp_path_factory.mktemp("worker-nolog"),
    )


@pytest.fixture(scope="module")
def prefork_worker(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[WorkerRun]:
    """A prefork worker, the pool whose children receive soft time limits."""
    yield from _start_worker(
        "prefork",
        {"OUTPUT_TEST_REDIRECT": "0"},
        tmp_path_factory.mktemp("worker-soft-limit"),
    )


def test_soft_time_limit_inside_the_capture_reaches_the_task(
    prefork_worker: WorkerRun,
) -> None:
    """SoftTimeLimitExceeded raised while print() is captured fails the task."""
    task_id = prefork_worker.send("output_test.soft_limited")

    events = prefork_worker.wait_for(task_id, TaskState.FAILURE)

    failure = next(e for e in events if e.state == TaskState.FAILURE)
    assert failure.exception is not None
    assert "SoftTimeLimitExceeded" in failure.exception
    assert TaskState.SUCCESS not in {e.state for e in events}
    assert "the soft time limit was swallowed" not in (failure.output or "")


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


def test_received_is_published_per_delivery_of_the_first_attempt(
    worker: WorkerRun,
) -> None:
    """The worker publishes RECEIVED when it takes the message, before STARTED.

    With prefork, RECEIVED comes from the main process (the consumer) and
    STARTED from a pool child. The retry message (``retries=1``) publishes no
    RECEIVED of its own: its RETRY event covers it.
    """
    task_id = worker.send("output_test.flaky")

    events = worker.wait_for(task_id, TaskState.FAILURE)

    pre_run = [
        (e.state, e.retries)
        for e in events
        if e.state in (TaskState.RECEIVED, TaskState.STARTED)
    ]
    assert pre_run == [
        (TaskState.RECEIVED, 0),
        (TaskState.STARTED, 0),
        (TaskState.STARTED, 1),
    ]
    received = next(e for e in events if e.state == TaskState.RECEIVED)
    first_start = next(e for e in events if e.state == TaskState.STARTED)
    assert received.timestamp <= first_start.timestamp


def test_received_is_not_published_unless_enabled(
    received_off_worker: WorkerRun,
) -> None:
    """Without publish_received the worker publishes STARTED and SUCCESS only."""
    task_id = received_off_worker.send("output_test.noisy")

    events = received_off_worker.wait_for(task_id, TaskState.SUCCESS)

    assert [e.state for e in events if e.task_id == task_id] == [
        TaskState.STARTED,
        TaskState.SUCCESS,
    ]


def test_redelivered_message_publishes_received_again(
    prefork_worker: WorkerRun,
) -> None:
    """acks_late + reject_on_worker_lost: a killed child's message is delivered twice.

    Each delivery of the first attempt (``retries=0``) publishes RECEIVED, so
    the task has two. The graph keeps attempt-0 RECEIVED events ahead of the
    first STARTED (their clocks come from the main process, not the child), so
    both RECEIVED precede both STARTED even though the second was published
    after the first run died.
    """
    worker = prefork_worker
    task_id = worker.send("output_test.dies_once")

    events = worker.wait_for(task_id, TaskState.SUCCESS)

    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
    node = graph.nodes[task_id]
    assert [(e.state, e.retries) for e in node.events] == [
        (TaskState.RECEIVED, 0),
        (TaskState.RECEIVED, 0),
        (TaskState.STARTED, 0),
        (TaskState.STARTED, 0),
        (TaskState.SUCCESS, 0),
    ]
    assert node.state == TaskState.SUCCESS


def test_concurrent_tasks_keep_their_own_output(worker: WorkerRun) -> None:
    """Tasks running at the same time (threads pool) never mix output."""
    tags = [f"tag{i}" for i in range(4)]
    task_ids = {tag: worker.send("output_test.tagged", tag) for tag in tags}

    for tag, task_id in task_ids.items():
        events = worker.wait_for(task_id, TaskState.SUCCESS)
        success = next(e for e in events if e.state == TaskState.SUCCESS)
        assert success.output == "\n".join(f"{tag} {i}" for i in range(5))


def test_print_chunks_are_captured_as_written(worker: WorkerRun) -> None:
    """Multi-argument print(), end="" and sep= come out as one would see them.

    With the redirect on, Celery's LoggingProxy logs each write() chunk as its
    own record; the captured output must still be the printed text.
    """
    task_id = worker.send("output_test.print_variants")

    events = worker.wait_for(task_id, TaskState.SUCCESS)

    success = next(e for e in events if e.state == TaskState.SUCCESS)
    assert success.output == "value: 42\nno newline then the rest\nerr-line"


def test_unconfigured_logging_still_prints_warnings(
    unconfigured_logging_worker: WorkerRun,
) -> None:
    """With no logging handlers, warnings still reach stderr (lastResort).

    Before and after the task's capture window, and inside it, where the
    warning is also captured exactly once.
    """
    worker = unconfigured_logging_worker
    task_id = worker.send("output_test.warn", "tag-1")

    events = worker.wait_for(task_id, TaskState.SUCCESS)

    success = next(e for e in events if e.state == TaskState.SUCCESS)
    assert success.output == "WARNING output_worker_lib: in-task warning tag-1"
    deadline = time.monotonic() + 10
    expected = ["in-task warning tag-1", f"after-task warning {task_id}"]
    log = ""
    while time.monotonic() < deadline:
        log = worker.log_path.read_text(errors="replace")
        if all(line in log for line in expected):
            break
        time.sleep(0.2)
    for line in expected:
        assert log.count(line) == 1, f"{line!r} not printed once; worker log:\n{log}"
