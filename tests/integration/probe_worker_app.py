"""Celery app run as a real worker by ``test_inspect_probe_real_worker.py``.

The worker sits in a slow ``worker_receiver`` for ``PROBE_TEST_READY_DELAY_S``
seconds after stemtrace published ``worker_ready``. With ``-P solo`` its event
loop, which drains the control queue, starts only after that, so an inspect sent
for the ``worker_ready`` goes unanswered for that long.
"""

import os
import time

from celery import Celery
from celery.signals import worker_ready

import stemtrace

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")

app = Celery("probe_worker_app", broker=REDIS_URL)
app.conf.update(
    task_default_queue=os.environ.get("PROBE_TEST_QUEUE", "probe-test"),
    task_serializer="json",
    accept_content=["json"],
)

stemtrace.init_worker(
    app,
    transport_url=REDIS_URL,
    prefix=os.environ.get("PROBE_TEST_PREFIX", "probe_test"),
    ttl=300,
)


# Connected after init_worker, so stemtrace's worker_ready handler runs first.
@worker_ready.connect
def _slow_ready_receiver(**_: object) -> None:
    """Hold the worker out of its event loop, as a slow receiver would."""
    time.sleep(float(os.environ.get("PROBE_TEST_READY_DELAY_S", "0")))
