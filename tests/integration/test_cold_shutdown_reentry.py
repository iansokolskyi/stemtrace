"""A worker's cold shutdown never hangs on a publish it interrupted.

Starts ``celery worker`` (prefork) on ``reentry_worker_app.py`` against a real
Redis. A long task keeps the pool process busy; then, with the app's hook
holding the publish breaker's lock in the main process during a RECEIVED
publish, the test sends SIGQUIT. Celery 5.6.3's cold shutdown cancels the task
from its signal handler and sends ``task_retry`` (apps/worker.py
``on_cold_shutdown``), so stemtrace publishes from inside that handler while
the interrupted publish holds the lock. Before the fix the worker hung there;
now the nested RETRY is dropped and the worker exits.

Requires Redis (``REDIS_URL``, default ``redis://localhost:6379/15``) and
Celery's cold-shutdown handler (Celery 5.5+).
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import celery
import celery.apps.worker as celery_worker_app
import pytest
import redis
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 60
EXIT_SECONDS = 30


def _redis_available() -> bool:
    try:
        redis.from_url(REDIS_URL).ping()
    except redis.RedisError:
        return False
    return True


def _has_cold_shutdown_handler() -> bool:
    return hasattr(celery_worker_app, "on_cold_shutdown")


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _redis_available(), reason="Redis not available"),
    pytest.mark.skipif(sys.platform == "win32", reason="needs SIGQUIT"),
    pytest.mark.skipif(
        not _has_cold_shutdown_handler(),
        reason=f"Celery {celery.__version__} has no cold-shutdown handler",
    ),
]


class Worker:
    """A worker subprocess, the stream it publishes to and the test's flags."""

    def __init__(self, tmp_path: Path) -> None:
        """Name the queue/stream and start the worker."""
        self.queue = f"reentry-{uuid.uuid4().hex[:8]}"
        self.flags = tmp_path / "flags"
        self.flags.mkdir()
        self.log_path = tmp_path / "worker.log"
        self.client = redis.from_url(REDIS_URL)
        env = {
            **os.environ,
            "REDIS_URL": REDIS_URL,
            "REENTRY_FLAGS": str(self.flags),
            "REENTRY_QUEUE": self.queue,
        }
        with self.log_path.open("wb") as log:
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "tests.integration.reentry_worker_app",
                    "worker",
                    "--pool=prefork",
                    "--concurrency=1",
                    "--loglevel=INFO",
                    "--without-gossip",
                    "--without-mingle",
                    "--without-heartbeat",
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.sender = Celery("reentry_sender", broker=REDIS_URL)
        self.sender.conf.task_default_queue = self.queue

    def events(self) -> list[tuple[str, str | None]]:
        """(state or worker event type, task name) of every event so far."""
        out: list[tuple[str, str | None]] = []
        for _, fields in self.client.xrange(f"{self.queue}:events"):
            data = json.loads(fields[b"data"])
            out.append((data.get("state") or data["event_type"], data.get("name")))
        return out

    def wait(self, ready: Callable[[], bool], what: str) -> None:
        """Wait until ``ready()``; fail with the worker log if it never is."""
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if ready():
                return
            time.sleep(0.2)
        raise AssertionError(f"{what} not seen; worker log:\n{self.log_tail()}")

    def log_tail(self) -> str:
        """The end of the worker's log."""
        return self.log_path.read_text(errors="replace")[-4000:]

    def kill(self) -> None:
        """Kill the worker and its pool processes."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()
        self.client.delete(
            f"{self.queue}:events", self.queue, f"_kombu.binding.{self.queue}"
        )


@pytest.fixture
def worker(tmp_path: Path) -> Iterator[Worker]:
    """A running worker, killed (with its pool) afterwards whatever happened."""
    worker = Worker(tmp_path)
    try:
        yield worker
    finally:
        worker.kill()


def test_sigquit_during_a_locked_publish_exits(worker: Worker) -> None:
    worker.wait(lambda: ("worker_ready", None) in worker.events(), "worker_ready")
    worker.sender.send_task("reentry.sleep", args=[60])
    worker.wait(
        lambda: ("STARTED", "reentry.sleep") in worker.events(), "sleep STARTED"
    )
    (worker.flags / "arm").write_text("1")
    worker.sender.send_task("reentry.noop")
    worker.wait(lambda: (worker.flags / "held").exists(), "breaker lock held")

    worker.proc.send_signal(signal.SIGQUIT)
    try:
        worker.proc.wait(timeout=EXIT_SECONDS)
    except subprocess.TimeoutExpired:
        worker.proc.send_signal(signal.SIGUSR1)  # Celery dumps the stacks
        time.sleep(2)
        raise AssertionError(
            f"worker still running {EXIT_SECONDS}s after SIGQUIT:\n{worker.log_tail()}"
        ) from None

    events = worker.events()
    # The interrupted RECEIVED publish finished; the cancelled task's RETRY,
    # published from the signal handler during it, was dropped.
    assert ("RECEIVED", "reentry.noop") in events
    assert ("RETRY", "reentry.sleep") not in events
