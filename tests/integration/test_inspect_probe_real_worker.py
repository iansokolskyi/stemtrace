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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.server import store as store_module
from stemtrace.server.api import params, routes
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.transports import get_transport

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 40
READY_DELAY_S = 3
# One shared interval (2 s) plus one attempt (three 0.5 s calls), with margin.
CONFIRM_BOUND_S = 8.0
BROADCASTS = 40
BROADCAST_GAP_S = 2.1  # just over the interval two misses need


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


def test_p6_real_fleet_killed_before_any_request_gives_the_item_hours_later(
    tmp_path: Path,
    worker_process: tuple[str, str, list[subprocess.Popen[bytes]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real worker joins and answers the probe; SIGKILL; one request 7 hours later."""
    prefix, nodename, procs = worker_process
    answered_names: list[object] = []
    real_refresh = routes._InspectRefresher.refresh_blocking

    def recording(self: routes._InspectRefresher) -> frozenset[str] | None:
        answered = real_refresh(self)
        answered_names.append(answered)
        return answered

    monkeypatch.setattr(routes._InspectRefresher, "refresh_blocking", recording)
    extension = StemtraceExtension(
        broker_url=REDIS_URL,
        transport_url=REDIS_URL,
        prefix=f"/{prefix}",
        serve_ui=False,
    )
    extension.store.add_event(
        TaskEvent(
            task_id="t1",
            name="app.known",
            state=TaskState.PENDING,
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
    )
    app = FastAPI()
    extension.init_app(app)

    with TestClient(app) as client:
        procs.append(_spawn_worker(prefix, nodename, tmp_path / "worker.log", 0))
        deadline = time.monotonic() + TIMEOUT_S
        while time.monotonic() < deadline:
            if any(nodename in (names or ()) for names in answered_names):
                break
            time.sleep(0.2)
        assert any(nodename in (names or ()) for names in answered_names)
        # Nobody has made a request. SIGKILL the fleet; the first request comes 7 hours later.
        os.killpg(procs[0].pid, signal.SIGKILL)
        procs[0].wait(timeout=20)
        later = timedelta(hours=7)
        monkeypatch.setattr(
            store_module, "_utcnow", lambda: datetime.now(timezone.utc) + later
        )
        monkeypatch.setattr(
            params, "utcnow", lambda: datetime.now(timezone.utc) + later
        )
        monkeypatch.setattr(routes, "_monotonic", lambda: time.monotonic() + 7 * 3600)

        started = time.monotonic()
        first = _fleet_items(client, prefix, "")
        items = first
        # D4d: that request is the first miss; the probe's confirm, one shared
        # interval and one attempt later, is the second. No second request.
        while not items and time.monotonic() - started < CONFIRM_BOUND_S:
            time.sleep(0.1)
            items = _fleet_items(client, prefix, "?refresh=false")
        confirmed_after = time.monotonic() - started

    assert first == [], "one unanswered broadcast must not be evidence"
    assert items == [("no_workers_online", 1)]
    assert confirmed_after <= CONFIRM_BOUND_S, f"confirmed after {confirmed_after:.1f}s"


def _fleet_items(client: TestClient, prefix: str, query: str) -> list[tuple[str, int]]:
    body = client.get(f"/{prefix}/api/anomalies{query}").json()
    return [(a["kind"], a["task_count"]) for a in body["anomalies"]]


def test_d4d_real_idle_worker_never_reads_offline_across_many_broadcasts(
    tmp_path: Path,
    worker_process: tuple[str, str, list[subprocess.Popen[bytes]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle healthy solo worker, past the stale window, over many real broadcasts.

    The registry's clock runs 10 minutes ahead, so the worker is past
    ``WORKER_STALE_TIMEOUT_MINUTES`` and only the two-miss rule keeps it online.
    Isolated misses (``stats()`` returning None now and then) are counted, not hidden.
    """
    prefix, nodename, procs = worker_process
    misses: list[int] = []  # per broadcast: 1 when the worker did not answer it
    extension = StemtraceExtension(
        broker_url=REDIS_URL,
        transport_url=REDIS_URL,
        prefix=f"/{prefix}",
        serve_ui=False,
    )
    extension.store.add_event(
        TaskEvent(
            task_id="t1",
            name="app.known",
            state=TaskState.PENDING,
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
    )
    app = FastAPI()
    extension.init_app(app)
    registry = extension.worker_registry
    refresher = routes._InspectRefresher(registry, REDIS_URL)

    with TestClient(app) as client:
        procs.append(_spawn_worker(prefix, nodename, tmp_path / "worker.log", 0))
        deadline = time.monotonic() + TIMEOUT_S
        while not registry.get_workers_by_hostname(nodename):
            assert time.monotonic() < deadline, "the worker never registered"
            time.sleep(0.1)
        # A worker still starting can miss runs of broadcasts; wait for its first answer.
        while nodename not in (refresher.refresh_blocking() or ()):
            assert time.monotonic() < deadline, "the worker never answered an inspect"
            time.sleep(BROADCAST_GAP_S)
        ahead = timedelta(minutes=10)
        monkeypatch.setattr(
            store_module, "_utcnow", lambda: datetime.now(timezone.utc) + ahead
        )
        monkeypatch.setattr(
            params, "utcnow", lambda: datetime.now(timezone.utc) + ahead
        )
        items_seen: list[list[tuple[str, int]]] = []
        for _ in range(BROADCASTS):
            answered = refresher.refresh_blocking()
            if answered is not None:  # None: the probe's own refresh held the slot
                misses.append(0 if nodename in answered else 1)
                items_seen.append(_fleet_items(client, prefix, "?refresh=false"))
            time.sleep(BROADCAST_GAP_S)

    print(f"broadcasts={len(misses)} worker-missed={sum(misses)} pattern={misses}")
    assert len(misses) >= BROADCASTS // 2
    assert items_seen == [[]] * len(misses)
