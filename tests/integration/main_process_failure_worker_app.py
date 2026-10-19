"""Celery app run as a real prefork worker by ``test_main_process_failure_worker.py``.

Configured through environment variables so each run has its own queue and
stemtrace stream:

- ``MP_BROKER_URL``: the Celery broker, result backend and event stream (Redis).
- ``MP_SUFFIX``: makes the queue and stream names unique.

``mp.late_first`` is killed in ``on_success`` after Celery sent the next chain step,
once the later steps reached the state its ``gate`` kwarg names.
The four ``mp.fail_*`` tasks fail four ways: an exception and a soft time
limit, which the pool child reports itself, and a hard time limit and a killed
pool child, which the worker's main process reports.
"""

import json
import os
import signal
import time
from typing import Any

import redis
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


GATE_TIMEOUT_S = 60


def _gate_open(client: "redis.Redis[bytes]", state: str, task_ids: list[str]) -> bool:
    """Whether the stemtrace stream holds a ``state`` event for every task in ``task_ids``."""
    seen = set()
    for _, fields in client.xrange(f"{EVENTS_PREFIX}:events"):
        payload = json.loads(fields[b"data"])
        if payload.get("state") == state:
            seen.add(payload.get("task_id"))
    return seen >= set(task_ids)


class KilledOnSuccess(Task):
    """A task whose ``on_success`` handler waits for its gate, then kills its pool child."""

    def on_success(
        self,
        retval: object,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Wait for ``kwargs["gate"]``, then die: the main process records the failure.

        Celery 5.6.3 app/trace.py runs this after it sent the next chain step. The
        gate is ``{"state": ..., "task_ids": [...]}``: the kill waits for a stemtrace
        event in that state from every listed task, so the later steps' progress
        never races it. After ``GATE_TIMEOUT_S`` the kill happens anyway, so a gate
        that never opens fails the test's assertions instead of hanging a pool slot.
        """
        gate = kwargs["gate"]
        client = redis.from_url(os.environ["MP_BROKER_URL"])
        deadline = time.monotonic() + GATE_TIMEOUT_S
        while time.monotonic() < deadline:
            if _gate_open(client, gate["state"], gate["task_ids"]):
                break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGKILL)


@app.task(name="mp.late_first", base=KilledOnSuccess)
def late_first(*_: object, **__: object) -> int:
    """Succeed, then be killed inside ``on_success``."""
    return 1


@app.task(name="mp.slow")
def slow(*_: object) -> int:
    """Run for 8 s: still running when ``mp.late_first`` is killed, once this has started."""
    time.sleep(8)
    return 1
