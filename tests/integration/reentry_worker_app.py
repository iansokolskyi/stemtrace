"""Celery app run as a real worker by ``test_cold_shutdown_reentry.py``.

Configured through environment variables:

- ``REENTRY_FLAGS``: a directory. Once the test creates ``arm`` in it, the
  next main-process publish holds the publish breaker's lock for
  ``HOLD_SECONDS`` (writing ``held`` first), so a SIGQUIT sent meanwhile
  lands while the main thread is inside the breaker's locked section.
- ``REENTRY_QUEUE``: the task queue and the stemtrace stream prefix.

The hook models a breaker that takes its lock on the next success (after
dropped events); the breaker's own code is unchanged.
"""

import os
import threading
import time
from pathlib import Path
from typing import Any

from celery import Celery
from celery.signals import worker_ready

import stemtrace

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
FLAGS = Path(os.environ.get("REENTRY_FLAGS", "/nonexistent"))
QUEUE = os.environ.get("REENTRY_QUEUE", "reentry-test")
HOLD_SECONDS = 6

app = Celery("reentry_worker_app", broker=REDIS_URL)
app.conf.update(
    task_default_queue=QUEUE, task_serializer="json", accept_content=["json"]
)
stemtrace.init_worker(
    app, transport_url=REDIS_URL, prefix=QUEUE, ttl=300, publish_received=True
)


class _HoldingLock:
    """Wraps the breaker's lock: the first acquisition once armed holds it."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._held_once = False

    def __enter__(self) -> "_HoldingLock":
        self._inner.acquire()
        if (FLAGS / "arm").exists() and not self._held_once:
            self._held_once = True
            (FLAGS / "held").write_text(str(threading.get_ident()))
            time.sleep(HOLD_SECONDS)  # the SIGQUIT handler runs during this
        return self

    def __exit__(self, *_: object) -> None:
        self._inner.release()


@worker_ready.connect
def _hold_breaker_lock_when_armed(**_: object) -> None:
    """In the worker's main process, wrap its publish breaker (test seam)."""
    breaker = stemtrace._transport._publish_breaker  # type: ignore[union-attr]  # set by init_worker
    breaker._lock = _HoldingLock(breaker._lock)
    record_success = breaker.record_success

    def record_success_after_drops(ticket: int) -> None:
        if (FLAGS / "arm").exists():
            breaker._dropped = max(breaker._dropped, 1)
        record_success(ticket)

    breaker.record_success = record_success_after_drops


@app.task(name="reentry.sleep")
def sleep(seconds: float) -> None:
    """Keep the pool process busy so cold shutdown has a task to cancel."""
    time.sleep(seconds)


@app.task(name="reentry.noop")
def noop() -> None:
    """Make the main process publish a RECEIVED event."""
