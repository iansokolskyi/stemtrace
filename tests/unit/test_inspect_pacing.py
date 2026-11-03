"""Rows I1, I3, I4 and I5 of D7a: a failed inspect counts toward pacing, backs off.

The inspector is a fake that models what Celery 5.6.3 / kombu 5.6.2 do with an
unreachable broker: ``stats()`` raises (``Mailbox._broadcast`` ->
``Connection.default_channel``) and every further call connects, and fails, again.
The clock is injected; nothing sleeps.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError

from stemtrace.server.api import routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.inspect_probe import InspectProbe
from stemtrace.server.store import GraphStore, WorkerRegistry

REAL_INSPECT_PROBE = True
BROKER = "redis://broker:6379/0"
NODE = "w1@host"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t


class Broker:
    """A fake broker: ``up`` False makes every inspector call raise, like kombu."""

    def __init__(self, error: Exception) -> None:
        self.up = False
        self.error = error
        self.stats_calls = 0
        self.other_calls = 0
        self.attempt_times: list[float] = []
        self.clock: Clock | None = None

    def inspector(self, _url: str, **_: object) -> "FakeInspect":
        return FakeInspect(self)


class FakeInspect:
    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    def stats(self) -> dict[str, object] | None:
        broker = self._broker
        broker.stats_calls += 1
        if broker.clock is not None:
            broker.attempt_times.append(broker.clock.now())
        if not broker.up:
            raise broker.error
        return None  # nobody answers: a successful broadcast, no new node

    def registered(self) -> dict[str, object] | None:
        self._broker.other_calls += 1
        if not self._broker.up:
            raise self._broker.error
        return None

    def ping(self) -> dict[str, object] | None:
        self._broker.other_calls += 1
        if not self._broker.up:
            raise self._broker.error
        return None


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(routes, "_monotonic", fake.now)
    return fake


@pytest.fixture
def broker(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> Broker:
    fake = Broker(OperationalError("refused"))
    fake.clock = clock
    monkeypatch.setattr(routes, "_get_inspector", fake.inspector)
    return fake


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(
        create_api_router(
            GraphStore(), worker_registry=WorkerRegistry(), broker_url=BROKER
        )
    )
    return TestClient(app)


def refresher() -> routes._InspectRefresher:
    return routes._InspectRefresher(WorkerRegistry(), BROKER)


# --- I1: a down broker costs few attempts, backing off 2, 4, 8 ... 60 ----------------


def test_i1_ten_requests_one_second_apart_attempt_at_0_2_and_6_seconds(
    broker: Broker, clock: Clock, client: TestClient
) -> None:
    start = clock.now()
    for second in range(10):
        clock.t = start + second
        assert client.get("/api/anomalies").status_code == 200

    assert [t - start for t in broker.attempt_times] == [0.0, 2.0, 6.0]


def test_i1_failed_inspect_stops_after_stats_without_reconnecting(
    broker: Broker, client: TestClient
) -> None:
    client.get("/api/anomalies")

    # One connect attempt per failed refresh: registered() and ping() would each
    # run kombu's whole connect again.
    assert (broker.stats_calls, broker.other_calls) == (1, 0)


def test_i1_a_raw_socket_error_from_every_call_still_counts_as_a_failure(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Broker(ConnectionResetError("reset"))
    monkeypatch.setattr(routes, "_get_inspector", fake.inspector)
    target = refresher()

    target.refresh_blocking()

    # stats() died mid-call: ping() gets its chance, and its failure backs off too.
    assert (fake.stats_calls, fake.other_calls) == (1, 2)
    assert target.ready_in() == 2.0


def test_i1_backoff_doubles_to_a_cap_of_sixty_seconds(
    broker: Broker, clock: Clock
) -> None:
    target = refresher()
    waits = []
    for _ in range(8):
        target.refresh_blocking()
        waits.append(target.ready_in())
        clock.t += target.ready_in()

    assert waits == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]


def test_i1_failure_counts_without_counting_as_a_successful_refresh(
    broker: Broker,
) -> None:
    target = refresher()

    target.refresh_blocking()

    assert target._last_refresh == 0.0
    assert target.ready_in() == 2.0


# --- I3: the broker recovers --------------------------------------------------------


def test_i3_recovery_after_backoff_succeeds_and_resets_the_backoff(
    broker: Broker, clock: Clock, client: TestClient
) -> None:
    start = clock.now()
    for second in (0, 2):  # fails at 0, then at 2: the next window is 4 s
        clock.t = start + second
        client.get("/api/anomalies")
    broker.up = True
    clock.t = start + 5  # inside the window: no attempt
    client.get("/api/anomalies")
    attempts_before = broker.stats_calls
    clock.t = start + 6
    client.get("/api/anomalies")

    assert (attempts_before, broker.stats_calls) == (2, 3)

    broker.up = False
    clock.t = start + 8  # past the 2 s success interval
    client.get("/api/anomalies")
    assert broker.stats_calls == 4
    clock.t = start + 9.9  # a reset backoff is 2 s, not the 8 s it would have been
    client.get("/api/anomalies")
    assert broker.stats_calls == 4
    clock.t = start + 10
    client.get("/api/anomalies")
    assert broker.stats_calls == 5


def test_i3_success_resets_the_backoff_to_the_regular_interval(
    broker: Broker, clock: Clock
) -> None:
    target = refresher()
    for _ in range(3):
        target.refresh_blocking()
        clock.t += target.ready_in()
    broker.up = True

    target.refresh_blocking()

    assert target._last_refresh == clock.now()
    assert target.ready_in() == routes._INSPECT_REFRESH_MIN_INTERVAL_SECONDS


# --- the probe and the requests share the one clock ---------------------------------


def test_request_failure_sets_the_wait_the_probe_reads_and_probe_failure_the_requests(
    broker: Broker, clock: Clock
) -> None:
    probe = InspectProbe(clock=clock.now, wait=lambda _e, _s: False)
    app = FastAPI()
    app.include_router(
        create_api_router(
            GraphStore(),
            worker_registry=WorkerRegistry(),
            broker_url=BROKER,
            inspect_probe=probe,
        )
    )
    client = TestClient(app)
    shared = probe.target
    assert shared is not None

    client.get("/api/anomalies")
    after_request = shared.ready_in()
    clock.t += after_request
    shared.refresh_blocking()  # what the probe thread runs
    after_probe = shared.ready_in()
    client.get("/api/anomalies")  # inside the probe's backoff: no attempt

    assert (after_request, after_probe, broker.stats_calls) == (2.0, 4.0, 2)


# --- I4: refresh=false never inspects -------------------------------------------------


def test_i4_refresh_false_on_anomalies_does_not_inspect(
    broker: Broker, client: TestClient
) -> None:
    broker.up = True

    response = client.get("/api/anomalies?refresh=false")

    assert response.status_code == 200
    assert (broker.stats_calls, broker.other_calls) == (0, 0)


def test_i4_refresh_true_is_the_default_and_inspects(
    broker: Broker, client: TestClient
) -> None:
    broker.up = True

    client.get("/api/anomalies")
    assert broker.stats_calls == 1


def test_i4_refresh_param_is_documented_in_openapi(client: TestClient) -> None:
    app = FastAPI()
    app.include_router(create_api_router(GraphStore()))
    parameters = (
        TestClient(app)
        .get("/openapi.json")
        .json()["paths"]["/api/anomalies"]["get"]["parameters"]
    )

    refresh = next(p for p in parameters if p["name"] == "refresh")

    assert refresh["schema"]["default"] is True
    assert "inspect" in refresh["description"].lower()


# --- I5: workers and registry share the bounds -----------------------------------------


@pytest.mark.parametrize("path", ["/api/workers", "/api/tasks/registry"])
def test_i5_workers_and_registry_back_off_after_a_failure(
    path: str, broker: Broker, clock: Clock, client: TestClient
) -> None:
    start = clock.now()
    for second in range(10):
        clock.t = start + second
        assert client.get(path).status_code == 200

    assert [t - start for t in broker.attempt_times] == [0.0, 2.0, 6.0]


def test_i5_one_failure_on_anomalies_holds_back_workers_and_registry(
    broker: Broker, client: TestClient
) -> None:
    client.get("/api/anomalies")
    client.get("/api/workers")
    client.get("/api/tasks/registry")

    assert broker.stats_calls == 1


# --- I2: the connect is bounded by what the app is built with ----------------------------


def test_i2_inspector_app_gets_one_bounded_connect_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, object] = {}

    class StubControl:
        def inspect(self, **_: object) -> object:
            return object()

    class StubCelery:
        control = StubControl()

        def __init__(self, *_: object, **kwargs: object) -> None:
            built.update(kwargs)

    monkeypatch.setattr(routes, "Celery", StubCelery)

    routes._get_inspector(BROKER)

    changes = built["changes"]
    assert isinstance(changes, dict)
    assert changes["broker_connection_timeout"] == 1.5
    assert changes["broker_transport_options"] == {
        "max_retries": 0,
        "socket_connect_timeout": 1.5,
    }
