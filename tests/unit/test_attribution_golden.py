"""Every value derived from a task's events matches the committed golden digests.

Guards the move of event attribution into one module: see
``tests/attribution_golden.py`` for the variants and how to regenerate.
"""

import json
from typing import Any

import pytest

from tests.attribution_golden import GOLDEN, flow_digests, load_flows

_FLOWS = load_flows()
_GOLDEN: dict[str, dict[str, str]] = json.loads(GOLDEN.read_text())


def test_golden_covers_every_recorded_flow() -> None:
    assert sorted(_GOLDEN) == sorted(_FLOWS)


@pytest.mark.parametrize("key", sorted(_FLOWS))
def test_replay_of_flow_matches_golden_digests(key: str) -> None:
    raw: list[dict[str, Any]] = _FLOWS[key]
    got = flow_digests(raw)
    changed = sorted(v for v in _GOLDEN[key] if got.get(v) != _GOLDEN[key][v])
    assert not changed, (
        f"{key}: variants {changed} changed; inspect one with "
        f"`uv run python -m tests.attribution_golden --show {key} VARIANT`"
    )
