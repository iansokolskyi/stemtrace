"""Celery app run as a real prefork worker by ``test_on_success_hard_limit_worker.py``.

``OS_BROKER_URL`` is the broker, result backend and stemtrace event stream (Redis);
``OS_SUFFIX`` makes the queue and stream names unique.

``os.limited`` returns, then its ``on_success`` handler outlives the task's 2 s hard
time limit, so the worker's main process records the failure while the pool child
is inside ``on_success``.
"""

import os
import time
from typing import Any

from celery import Celery, Task

import stemtrace

SUFFIX = os.environ.get("OS_SUFFIX", "local")
QUEUE = f"os-{SUFFIX}"
EVENTS_PREFIX = f"os_test_{SUFFIX}"

app = Celery(
    "on_success_limit_worker_app",
    broker=os.environ.get("OS_BROKER_URL"),
    backend=os.environ.get("OS_BROKER_URL"),
)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_default_queue=QUEUE,
    result_expires=300,
)

stemtrace.init_worker(
    app,
    transport_url=os.environ.get("OS_BROKER_URL"),
    prefix=EVENTS_PREFIX,
    ttl=300,
)


class SlowOnSuccess(Task):
    """A task whose ``on_success`` handler outlives the hard time limit."""

    def on_success(
        self,
        retval: object,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Sleep far past ``time_limit``."""
        time.sleep(30)


@app.task(name="os.limited", base=SlowOnSuccess, time_limit=2)
def limited() -> int:
    """Return at once; the limit hits in ``on_success``."""
    return 1
