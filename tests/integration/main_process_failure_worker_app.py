"""Celery app run as a real prefork worker by ``test_main_process_failure_worker.py``.

Configured through environment variables so each run has its own queue and
stemtrace stream:

- ``MP_BROKER_URL``: the Celery broker, result backend and event stream (Redis).
- ``MP_SUFFIX``: makes the queue and stream names unique.

``mp.late_first`` outlives its hard time limit after Celery sent the next chain step.
The four ``mp.fail_*`` tasks fail four ways: an exception and a soft time
limit, which the pool child reports itself, and a hard time limit and a killed
pool child, which the worker's main process reports.
"""

import os
import signal
import time
from typing import Any

from celery import Celery, Task

import stemtrace

SUFFIX = os.environ.get("MP_SUFFIX", "local")
QUEUE = f"mp-{SUFFIX}"
EVENTS_PREFIX = f"mp_test_{SUFFIX}"

app = Celery(
    "main_process_failure_worker_app",
    broker=os.environ.get("MP_BROKER_URL"),
    backend=os.environ.get("MP_BROKER_URL"),
)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_default_queue=QUEUE,
    result_expires=300,
)

stemtrace.init_worker(
    app,
    transport_url=os.environ.get("MP_BROKER_URL"),
    prefix=EVENTS_PREFIX,
    ttl=300,
)


class Boom(Exception):
    """A task's own exception."""


@app.task(name="mp.ok")
def ok(*_: object) -> int:
    """Succeed."""
    return 1


@app.task(name="mp.callback")
def callback(*_: object) -> int:
    """A chord body."""
    return 1


@app.task(name="mp.fail_exception")
def fail_exception(*_: object) -> None:
    """Raise: the pool child reports the FAILURE."""
    raise Boom("raised")


@app.task(name="mp.fail_soft", soft_time_limit=1, time_limit=30)
def fail_soft(*_: object) -> None:
    """Outlive the soft time limit: the pool child reports the FAILURE."""
    time.sleep(30)


@app.task(name="mp.fail_hard", time_limit=1)
def fail_hard(*_: object) -> None:
    """Outlive the hard time limit: the main process reports the FAILURE."""
    time.sleep(30)


@app.task(name="mp.fail_killed")
def fail_killed(*_: object) -> None:
    """Kill the pool child: the main process reports the FAILURE (WorkerLostError)."""
    os.kill(os.getpid(), signal.SIGKILL)


class SlowOnSuccess(Task):
    """A task whose ``on_success`` handler outlives its time limit."""

    def on_success(
        self,
        retval: object,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Run in the pool child after the next chain step was sent (celery 5.6.3 app/trace.py)."""
        time.sleep(30)


@app.task(name="mp.late_first", base=SlowOnSuccess, time_limit=4)
def late_first(*_: object) -> int:
    """Succeed, then be hard-limited inside ``on_success``."""
    return 1


@app.task(name="mp.slow")
def slow(*_: object) -> int:
    """Run for 8 s: still running when ``mp.late_first`` is stopped (4 s limit)."""
    time.sleep(8)
    return 1
