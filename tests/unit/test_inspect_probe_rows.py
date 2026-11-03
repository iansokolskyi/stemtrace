"""Rows P1-P5 of the D4c inspect probe, through a real extension and router.

``FakeFleet`` models what a Celery cluster answers to broadcast: Celery 5.6.3
``control.inspect`` returns None when nobody answers within the timeout, and an
unreachable broker raises (measured: about 6 s per call, kombu retries). A call
can block until the test releases it, which stands for the 0.5 s reply wait. A
reply describes the cluster as of the moment the call started. The probe's
clock and wait are faked, so no test waits out the 2 s interval.
"""

import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server import inspect_probe
from stemtrace.server.api import routes
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.consumer import AsyncEventConsumer, EventConsumer
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore, WorkerRegistry

REAL_INSPECT_PROBE = True
BROKER = "redis://broker:6379/0"
NODE_A, NODE_B, NODE_C = "celery@a", "celery@b", "celery@c"
BOUND_S = 5.0
T = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def take(semaphore: threading.Semaphore) -> None:
    assert semaphore.acquire(timeout=BOUND_S), "the probe did not get this far"


class FakeClock:
    def __init__(self) -> None:
        self._now = 1000.0
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def wait(self, stop: threading.Event, seconds: float) -> bool:
        with self._lock:
            self._now += seconds
        return stop.is_set()


class FakeInspect:
    """One ``Inspect`` object: its three calls answer from the cluster at stats() time."""

    def __init__(self, fleet: "FakeFleet", attempt: int) -> None:
        self._fleet = fleet
        self._attempt = attempt
        self._snapshot: dict[str, tuple[int, list[str]]] = {}

    def stats(self) -> object:
        fleet = self._fleet
        fleet.stats_calls += 1
        # A node whose control loop hasn't started yet (a worker still inside
        # its worker_ready receivers) hasn't answered by "silent_until".
        now = fleet.clock.now()
        fleet.stats_times.append(now)
        self._snapshot = {
            name: node
            for name, node in fleet.nodes.items()
            if fleet.silent_until.get(name, 0.0) <= now
        }
        fleet.attempt_started.release()
        if self._attempt == 1:
            fleet.first_attempt_gate.wait(BOUND_S)
        if fleet.mode == "raises":
            raise ConnectionError("broker unreachable")
        if fleet.mode == "silent":
            return None
        return {name: {"pid": pid} for name, (pid, _) in self._snapshot.items()}

    def registered(self) -> object:
        fleet = self._fleet
        fleet.registered_calls += 1
        if fleet.mode == "raises":
            raise ConnectionError("broker unreachable")
        if fleet.mode == "silent":
            return None
        answer = {name: tasks for name, (_, tasks) in self._snapshot.items()}
        fleet.attempt_done.release()
        return answer

    def ping(self) -> object:
        fleet = self._fleet
        fleet.ping_calls += 1
        if fleet.mode != "up":
            fleet.attempt_done.release()
        if fleet.mode == "raises":
            raise ConnectionError("broker unreachable")
        return None


class FakeFleet:
    def __init__(self) -> None:
        self.nodes: dict[str, tuple[int, list[str]]] = {}
        self.mode = "up"
        self.clock = FakeClock()
        self.silent_until: dict[str, float] = {}
        self.stats_times: list[float] = []
        self.attempts = 0
        self.stats_calls = 0
        self.registered_calls = 0
        self.ping_calls = 0
        self.first_attempt_gate = threading.Event()
        self.first_attempt_gate.set()
        self.attempt_started = threading.Semaphore(0)
        self.attempt_done = threading.Semaphore(0)

    def block_first_attempt(self) -> None:
        self.first_attempt_gate.clear()

    def inspector(self, _url: str, **_: object) -> FakeInspect:
        self.attempts += 1
        return FakeInspect(self, self.attempts)


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeFleet]:
    fake = FakeFleet()
    clock = fake.clock
    monkeypatch.setattr(routes, "_get_inspector", fake.inspector)
    monkeypatch.setattr(routes, "_monotonic", clock.now)
    monkeypatch.setattr(inspect_probe, "_clock", clock.now)
    monkeypatch.setattr(inspect_probe, "_wait", clock.wait)
    yield fake
    fake.first_attempt_gate.set()


def extension_app() -> tuple[StemtraceExtension, FastAPI]:
    extension = StemtraceExtension(
        broker_url=BROKER, embedded_consumer=False, serve_ui=False
    )
    app = FastAPI()
    extension.init_app(app)
    return extension, app


def task_event(hostname: str) -> TaskEvent:
    return TaskEvent(
        task_id="t-1",
        name="app.t",
        state=TaskState.STARTED,
        timestamp=T,
        hostname=hostname,
    )


# --- P1: the server starts with a live fleet and nobody requests --------------


def test_p1_extension_start_fills_registered_tasks_without_any_request(
    fleet: FakeFleet,
) -> None:
    fleet.nodes = {NODE_A: (11, ["app.t"])}
    extension, app = extension_app()

    with TestClient(app):
        take(fleet.attempt_done)

    registry = extension.worker_registry
    assert registry.get_registered_tasks(NODE_A, 11) == ["app.t"]


def test_p1_create_router_without_a_consumer_probes_once_at_app_start(
    fleet: FakeFleet,
) -> None:
    fleet.nodes = {NODE_A: (11, ["app.t"])}
    registry = WorkerRegistry()
    app = FastAPI()
    app.include_router(create_router(worker_registry=registry, broker_url=BROKER))

    with TestClient(app):
        take(fleet.attempt_done)

    assert registry.get_registered_tasks(NODE_A, 11) == ["app.t"]


def test_p1_create_router_with_a_consumer_passed_in_probes_when_a_worker_joins(
    fleet: FakeFleet,
) -> None:
    registry = WorkerRegistry()
    consumer = AsyncEventConsumer("memory://", GraphStore(), worker_registry=registry)
    app = FastAPI()
    app.include_router(
        create_router(consumer=consumer, worker_registry=registry, broker_url=BROKER)
    )

    with TestClient(app):
        take(fleet.attempt_done)
        fleet.nodes = {NODE_B: (22, ["app.b"])}
        registry.register_worker(NODE_B, 22, [])
        take(fleet.attempt_done)

    assert registry.get_registered_tasks(NODE_B, 22) == ["app.b"]


def test_p1_without_a_broker_url_nothing_is_probed(fleet: FakeFleet) -> None:
    registry = WorkerRegistry()
    app = FastAPI()
    app.include_router(create_router(worker_registry=registry))

    with TestClient(app):
        registry.register_worker(NODE_A, 1, ["app.t"])

    assert fleet.attempts == 0


def test_p1_extension_without_a_consumer_still_probes_at_start(
    fleet: FakeFleet,
) -> None:
    extension, app = extension_app()

    assert extension.consumer is None
    with TestClient(app):
        take(fleet.attempt_done)

    assert fleet.attempts == 1


# --- P2: a worker joins ---------------------------------------------------------


def test_p2_worker_ready_triggers_a_refresh_that_fills_its_tasks(
    fleet: FakeFleet,
) -> None:
    extension, app = extension_app()
    registry = extension.worker_registry

    with TestClient(app):
        take(fleet.attempt_done)
        fleet.nodes = {NODE_B: (22, ["app.b", "app.b2"])}
        registry.register_worker(NODE_B, 22, ["app.b"])
        take(fleet.attempt_done)

    assert registry.get_registered_tasks(NODE_B, 22) == ["app.b", "app.b2"]
    assert fleet.attempts == 2


def test_p2_worker_ready_inside_a_running_refresh_gets_a_follow_up_refresh(
    fleet: FakeFleet,
) -> None:
    fleet.nodes = {NODE_A: (11, ["app.a"])}
    fleet.block_first_attempt()
    extension, app = extension_app()
    registry = extension.worker_registry

    with TestClient(app):
        take(fleet.attempt_started)
        # B's ready lands while the first inspect waits; the reply it will
        # get was produced before B existed.
        fleet.nodes[NODE_B] = (22, ["app.b", "app.late"])
        registry.register_worker(NODE_B, 22, ["app.b"])
        fleet.first_attempt_gate.set()
        take(fleet.attempt_done)
        take(fleet.attempt_done)

    assert registry.get_registered_tasks(NODE_B, 22) == ["app.b", "app.late"]
    assert fleet.attempts == 2


def test_p2_task_event_from_an_unseen_nodename_triggers_a_refresh(
    fleet: FakeFleet,
) -> None:
    extension, app = extension_app()
    consumer = EventConsumer(
        "memory://", extension.store, worker_registry=extension.worker_registry
    )

    with TestClient(app):
        take(fleet.attempt_done)
        fleet.nodes[NODE_C] = (33, ["app.c"])
        consumer._process_event(task_event(NODE_C))
        take(fleet.attempt_done)
        # The reply names NODE_C, which the registry hadn't registered: that is
        # one more follow-up (a stats reply naming a new node), and then none.
        take(fleet.attempt_done)
        assert not got(fleet.attempt_done, 0.5)

    assert fleet.attempts == 3


def test_p2_stats_reply_naming_a_new_node_gives_one_follow_up_and_no_more(
    fleet: FakeFleet,
) -> None:
    fleet.nodes = {NODE_A: (11, ["app.a"])}
    extension, app = extension_app()

    with TestClient(app):
        take(fleet.attempt_done)
        take(fleet.attempt_done)

    # Attempt 1's reply taught the registry NODE_A; that is one follow-up, and
    # the follow-up's reply names nothing new.
    assert fleet.attempts == 2
    assert extension.worker_registry.get_registered_tasks(NODE_A, 11) == ["app.a"]


# --- P3: a storm of joins -----------------------------------------------------


def test_p3_one_thousand_joins_in_a_second_give_at_most_two_refreshes(
    fleet: FakeFleet,
) -> None:
    fleet.block_first_attempt()
    extension, app = extension_app()
    registry = extension.worker_registry

    with TestClient(app):
        take(fleet.attempt_started)
        for number in range(1000):
            node = f"celery@n{number}"
            fleet.nodes[node] = (number + 1, ["app.x"])
            registry.register_worker(node, number + 1, ["app.x"])
        fleet.first_attempt_gate.set()
        take(fleet.attempt_done)
        take(fleet.attempt_done)
        assert not got(fleet.attempt_done, 0.5)

    assert fleet.attempts == 2


# --- P7, P8: a join is owed an answer ---------------------------------------


def got(semaphore: threading.Semaphore, timeout: float = 1.0) -> bool:
    return semaphore.acquire(timeout=timeout)


# Attempts start 0, 2, 6, 14, 30 s after the join (the first plus the 2, 4, 8, 16 s
# backoff steps; the 30 s step is the fifth extra attempt).
@pytest.mark.parametrize(
    ("answers_after_s", "attempts_needed"),
    [(0.0, 1), (0.4, 2), (0.6, 2), (3.0, 3), (20.0, 5)],
)
def test_p7_a_worker_that_answers_later_than_its_ready_is_online_without_a_request(
    fleet: FakeFleet, answers_after_s: float, attempts_needed: int
) -> None:
    extension, app = extension_app()
    registry = extension.worker_registry

    with TestClient(app):
        take(fleet.attempt_done)
        fleet.clock.wait(threading.Event(), 2.0)
        now = fleet.clock.now()
        fleet.nodes[NODE_B] = (22, ["app.b"])
        fleet.silent_until[NODE_B] = now + answers_after_s
        registry.register_worker(NODE_B, 22, ["app.b"])
        for _ in range(attempts_needed):
            take(fleet.attempt_done)

    worker = registry.get_worker(NODE_B, 22)
    assert worker is not None
    assert worker.status == WorkerStatus.ONLINE


def test_p8_a_worker_that_never_answers_gets_exactly_five_extra_attempts(
    fleet: FakeFleet,
) -> None:
    extension, app = extension_app()
    registry = extension.worker_registry

    with TestClient(app):
        take(fleet.attempt_done)
        fleet.clock.wait(threading.Event(), 2.0)
        before = fleet.attempts
        fleet.nodes[NODE_B] = (22, ["app.b"])
        fleet.silent_until[NODE_B] = float("inf")
        registry.register_worker(NODE_B, 22, ["app.b"])
        for _ in range(6):
            take(fleet.attempt_done)
        assert not got(fleet.attempt_done, 0.5)

    assert fleet.attempts - before == 6


# --- P9: one shared clock -------------------------------------------------------


def test_p9_a_new_nodename_right_after_a_page_load_is_deferred_by_the_interval(
    fleet: FakeFleet,
) -> None:
    extension, app = extension_app()
    registry = extension.worker_registry

    with TestClient(app) as client:
        take(fleet.attempt_done)
        fleet.clock.wait(threading.Event(), 2.0)
        client.get("/stemtrace/api/workers?refresh=true").raise_for_status()
        take(fleet.attempt_done)
        page_load_at = fleet.stats_times[-1]
        registry.register_worker(NODE_B, 22, ["app.b"])
        take(fleet.attempt_done)

    assert fleet.stats_times[-1] - page_load_at >= 2.0


def test_p9_a_page_load_right_after_a_probe_refresh_does_not_inspect_again(
    fleet: FakeFleet,
) -> None:
    _, app = extension_app()

    with TestClient(app) as client:
        take(fleet.attempt_done)
        client.get("/stemtrace/api/workers?refresh=true").raise_for_status()

    assert fleet.attempts == 1


# --- P4: the broker is down ---------------------------------------------------


@pytest.mark.parametrize("mode", ["raises", "silent"])
def test_p4_broker_down_makes_one_bounded_attempt_for_the_startup_trigger(
    fleet: FakeFleet, mode: str
) -> None:
    fleet.mode = mode
    _, app = extension_app()

    with TestClient(app):
        take(fleet.attempt_done)

    assert (
        fleet.attempts,
        fleet.stats_calls,
        fleet.registered_calls,
        fleet.ping_calls,
    ) == (1, 1, 1, 1)


def test_p4_broker_down_bounds_a_joins_attempts_to_one_plus_five(
    fleet: FakeFleet,
) -> None:
    fleet.mode = "raises"
    extension, app = extension_app()
    registry = extension.worker_registry

    with TestClient(app):
        take(fleet.attempt_done)
        fleet.clock.wait(threading.Event(), 2.0)
        registry.register_worker(NODE_A, 1, ["app.a"])
        for _ in range(6):
            take(fleet.attempt_done)
        assert not got(fleet.attempt_done, 0.5)

    assert fleet.attempts == 7
    assert registry.get_registered_tasks(NODE_A, 1) == ["app.a"]


def test_p4_a_failed_attempt_is_not_retried_without_a_trigger(
    fleet: FakeFleet,
) -> None:
    fleet.mode = "raises"
    _, app = extension_app()

    with TestClient(app):
        take(fleet.attempt_done)

    assert fleet.attempts == 1


def test_p4_a_blocked_inspect_does_not_block_event_processing(
    fleet: FakeFleet,
) -> None:
    fleet.mode = "silent"
    fleet.block_first_attempt()
    extension, app = extension_app()
    registry = extension.worker_registry

    with TestClient(app):
        take(fleet.attempt_started)
        began = time.monotonic()
        registry.register_worker(NODE_A, 1, ["app.a"])
        registry.mark_seen(NODE_A, T)
        elapsed = time.monotonic() - began
        fleet.first_attempt_gate.set()

    assert elapsed < 1.0


# --- P5: shutdown during a probe ------------------------------------------------


def test_p5_shutdown_during_a_blocked_probe_returns_within_the_stop_bound(
    fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inspect_probe, "INSPECT_PROBE_STOP_TIMEOUT_SECONDS", 0.2)
    fleet.block_first_attempt()
    _, app = extension_app()
    client = TestClient(app)
    client.__enter__()
    take(fleet.attempt_started)

    began = time.monotonic()
    client.__exit__(None, None, None)
    elapsed = time.monotonic() - began

    assert not fleet.first_attempt_gate.is_set()
    assert elapsed < 2.0


# --- the refresher the probe drives ---------------------------------------------


def test_refresh_blocking_returns_false_while_another_refresh_is_in_flight(
    fleet: FakeFleet,
) -> None:
    refresher = routes._InspectRefresher(WorkerRegistry(), BROKER)

    with refresher._lock:
        busy = refresher.refresh_blocking()

    assert busy is None
    assert fleet.attempts == 0
    assert refresher.refresh_blocking() == frozenset()
    assert fleet.attempts == 1


def test_refresh_blocking_returns_the_nodenames_that_answered(
    fleet: FakeFleet,
) -> None:
    fleet.nodes = {NODE_A: (11, ["app.a"]), NODE_B: (22, ["app.b"])}
    refresher = routes._InspectRefresher(WorkerRegistry(), BROKER)

    assert refresher.refresh_blocking() == frozenset({NODE_A, NODE_B})


def test_refresh_blocking_returns_the_nodenames_a_ping_named(
    fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PingOnly:
        def stats(self) -> object:
            return None

        def registered(self) -> object:
            return None

        def ping(self) -> object:
            return {NODE_A: {"ok": "pong"}}

    monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: PingOnly())
    refresher = routes._InspectRefresher(WorkerRegistry(), BROKER)

    assert refresher.refresh_blocking() == frozenset({NODE_A})


def test_ready_in_is_zero_before_any_refresh_and_counts_down_after_one(
    fleet: FakeFleet,
) -> None:
    refresher = routes._InspectRefresher(WorkerRegistry(), BROKER)
    assert refresher.ready_in() == 0.0

    refresher.refresh_blocking()
    assert refresher.ready_in() == pytest.approx(2.0)

    fleet.clock.wait(threading.Event(), 0.5)
    assert refresher.ready_in() == pytest.approx(1.5)

    fleet.clock.wait(threading.Event(), 5.0)
    assert refresher.ready_in() == 0.0


def test_refresh_blocking_without_a_registry_or_broker_does_nothing(
    fleet: FakeFleet,
) -> None:
    assert routes._InspectRefresher(None, BROKER).refresh_blocking() == frozenset()
    registry_only = routes._InspectRefresher(WorkerRegistry(), None)
    assert registry_only.refresh_blocking() == frozenset()

    assert fleet.attempts == 0


async def test_a_probe_refresh_counts_toward_the_request_rate_limit(
    fleet: FakeFleet,
) -> None:
    refresher = routes._InspectRefresher(WorkerRegistry(), BROKER)
    refresher.refresh_blocking()

    await refresher.maybe_refresh()

    assert fleet.attempts == 1


def test_refresh_blocking_that_fails_does_not_count_as_a_refresh(
    fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: object) -> None:
        raise RuntimeError("registry refused")

    monkeypatch.setattr(routes, "_refresh_worker_registry_from_inspect", explode)
    refresher = routes._InspectRefresher(WorkerRegistry(), BROKER)

    assert refresher.refresh_blocking() == frozenset()
    assert refresher._last_refresh == 0.0


# --- one refresher per extension --------------------------------------------------


def test_extension_router_built_twice_shares_one_refresher_and_one_rate_limit(
    fleet: FakeFleet,
) -> None:
    extension = StemtraceExtension(
        broker_url=BROKER, embedded_consumer=False, serve_ui=False
    )
    _ = extension.router
    first = extension._inspect_probe.target
    app = FastAPI()
    extension.init_app(app)

    assert first is not None
    assert extension._inspect_probe.target is first
    with TestClient(app) as client:
        take(fleet.attempt_done)
        client.get("/stemtrace/api/workers?refresh=true").raise_for_status()

    # The startup probe's refresh counts for the second router's page load.
    assert fleet.attempts == 1
