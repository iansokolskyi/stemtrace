"""Row R1 of D7c against a real ``-P solo`` Celery worker and Redis (``REDIS_URL``).

The server has just started and knows no worker (no events seen yet). Two
``refresh=true`` requests arrive 1 ms apart, the way a page and the sidebar badge
do. One inspect runs; both must answer from the state after it.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import redis
from fastapi import FastAPI

from stemtrace.server.api import routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.inspect_probe import InspectProbe
from stemtrace.server.store import GraphStore, WorkerRegistry

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]
UP_TIMEOUT_S = 40


def _redis_available() -> bool:
    try:
        redis.from_url(REDIS_URL).ping()
    except redis.RedisError:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _redis_available(), reason="Redis not available"),
]


class QuietProbe(InspectProbe):
    """No probe attempts: only the two requests may inspect."""

    def trigger(self, nodename: str | None = None) -> None:
        """Ignore a joining worker."""

    def confirm(self) -> None:
        """Ignore a first miss."""


@pytest.fixture
def worker(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Start one real worker; yield its nodename and queue prefix."""
    suffix = uuid.uuid4().hex[:8]
    prefix, nodename = f"wait_{suffix}", f"wait{suffix}@localhost"
    with (tmp_path / "worker.log").open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "tests.integration.probe_worker_app",
                "worker",
                "--pool=solo",
                f"--hostname={nodename}",
                "--loglevel=INFO",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "REDIS_URL": REDIS_URL,
                "PROBE_TEST_PREFIX": prefix,
                "PROBE_TEST_QUEUE": f"probe-{suffix}",
                "PROBE_TEST_READY_DELAY_S": "0",
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        yield prefix, nodename
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=20)
        redis.from_url(REDIS_URL).delete(
            f"{prefix}:events", f"probe-{suffix}", f"_kombu.binding.probe-{suffix}"
        )


def _wait_until_it_answers(nodename: str) -> None:
    deadline = time.monotonic() + UP_TIMEOUT_S
    while time.monotonic() < deadline:
        inspector = routes._get_inspector(REDIS_URL, timeout_seconds=1.0)
        if inspector is not None and nodename in (inspector.ping() or {}):
            return
        time.sleep(0.5)
    pytest.fail("the worker never answered a ping")


async def test_r1_two_concurrent_refresh_requests_both_read_the_worker_online(
    worker: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, nodename = worker
    _wait_until_it_answers(nodename)
    inspections: list[float] = []
    real_get = routes._get_inspector

    def counting(url: str, **kwargs: float) -> object:
        inspections.append(time.monotonic())
        return real_get(url, **kwargs)  # type: ignore[arg-type]  # test passthrough

    monkeypatch.setattr(routes, "_get_inspector", counting)
    registry = WorkerRegistry()
    app = FastAPI()
    app.include_router(
        create_api_router(
            GraphStore(),
            worker_registry=registry,
            broker_url=REDIS_URL,
            inspect_probe=QuietProbe(),
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        page = asyncio.create_task(client.get("/api/workers?refresh=true"))
        await asyncio.sleep(0.001)
        badge = asyncio.create_task(client.get("/api/workers?refresh=true"))
        responses = await asyncio.gather(page, badge)

    statuses = [
        sorted(w["status"] for w in r.json()["workers"] if w["hostname"] == nodename)
        for r in responses
    ]
    assert len(inspections) == 1
    assert statuses == [[WorkerStatus.ONLINE.value]] * 2


async def test_r1_aged_worker_reads_online_for_both_concurrent_refresh_requests(
    worker: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, nodename = worker
    _wait_until_it_answers(nodename)
    inspections: list[float] = []
    real_get = routes._get_inspector

    def counting(url: str, **kwargs: float) -> object:
        inspections.append(time.monotonic())
        return real_get(url, **kwargs)  # type: ignore[arg-type]  # test passthrough

    monkeypatch.setattr(routes, "_get_inspector", counting)
    registry = WorkerRegistry()
    refresher = routes._InspectRefresher(registry, REDIS_URL)
    assert refresher.refresh_blocking() == frozenset({nodename})  # the first inspect
    # Idle past the stale window, and the stale sweep has run (D7c reading 4).
    for info in registry.get_workers_by_hostname(nodename):
        info.last_seen = datetime.now(timezone.utc) - timedelta(minutes=4)
    registry.remove_stale_workers()
    assert [w.status for w in registry.get_workers_by_hostname(nodename)] == [
        WorkerStatus.OFFLINE
    ]
    time.sleep(routes._INSPECT_REFRESH_MIN_INTERVAL_SECONDS + 0.2)
    inspections.clear()
    probe = QuietProbe()
    probe.bind(refresher)
    app = FastAPI()
    app.include_router(
        create_api_router(
            GraphStore(),
            worker_registry=registry,
            broker_url=REDIS_URL,
            inspect_probe=probe,
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        page = asyncio.create_task(client.get("/api/workers?refresh=true"))
        await asyncio.sleep(0.001)
        badge = asyncio.create_task(client.get("/api/workers?refresh=true"))
        responses = await asyncio.gather(page, badge)

    statuses = [
        sorted(w["status"] for w in r.json()["workers"] if w["hostname"] == nodename)
        for r in responses
    ]
    assert len(inspections) == 1
    assert statuses == [[WorkerStatus.ONLINE.value]] * 2
