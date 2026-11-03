"""Per-node schedules of the inspect probe (D4c round 2, rows P8 and P10).

Every point where the probe thread blocks goes through the injected wait, so a
``Stepper`` can walk the thread through its waits one at a time on a fake
clock: no test sleeps, and every blocking call has a bound. The protocol: each
action (a trigger, an expiry) is followed by one ``next_block()``, which returns
once the probe is blocked again.
"""

import queue
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from stemtrace.server.inspect_probe import (
    PENDING_BACKOFF_SECONDS,
    InspectProbe,
)
from tests.unit.test_inspect_probe import FakeClock

INTERVAL_S = 2.0
BOUND_S = 5.0
# Owed times from the join: 2, 6, 14, 30 and 60 s.
DUES = tuple(sum(PENDING_BACKOFF_SECONDS[: i + 1]) for i in range(5))


class Stepper:
    """Reports each wait the probe enters and lets the test end it or wake it."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.probe: InspectProbe | None = None
        self.owed = False
        self._blocked: queue.Queue[tuple[float | None, bool]] = queue.Queue()
        self._expire = threading.Event()

    def wait(self, event: threading.Event, seconds: float | None) -> bool:
        # A set wake means a trigger is already owed: the wait is for the interval
        # and only stop() ends it early; otherwise a new trigger wakes it.
        owed = self.probe is not None and self.probe._wake.is_set()
        self._blocked.put((seconds, owed))
        deadline = time.monotonic() + BOUND_S
        while time.monotonic() < deadline:
            if event.wait(0.002):
                return True
            if seconds is not None and self._expire.is_set():
                self._expire.clear()
                self.clock.advance(seconds)
                return False
        raise AssertionError("the stepper wait was never released")

    def next_block(self) -> float | None:
        """Seconds of the wait the probe is now blocked in (None: idle, no timer)."""
        seconds, self.owed = self._blocked.get(timeout=BOUND_S)
        return seconds

    def expire(self) -> None:
        """End the wait the probe is in, as if its time had passed."""
        self._expire.set()


class Recorder:
    """A target that records every attempt and answers per the fake cluster."""

    def __init__(
        self,
        clock: FakeClock,
        answers: Callable[[float], frozenset[str]] = lambda _now: frozenset(),
    ) -> None:
        self._clock = clock
        self._answers = answers
        self.ready_in_s = 0.0
        self.starts: list[float] = []
        self.named: list[tuple[float, frozenset[str]]] = []

    def refresh_blocking(self) -> frozenset[str] | None:
        now = self._clock.now()
        self.starts.append(now)
        names = self._answers(now)
        self.named.append((now, names))
        return names

    def ready_in(self) -> float:
        return self.ready_in_s


@pytest.fixture
def probes() -> Iterator[list[InspectProbe]]:
    made: list[InspectProbe] = []
    yield made
    for probe in made:
        probe.stop(timeout=BOUND_S)


def build(
    probes: list[InspectProbe],
    answers: Callable[[float], frozenset[str]] | None = None,
) -> tuple[InspectProbe, Stepper, Recorder, FakeClock]:
    clock = FakeClock()
    stepper = Stepper(clock)
    recorder = Recorder(clock, answers or (lambda _now: frozenset()))
    probe = InspectProbe(INTERVAL_S, clock=clock.now, wait=stepper.wait)
    probe.bind(recorder)
    stepper.probe = probe
    probes.append(probe)
    return probe, stepper, recorder, clock


def finish(stepper: Stepper, block: float | None) -> None:
    """Let every owed timer expire until the probe is idle."""
    while block is not None:
        stepper.expire()
        block = stepper.next_block()


def assert_asked_at_each_due(
    starts: list[float], joined: float, *, after: float = 0.0
) -> None:
    """Some attempt began at or after each owed time (within one interval)."""
    for due in DUES:
        if joined + due > after:
            assert any(0 <= s - (joined + due) <= INTERVAL_S for s in starts), due


def test_schedule_is_two_six_fourteen_thirty_sixty_seconds_from_the_join() -> None:
    assert DUES == (2.0, 6.0, 14.0, 30.0, 60.0)


def test_p8_a_nodename_that_never_answers_gets_exactly_five_owed_attempts(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock = build(probes)
    joined = clock.now()

    probe.trigger("celery@a")
    finish(stepper, stepper.next_block())

    assert [s - joined for s in recorder.starts] == pytest.approx([0.0, *DUES])


@pytest.mark.parametrize("fraction", [0.0, 0.3, 0.9])
@pytest.mark.parametrize("step", [1, 2, 3, 4, 5])
def test_p10_a_join_during_another_nodes_backoff_is_inspected_within_one_interval(
    probes: list[InspectProbe], step: int, fraction: float
) -> None:
    probe, stepper, recorder, clock = build(probes)
    joined = clock.now()
    probe.trigger("celery@a")
    block = stepper.next_block()
    for _ in range(step - 1):
        stepper.expire()
        block = stepper.next_block()
    assert block is not None
    clock.advance(fraction * block)
    b_joined = clock.now()

    probe.trigger("celery@b")
    block = stepper.next_block()
    while not any(s >= b_joined for s in recorder.starts):
        stepper.expire()
        block = stepper.next_block()
    b_first = min(s for s in recorder.starts if s >= b_joined)
    finish(stepper, block)

    assert b_first - b_joined <= INTERVAL_S
    assert_asked_at_each_due(recorder.starts, joined)
    assert_asked_at_each_due(recorder.starts, b_joined)
    assert not probe._pending


def test_p10_a_join_while_the_probe_sleeps_is_not_deferred_to_the_other_nodes_due_time(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock = build(probes)
    probe.trigger("celery@a")
    block = stepper.next_block()
    for _ in range(3):
        stepper.expire()
        block = stepper.next_block()
    # A is 16 s from its fourth owed attempt when B joins.
    assert block == pytest.approx(DUES[3] - DUES[2])
    b_joined = clock.now()

    probe.trigger("celery@b")
    stepper.next_block()
    stepper.expire()
    stepper.next_block()

    assert recorder.starts[-1] - b_joined <= INTERVAL_S


def test_other_attempts_do_not_consume_a_nodes_schedule(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock = build(probes)
    joined = clock.now()
    probe.trigger("celery@a")
    block = stepper.next_block()
    # Ten attempts for other nodes inside A's first 2 s of waiting.
    for number in range(10):
        clock.advance(0.1)
        probe.trigger(f"celery@x{number}")
        stepper.next_block()
        stepper.expire()
        block = stepper.next_block()
    finish(stepper, block)

    assert_asked_at_each_due(recorder.starts, joined)


def test_schedule_counts_from_the_join_not_from_the_first_attempt(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock = build(probes)
    joined = clock.now()
    # The shared clock defers the first attempt by 10 s.
    recorder.ready_in_s = 10.0

    probe.trigger("celery@a")
    stepper.next_block()
    recorder.ready_in_s = 0.0
    stepper.expire()
    finish(stepper, stepper.next_block())

    assert recorder.starts[0] - joined == pytest.approx(10.0)
    # Owed times 2 and 6 passed inside that deferral; 14, 30 and 60 still count
    # from the join, so the last attempt is not pushed past the 60 s one.
    assert recorder.starts[-1] - joined <= DUES[-1] + INTERVAL_S
    assert_asked_at_each_due(recorder.starts, joined, after=10.0)


@pytest.mark.parametrize(
    ("slow_answers_after_s", "bound_s"), [(15.0, 19.0), (55.0, 62.0)]
)
def test_rolling_deploy_a_slow_worker_is_asked_after_it_can_answer(
    probes: list[InspectProbe], slow_answers_after_s: float, bound_s: float
) -> None:
    joins = [(2.0 * i, f"celery@n{i}") for i in range(10)]
    slow = "celery@n1"
    join_at: dict[str, float] = {}

    def answers(now: float) -> frozenset[str]:
        return frozenset(
            name
            for name, at in join_at.items()
            if now >= at + (slow_answers_after_s if name == slow else 0.0)
        )

    probe, stepper, recorder, clock = build(probes, answers)
    base = clock.now()
    index = 0
    block: float | None = None
    started = False
    while True:
        now = clock.now()
        next_join = base + joins[index][0] if index < len(joins) else None
        if next_join is not None and now >= next_join:
            name = joins[index][1]
            join_at[name] = now
            index += 1
            probe.trigger(name)
            started = True
            block = stepper.next_block()
        elif not started:
            clock.advance(base + joins[0][0] - now)
        elif block is None:
            if next_join is None:
                break
            clock.advance(next_join - now)
        elif not stepper.owed and next_join is not None and next_join < now + block:
            clock.advance(next_join - now)
        else:
            stepper.expire()
            block = stepper.next_block()

    named_at = next(at for at, names in recorder.named if slow in names)
    assert slow_answers_after_s <= named_at - join_at[slow] <= bound_s
    assert not probe._pending
