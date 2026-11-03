"""The inspect probe scheduler (D4c): trigger, coalesce, pace, stop.

The probe runs ``refresh`` on its own thread. The clock and the wait are
injected, so pacing is asserted on a fake clock and nothing sleeps. Every
blocking wait here has a bound, so a regression fails instead of hanging.
"""

import inspect
import threading
import time
from collections.abc import Iterator

import pytest

from stemtrace.server.consumer import EventConsumer
from stemtrace.server.inspect_probe import (
    INSPECT_PROBE_STOP_TIMEOUT_SECONDS,
    InspectProbe,
)

INTERVAL_S = 2.0
BOUND_S = 5.0


def take(semaphore: threading.Semaphore) -> None:
    """Wait for one release, failing the test if it never comes."""
    assert semaphore.acquire(timeout=BOUND_S), "probe did not reach this point"


class FakeClock:
    """A clock that only the fake wait moves, so pacing is exact."""

    def __init__(self) -> None:
        self._now = 100.0
        self._lock = threading.Lock()
        self.waits: list[float] = []

    def now(self) -> float:
        with self._lock:
            return self._now

    def wait(self, stop: threading.Event, seconds: float) -> bool:
        with self._lock:
            self.waits.append(seconds)
            self._now += seconds
        return stop.is_set()


class FakeRefresh:
    """A refresh that records when and where it ran, and can block until released."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        block_first: bool = False,
        results: list[bool] | None = None,
        raises_first: bool = False,
    ) -> None:
        self._clock = clock
        self._results = results or []
        self._raises_first = raises_first
        self.gate = threading.Event()
        if not block_first:
            self.gate.set()
        self.started = threading.Semaphore(0)
        self.finished = threading.Semaphore(0)
        self.starts: list[float] = []
        self.thread_names: list[str] = []

    def __call__(self) -> bool:
        call = len(self.starts)
        self.starts.append(self._clock.now())
        self.thread_names.append(threading.current_thread().name)
        self.started.release()
        if call == 0:
            self.gate.wait(BOUND_S)
        self.finished.release()
        if call == 0 and self._raises_first:
            raise RuntimeError("inspect exploded")
        return self._results[call] if call < len(self._results) else True


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def probes() -> Iterator[list[InspectProbe]]:
    made: list[InspectProbe] = []
    yield made
    for probe in made:
        probe.stop(timeout=BOUND_S)


def make_probe(
    probes: list[InspectProbe], clock: FakeClock, refresh: FakeRefresh
) -> InspectProbe:
    probe = InspectProbe(INTERVAL_S, clock=clock.now, wait=clock.wait)
    probe.bind(refresh)
    probes.append(probe)
    return probe


def test_trigger_runs_the_refresh_on_the_probe_thread_not_the_caller(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    probe = make_probe(probes, clock, refresh)

    probe.trigger()
    take(refresh.finished)

    assert refresh.thread_names == ["stemtrace-inspect-probe"]


def test_trigger_returns_while_the_refresh_is_still_blocked(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock, block_first=True)
    probe = make_probe(probes, clock, refresh)

    probe.trigger()
    take(refresh.started)

    # The refresh is parked on its gate and trigger() has already returned.
    assert not refresh.gate.is_set()
    probe.trigger()
    refresh.gate.set()


def test_trigger_during_a_running_refresh_gives_exactly_one_follow_up(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock, block_first=True)
    probe = make_probe(probes, clock, refresh)
    probe.trigger()
    take(refresh.started)

    for _ in range(3):
        probe.trigger()
    refresh.gate.set()
    take(refresh.finished)
    take(refresh.started)
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    assert len(refresh.starts) == 2
    assert refresh.starts[1] - refresh.starts[0] >= INTERVAL_S


def test_trigger_storm_of_1000_gives_one_running_refresh_and_one_follow_up(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock, block_first=True)
    probe = make_probe(probes, clock, refresh)
    probe.trigger()
    take(refresh.started)

    for _ in range(1000):
        probe.trigger()
    refresh.gate.set()
    take(refresh.finished)
    take(refresh.started)
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    assert len(refresh.starts) == 2


def test_two_triggers_are_at_least_one_interval_apart(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    probe = make_probe(probes, clock, refresh)

    probe.trigger()
    take(refresh.finished)
    probe.trigger()
    take(refresh.finished)

    assert refresh.starts[1] - refresh.starts[0] >= INTERVAL_S
    assert clock.waits == [pytest.approx(INTERVAL_S)]


def test_trigger_after_the_interval_has_passed_runs_without_waiting(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    probe = make_probe(probes, clock, refresh)
    probe.trigger()
    take(refresh.finished)

    clock.wait(threading.Event(), INTERVAL_S + 1)
    clock.waits.clear()
    probe.trigger()
    take(refresh.finished)

    assert clock.waits == []


def test_refresh_that_found_another_in_flight_is_retried_after_the_interval(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock, results=[False, True])
    probe = make_probe(probes, clock, refresh)

    probe.trigger()
    take(refresh.finished)
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    assert len(refresh.starts) == 2
    assert refresh.starts[1] - refresh.starts[0] >= INTERVAL_S


def test_refresh_that_raises_is_swallowed_and_the_next_trigger_still_runs(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock, raises_first=True)
    probe = make_probe(probes, clock, refresh)

    probe.trigger()
    take(refresh.finished)
    probe.trigger()
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    assert len(refresh.starts) == 2


def test_start_runs_one_refresh_and_does_not_repeat_on_its_own(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    probe = make_probe(probes, clock, refresh)

    probe.start()
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    assert len(refresh.starts) == 1


def test_start_twice_gives_one_refresh_for_one_startup(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock, block_first=True)
    probe = make_probe(probes, clock, refresh)

    probe.start()
    take(refresh.started)
    probe.start()
    refresh.gate.set()
    take(refresh.finished)
    take(refresh.started)
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    # The second start() coalesced into the follow-up, which is one refresh.
    assert len(refresh.starts) == 2


def test_trigger_and_start_without_a_bound_refresh_do_nothing() -> None:
    probe = InspectProbe(INTERVAL_S)

    probe.start()
    probe.trigger()
    probe.stop(timeout=BOUND_S)

    assert not [t for t in threading.enumerate() if t.name == "stemtrace-inspect-probe"]


def test_trigger_after_stop_does_not_run_the_refresh(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    probe = make_probe(probes, clock, refresh)
    probe.start()
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    probe.trigger()
    probe.stop(timeout=BOUND_S)

    assert len(refresh.starts) == 1


def test_stop_during_a_blocked_refresh_returns_within_its_timeout(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock, block_first=True)
    probe = make_probe(probes, clock, refresh)
    probe.trigger()
    take(refresh.started)

    began = time.monotonic()
    probe.stop(timeout=0.2)
    elapsed = time.monotonic() - began

    assert elapsed < 2.0
    assert not refresh.gate.is_set()
    refresh.gate.set()


def test_stop_while_waiting_for_the_interval_does_not_wait_it_out(
    probes: list[InspectProbe],
) -> None:
    refresh = FakeRefresh(FakeClock())
    probe = InspectProbe(3600.0)
    probe.bind(refresh)
    probes.append(probe)
    probe.trigger()
    take(refresh.finished)
    probe.trigger()

    began = time.monotonic()
    probe.stop(timeout=BOUND_S)

    assert time.monotonic() - began < 2.0
    assert len(refresh.starts) == 1


def test_stop_timeout_is_below_the_consumers_stop_timeout() -> None:
    consumer_default = (
        inspect.signature(EventConsumer.stop).parameters["timeout"].default
    )

    assert consumer_default > INSPECT_PROBE_STOP_TIMEOUT_SECONDS
