"""Celery app run as a real worker by ``test_output_capture_worker.py``.

Configured through environment variables so each test run uses its own
queue and stemtrace stream:

- ``OUTPUT_TEST_REDIRECT=0`` turns off Celery's stdout/stderr redirect.
- ``OUTPUT_TEST_PUBLISH_RECEIVED=0`` leaves the opt-in RECEIVED event off
  (``publish_received`` defaults to False in ``init_worker``; the other tests
  here turn it on).
- ``OUTPUT_TEST_NOOP_LOGGING=1`` connects a ``setup_logging`` receiver that
  configures nothing, so the worker has no logging handlers and Python falls
  back to ``logging.lastResort`` (WARNING+ to stderr).
"""

import logging
import os
import signal
import sys
import time

import redis
from celery import Celery, Task
from celery.signals import setup_logging, task_postrun
from celery.utils.log import get_task_logger

import stemtrace
from stemtrace.library import output_capture

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")

app = Celery("output_worker_app", broker=REDIS_URL)
app.conf.update(
    task_default_queue=os.environ.get("OUTPUT_TEST_QUEUE", "output-test"),
    task_serializer="json",
    accept_content=["json"],
    worker_hijack_root_logger=True,
    worker_redirect_stdouts=os.environ.get("OUTPUT_TEST_REDIRECT", "1") != "0",
)

if os.environ.get("OUTPUT_TEST_NOOP_LOGGING") == "1":

    @setup_logging.connect
    def _leave_logging_unconfigured(**_: object) -> None:
        """Take over logging setup and configure nothing."""


stemtrace.init_worker(
    app,
    transport_url=REDIS_URL,
    prefix=os.environ.get("OUTPUT_TEST_PREFIX", "output_test"),
    ttl=300,
    capture_output=True,
    publish_received=os.environ.get("OUTPUT_TEST_PUBLISH_RECEIVED", "1") != "0",
)

task_logger = get_task_logger(__name__)
# A plain module logger outside the task-logger tree (reaches root, not celery.task).
module_logger = logging.getLogger("output_worker_lib")


@app.task(name="output_test.noisy")
def noisy() -> str:
    """Write through every channel a task commonly uses."""
    print("printed line")
    print("printed to stderr", file=sys.stderr)
    task_logger.info("task logger line")
    module_logger.warning("module logger line")
    return "done"


@app.task(name="output_test.flaky", bind=True, max_retries=1)
def flaky(self: Task) -> None:
    """Retry once, then fail; each attempt prints its number."""
    retries = self.request.retries
    print(f"attempt {retries}")
    if retries == 0:
        raise self.retry(countdown=0)
    raise ValueError("gave up")


@app.task(
    name="output_test.dies_once",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def dies_once(self: Task) -> str:
    """Kill the pool child on the first delivery; Celery requeues, so a second runs.

    Prefork only: elsewhere this would kill the worker itself. The marker lives
    in Redis because the second run is in a new process.
    """
    marker = (
        f"{os.environ.get('OUTPUT_TEST_QUEUE', 'output-test')}:died:{self.request.id}"
    )
    client = redis.Redis.from_url(REDIS_URL)
    if client.set(marker, 1, nx=True, ex=300):
        os.kill(os.getpid(), signal.SIGKILL)
    return "survived"


@app.task(name="output_test.tagged")
def tagged(tag: str) -> None:
    """Print a tag a few times, slowly, so concurrent tasks interleave."""
    for i in range(5):
        print(f"{tag} {i}")
        time.sleep(0.05)


@app.task(name="output_test.print_variants")
def print_variants() -> None:
    """print() with several arguments and with end="" (several write chunks)."""
    print("value:", 42)
    print("no newline", end="")
    print(" then the rest")
    print("err", "line", sep="-", file=sys.stderr)


@app.task(name="output_test.warn")
def warn(tag: str) -> None:
    """Log one warning from a plain module logger."""
    module_logger.warning("in-task warning %s", tag)


if os.environ.get("OUTPUT_TEST_NOOP_LOGGING") == "1":

    @task_postrun.connect
    def _warn_after_task(task_id: str, **_: object) -> None:
        """Log a warning after stemtrace stopped capturing (outside the task)."""
        module_logger.warning("after-task warning %s", task_id)


@app.task(name="output_test.soft_limited", soft_time_limit=1)
def soft_limited() -> str:
    """Hit the soft time limit while print() is inside the output capture.

    The limit arrives as SIGUSR1 in this prefork child and raises
    SoftTimeLimitExceeded wherever the task is, here inside ``TeeStream.write``
    (a stalled capture lookup stands in for the microseconds a real task
    spends there). The task must fail; if the capture code swallowed the
    exception, the second print would run and the task would succeed.
    """
    original = output_capture._record_being_emitted

    def stalled() -> None:
        time.sleep(30)

    output_capture._record_being_emitted = stalled  # type: ignore[assignment]  # test stand-in for a slow lookup
    try:
        print("inside the capture")
    finally:
        output_capture._record_being_emitted = original
    print("the soft time limit was swallowed")
    return "finished"


@app.task(name="output_test.sleepy")
def sleepy(seconds: float) -> str:
    """Run for ``seconds``, long enough to stop the worker meanwhile."""
    time.sleep(seconds)
    return "slept"


@app.task(name="output_test.sleepy_late", acks_late=True)
def sleepy_late(seconds: float) -> str:
    """Like ``sleepy``, acknowledged only after it finishes (redelivered if lost)."""
    time.sleep(seconds)
    return "slept"
