"""Replay recorded Celery chord flows through ``TaskGraph`` and judge what it draws.

``tests/data/chord_truth_flows.json`` holds, per flow, the events stemtrace
published, the truth Celery recorded at send time (each chord's header group ID
and where its body starts) and ``checks`` comparing the two. A chord is judged
by its header group ID: the callback its CHORD node draws is the truth
(``ok``), nothing (``none``) or another node (``WRONG``); a chord whose body
never ran has no callback to draw.
"""

import functools
import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from stemtrace.core.events import TaskEvent
from stemtrace.core.graph import NodeType, TaskGraph

DATA = Path(__file__).parent / "data"
FLOWS = DATA / "chord_truth_flows.json"
MAIN_OK = DATA / "chord_truth_main_ok.json"
FIELD = "chord_body_first_id"
Capture = dict[str, Any]
# The events as recorded; without the field (workers from 0.4.0 on that don't
# send it yet); as workers before 0.4.0 sent them (also no parent and group IDs
# on PENDING); and with the field on one header per chord only (a rolling
# upgrade).
VARIANTS = ("field", "plain", "legacy", "mixed")


@functools.cache
def load_captures() -> dict[str, Capture]:
    """Every recorded flow, by key (``NAME@native|unlock|redis[+wp]@celeryX.Y.Z``)."""
    captures: dict[str, Capture] = json.loads(FLOWS.read_text())
    return captures


def cell_of(key: str, chord_truth: dict[str, Any]) -> tuple[str, str, str]:
    """(backend, Celery version, body class) of a chord: ``chain>task`` for a chain body."""
    _, backend, version = key.split("@")
    body = chord_truth["body_kind"]
    if body == "chain":
        body = f"chain>{chord_truth['body_first_kind']}"
    return backend.split("+")[0], version.removeprefix("celery"), body


def _variant(events: list[dict[str, Any]], variant: str) -> list[dict[str, Any]]:
    """The recorded events as one of ``VARIANTS`` has them."""
    if variant == "field":
        return events
    plain = [{k: v for k, v in e.items() if k != FIELD} for e in events]
    if variant == "plain":
        return plain
    if variant == "legacy":
        bare = ("group_id", "parent_id")
        return [
            {k: v for k, v in e.items() if k not in bare}
            if e["state"] == "PENDING"
            else e
            for e in plain
        ]
    # mixed: the field stays on the events of the smallest task ID per group.
    keepers: dict[str, str] = {}
    for event in events:
        if FIELD in event:
            group = event["group_id"]
            keepers[group] = min(keepers.get(group, event["task_id"]), event["task_id"])
    return [
        e if FIELD not in e or keepers[e["group_id"]] == e["task_id"] else plain_e
        for e, plain_e in zip(events, plain, strict=True)
    ]


@functools.cache
def events_of(key: str, variant: str) -> list[TaskEvent]:
    """A capture's events, as ``variant`` has them, in the order recorded."""
    events = _variant(load_captures()[key]["events"], variant)
    return [TaskEvent.model_validate(e) for e in events]


def chords_of(capture: Capture) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each chord's truth with its check."""
    return list(zip(capture["truth"]["chords"], capture["checks"], strict=True))


def expected(chord_truth: dict[str, Any], check: dict[str, Any]) -> str | None:
    """The node ID the chord should draw as its callback (None: it never ran)."""
    if not check["callback_seen"]:
        return None
    callback = chord_truth["callback_id"]
    return callback if chord_truth["callback_kind"] == "task" else f"group:{callback}"


def drawn(graph: TaskGraph, group_id: str) -> str | None:
    """The node ID the chord's CHORD node draws as its callback (None: none)."""
    node = graph.nodes.get(f"group:{group_id}")
    if node is None or node.node_type != NodeType.CHORD:
        return None
    return node.chord_id


def judge(graph: TaskGraph, chord_truth: dict[str, Any], check: dict[str, Any]) -> str:
    """``ok``, ``none`` or ``WRONG`` for one chord.

    A group body is drawn as the group itself: right when it lists tasks of
    the body only (all of them once every event arrived, see ``is_complete``).
    """
    want = expected(chord_truth, check)
    got = drawn(graph, chord_truth["group_id"])
    if got is None:
        return "none"
    if got != want:
        return "WRONG"
    if chord_truth["callback_kind"] == "group":
        listed = set(graph.get_group_members(got.removeprefix("group:")))
        if not listed <= set(chord_truth["callback_member_ids"]):
            return "WRONG"
    return "ok"


def is_complete(graph: TaskGraph, chord_truth: dict[str, Any]) -> bool:
    """Whether a drawn group body lists every task of the body."""
    got = drawn(graph, chord_truth["group_id"])
    if chord_truth["callback_kind"] != "group" or got is None:
        return True
    listed = set(graph.get_group_members(got.removeprefix("group:")))
    return listed == set(chord_truth["callback_member_ids"])


def replay(events: list[TaskEvent]) -> TaskGraph:
    """The graph after adding the events in the order given.

    Celery's events always settle a chord's resolution (the graph counts the
    chords whose resolution kept being asked again after its last pass).
    """
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
    assert graph._resolve_overflows == 0
    return graph


def shuffles(
    key: str, events: list[TaskEvent], count: int
) -> Iterator[list[TaskEvent]]:
    """``count`` seeded permutations of the events (the same for a key every time)."""
    for index in range(count):
        yield random.Random(f"7:{key}:{index}").sample(events, len(events))


def wrong_in_prefixes(
    capture: Capture, order: list[TaskEvent]
) -> list[tuple[int, str]]:
    """Chords drawn WRONG after any prefix of ``order`` (prefix length, group ID)."""
    chords = chords_of(capture)
    graph = TaskGraph()
    found: list[tuple[int, str]] = []
    for length, event in enumerate(order, 1):
        graph.add_event(event)
        found += [
            (length, c["group_id"])
            for c, ch in chords
            if judge(graph, c, ch) == "WRONG"
        ]
    assert graph._resolve_overflows == 0
    return found


def cell_counts(variant: str) -> dict[str, dict[str, int]]:
    """Per ``backend|version|body`` cell: chords, ok, wrong, and chords never run.

    ``drawn_never_run`` counts chords whose body never ran that draw a callback.
    """
    cells: dict[str, Counter[str]] = defaultdict(Counter)
    for key, capture in load_captures().items():
        graph = replay(events_of(key, variant))
        for c, ch in chords_of(capture):
            cell = "|".join(cell_of(key, c))
            result = judge(graph, c, ch)
            ran = expected(c, ch) is not None
            cells[cell]["n"] += 1
            cells[cell]["never_run"] += not ran
            cells[cell]["wrong"] += result == "WRONG"
            cells[cell]["drawn_never_run"] += not ran and result != "none"
            cells[cell]["ok"] += result == "ok" and is_complete(graph, c)
    return {cell: dict(count) for cell, count in sorted(cells.items())}


def lost_from_main(variant: str, main_ok: dict[str, list[int]]) -> dict[str, int]:
    """Per cell: chords v0.4.2 drew right (``main_ok``: key -> chord seqs) that don't now."""
    lost: Counter[str] = Counter()
    for key, capture in load_captures().items():
        graph = replay(events_of(key, variant))
        for c, ch in chords_of(capture):
            if c["seq"] in main_ok.get(key, ()) and judge(graph, c, ch) != "ok":
                lost["|".join(cell_of(key, c))] += 1
    return dict(sorted(lost.items()))


def expected_tables() -> dict[str, Any]:
    """The tables ``test_chord_callback_attribution.py`` compares against."""
    main_ok = json.loads(MAIN_OK.read_text())
    return {
        "cells": {
            v: {
                cell: {k: n for k, n in count.items() if k in ("n", "ok")}
                for cell, count in cell_counts(v).items()
            }
            for v in ("field", "plain")
        },
        "ok_to_none": {v: lost_from_main(v, main_ok) for v in ("field", "plain")},
    }


if __name__ == "__main__":
    # Regenerate tests/data/chord_truth_expected.json after a deliberate change:
    # python -m tests.chord_truth > tests/data/chord_truth_expected.json
    json.dump(expected_tables(), sys.stdout, indent=1)
    sys.stdout.write("\n")
