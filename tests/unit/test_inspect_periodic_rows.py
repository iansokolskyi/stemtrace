"""Rows P11 to P18 of D7b, through a real extension, router and refresher.

The probe's clock and wait are stepped (see ``test_inspect_probe_schedule``), the
registry and the anomaly route read one datetime clock derived from it, and
``FakeFleet`` answers inspect as a Celery cluster does (``test_inspect_probe_rows``).
"""

import itertools
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
from stemtrace.server.store import WorkerRegistry
from tests.unit.test_inspect_probe import FakeClock
from tests.unit.test_inspect_probe_rows import BROKER, NODE_A, NODE_B, FakeFleet
from tests.unit.test_inspect_probe_schedule import Stepper

REAL_INSPECT_PROBE = True
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
HALF_WINDOW_S = 300.0  # the default 10 min authority_window, halved
KNOWN, NEW = "app.known", "app.new"


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
    """Expire owed times until the probe sleeps until a refresh is due in ``period_s``."""
    for _ in range(40):
        if stepper.next_block() == period_s:
            return
        stepper.expire()
    raise AssertionError("the probe never settled into its period wait")


def step(stepper: Stepper, times: int = 1) -> float | None:
    """End the probe's wait ``times`` times; the wait it ends up in."""
    block: float | None = None
    for _ in range(times):
        stepper.expire()
        block = stepper.next_block()
    return block


def anomaly_names(client: TestClient) -> list[str]:
    response = client.get("/stemtrace/api/anomalies?refresh=false&kind=unregistered")
    assert response.status_code == 200, response.text
    body: dict[str, list[dict[str, str]]] = response.json()
    return [item["name"] for item in body["anomalies"]]


def pending_new_name(extension: StemtraceExtension) -> None:
    extension.store.add_event(
        TaskEvent(task_id="n1", name=NEW, state=TaskState.PENDING, timestamp=T0)
    )


def test_p11_an_idle_fleet_keeps_authority_younger_than_the_window_for_30_minutes(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    pending_new_name(extension)
    registry = extension.worker_registry
    ages: list[float] = []

    with TestClient(app) as client:
        settle_to_period(stepper, HALF_WINDOW_S)
        for _ in range(6):  # 30 minutes
            ages.append(registry.authority_age_s() or 0.0)
            step(stepper)
            ages.append(registry.authority_age_s() or 0.0)

        assert clock.now() - 100.0 >= 6 * HALF_WINDOW_S
        assert max(ages) <= 600.0
        assert anomaly_names(client) == [NEW]


def test_p11_without_a_periodic_refresh_the_same_idle_fleet_loses_authority(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    pending_new_name(extension)

    with TestClient(app) as client:
        settle_to_period(stepper, HALF_WINDOW_S)
        # The control: authority is 30 minutes old when nothing refreshed it.
        clock.advance(6 * HALF_WINDOW_S)

        assert anomaly_names(client) == []


@pytest.mark.parametrize(
    ("window", "period_s"),
    [("2m", 60.0), ("30s", 15.0), ("4s", 10.0), ("1s", 10.0)],
)
def test_the_period_is_half_the_configured_window_with_a_floor_of_ten_seconds(
    fleet: FakeFleet, stepper: Stepper, window: str, period_s: float
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app(window=window)
    stepper.probe = extension._inspect_probe

    with TestClient(app):
        settle_to_period(stepper, period_s)


def test_p12_a_down_broker_stays_on_the_backoff_ladder_for_30_minutes(
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

    times = fleet.stats_times
    gaps = [b - a for a, b in itertools.pairwise(times)]
    # The ladder (2, 4, 8, 16, 32), then one attempt per 60 s: bounded, no tight loop.
    assert gaps[:5] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert gaps[5:] == [60.0] * len(gaps[5:])
    assert len(times) <= 1 + 5 + 1800 // 60


def test_p13_two_missed_registered_replies_never_let_authority_age_past_the_window(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    registry = extension.worker_registry

    with TestClient(app):
        settle_to_period(stepper, HALF_WINDOW_S)
        stamped_at = clock.now()
        before = len(fleet.stats_times)
        ages: list[float] = []
        # Two broadcasts where node A answers stats() but registered() omits it.
        fleet.omit_from_registered = {NODE_A}
        for _ in range(2):
            step(stepper)
            ages.append(registry.authority_age_s() or 0.0)
        fleet.omit_from_registered = set()
        step(stepper)
        ages.append(registry.authority_age_s() or 0.0)

    attempts = [t - stamped_at for t in fleet.stats_times[before:]]
    assert attempts == [HALF_WINDOW_S, HALF_WINDOW_S + 2.0, HALF_WINDOW_S + 6.0]
    assert max(ages) <= 600.0
    assert ages[-1] == 0.0  # the third attempt stamped


def test_a_request_refresh_that_misses_a_registered_reply_backs_off_like_a_failure(
    fleet: FakeFleet, clock: FakeClock
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    fleet.omit_from_registered = {NODE_A}
    target = routes._InspectRefresher(WorkerRegistry(), BROKER)
    waits: list[float] = []

    for _ in range(3):
        target.refresh_blocking()
        waits.append(target.ready_in())
        clock.advance(waits[-1])
    fleet.omit_from_registered = set()
    target.refresh_blocking()

    assert waits == [2.0, 4.0, 8.0]
    assert target.ready_in() == 2.0


def test_p14_an_outage_of_35_minutes_on_an_idle_fleet_ends_with_a_stamp_after_recovery(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    registry = extension.worker_registry

    with TestClient(app):
        settle_to_period(stepper, HALF_WINDOW_S)
        fleet.mode = "raises"
        outage_began = clock.now()
        while clock.now() - outage_began < 35 * 60:
            step(stepper)
            # What the consumer does on any event: the cleanup that drops stale workers.
            registry.remove_stale_workers()
        fleet.mode = "up"
        recovered = clock.now()
        stamps = registry.authority_stamps
        for _ in range(10):
            if registry.authority_stamps != stamps:
                break
            step(stepper)

    assert registry.authority_stamps == stamps + 1
    assert clock.now() - recovered <= 60.0


def test_p14_with_every_worker_stopped_a_failed_attempt_keeps_retrying_on_the_ladder(
    fleet: FakeFleet, stepper: Stepper
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    registry = extension.worker_registry

    with TestClient(app):
        settle_to_period(stepper, HALF_WINDOW_S)
        registry.mark_shutdown(NODE_A, 11)
        fleet.mode = "raises"
        extension._inspect_probe.trigger()
        before = len(fleet.stats_times)
        for _ in range(12):
            if len(fleet.stats_times) - before >= 4:
                break
            step(stepper)

    # Nobody is online, but the failed attempt keeps the refresh due: the server
    # can't hear whether the fleet came back.
    assert len(fleet.stats_times) - before >= 4


def test_a_stop_reported_for_the_whole_fleet_idles_the_probe(
    fleet: FakeFleet, stepper: Stepper
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe

    with TestClient(app):
        settle_to_period(stepper, HALF_WINDOW_S)
        extension.worker_registry.mark_shutdown(NODE_A, 11)
        before = len(fleet.stats_times)

        assert step(stepper) is None
        assert len(fleet.stats_times) == before


def test_p17_a_fleet_known_only_from_task_events_gets_the_periodic_refresh(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.mode = "silent"  # remote control off: nothing answers, nothing is proven
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    began = clock.now()

    with TestClient(app):
        extension.worker_registry.mark_seen(NODE_A, T0)
        while clock.now() - began < 300.0:
            try:
                stepper.next_block()
            except queue.Empty:
                break
            stepper.expire()

    # The owed times of the new nodename end at 60 s; later attempts are the periodic's.
    assert fleet.stats_times[-1] - began > 70.0


def test_p18_a_restart_under_the_same_nodename_refreshes_old_authority_at_once(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.nodes = {NODE_A: (11, [KNOWN])}
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    registry = extension.worker_registry

    with TestClient(app):
        settle_to_period(stepper, HALF_WINDOW_S)
        registry.mark_shutdown(NODE_A, 11)
        assert step(stepper) is None  # nobody online: the probe idles
        clock.advance(2 * HALF_WINDOW_S)  # authority is now older than the period
        fleet.nodes = {NODE_A: (12, [KNOWN])}
        restarted = clock.now()

        registry.register_worker(NODE_A, 12, [KNOWN])
        stepper.next_block()

    assert fleet.stats_times[-1] - restarted <= 2.0


def test_i10_a_join_after_an_outage_is_inspected_within_one_interval(
    fleet: FakeFleet, stepper: Stepper, clock: FakeClock
) -> None:
    fleet.mode = "raises"
    extension, app = extension_app()
    stepper.probe = extension._inspect_probe
    registry = extension.worker_registry

    with TestClient(app):
        shared = extension._inspect_probe.target
        assert shared is not None
        for _ in range(12):  # the outage drives the backoff up to its 60 s cap
            if shared.ready_in() >= 60.0:
                break
            step(stepper)
        assert shared.ready_in() >= 60.0 - 1e-9
        fleet.mode = "up"
        fleet.nodes = {NODE_B: (22, ["app.b"])}
        joined_at = clock.now()
        attempts = len(fleet.stats_times)

        registry.register_worker(NODE_B, 22, ["app.b"])
        stepper.next_block()  # the trigger is owed within the interval: the probe waits it out
        step(stepper)

    assert len(fleet.stats_times) > attempts
    assert (
        fleet.stats_times[attempts] - joined_at
        <= inspect_probe.INSPECT_REFRESH_MIN_INTERVAL_SECONDS
    )
