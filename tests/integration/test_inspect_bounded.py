"""Rows I1 and I2 of D7a against real connection failures (no broker is running).

Real kombu, real sockets: a closed local port refuses at once, a non-routable
address (``10.255.255.1``) never answers, so only our own bounds end the attempt.
Bounds are generous on purpose; the spec's are 3.5 s per attempt, 4 s per request.
"""

import socket
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.server.api.routes import _InspectRefresher, create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry

pytestmark = pytest.mark.integration

ATTEMPT_BOUND_S = 3.5
REQUEST_BOUND_S = 4.0
BACKOFF_WINDOW_BOUND_S = 0.2


def _refused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _urls() -> dict[str, str]:
    port = _refused_port()
    return {
        "redis-refused": f"redis://127.0.0.1:{port}/0",
        "amqp-refused": f"amqp://guest:guest@127.0.0.1:{port}//",
        "redis-blackholed": "redis://10.255.255.1:6379/0",
        "amqp-blackholed": "amqp://guest:guest@10.255.255.1:5672//",
    }


URLS = _urls()


@pytest.fixture(params=sorted(URLS))
def broker_url(request: pytest.FixtureRequest) -> str:
    return URLS[request.param]


def test_i1_i2_one_inspect_attempt_ends_within_the_bound(broker_url: str) -> None:
    refresher = _InspectRefresher(WorkerRegistry(), broker_url)

    started = time.monotonic()
    refresher.refresh_blocking()
    elapsed = time.monotonic() - started

    assert elapsed < ATTEMPT_BOUND_S
    assert refresher.ready_in() > 0.0


def test_i1_i2_requests_return_within_the_bound_and_fast_inside_the_backoff(
    broker_url: str,
) -> None:
    app = FastAPI()
    app.include_router(
        create_api_router(
            GraphStore(), worker_registry=WorkerRegistry(), broker_url=broker_url
        )
    )
    client = TestClient(app)

    started = time.monotonic()
    first = client.get("/api/anomalies")
    first_elapsed = time.monotonic() - started
    durations = []
    for path in ("/api/anomalies", "/api/workers", "/api/tasks/registry"):
        started = time.monotonic()
        response = client.get(path)
        durations.append(time.monotonic() - started)
        assert response.status_code == 200

    assert first.status_code == 200
    assert first_elapsed < REQUEST_BOUND_S
    assert max(durations) < BACKOFF_WINDOW_BOUND_S
