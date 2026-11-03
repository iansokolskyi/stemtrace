"""The inspect probe scheduler (D4c): trigger, coalesce, pace, stop.

The probe runs ``refresh`` on its own thread. The clock and the wait are
injected, so pacing is asserted on a fake clock and nothing sleeps. Every
blocking wait here has a bound, so a regression fails instead of hanging.
"""

import inspect
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from stemtrace.server import inspect_probe
from stemtrace.server.consumer import EventConsumer
from stemtrace.server.inspect_probe import (
    INSPECT_PROBE_STOP_TIMEOUT_SECONDS,
    PENDING_BACKOFF_SECONDS,
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

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def wait(self, event: threading.Event, seconds: float | None) -> bool:
        if seconds is None:
            return event.wait(BOUND_S)
        if event.is_set():
            return True
        with self._lock:
            self.waits.append(seconds)
            self._now += seconds
        return False


class FakeRefresh:
    """A refresh that records when and where it ran, and can block until released."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        block_first: bool = False,
        results: list[bool] | None = None,
        raises_first: bool = False,
        answers: Callable[[int], frozenset[str]] | None = None,
    ) -> None:
        self._clock = clock
        self._answers = answers or (lambda _call: frozenset())
        self.ready_in_s = 0.0
        self._results = results or []
        self._raises_first = raises_first
        self.gate = threading.Event()
        if not block_first:
            self.gate.set()
        self.started = threading.Semaphore(0)
        self.finished = threading.Semaphore(0)
        self.starts: list[float] = []
        self.thread_names: list[str] = []

    def ready_in(self) -> float:
        return self.ready_in_s

    def refresh_blocking(self) -> frozenset[str] | None:
        call = len(self.starts)
        self.starts.append(self._clock.now())
        self.thread_names.append(threading.current_thread().name)
        self.started.release()
        if call == 0:
            self.gate.wait(BOUND_S)
        self.finished.release()
        if call == 0 and self._raises_first:
            raise RuntimeError("inspect exploded")
        if call < len(self._results) and not self._results[call]:
            return None
        return self._answers(call)


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
    before = set(threading.enumerate())

    probe.start()
    probe.trigger()
    probe.stop(timeout=BOUND_S)

    # Compare with the threads before: other tests' probes are not this probe's doing.
    assert not [
        t
        for t in set(threading.enumerate()) - before
        if t.name == "stemtrace-inspect-probe"
    ]


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
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    waiting = threading.Semaphore(0)

    def wait_for_stop(stop: threading.Event, seconds: float | None) -> bool:
        waiting.release()
        return stop.wait(seconds)

    probe = InspectProbe(3600.0, clock=clock.now, wait=wait_for_stop)
    probe.bind(refresh)
    probes.append(probe)
    probe.trigger()
    take(refresh.finished)
    probe.trigger()
    take(waiting)

    began = time.monotonic()
    probe.stop(timeout=BOUND_S)

    assert time.monotonic() - began < 2.0
    assert len(refresh.starts) == 1


def test_start_after_stop_runs_the_refresh_again(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    probe = make_probe(probes, clock, refresh)
    probe.start()
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    probe.start()
    take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    assert len(refresh.starts) == 2


def test_default_wait_returns_true_at_once_when_stopped_and_false_after_the_wait() -> (
    None
):
    stopped = threading.Event()
    stopped.set()

    assert inspect_probe._wait(stopped, 3600.0) is True
    assert inspect_probe._wait(threading.Event(), 0.0) is False


def test_default_clock_is_monotonic() -> None:
    first = inspect_probe._clock()

    assert inspect_probe._clock() >= first


def test_trigger_with_a_nodename_no_reply_names_owes_attempts_on_the_backoff(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    probe = make_probe(probes, clock, refresh)

    probe.trigger("celery@b")
    for _ in range(6):
        take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    # The first attempt, then five more at 2, 4, 8, 16 and 30 s; then none.
    assert len(refresh.starts) == 6
    assert clock.waits == pytest.approx(list(PENDING_BACKOFF_SECONDS))


def test_nodename_that_answers_on_the_third_attempt_stops_the_backoff(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(
        clock,
        answers=lambda call: frozenset({"celery@b"}) if call >= 2 else frozenset(),
    )
    probe = make_probe(probes, clock, refresh)

    probe.trigger("celery@b")
    for _ in range(3):
        take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    assert len(refresh.starts) == 3
    assert clock.waits == pytest.approx([2.0, 4.0])


def test_nodename_that_joined_during_an_attempt_keeps_a_schedule_of_its_own(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock, block_first=True)
    probe = make_probe(probes, clock, refresh)
    probe.trigger("celery@a")
    take(refresh.started)
    clock.advance(1.0)
    probe.trigger("celery@late")
    refresh.gate.set()
    # a is owed 102, 106, 114, 130 and 160; late (joined at 101) 103, 107, 115,
    # 131 and 161; the shared 2 s interval makes the starts 100, 102, 104, 106,
    # 108, 114, 116, 130, 132, 160 and 162.
    for _ in range(11):
        take(refresh.finished)
    probe.stop(timeout=BOUND_S)

    assert refresh.starts[-1] == pytest.approx(162.0)


def test_pending_nodenames_are_bounded_and_the_oldest_is_dropped(
    clock: FakeClock, probes: list[InspectProbe], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inspect_probe, "PENDING_NODES_MAX", 2)
    refresh = FakeRefresh(clock, block_first=True)
    probe = make_probe(probes, clock, refresh)
    probe.trigger("celery@1")
    take(refresh.started)
    for name in ("celery@2", "celery@3"):
        probe.trigger(name)

    assert list(probe._pending) == ["celery@2", "celery@3"]
    refresh.gate.set()


def test_attempt_waits_for_the_target_to_be_ready(
    clock: FakeClock, probes: list[InspectProbe]
) -> None:
    refresh = FakeRefresh(clock)
    refresh.ready_in_s = 7.0
    probe = make_probe(probes, clock, refresh)

    probe.trigger()
    take(refresh.finished)

    assert clock.waits == pytest.approx([7.0])


def test_target_is_none_until_bound(probes: list[InspectProbe]) -> None:
    probe = InspectProbe(INTERVAL_S)
    assert probe.target is None
    refresh = FakeRefresh(FakeClock())

    probe.bind(refresh)

    assert probe.target is refresh


def test_stop_timeout_is_below_the_consumers_stop_timeout() -> None:
    consumer_default = (
        inspect.signature(EventConsumer.stop).parameters["timeout"].default
    )

    assert consumer_default > INSPECT_PROBE_STOP_TIMEOUT_SECONDS
