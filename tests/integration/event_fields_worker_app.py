"""Celery app run as a real worker by ``test_event_fields_worker.py``.

Configured through environment variables so each run has its own queues,
exchange and stemtrace stream:

- ``EF_BROKER_URL``: the Celery broker (Redis or RabbitMQ).
- ``EF_EVENTS_URL``: where stemtrace publishes events (Redis, read back by the test).
- ``EF_SUFFIX``: makes queue, exchange and stream names unique.

Routes as ``event_fields_routing.routing_config`` sets them.
"""

import os
import time

from celery import Celery, Task

import stemtrace
from tests.integration.event_fields_routing import Names, routing_config

NAMES = Names(os.environ.get("EF_SUFFIX", "local"))


class PartnerAPIError(Exception):
    """A user-defined exception, as a task module would declare it."""


app = Celery("event_fields_worker_app", broker=os.environ.get("EF_BROKER_URL"))
app.conf.update(routing_config(NAMES))

stemtrace.init_worker(
    app,
    transport_url=os.environ.get("EF_EVENTS_URL"),
    prefix=NAMES.events_prefix,
    ttl=300,
    publish_received=True,
)


@app.task(name="ef.default")
def default() -> None:
    """Run from the default queue."""


@app.task(name="ef.routed")
def routed() -> None:
    """Run from the queue task_routes names."""


@app.task(name="ef.topic")
def topic() -> None:
    """Run from the queue bound to the topic exchange."""


@app.task(name="ef.fails")
def fails() -> None:
    """Fail with a user-defined exception."""
    raise PartnerAPIError("partner answered 502 for token=secret-token")


@app.task(name="ef.retries_in_except", bind=True, max_retries=1, default_retry_delay=0)
def retries_in_except(self: Task) -> None:
    """Retry once from the except block that handles the error, then succeed."""
    try:
        if self.request.retries == 0:
            raise ConnectionError("partner refused the connection")
    except ConnectionError:
        raise self.retry()  # noqa: B904 (the usual Celery idiom; retry() raises)


@app.task(name="ef.hard_limit", time_limit=1)
def hard_limit() -> None:
    """Outlive the hard time limit: the main process reports the failure."""
    time.sleep(30)


@app.task(
    name="ef.retry_then_hard_limit",
    bind=True,
    time_limit=1,
    max_retries=1,
    default_retry_delay=0,
)
def retry_then_hard_limit(self: Task) -> None:
    """Retry once; the second attempt outlives the hard time limit."""
    if self.request.retries == 0:
        raise self.retry(exc=PartnerAPIError("try again"))
    time.sleep(30)


@app.task(name="ef.retries_once", bind=True, max_retries=1, default_retry_delay=0)
def retries_once(self: Task) -> None:
    """Retry once because of a user-defined exception, then succeed."""
    if self.request.retries == 0:
        raise self.retry(exc=PartnerAPIError("try again"))
