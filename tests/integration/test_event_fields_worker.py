"""exception_type, hostname, queue and routing_key from a real Celery worker.

Starts ``celery worker`` (prefork) on ``event_fields_worker_app.py`` against a
real broker, Redis and RabbitMQ in turn, and sends tasks from this process
with stemtrace's signals connected, so both the sender's PENDING and the
worker's RECEIVED/STARTED/terminal events are real. Routes: the default
queue, a queue named in ``task_routes`` and a topic exchange (see
``event_fields_routing.py``). stemtrace's own events go to a Redis stream in
both cases, where the test reads them.

Requires Redis (``REDIS_URL``, default ``redis://localhost:6379/15``); the
RabbitMQ cases also need ``RABBITMQ_URL`` (default
``amqp://guest:guest@localhost:5672//``) and skip without it.
"""

import contextlib
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import redis
from amqp.exceptions import AMQPError
from celery import Celery
from kombu import Connection
from kombu.exceptions import KombuError

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent
from stemtrace.core.graph import TaskGraph
from stemtrace.core.ports import ClosableTransport
from stemtrace.library.signals import connect_signals, disconnect_signals
from stemtrace.transports import get_transport
from tests.integration.event_fields_routing import (
    TOPIC_ROUTING_KEY,
    Names,
    routing_config,
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 90
APP_MODULE = "tests.integration.event_fields_worker_app"
PARTNER_ERROR = f"{APP_MODULE}.PartnerAPIError"


def _redis_available() -> bool:
    try:
        redis.from_url(REDIS_URL).ping()
    except redis.RedisError:
        return False
    return True


def _rabbitmq_available() -> bool:
    try:
        with Connection(RABBITMQ_URL, connect_timeout=1) as conn:
            conn.connect()
    except (OSError, AMQPError, KombuError):
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _redis_available(), reason="Redis not available"),
]


class Run:
    """A worker on one broker, the tasks sent to it and the events they produced."""

    def __init__(self, broker_url: str, suffix: str, log_path: Path) -> None:
        """Name the run's queues and stream; connect to the event stream."""
        self.broker_url = broker_url
        self.suffix = suffix
        self.names = Names(suffix)
        self.nodename = f"ef-{suffix}@{socket.gethostname()}"
        self.log_path = log_path
        self.stream_key = f"{self.names.events_prefix}:events"
        self.client = redis.from_url(REDIS_URL)
        self.task_ids: dict[str, str] = {}

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

    def task_events(self, task: str) -> list[TaskEvent]:
        """The events of the task sent as ``task``, in arrival order."""
        task_id = self.task_ids[task]
        return [
            e
            for e in self.events()
            if isinstance(e, TaskEvent) and e.task_id == task_id
        ]

    def event(self, task: str, state: TaskState, retries: int = 0) -> TaskEvent:
        """The one ``state`` event of attempt ``retries`` of ``task``."""
        [event] = [
            e
            for e in self.task_events(task)
            if e.state == state and e.retries == retries
        ]
        return event

    def worker_hostname(self) -> str:
        """The hostname the worker_ready event reports (the Workers page's)."""
        [ready] = [
            e
            for e in self.events()
            if isinstance(e, WorkerEvent) and e.event_type == "worker_ready"
        ]
        return ready.hostname

    def wait(self, ready: Callable[[], bool], what: str) -> None:
        """Wait until ``ready()``; fail with the worker log if it never is."""
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if ready():
                return
            time.sleep(0.2)
        log_tail = self.log_path.read_text(errors="replace")[-4000:]
        raise AssertionError(f"{what} not seen within timeout; worker log:\n{log_tail}")


TERMINAL = {TaskState.SUCCESS, TaskState.FAILURE}


def _finished(run: "Run", task: str) -> bool:
    """Whether ``task`` sent its last event (attempt 0's late FAILURE for one)."""
    states = {e.state for e in run.task_events(task)}
    if task == "ef.throw_false_then_hard_limit":
        attempts = {(e.state, e.retries) for e in run.task_events(task)}
        return {(TaskState.FAILURE, 0), (TaskState.SUCCESS, 1)} <= attempts
    return bool(states & TERMINAL)


TASKS = [
    "ef.default",
    "ef.routed",
    "ef.topic",
    "ef.fails",
    "ef.retries_once",
    "ef.retries_in_except",
    "ef.hard_limit",
    "ef.retry_then_hard_limit",
    "ef.throw_false_then_hard_limit",
]


def _start_worker(run: Run) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "EF_BROKER_URL": run.broker_url,
        "EF_EVENTS_URL": REDIS_URL,
        "EF_SUFFIX": run.suffix,
    }
    queues = [run.names.default_queue, run.names.routed_queue, run.names.topic_queue]
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
                "--concurrency=2",
                f"--hostname={run.nodename}",
                f"--queues={','.join(queues)}",
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


def _delete_routes(sender: Celery) -> None:
    """Delete the run's queues and exchange from the broker."""
    with sender.connection_for_write() as conn:
        channel = conn.default_channel
        for queue in sender.conf.task_queues:
            with contextlib.suppress(Exception):  # best-effort cleanup
                queue.bind(channel).delete()
                if queue.exchange.name:
                    queue.exchange.bind(channel).delete()


@pytest.fixture(scope="module", params=["redis", "rabbitmq"])
def run(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Run]:
    """Run a worker on the broker, send every task once and wait for them."""
    if request.param == "rabbitmq" and not _rabbitmq_available():
        pytest.skip("RabbitMQ not available")
    broker_url = REDIS_URL if request.param == "redis" else RABBITMQ_URL
    suffix = uuid.uuid4().hex[:8]
    run = Run(broker_url, suffix, tmp_path_factory.mktemp("ef") / "worker.log")

    sender = Celery("ef_sender", broker=broker_url)
    sender.conf.update(routing_config(run.names))
    transport = get_transport(REDIS_URL, prefix=run.names.events_prefix, ttl=300)
    proc = _start_worker(run)
    try:
        run.wait(
            lambda: any(isinstance(e, WorkerEvent) for e in run.events()),
            "worker_ready",
        )
        connect_signals(transport)
        try:
            for task in TASKS:
                run.task_ids[task] = str(sender.send_task(task).id)
        finally:
            disconnect_signals()
        run.wait(
            lambda: all(_finished(run, t) for t in TASKS),
            "a terminal event for every task",
        )
        yield run
    finally:
        _stop_worker(proc)
        if isinstance(transport, ClosableTransport):
            transport.close()
        _delete_routes(sender)
        run.client.delete(run.stream_key)


Destination = Callable[[Names], tuple[str | None, str]]
ROUTES: dict[str, Destination] = {
    "ef.default": lambda n: (n.default_queue, n.default_queue),
    "ef.routed": lambda n: (n.routed_queue, n.routed_queue),
    # Through a named exchange the queue isn't known; the routing key is.
    "ef.topic": lambda _: (None, TOPIC_ROUTING_KEY),
}


@pytest.mark.parametrize("task", ROUTES)
def test_pending_carries_queue_and_routing_key(run: Run, task: str) -> None:
    pending = run.event(task, TaskState.PENDING)

    assert (pending.queue, pending.routing_key) == ROUTES[task](run.names)
    assert pending.hostname is None


@pytest.mark.parametrize("task", ROUTES)
@pytest.mark.parametrize(
    "state", [TaskState.RECEIVED, TaskState.STARTED, TaskState.SUCCESS]
)
def test_worker_events_carry_hostname_queue_and_routing_key(
    run: Run, task: str, state: TaskState
) -> None:
    event = run.event(task, state)

    assert (event.hostname, event.queue, event.routing_key) == (
        run.nodename,
        *ROUTES[task](run.names),
    )


def test_hostname_is_the_workers_page_hostname(run: Run) -> None:
    assert run.event("ef.default", TaskState.STARTED).hostname == run.worker_hostname()


def test_failure_carries_exception_type_not_message(run: Run) -> None:
    failure = run.event("ef.fails", TaskState.FAILURE)

    assert failure.exception_type == PARTNER_ERROR
    assert failure.exception is not None
    assert "secret-token" in failure.exception
    assert failure.hostname == run.nodename


def test_retry_carries_cause_and_node_keeps_it(run: Run) -> None:
    retry = run.event("ef.retries_once", TaskState.RETRY)
    graph = TaskGraph()
    for event in run.task_events("ef.retries_once"):
        graph.add_event(event)
    node = graph.nodes[run.task_ids["ef.retries_once"]]

    assert retry.exception_type == PARTNER_ERROR
    assert (node.state, node.exception_type, node.hostname, node.queue) == (
        TaskState.SUCCESS,
        PARTNER_ERROR,
        run.nodename,
        run.names.default_queue,
    )


def test_retry_inside_except_carries_the_handled_exception(run: Run) -> None:
    retry = run.event("ef.retries_in_except", TaskState.RETRY)

    assert retry.exception_type == "ConnectionError"


def test_failure_from_main_process_carries_its_worker(run: Run) -> None:
    # celery 5.6.3 worker/request.py Request.on_failure sends task_failure from
    # the worker's main process for a hard time limit, where task.request is
    # empty; stemtrace reads the failing Request from on_failure's frame.
    failure = run.event("ef.hard_limit", TaskState.FAILURE)

    assert failure.exception_type == "billiard.exceptions.TimeLimitExceeded"
    assert (failure.hostname, failure.queue, failure.routing_key) == (
        run.nodename,
        run.names.default_queue,
        run.names.default_queue,
    )


def test_main_process_failure_of_a_retry_carries_its_attempt(run: Run) -> None:
    # Attempt 1 hits the hard time limit: before, its FAILURE said retries=0.
    [failure] = [
        e
        for e in run.task_events("ef.retry_then_hard_limit")
        if e.state == TaskState.FAILURE
    ]
    graph = TaskGraph()
    for event in run.task_events("ef.retry_then_hard_limit"):
        graph.add_event(event)
    node = graph.nodes[run.task_ids["ef.retry_then_hard_limit"]]
    started_1 = run.event("ef.retry_then_hard_limit", TaskState.STARTED, retries=1)

    assert (failure.retries, failure.hostname) == (1, run.nodename)
    # The last run is attempt 1's, on one worker's clock (about the 1 s limit).
    assert node.runtime_ms is not None
    assert node.runtime_ms == (failure.timestamp - started_1.timestamp) // timedelta(
        milliseconds=1
    )
    assert 900 <= node.runtime_ms <= 5000


def test_main_process_failure_of_attempt_zero_after_attempt_one(run: Run) -> None:
    # self.retry(throw=False): attempt 1 arrives and finishes while attempt 0
    # still runs; attempt 0 then hits its 15 s hard time limit. Its FAILURE is
    # attempt 0's, and it ends attempt 0's own run, never attempt 1's.
    events = run.task_events("ef.throw_false_then_hard_limit")
    [failure] = [e for e in events if e.state == TaskState.FAILURE]
    started_0 = run.event("ef.throw_false_then_hard_limit", TaskState.STARTED, 0)
    success_1 = run.event("ef.throw_false_then_hard_limit", TaskState.SUCCESS, 1)
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
    node = graph.nodes[run.task_ids["ef.throw_false_then_hard_limit"]]
    ms = timedelta(milliseconds=1)

    assert (failure.retries, failure.hostname) == (0, run.nodename)
    assert success_1.timestamp < failure.timestamp
    assert node.state == TaskState.FAILURE
    assert node.runtime_ms == (failure.timestamp - started_0.timestamp) // ms
    assert node.runtime_ms >= 14000
