"""Celery app run as a real worker by ``test_output_capture_worker.py``.

Configured through environment variables so each test run uses its own
queue and stemtrace stream.
"""

import logging
import os
import sys
import time

from celery import Celery, Task
from celery.utils.log import get_task_logger

import stemtrace

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")

app = Celery("output_worker_app", broker=REDIS_URL)
app.conf.update(
    task_default_queue=os.environ.get("OUTPUT_TEST_QUEUE", "output-test"),
    task_serializer="json",
    accept_content=["json"],
    worker_hijack_root_logger=True,
)

stemtrace.init_worker(
    app,
    transport_url=REDIS_URL,
    prefix=os.environ.get("OUTPUT_TEST_PREFIX", "output_test"),
    ttl=300,
    capture_output=True,
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


@app.task(name="output_test.tagged")
def tagged(tag: str) -> None:
    """Print a tag a few times, slowly, so concurrent tasks interleave."""
    for i in range(5):
        print(f"{tag} {i}")
        time.sleep(0.05)
