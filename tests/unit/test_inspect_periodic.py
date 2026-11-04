"""The probe's periodic refresh (D7b): a refresh whenever authority is due.

Same stepping protocol as ``test_inspect_probe_schedule``: each action is
followed by one ``next_block()`` and nothing sleeps. ``Authority`` stands for the
registry's stamp: an attempt that stamps makes the next refresh due one period
later (reading 1), and one that doesn't leaves it due.
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


class Authority:
    """The stamp's age as the probe's target reports it."""

    def __init__(self, clock: FakeClock, period_s: float = PERIOD_S) -> None:
        self.clock = clock
        self.period_s = period_s
        self.due_at: float | None = None  # None: never stamped, due now
        self.online = True
        self.errored = False
        self.stamps = True

    def remaining(self) -> float | None:
        if not (self.online or self.errored):
            return None
        return 0.0 if self.due_at is None else self.due_at - self.clock.now()

    def stamp(self) -> None:
        self.due_at = self.clock.now() + self.period_s


class StampingRecorder(Recorder):
    """A target whose attempts stamp authority while ``authority.stamps`` is true."""

    def __init__(self, clock: FakeClock, authority: Authority) -> None:
        super().__init__(clock)
        self._authority = authority

    def refresh_blocking(self) -> frozenset[str] | None:
        result = super().refresh_blocking()
        if self._authority.stamps:
            self._authority.stamp()
        return result


@pytest.fixture
def probes() -> Iterator[list[InspectProbe]]:
    made: list[InspectProbe] = []
    yield made
    for probe in made:
        probe.stop(timeout=BOUND_S)


def build(
    probes: list[InspectProbe],
    *,
    recorder: Recorder | None = None,
    clock: FakeClock | None = None,
    authority: Authority | None = None,
) -> tuple[InspectProbe, Stepper, Recorder, FakeClock, Authority]:
    clock = clock or FakeClock()
    authority = authority or Authority(clock)
    stepper = Stepper(clock)
    recorder = recorder or StampingRecorder(clock, authority)
    probe = InspectProbe(INTERVAL_S, clock=clock.now, wait=stepper.wait)
    probe.bind(recorder, Periodic(authority.remaining))
    stepper.probe = probe
    probes.append(probe)
    return probe, stepper, recorder, clock, authority


def run_periods(stepper: Stepper, block: float | None, periods: int) -> float | None:
    """Expire ``periods`` timers in a row; the wait the probe ends up in."""
    for _ in range(periods):
        stepper.expire()
        block = stepper.next_block()
    return block


def test_p11_authority_due_refreshes_every_period_without_any_trigger(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, _ = build(probes)
    began = clock.now()

    probe.start()
    block = stepper.next_block()
    assert block == PERIOD_S
    run_periods(stepper, block, 3)

    assert [s - began for s in recorder.starts] == [0.0, 300.0, 600.0, 900.0]


def test_a_stamp_from_a_request_pushes_the_next_periodic_refresh_out(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, authority = build(probes)
    began = clock.now()
    probe.start()
    assert stepper.next_block() == PERIOD_S

    # A request stamps authority while the probe sleeps: due 120 s after the wait ends.
    authority.due_at = clock.now() + PERIOD_S + 120.0
    stepper.expire()
    block = stepper.next_block()

    assert block == 120.0
    stepper.expire()
    stepper.next_block()
    assert [s - began for s in recorder.starts] == [0.0, PERIOD_S + 120.0]


def test_an_attempt_that_does_not_stamp_leaves_the_period_owed(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, authority = build(probes)
    authority.stamps = False
    began = clock.now()

    probe.start()
    run_periods(stepper, stepper.next_block(), 3)

    # Nothing stamped, so every pass is due at once; only the interval holds it back.
    assert [s - began for s in recorder.starts] == [0.0, 2.0, 4.0, 6.0]


class BackingOffAfterFirst(Recorder):
    """Reports a failure backoff (D7a) from the first attempt on."""

    def __init__(
        self, clock: FakeClock, authority: Authority, backoff_s: float
    ) -> None:
        super().__init__(clock)
        self._authority = authority
        self._backoff_s = backoff_s

    def refresh_blocking(self) -> frozenset[str] | None:
        result = super().refresh_blocking()
        self._authority.stamp()
        return result

    def ready_in(self) -> float:
        return self._backoff_s if self.starts else 0.0


def test_a_periodic_refresh_waits_for_the_failure_backoff(
    probes: list[InspectProbe],
) -> None:
    clock = FakeClock()
    authority = Authority(clock)
    backing_off = BackingOffAfterFirst(clock, authority, backoff_s=400.0)
    probe, stepper, _, _, _ = build(
        probes, recorder=backing_off, clock=clock, authority=authority
    )
    began = clock.now()

    probe.start()
    block = stepper.next_block()
    assert block == 400.0  # the period is 300 s; the backoff is longer
    stepper.expire()
    stepper.next_block()

    assert [s - began for s in backing_off.starts] == [0.0, 400.0]


def test_no_periodic_refresh_runs_while_no_worker_is_online(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, authority = build(probes)
    authority.online = False
    began = clock.now()

    probe.start()
    block = stepper.next_block()

    # The startup attempt ran; with nobody online nothing is owed, so the probe idles.
    assert block is None
    assert [s - began for s in recorder.starts] == [0.0]


def test_a_failed_attempt_keeps_the_period_due_even_with_no_worker_online(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, authority = build(probes)
    authority.online = False
    authority.errored = True
    authority.stamps = False
    began = clock.now()

    probe.start()
    run_periods(stepper, stepper.next_block(), 2)

    assert [s - began for s in recorder.starts] == [0.0, 2.0, 4.0]


def test_a_poke_refreshes_when_authority_is_due_and_does_nothing_when_it_is_fresh(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, authority = build(probes)
    authority.online = False
    began = clock.now()
    probe.start()
    assert stepper.next_block() is None

    # A worker turns live while the stamp is still fresh: re-evaluated, no attempt.
    authority.online = True
    probe.poke()
    assert stepper.next_block() == PERIOD_S
    assert len(recorder.starts) == 1

    # Now the stamp is old: a liveness change brings the refresh within one interval.
    authority.online = False
    stepper.expire()
    assert stepper.next_block() is None
    clock.advance(2 * PERIOD_S)
    authority.online = True
    probe.poke()
    stepper.next_block()

    assert [s - began for s in recorder.starts][-1] == pytest.approx(
        clock.now() - began, abs=INTERVAL_S
    )
    assert len(recorder.starts) == 2


def test_a_poke_before_start_does_nothing(probes: list[InspectProbe]) -> None:
    probe, _, recorder, _, _ = build(probes)

    probe.poke()

    assert recorder.starts == []


def test_a_join_keeps_every_owed_time_next_to_the_periodic_refresh(
    probes: list[InspectProbe],
) -> None:
    probe, stepper, recorder, clock, _ = build(probes)
    probe.start()
    stepper.next_block()
    clock.advance(100.0)
    joined = clock.now()

    probe.trigger("celery@a")
    block = stepper.next_block()
    while block != PERIOD_S:
        stepper.expire()
        block = stepper.next_block()

    assert_asked_at_each_due(recorder.starts, joined)
    assert [s - joined for s in recorder.starts[2:7]] == pytest.approx(list(DUES))


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


class BlockingSecond(StampingRecorder):
    """Blocks inside its second attempt (the periodic one) until released."""

    def __init__(self, clock: FakeClock, authority: Authority) -> None:
        super().__init__(clock, authority)
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
    authority = Authority(clock)
    blocking = BlockingSecond(clock, authority)
    probe, stepper, _, _, _ = build(
        probes, recorder=blocking, clock=clock, authority=authority
    )

    probe.start()
    stepper.next_block()
    stepper.expire()  # the periodic refresh starts and blocks in the target
    assert blocking.entered.acquire(timeout=BOUND_S)

    began = time.monotonic()
    probe.stop(timeout=0.3)
    elapsed = time.monotonic() - began
    blocking.gate.set()

    assert elapsed < 2.0
