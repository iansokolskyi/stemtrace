"""Rows R1-R5 of D7c: a ``refresh=true`` request waits for the inspect in flight.

``GatedInspect`` models Celery 5.6.3 ``control.inspect``: ``stats()`` blocks until
the broker's replies come in (here, until the test opens the gate), then answers
like a live worker, or raises like an unreachable broker (kombu 5.6.2
``Connection.default_channel``). Requests run concurrently on one event loop
through an ASGI transport, so a waiter's cost in threads is observable.
"""

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import FastAPI
from kombu.exceptions import OperationalError

from stemtrace.server.api import routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.inspect_probe import InspectProbe
from stemtrace.server.store import GraphStore, WorkerRegistry

REAL_INSPECT_PROBE = True
BROKER = "redis://broker:6379/0"
NODE = "w1@host"
BOUND_S = 5.0
SHORT_WAIT_S = 0.4


class GatedBroker:
    """The cluster: ``stats()`` blocks on ``gate``, then answers or raises."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.started = threading.Event()
        self.fails = False
        self.attempts = 0

    def inspector(self, _url: str, **_: object) -> "GatedInspect":
        self.attempts += 1
        return GatedInspect(self)


class GatedInspect:
    def __init__(self, broker: GatedBroker) -> None:
        self._broker = broker

    def stats(self) -> object:
        self._broker.started.set()
        self._broker.gate.wait(BOUND_S + 5)
        if self._broker.fails:
            raise OperationalError("broker unreachable")
        return {NODE: {"pid": 11}}

    def registered(self) -> object:
        return {NODE: ["app.t"]}

    def ping(self) -> object:
        return {NODE: {"ok": "pong"}}


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> Iterator[GatedBroker]:
    fake = GatedBroker()
    monkeypatch.setattr(routes, "_get_inspector", fake.inspector)
    yield fake
    fake.gate.set()  # never leave an attempt's thread blocked


class IdleProbe(InspectProbe):
    """A probe that never starts an attempt: only requests and the test do."""

    def trigger(self, nodename: str | None = None) -> None:
        """Ignore a joining worker (the real probe would inspect again)."""

    def confirm(self) -> None:
        """Ignore a first miss."""


class Server:
    def __init__(
        self, client: httpx.AsyncClient, refresher: routes._InspectRefresher
    ) -> None:
        self.client = client
        self.refresher = refresher

    async def get(self, path: str = "/api/workers?refresh=true") -> httpx.Response:
        return await self.client.get(path)


@pytest.fixture
async def server(broker: GatedBroker) -> AsyncIterator[Server]:
    probe = IdleProbe()
    app = FastAPI()
    app.include_router(
        create_api_router(
            GraphStore(),
            worker_registry=WorkerRegistry(),
            broker_url=BROKER,
            inspect_probe=probe,
        )
    )
    refresher = probe.target
    assert isinstance(refresher, routes._InspectRefresher)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Server(client, refresher)


async def started(broker: GatedBroker) -> None:
    """Wait (off the loop) until an attempt's ``stats()`` is running."""
    assert await asyncio.to_thread(broker.started.wait, BOUND_S)


def names(response: httpx.Response) -> list[tuple[str, list[str]]]:
    return [(w["hostname"], w["registered_tasks"]) for w in response.json()["workers"]]


# --- R1: a page request that loses the race to the badge reads post-inspect state ------


async def test_r1_second_refresh_request_sees_the_workers_the_first_inspect_found(
    server: Server, broker: GatedBroker
) -> None:
    badge = asyncio.create_task(server.get("/api/anomalies?refresh=true"))
    await started(broker)
    page = asyncio.create_task(server.get())
    await asyncio.sleep(0.2)
    broker.gate.set()

    badge_response, page_response = await asyncio.gather(badge, page)

    assert badge_response.status_code == 200
    assert names(page_response) == [(NODE, ["app.t"])]
    assert broker.attempts == 1


@pytest.mark.parametrize(
    "path",
    [
        "/api/workers?refresh=true",
        "/api/tasks/registry?refresh=true",
        "/api/anomalies?refresh=true",
    ],
)
async def test_r1_a_waiter_returns_only_after_the_attempt_finishes(
    server: Server, broker: GatedBroker, path: str
) -> None:
    first = asyncio.create_task(server.get("/api/workers?refresh=true"))
    await started(broker)
    waiter = asyncio.create_task(server.get(path))

    done, _ = await asyncio.wait({waiter}, timeout=0.3)
    assert not done
    broker.gate.set()
    await first

    assert (await waiter).status_code == 200
    assert broker.attempts == 1


async def test_r1_refresh_false_does_not_wait(
    server: Server, broker: GatedBroker
) -> None:
    first = asyncio.create_task(server.get())
    await started(broker)

    began = time.monotonic()
    response = await server.get("/api/workers?refresh=false")
    elapsed = time.monotonic() - began
    broker.gate.set()
    await first

    assert response.json()["total"] == 0
    assert elapsed < 0.2


# --- R2: the wait is bounded ----------------------------------------------------------


async def test_r2_a_waiter_returns_current_state_when_the_bound_passes(
    server: Server, broker: GatedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes, "_INSPECT_WAIT_SECONDS", SHORT_WAIT_S)
    first = asyncio.create_task(server.get())
    await started(broker)  # the attempt never finishes: a blackholed broker

    began = time.monotonic()
    response = await server.get()
    elapsed = time.monotonic() - began
    broker.gate.set()
    await first

    assert response.json()["total"] == 0
    assert SHORT_WAIT_S <= elapsed < SHORT_WAIT_S + 1.0
    assert broker.attempts == 1
    assert server.refresher._waiters == []


def test_r2_the_bound_is_below_the_clients_timeout() -> None:
    assert 5.0 < routes._INSPECT_WAIT_SECONDS < 10.0


# --- R3: waiting costs no threads and doesn't block the loop -----------------------------


async def test_r3_two_hundred_waiters_hold_no_threads_and_other_routes_answer(
    server: Server, broker: GatedBroker
) -> None:
    first = asyncio.create_task(server.get())
    await started(broker)
    threads_before = threading.active_count()

    waiters = [asyncio.create_task(server.get()) for _ in range(200)]
    await asyncio.sleep(0.5)
    threads_with_waiters = threading.active_count()
    began = time.monotonic()
    other = await server.get("/api/tasks")
    elapsed = time.monotonic() - began
    pending = [w for w in waiters if not w.done()]
    broker.gate.set()
    responses = await asyncio.gather(first, *waiters)

    assert other.status_code == 200
    assert elapsed < 0.2
    assert len(pending) == 200
    assert threads_with_waiters - threads_before <= 2
    assert {r.status_code for r in responses} == {200}
    assert {len(names(r)) for r in responses} == {1}
    assert broker.attempts == 1


# --- R4: a failed attempt releases its waiters, with no second attempt --------------------


async def test_r4_a_failed_attempt_releases_waiters_without_another_attempt(
    server: Server, broker: GatedBroker
) -> None:
    broker.fails = True
    first = asyncio.create_task(server.get())
    await started(broker)
    waiters = [asyncio.create_task(server.get()) for _ in range(3)]
    await asyncio.sleep(0.2)
    began = time.monotonic()
    broker.gate.set()

    responses = await asyncio.gather(first, *waiters)

    assert time.monotonic() - began < 2.0
    assert [r.json()["total"] for r in responses] == [0, 0, 0, 0]
    assert broker.attempts == 1


async def test_r4_a_waiter_never_starts_a_second_attempt_after_the_first_ends(
    server: Server, broker: GatedBroker
) -> None:
    first = asyncio.create_task(server.get())
    await started(broker)
    waiter = asyncio.create_task(server.get())
    await asyncio.sleep(0.2)
    broker.gate.set()
    await asyncio.gather(first, waiter)

    assert broker.attempts == 1  # the waiter is paced out, not a second attempt


# --- R5: the probe's attempt in flight is waited for too ------------------------------------


async def test_r5_a_page_request_waits_for_the_probes_attempt(
    server: Server, broker: GatedBroker
) -> None:
    probe_thread = threading.Thread(target=server.refresher.refresh_blocking)
    probe_thread.start()
    try:
        await started(broker)
        page = asyncio.create_task(server.get())
        done, _ = await asyncio.wait({page}, timeout=0.3)
        assert not done
        broker.gate.set()
        response = await page
    finally:
        broker.gate.set()
        probe_thread.join(BOUND_S)

    assert names(response) == [(NODE, ["app.t"])]
    assert broker.attempts == 1


async def test_r5_the_lock_is_free_after_each_attempt(
    server: Server, broker: GatedBroker
) -> None:
    broker.gate.set()

    await server.get()
    await server.get()

    assert server.refresher._lock.acquire(blocking=False)
    server.refresher._lock.release()
    assert broker.attempts == 1  # the second is paced out
