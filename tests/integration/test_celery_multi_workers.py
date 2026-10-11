"""Workers started with ``celery multi`` against a real Redis broker.

Starts two nodes with ``celery multi start`` on
``tests/integration/output_worker_app.py``, consumes their worker events with
the real ``EventConsumer`` and lists workers through the API, which refreshes
their status with a real ``celery inspect``. Each node must be listed once,
online, under its Celery nodename (``name@host``).

Requires Redis (``REDIS_URL``, default ``redis://localhost:6379/15``).
"""

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.server.consumer import EventConsumer
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore, WorkerRegistry

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 60


def _redis_available() -> bool:
    try:
        redis.from_url(REDIS_URL).ping()
    except redis.RedisError:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _redis_available(), reason="Redis not available"),
    pytest.mark.skipif(sys.platform == "win32", reason="celery multi needs POSIX"),
]


class MultiNodes:
    """Two ``celery multi`` nodes plus the stemtrace prefix they publish to."""

    def __init__(self, suffix: str, run_dir: Path) -> None:
        """Name the nodes, queue and stream after ``suffix``."""
        self.names = [f"w1-{suffix}", f"w2-{suffix}"]
        self.prefix = f"multi_test_{suffix}"
        self.queue = f"multi-test-{suffix}"
        self.run_dir = run_dir

    @property
    def nodenames(self) -> set[str]:
        """Nodenames Celery gives the nodes (``name@host``)."""
        host = socket.gethostname()
        return {f"{name}@{host}" for name in self.names}

    def multi(self, command: str) -> None:
        """Run ``celery multi <command>`` for both nodes."""
        env = {
            **os.environ,
            "REDIS_URL": REDIS_URL,
            "OUTPUT_TEST_QUEUE": self.queue,
            "OUTPUT_TEST_PREFIX": self.prefix,
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "celery",
                "multi",
                command,
                *self.names,
                "-A",
                "tests.integration.output_worker_app",
                f"--pidfile={self.run_dir}/%n.pid",
                f"--logfile={self.run_dir}/%n%I.log",
                "--concurrency=1",
            ],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
        )

    def kill_leftovers(self) -> None:
        """Kill any node ``stopwait`` left running (its pidfile still exists)."""
        for pidfile in self.run_dir.glob("*.pid"):
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)


@pytest.fixture
def nodes(tmp_path: Path) -> Iterator[MultiNodes]:
    """Start two nodes with ``celery multi`` and stop them afterwards."""
    multi = MultiNodes(uuid.uuid4().hex[:8], tmp_path)
    multi.multi("start")
    try:
        yield multi
    finally:
        multi.multi("stopwait")
        multi.kill_leftovers()
        client = redis.from_url(REDIS_URL)
        client.delete(
            f"{multi.prefix}:events", multi.queue, f"_kombu.binding.{multi.queue}"
        )


def _wait_for_registered(registry: WorkerRegistry, count: int) -> None:
    """Wait until the registry has ``count`` workers from worker_ready events."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if len(registry.get_all_workers()) >= count:
            return
        time.sleep(0.2)
    raise AssertionError(f"fewer than {count} workers registered within timeout")


def test_multi_nodes_listed_once_each_under_nodename(nodes: MultiNodes) -> None:
    """Refreshing from inspect keeps one online entry per node, by nodename."""
    store = GraphStore()
    registry = WorkerRegistry()
    consumer = EventConsumer(
        REDIS_URL, store, prefix=nodes.prefix, worker_registry=registry
    )
    consumer.start()
    try:
        _wait_for_registered(registry, 2)
        app = FastAPI()
        app.include_router(
            create_router(store=store, worker_registry=registry, broker_url=REDIS_URL)
        )

        response = TestClient(app).get("/api/workers?refresh=true")
    finally:
        consumer.stop()

    assert response.status_code == 200
    workers = response.json()["workers"]
    assert sorted(w["hostname"] for w in workers) == sorted(nodes.nodenames)
    assert {w["status"] for w in workers} == {"online"}
    assert all("output_test.noisy" in w["registered_tasks"] for w in workers)
