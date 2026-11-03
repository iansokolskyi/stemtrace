"""Property test of the fleet item (D4b, delivery rule 2a): the invariant, both ways.

Seeded interleavings of refreshes (in flight while events and evaluations
happen), task events from known and unknown nodenames, worker stops, kills, the
30-minute cleanup, clock jumps, a broker that is up or down, and inspect
raising. Every evaluation must agree with an oracle written from the spec, never
report the fleet offline while a worker answered the latest completed broadcast
or sent an event within the stale window, and always report a fleet whose every
known worker stopped. The test fails unless every spec row was reached.
"""

from collections import Counter

import pytest

from tests.unit.fleet_world import ROWS, World, patched_clock

SEEDS = range(240)
STEPS = 70
MIN_REACH = 3


def run_seed(monkeypatch: pytest.MonkeyPatch, seed: int, reached: Counter[str]) -> None:
    world = World(seed, reached, broker=seed % 4 != 0)
    patched_clock(monkeypatch, world)
    world.run(STEPS)


def test_invariant_holds_over_seeded_interleavings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached: Counter[str] = Counter()
    for seed in SEEDS:
        run_seed(monkeypatch, seed, reached)
    print("rows reached:", {row: reached[row] for row in ROWS})

    assert {row: reached[row] for row in ROWS if reached[row] < MIN_REACH} == {}
