"""Row P7 against a real Celery worker and Redis (``REDIS_URL``).

A ``-P solo`` worker publishes ``worker_ready`` and then sits in a slow receiver,
so the server's first inspect for it goes unanswered. Nobody requests anything:
the probe must still bring the worker online once it answers.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import WorkerEvent, WorkerEventType
from stemtrace.server.api import routes
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.transports import get_transport

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 40
READY_DELAY_S = 3


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


@pytest.fixture
def worker_process(
    tmp_path: Path,
) -> Iterator[tuple[str, str, list[subprocess.Popen[bytes]]]]:
    """Names for one run, and the list the test starts its worker into."""
    suffix = uuid.uuid4().hex[:8]
    prefix = f"probe_{suffix}"
    procs: list[subprocess.Popen[bytes]] = []
    yield prefix, f"probe{suffix}@localhost", procs
    for proc in procs:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=20)
    client = redis.from_url(REDIS_URL)
    client.delete(
        f"{prefix}:events", f"probe-{suffix}", f"_kombu.binding.probe-{suffix}"
    )


def test_p7_slow_worker_ready_receiver_still_ends_online_without_a_request(
    tmp_path: Path,
    worker_process: tuple[str, str, list[subprocess.Popen[bytes]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix, nodename, procs = worker_process
    attempts: list[tuple[float, object]] = []
    real_refresh = routes._InspectRefresher.refresh_blocking

    def recording(self: routes._InspectRefresher) -> frozenset[str] | None:
        answered = real_refresh(self)
        attempts.append((round(time.monotonic(), 1), answered))
        return answered

    monkeypatch.setattr(routes._InspectRefresher, "refresh_blocking", recording)
    extension = StemtraceExtension(
        broker_url=REDIS_URL,
        transport_url=REDIS_URL,
        prefix=f"/{prefix}",
        serve_ui=False,
    )
    registry = extension.worker_registry
    app = FastAPI()
    extension.init_app(app)

    with TestClient(app), (tmp_path / "worker.log").open("wb") as log:
        procs.append(
            subprocess.Popen(
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
                    "PROBE_TEST_QUEUE": f"probe-{prefix.removeprefix('probe_')}",
                    "PROBE_TEST_READY_DELAY_S": str(READY_DELAY_S),
                },
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )
        # An inspect reply naming the worker is the proof it answered.
        deadline = time.monotonic() + TIMEOUT_S
        while time.monotonic() < deadline:
            if any(nodename in (names or ()) for _, names in attempts):
                break
            time.sleep(0.2)

    workers = registry.get_workers_by_hostname(nodename)
    log = (tmp_path / "worker.log").read_text(errors="replace")[-3000:]
    assert any(nodename in (names or ()) for _, names in attempts), (
        f"no inspect reply named the worker; attempts {attempts}; log:\n{log}"
    )
    assert [w.status for w in workers] == [WorkerStatus.ONLINE]


def _spawn_worker(
    prefix: str, nodename: str, log_path: Path, ready_delay_s: float
) -> subprocess.Popen[bytes]:
    with log_path.open("wb") as log:
        return subprocess.Popen(
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
                "PROBE_TEST_QUEUE": f"probe-{prefix.removeprefix('probe_')}",
                "PROBE_TEST_READY_DELAY_S": str(ready_delay_s),
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def test_p10_real_worker_joining_during_a_dead_nodenames_backoff_is_named_promptly(
    tmp_path: Path,
    worker_process: tuple[str, str, list[subprocess.Popen[bytes]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix, nodename, procs = worker_process
    attempts: list[tuple[float, object]] = []
    real_refresh = routes._InspectRefresher.refresh_blocking

    def recording(self: routes._InspectRefresher) -> frozenset[str] | None:
        answered = real_refresh(self)
        attempts.append((time.monotonic(), answered))
        return answered

    monkeypatch.setattr(routes._InspectRefresher, "refresh_blocking", recording)
    # A worker_ready for a worker that died: Redis replays it at server start.
    ghost = f"ghost{prefix.removeprefix('probe_')}@localhost"
    get_transport(REDIS_URL, prefix=prefix, ttl=300).publish(
        WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname=ghost,
            pid=999_999,
            timestamp=datetime.now(timezone.utc),
        )
    )
    extension = StemtraceExtension(
        broker_url=REDIS_URL,
        transport_url=REDIS_URL,
        prefix=f"/{prefix}",
        serve_ui=False,
    )
    registry = extension.worker_registry
    app = FastAPI()
    extension.init_app(app)

    with TestClient(app):
        deadline = time.monotonic() + TIMEOUT_S
        while not registry.get_workers_by_hostname(ghost):
            assert time.monotonic() < deadline, "the ghost worker_ready wasn't replayed"
            time.sleep(0.05)
        # Let the probe make four attempts for the ghost, so it sleeps through
        # a long step of its schedule, then start a real worker.
        while len(attempts) < 4:
            assert time.monotonic() < deadline, (
                f"the probe made {len(attempts)} attempts"
            )
            time.sleep(0.05)
        procs.append(_spawn_worker(prefix, nodename, tmp_path / "worker.log", 0))
        deadline = time.monotonic() + TIMEOUT_S
        while not registry.get_workers_by_hostname(nodename):
            assert time.monotonic() < deadline, "the worker's ready never arrived"
            time.sleep(0.05)
        joined = time.monotonic()
        while time.monotonic() < deadline:
            if any(nodename in (names or ()) for _, names in attempts):
                break
            time.sleep(0.05)

    named = [at for at, names in attempts if nodename in (names or ())]
    assert named, f"no inspect reply named the worker; attempts {attempts}"
    # One shared interval, plus the inspect's own time (three calls, 0.5 s each).
    timeline = [(round(at - joined, 1), sorted(names or ())) for at, names in attempts]
    assert named[0] - joined <= 2.0 + 3 * 0.5 + 2.0, (
        f"attempts relative to the join: {timeline}"
    )
