"""Differential tests: chord_unlock fixes against the v0.4.2 graph code.

The chord callback rules changed to fix chains of chords on a ``chord_unlock``
backend. Every real capture of a chord workflow is replayed through the pinned
v0.4.2 module (``tests/graph_baseline.py``) and the current one, in the
captured order and 25 seeded shuffles:

* the current code draws what v0.4.2 drew, or a shape listed in
  ``_IMPROVEMENTS`` (and each listed shape really differs from v0.4.2);
* the current code never adds order dependence: the structures it draws over
  the orders are a subset of v0.4.2's.
"""

import re
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent
from stemtrace.core.graph import TaskGraph
from tests.graph_baseline import load_baseline
from tests.unit.test_graph_celery_flows import _CAPTURES, _orders, _structure

_FLOW = re.compile(
    r"chord_chord_header|chord_in_chord|chain_chord_chord_body|chord_chain_chord_body"
    r"|chain_add_chord_chord_body|chord_depth3_body|chord_chain_tsum_chord_body"
    r"|chain_chords_|^chord(@|$)|chord_with_chain_body|^chain_a_group_tsum_add@"
)
_FLOWS = sorted(name for name in _CAPTURES if _FLOW.search(name))

# Flows where the current code deliberately draws something else than v0.4.2,
# and the shape it draws (the callback of the first chord of a chain is its own
# first task, not a later chord's).
_IMPROVEMENTS: dict[str, str] = {
    "chain_chords_3@chord_unlock": (
        "chord{*>tsum(celery.chord_unlock(tsum(celery.chord_unlock, "
        "chord{*tsum, +add, +add})), chord{+add, +add}), +add, +add, "
        "celery.chord_unlock}"
    ),
    "chain_chords_4@chord_unlock": (
        "chord{*>tsum(celery.chord_unlock(tsum(celery.chord_unlock(tsum("
        "celery.chord_unlock, chord{*tsum, +add, +add})), chord{+add, +add})), "
        "chord{+add, +add}), +add, +add, celery.chord_unlock}"
    ),
}


def _structures(cls: Any, events: list[dict[str, Any]]) -> dict[str, int]:
    """How many of the orders draw each structure."""
    counts: dict[str, int] = {}
    for order in _orders(events):
        graph = cls()
        for event in order:
            graph.add_event(TaskEvent.model_validate(event))
        structure = _structure(graph)
        counts[structure] = counts.get(structure, 0) + 1
    return counts


@pytest.mark.parametrize("flow", _FLOWS)
def test_current_graph_matches_v0_4_2_or_a_listed_improvement(flow: str) -> None:
    baseline = load_baseline()
    current = _structures(TaskGraph, _CAPTURES[flow])
    before = _structures(baseline.TaskGraph, _CAPTURES[flow])

    if flow in _IMPROVEMENTS:
        assert set(current) == {_IMPROVEMENTS[flow]}
        assert set(before) != set(current)
    else:
        assert set(current) == set(before)


@pytest.mark.parametrize("flow", _FLOWS)
def test_current_graph_never_adds_order_dependence(flow: str) -> None:
    baseline = load_baseline()
    current = _structures(TaskGraph, _CAPTURES[flow])
    before = _structures(baseline.TaskGraph, _CAPTURES[flow])

    assert set(current) <= set(before) or flow in _IMPROVEMENTS
    assert len(current) <= max(len(before), 1)
