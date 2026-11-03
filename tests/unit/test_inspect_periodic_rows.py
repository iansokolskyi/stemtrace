"""Rows P11 and P12 of D7b, through a real extension, router and refresher.

The probe's clock and wait are stepped (see ``test_inspect_probe_schedule``), the
registry and the anomaly route read one datetime clock derived from it, and
``FakeFleet`` answers inspect as a Celery cluster does (``test_inspect_probe_rows``).
"""

import queue
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server import inspect_probe, store
from stemtrace.server.api import params, routes
from stemtrace.server.fastapi.extension import StemtraceExtension
from tests.unit.test_inspect_probe import FakeClock
from tests.unit.test_inspect_probe_rows import BROKER, NODE_A, FakeFleet
from tests.unit.test_inspect_probe_schedule import Stepper

REAL_INSPECT_PROBE = True
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
HALF_WINDOW_S = 300.0  # the default 10 min authority_window, halved
KNOWN, NEW = "app.known", "app.new"


class Env:
    """The extension under test with its stepped clock."""

    def __init__(self, extension: StemtraceExtension, app: FastAPI) -> None:
        self.extension = extension
        self.app = app


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> Iterator[FakeFleet]:
    fake = FakeFleet()
    fake.clock = clock  # type: ignore[assignment]  # same now() the fleet reads
    monkeypatch.setattr(routes, "_get_inspector", fake.inspector)
    monkeypatch.setattr(routes, "_monotonic", clock.now)
    monkeypatch.setattr(inspect_probe, "_clock", clock.now)

    def wall() -> datetime:
        return T0 + timedelta(seconds=clock.now() - 100.0)

    monkeypatch.setattr(store, "_utcnow", wall)
    monkeypatch.setattr(params, "utcnow", wall)
    yield fake
    fake.first_attempt_gate.set()


@pytest.fixture
def stepper(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> Stepper:
    stepped = Stepper(clock)
    monkeypatch.setattr(inspect_probe, "_wait", stepped.wait)
    return stepped


def extension_app(window: str | None = None) -> tuple[StemtraceExtension, FastAPI]:
    extension = StemtraceExtension(
        broker_url=BROKER,
        embedded_consumer=False,
        serve_ui=False,
        anomaly_registry_window=window,
    )
    app = FastAPI()
    extension.init_app(app)
    return extension, app


def settle_to_period(stepper: Stepper, period_s: float) -> None:
    """Expire owed times until the probe sleeps for a whole period."""
    for _ in range(40):
        if stepper.next_block() == period_s:
            return
        stepper.expire()
    raise AssertionError("the probe never settled into its period wait")


def anomaly_names(client: TestClient) -> list[str]:
    response = client.get("/stemtrace/api/anomalies?refresh=false&kind=unregistered")
    assert response.status_code == 200, response.text
    body: dict[str, list[dict[str, str]]] = response.json()
    return [item["name"] for item in body["anomalies"]]


def test_p11_an_idle_fleet_keeps_authority_fresh_for_30_minutes_without_requests(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    extension.store.add_event(
        TaskEvent(task_id="n1", name=NEW, state=TaskState.PENDING, timestamp=T0)
    )

    with TestClient(app) as client:
        settle_to_period(stepper, HALF_WINDOW_S)
        for _ in range(6):  # 30 minutes
            stepper.expire()
            stepper.next_block()

        assert clock.now() - 100.0 >= 6 * HALF_WINDOW_S
        assert anomaly_names(client) == [NEW]


def test_p11_without_a_periodic_refresh_the_same_idle_fleet_loses_authority(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    extension.store.add_event(
        TaskEvent(task_id="n1", name=NEW, state=TaskState.PENDING, timestamp=T0)
    )

    with TestClient(app) as client:
        settle_to_period(stepper, HALF_WINDOW_S)
        # The control: authority is 30 minutes old when nothing refreshed it.
        clock.advance(6 * HALF_WINDOW_S)

        assert anomaly_names(client) == []


def test_a_configured_authority_window_sets_the_period(
    fleet: FakeFleet, stepper: Stepper
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app(window="2m")
    stepper.probe = extension._inspect_probe

    with TestClient(app):
        settle_to_period(stepper, 60.0)


def test_p12_a_down_broker_backs_off_and_stays_bounded_over_30_minutes(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.mode = "raises"
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    extension.worker_registry.register_worker(NODE_A, 11, [KNOWN])
    began = clock.now()

    with TestClient(app):
        while clock.now() - began < 1800.0:
            try:
                stepper.next_block()
            except queue.Empty:
                break
            stepper.expire()

    gaps = [
        b - a for a, b in zip(fleet.stats_times, fleet.stats_times[1:], strict=False)
    ]
    # The joined node's owed attempts follow the D7a backoff (each gap doubles); once
    # they are served, only the period remains: one attempt per 300 s, no tight loop.
    assert gaps[:5] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert gaps[5:] == [HALF_WINDOW_S] * len(gaps[5:])
    assert len(gaps[5:]) >= 4
    assert len(fleet.stats_times) <= 12
