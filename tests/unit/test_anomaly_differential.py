"""Differential fuzz: classifier vs the independent reference (see ``anomaly_fuzz``).

CI runs a seeded sample. The full sweep is opt-in and sharded so each locked run
stays under 10 minutes::

    ANOMALY_FUZZ_SHARD=0/40 pytest tests/unit/test_anomaly_differential.py -k full
"""

import os
import random
import time

import pytest

from stemtrace.core.anomalies import AnomalyEvidence
from tests.unit.anomaly_fuzz import (
    build,
    candidate_nows,
    classify_fast,
    full_cells,
    load_flows,
    random_cell,
    run_cell,
)

SAMPLE_SEEDS = (1, 2, 3)
SAMPLE_CELLS = 250
SAMPLE_BUDGET_S = 30


@pytest.mark.parametrize("seed", SAMPLE_SEEDS)
def test_classifier_matches_reference_on_a_seeded_sample(seed: int) -> None:
    flows = load_flows()
    rng = random.Random(seed)
    started = time.monotonic()
    problems: list[str] = []

    for _ in range(SAMPLE_CELLS):
        problems += run_cell(random_cell(rng, flows))

    assert problems[:5] == []
    assert time.monotonic() - started < SAMPLE_BUDGET_S


def _shard() -> tuple[int, int]:
    index, _, count = os.environ.get("ANOMALY_FUZZ_SHARD", "0/1").partition("/")
    return int(index), int(count)


@pytest.mark.skipif(
    not os.environ.get("ANOMALY_FUZZ_FULL"), reason="full sweep: set ANOMALY_FUZZ_FULL"
)
def test_classifier_matches_reference_on_the_full_sweep_shard() -> None:
    index, count = _shard()
    problems: list[str] = []
    cells = 0
    for position, cell in enumerate(full_cells(load_flows())):
        if position % count != index:
            continue
        cells += 1
        problems += run_cell(cell)
        if len(problems) > 20:
            break

    assert problems[:10] == []
    assert cells > 0


def test_sample_reaches_every_kind_evidence_and_threshold_branch() -> None:
    flows = load_flows()
    rng = random.Random(7)
    seen: set[object] = set()
    for _ in range(300):
        built = build(random_cell(rng, flows))
        for now in candidate_nows(built):
            for anomaly in classify_fast(built, now).anomalies.values():
                seen.add(anomaly.kind.value)
                seen.add(anomaly.evidence)
                seen.add(anomaly.threshold_ms)

    assert {"stuck", "interrupted"} <= seen
    assert {1000, 4500, 5000} <= seen  # floor, k x p95, ceiling
    assert {
        "worker_shutdown",
        "worker_terminating",
        "worker_restarted",
        "cancelled_retry",
    } <= {e.value for e in seen if isinstance(e, AnomalyEvidence)}
