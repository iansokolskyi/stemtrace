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
# `celery multi start` imports the app in two new processes; on a loaded host that
# alone took 25-60 s, so it gets more room than the waits on events do.
MULTI_TIMEOUT_SECONDS = 180


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

    def multi(self, command: str, names: list[str] | None = None) -> None:
        """Run ``celery multi <command>`` for ``names`` (default: both nodes)."""
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
                *(names or self.names),
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
            timeout=MULTI_TIMEOUT_SECONDS,
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
    try:
        multi.multi("start")
        yield multi
    finally:
        multi.multi("stopwait")
        multi.kill_leftovers()
        client = redis.from_url(REDIS_URL)
        client.delete(
            f"{multi.prefix}:events", multi.queue, f"_kombu.binding.{multi.queue}"
        )


def _main_pid(nodes: MultiNodes, name: str) -> int:
    """PID of a node's main process, from its ``celery multi`` pidfile."""
    return int((nodes.run_dir / f"{name}.pid").read_text().strip())


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


def _refresh_and_list(
    nodes: MultiNodes, store: GraphStore, registry: WorkerRegistry
) -> dict[tuple[str, int], str]:
    """List workers through the API with a real ``celery inspect`` refresh."""
    app = FastAPI()
    app.include_router(
        create_router(store=store, worker_registry=registry, broker_url=REDIS_URL)
    )
    response = TestClient(app).get("/api/workers?refresh=true")
    assert response.status_code == 200
    return {(w["hostname"], w["pid"]): w["status"] for w in response.json()["workers"]}


def test_restarted_node_shows_only_its_new_process_online(nodes: MultiNodes) -> None:
    """After ``celery multi restart`` each node has one online pid, the new one."""
    store = GraphStore()
    registry = WorkerRegistry()
    consumer = EventConsumer(
        REDIS_URL, store, prefix=nodes.prefix, worker_registry=registry
    )
    consumer.start()
    try:
        _wait_for_registered(registry, 2)
        old_pids = {name: _main_pid(nodes, name) for name in nodes.names}

        nodes.multi("restart")
        _wait_for_registered(registry, 4)
        listed = _refresh_and_list(nodes, store, registry)
    finally:
        consumer.stop()

    new_pids = {name: _main_pid(nodes, name) for name in nodes.names}
    host = socket.gethostname()
    for name in nodes.names:
        nodename = f"{name}@{host}"
        assert new_pids[name] != old_pids[name]
        assert listed[(nodename, new_pids[name])] == "online"
        assert listed[(nodename, old_pids[name])] == "offline"


def test_node_killed_without_shutdown_event_is_offline_once_restarted(
    nodes: MultiNodes,
) -> None:
    """A SIGKILLed node sends no worker_shutdown; its restart retires the old pid."""
    store = GraphStore()
    registry = WorkerRegistry()
    consumer = EventConsumer(
        REDIS_URL, store, prefix=nodes.prefix, worker_registry=registry
    )
    consumer.start()
    killed, kept = nodes.names
    try:
        _wait_for_registered(registry, 2)
        killed_pid = _main_pid(nodes, killed)
        kept_pid = _main_pid(nodes, kept)
        os.kill(killed_pid, signal.SIGKILL)
        (nodes.run_dir / f"{killed}.pid").unlink()

        nodes.multi("start", [killed])
        _wait_for_registered(registry, 3)
        listed = _refresh_and_list(nodes, store, registry)
        new_pid = _main_pid(nodes, killed)
    finally:
        consumer.stop()

    host = socket.gethostname()
    assert listed == {
        (f"{killed}@{host}", killed_pid): "offline",
        (f"{killed}@{host}", new_pid): "online",
        (f"{kept}@{host}", kept_pid): "online",
    }
