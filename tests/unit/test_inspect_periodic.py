"""The probe's periodic refresh (D7b): a refresh every period while a worker is live.

Same stepping protocol as ``test_inspect_probe_schedule``: each action is
followed by one ``next_block()`` and nothing sleeps. The period is the target's
(``authority_window / 2`` in production), so these tests pick their own.
"""

import threading
import time
from collections.abc import Iterator

import pytest

from stemtrace.server.inspect_probe import InspectProbe, Periodic
from tests.unit.test_inspect_probe import FakeClock
from tests.unit.test_inspect_probe_schedule import (
    BOUND_S,
    DUES,
    INTERVAL_S,
    Recorder,
    Stepper,
    assert_asked_at_each_due,
)

PERIOD_S = 300.0


class Live:
    """A switch for whether the registry has a live worker."""

    def __init__(self, live: bool = True) -> None:
        self.live = live

    def __call__(self) -> bool:
        return self.live


@pytest.fixture
def probes() -> Iterator[list[InspectProbe]]:
    made: list[InspectProbe] = []
    yield made
    for probe in made:
        probe.stop(timeout=BOUND_S)


def build(
    probes: list[InspectProbe],
    *,
    period_s: float = PERIOD_S,
    live: Live | None = None,
    recorder: Recorder | None = None,
    clock: FakeClock | None = None,
) -> tuple[InspectProbe, Stepper, Recorder, FakeClock, Live]:
    clock = clock or FakeClock()
    stepper = Stepper(clock)
    recorder = recorder or Recorder(clock)
    live = live or Live()
    probe = InspectProbe(INTERVAL_S, clock=clock.now, wait=stepper.wait)
    probe.bind(recorder, Periodic(period_s, live))
    stepper.probe = probe
    probes.append(probe)
    return probe, stepper, recorder, clock, live


def run_periods(stepper: Stepper, block: float | None, periods: int) -> float | None:
    """Expire ``periods`` timers in a row; the wait the probe ends up in."""
    for _ in range(periods):
        stepper.expire()
        block = stepper.next_block()
    return block


def test_p11_a_live_worker_gets_a_refresh_every_period_without_any_trigger(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, _ = build(probes)
    began = clock.now()

    probe.start()
    block = stepper.next_block()
    assert block == PERIOD_S
    run_periods(stepper, block, 3)

    assert [s - began for s in recorder.starts] == [0.0, 300.0, 600.0, 900.0]


@pytest.mark.parametrize("period_s", [7.0, 120.0, 600.0])
def test_the_period_is_the_one_bound_not_a_fixed_number(
    probes: list[InspectProbe], period_s: float
) -> None:
    probe, stepper, recorder, clock, _ = build(probes, period_s=period_s)
    began = clock.now()

    probe.start()
    run_periods(stepper, stepper.next_block(), 2)

    assert [s - began for s in recorder.starts] == [0.0, period_s, 2 * period_s]


def test_no_periodic_refresh_runs_while_no_worker_is_live(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, _ = build(probes, live=Live(False))
    began = clock.now()

    probe.start()
    block = run_periods(stepper, stepper.next_block(), 4)

    # Only the startup attempt ran; the probe still wakes each period to look.
    assert [s - began for s in recorder.starts] == [0.0]
    assert block == PERIOD_S


def test_a_worker_that_goes_live_gets_the_next_periodic_refresh(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, live = build(probes, live=Live(False))
    began = clock.now()

    probe.start()
    block = run_periods(stepper, stepper.next_block(), 2)
    live.live = True
    run_periods(stepper, block, 1)

    assert [s - began for s in recorder.starts] == [0.0, 3 * PERIOD_S]


class BackingOffAfterFirst(Recorder):
    """Reports a failure backoff (D7a) from the first attempt on."""

    def __init__(self, clock: FakeClock, backoff_s: float) -> None:
        super().__init__(clock)
        self._backoff_s = backoff_s

    def ready_in(self) -> float:
        return self._backoff_s if self.starts else 0.0


def test_a_periodic_refresh_waits_for_the_failure_backoff(
    probes: list[InspectProbe],
) -> None:
    clock = FakeClock()
    backing_off = BackingOffAfterFirst(clock, backoff_s=400.0)
    probe, stepper, _, _, _ = build(probes, recorder=backing_off, clock=clock)
    began = clock.now()

    probe.start()
    block = stepper.next_block()
    assert block == 400.0  # the period is 300 s; the backoff is longer
    stepper.expire()
    stepper.next_block()

    assert [s - began for s in backing_off.starts] == [0.0, 400.0]


def test_a_periodic_refresh_waits_out_the_shared_interval(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, _ = build(probes, period_s=1.0)
    began = clock.now()

    probe.start()
    run_periods(stepper, stepper.next_block(), 2)

    # A period shorter than the interval is held to the interval.
    assert [s - began for s in recorder.starts] == [0.0, INTERVAL_S, 2 * INTERVAL_S]


def test_a_join_keeps_every_owed_time_and_the_period_restarts_from_its_attempts(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, _ = build(probes)
    began = clock.now()
    probe.start()
    stepper.next_block()
    clock.advance(100.0)
    joined = clock.now()

    probe.trigger("celery@a")
    block = stepper.next_block()
    while block != PERIOD_S:
        stepper.expire()
        block = stepper.next_block()
    stepper.expire()
    stepper.next_block()

    assert_asked_at_each_due(recorder.starts, joined)
    assert [s - joined for s in recorder.starts[2:7]] == pytest.approx(list(DUES))
    # The period counts from the join's last owed attempt, not from the start.
    assert recorder.starts[-1] - recorder.starts[-2] == PERIOD_S
    assert recorder.starts[1] - began == pytest.approx(100.0)


def test_a_trigger_storm_during_the_period_wait_still_coalesces_to_one_follow_up(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, _, _ = build(probes)
    probe.start()
    stepper.next_block()

    for number in range(1000):
        probe.trigger(f"celery@n{number}")
    block = stepper.next_block()
    assert block == INTERVAL_S
    stepper.expire()
    stepper.next_block()

    assert len(recorder.starts) == 2


class BlockingSecond(Recorder):
    """Blocks inside its second attempt (the periodic one) until released."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.gate = threading.Event()
        self.entered = threading.Semaphore(0)

    def refresh_blocking(self) -> frozenset[str] | None:
        result = super().refresh_blocking()
        if len(self.starts) == 2:
            self.entered.release()
            self.gate.wait(BOUND_S)
        return result


def test_stop_during_a_periodic_refresh_returns_within_its_bound(
    probes: list[InspectProbe],
) -> None:
    clock = FakeClock()
    blocking = BlockingSecond(clock)
    probe, stepper, _, _, _ = build(probes, recorder=blocking, clock=clock)

    probe.start()
    stepper.next_block()
    stepper.expire()  # the periodic refresh starts and blocks in the target
    assert blocking.entered.acquire(timeout=BOUND_S)

    began = time.monotonic()
    probe.stop(timeout=0.3)
    elapsed = time.monotonic() - began
    blocking.gate.set()

    assert elapsed < 2.0
