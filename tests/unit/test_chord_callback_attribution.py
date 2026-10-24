"""The callback a CHORD node draws, checked against the truth Celery recorded.

``tests/data/chord_truth_flows.json`` has the events real workers published for
every chord shape (backend, Celery version, header and body kinds, nesting,
``replace()``, headers that fail or never run, seeded random canvases) and the
truth about each chord. Every chord must draw its real callback or nothing,
never another node, in every state the graph passes through:

* after every prefix of the events, as recorded and in seeded shuffles;
* after a single event is lost, and after a single task is removed (what
  eviction does);

for the events as recorded, as workers without ``chord_body_first_id`` publish
them (0.4.0 and later, and before 0.4.0, which also left the group and parent
IDs off PENDING) and as a half-upgraded fleet does (the field on one header per
chord). Counts per backend, Celery version and body kind pin how often the real
callback is drawn (``tests/data/chord_truth_expected.json``) and where a chord
that v0.4.2 drew right now draws nothing (``chord_truth_main_ok.json``).

Replaying every capture in every way takes minutes, so a run checks a seeded
selection: every capture as recorded and without the field, in the order
recorded, and for each other check (shuffles, the other two variants, a lost
event, a removed task) a different quarter of the captures. Set
``STEMTRACE_CHORD_FULL=1`` to run every check on every capture, with 25 shuffles
and every event and task.
"""

import json
import os
import random
import zlib
from collections import defaultdict
from pathlib import Path

import pytest

from stemtrace.core.graph import TaskGraph
from tests import chord_truth as ct
from tests.graph_invariants import assert_graph_invariants

DATA = Path(__file__).parents[1] / "data"
EXPECTED = json.loads((DATA / "chord_truth_expected.json").read_text())
MAIN_OK: dict[str, list[int]] = json.loads(
    (DATA / "chord_truth_main_ok.json").read_text()
)
FULL = bool(os.environ.get("STEMTRACE_CHORD_FULL"))
SHUFFLES = 25 if FULL else 2
DROPS = 10_000 if FULL else 2  # events lost one at a time, per capture
REMOVALS = 10_000 if FULL else 2  # tasks removed one at a time, per capture


def _chunk_of(key: str) -> str:
    name, backend, version = key.split("@")
    kind = "gen" if name.startswith("gen") else "flows"
    return f"{kind}@{backend}@{version.removeprefix('celery')}"


CHUNKS = sorted({_chunk_of(key) for key in ct.load_captures()})


def _members(chunk: str) -> list[tuple[str, ct.Capture]]:
    return [(k, c) for k, c in ct.load_captures().items() if _chunk_of(k) == chunk]


def _selected(key: str, check: str, one_in: int = 4) -> bool:
    """Whether this run does ``check`` on a capture: one in ``one_in``, seeded."""
    return FULL or zlib.crc32(f"{key}:{check}".encode()) % one_in == 0


def _sample(key: str, label: str, count: int, limit: int) -> list[int]:
    """Up to ``limit`` of the indexes below ``count``, seeded by the capture."""
    if count <= limit:
        return list(range(count))
    return sorted(random.Random(f"{key}:{label}").sample(range(count), limit))


def _wrong(graph: TaskGraph, capture: ct.Capture) -> list[str]:
    return [
        c["group_id"]
        for c, ch in ct.chords_of(capture)
        if ct.judge(graph, c, ch) == "WRONG"
    ]


@pytest.mark.parametrize("chunk", CHUNKS)
@pytest.mark.parametrize("variant", ct.VARIANTS)
def test_replay_chord_flow_never_draws_a_wrong_callback_at_any_prefix(
    chunk: str, variant: str
) -> None:
    for key, capture in _members(chunk):
        events = ct.events_of(key, variant)
        orders = []
        if variant in ("field", "plain") or _selected(key, f"prefix:{variant}"):
            orders.append(events)
        if _selected(
            key, f"shuffle:{variant}", 4 if variant in ("field", "plain") else 8
        ):
            orders += ct.shuffles(key, events, SHUFFLES)
        for number, order in enumerate(orders):
            found = ct.wrong_in_prefixes(capture, order)
            assert found == [], (key, variant, number, found[:3])


@pytest.mark.parametrize("chunk", CHUNKS)
@pytest.mark.parametrize("variant", ["field", "plain"])
def test_replay_chord_flow_without_a_single_event_never_draws_a_wrong_callback(
    chunk: str, variant: str
) -> None:
    for key, capture in _members(chunk):
        events = ct.events_of(key, variant)
        for index in _sample(key, f"drop:{variant}", len(events), DROPS):
            graph = ct.replay(events[:index] + events[index + 1 :])
            assert _wrong(graph, capture) == [], (key, variant, index)


@pytest.mark.parametrize("chunk", CHUNKS)
@pytest.mark.parametrize("variant", ct.VARIANTS)
def test_remove_a_single_task_of_a_chord_flow_never_draws_a_wrong_callback(
    chunk: str, variant: str
) -> None:
    for key, capture in _members(chunk):
        if variant not in ("field", "plain") and not _selected(
            key, f"remove:{variant}"
        ):
            continue
        events = ct.events_of(key, variant)
        task_ids = sorted({e.task_id for e in events})
        for index in _sample(key, f"remove:{variant}", len(task_ids), REMOVALS):
            graph = ct.replay(events)
            graph.remove_node(task_ids[index])
            assert_graph_invariants(graph)
            assert _wrong(graph, capture) == [], (key, variant, task_ids[index])


@pytest.mark.parametrize("chunk", CHUNKS)
def test_replay_chord_flow_draws_the_same_callbacks_in_every_order(chunk: str) -> None:
    for key, capture in _members(chunk):
        group_ids = [c["group_id"] for c in capture["truth"]["chords"]]
        for variant in ct.VARIANTS:
            if not _selected(key, f"order:{variant}", 4 if variant == "field" else 8):
                continue
            events = ct.events_of(key, variant)
            drawn = {
                tuple(ct.drawn(ct.replay(order), g) for g in group_ids)
                for order in [events, *ct.shuffles(key, events, SHUFFLES)]
            }
            assert len(drawn) == 1, (key, variant, drawn)


@pytest.fixture(scope="module")
def counted() -> dict[str, dict[str, dict[str, int]]]:
    return {variant: ct.cell_counts(variant) for variant in ("field", "plain")}


@pytest.mark.parametrize("variant", ["field", "plain"])
def test_chord_flow_counts_per_cell_match_the_recorded_expectation(
    counted: dict[str, dict[str, dict[str, int]]], variant: str
) -> None:
    got = {
        cell: {k: v for k, v in count.items() if k in ("n", "ok")}
        for cell, count in counted[variant].items()
    }

    assert got == EXPECTED["cells"][variant]


@pytest.mark.parametrize("variant", ["field", "plain"])
def test_no_chord_flow_is_drawn_wrong_and_none_that_never_ran_has_a_callback(
    counted: dict[str, dict[str, dict[str, int]]], variant: str
) -> None:
    for cell, count in counted[variant].items():
        assert count["wrong"] == 0, (variant, cell)
        assert count["drawn_never_run"] == 0, (variant, cell)
    assert sum(c["never_run"] for c in counted[variant].values()) >= 40


@pytest.mark.parametrize(
    ("other", "same_as"), [("mixed", "field"), ("legacy", "plain")]
)
def test_a_half_upgraded_fleet_and_workers_before_0_4_0_draw_what_their_twin_does(
    other: str, same_as: str
) -> None:
    """Mixed events draw what the field's do, and events from workers before
    0.4.0 (PENDING without group and parent IDs) what the field-less ones do."""
    for key, capture in ct.load_captures().items():
        if not _selected(key, f"twin:{other}"):
            continue
        group_ids = [c["group_id"] for c in capture["truth"]["chords"]]
        got = [ct.drawn(ct.replay(ct.events_of(key, other)), g) for g in group_ids]
        want = [ct.drawn(ct.replay(ct.events_of(key, same_as)), g) for g in group_ids]
        assert got == want, (key, other)


@pytest.mark.parametrize("variant", ["field", "plain"])
def test_chords_v0_4_2_drew_right_lose_their_callback_only_where_listed(
    variant: str,
) -> None:
    assert ct.lost_from_main(variant, MAIN_OK) == EXPECTED["ok_to_none"][variant]


def test_recorded_header_events_name_the_first_task_of_the_body() -> None:
    """The field the worker extracts is the first task Celery recorded."""
    checked: defaultdict[str, int] = defaultdict(int)
    for key, capture in ct.load_captures().items():
        by_group: defaultdict[str, set[str]] = defaultdict(set)
        for event in capture["events"]:
            if ct.FIELD in event:
                by_group[event["group_id"]].add(event[ct.FIELD])
        for c in capture["truth"]["chords"]:
            task_first = c["body_first_kind"] == "task"
            want = {c["callback_id_frozen"]} if task_first else set()
            assert by_group.get(c["group_id"], set()) == want, (key, c["seq"])
            checked[c["body_first_kind"]] += 1
    assert checked["task"] > 100
    assert checked["chord"] > 0
    assert checked["group"] > 0
