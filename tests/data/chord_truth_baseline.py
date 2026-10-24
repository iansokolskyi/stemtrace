"""Compare the callback stemtrace draws for each chord with the recorded truth.

Replays every capture in ``chord_truth_flows.json`` (written by
``capture_celery_flows.py --truth-flows|--generate``) through ``TaskGraph``: in
the captured order and in seeded shuffles. For every chord (by its header group
ID) it reports what the CHORD node draws as the callback against the truth
Celery recorded at send time, as ``ok``, ``none`` (no node, or no callback) or
``wrong`` (another ID), and whether the drawing changes with the order.

Run: ``uv run python tests/data/chord_truth_baseline.py [--src DIR]
[--shuffles 25] [--seed 7] [--json OUT] [--examples 3]``. ``--src`` points at
another checkout's ``src`` (e.g. the parked branch) to compare its graph code.
"""

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DATA = Path(__file__).with_name("chord_truth_flows.json")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=Path(__file__).parents[2] / "src")
    parser.add_argument("--shuffles", type=int, default=25)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--examples", type=int, default=3)
    parser.add_argument("--data", type=Path, default=DATA)
    return parser.parse_args()


ARGS = _args() if __name__ == "__main__" else None
if ARGS is not None:
    sys.path.insert(0, str(ARGS.src))

from stemtrace.core import graph as graph_module  # noqa: E402
from stemtrace.core.events import TaskEvent  # noqa: E402
from stemtrace.core.graph import NodeType, TaskGraph  # noqa: E402

_PREFIX = getattr(graph_module, "_GROUP_NODE_PREFIX", "group:")


def drawn_callback(graph: TaskGraph, chord_truth: dict[str, Any]) -> str:
    """What the graph draws for one chord, as a comparable string.

    ``no_node``: no CHORD node for the header group. ``none``: a CHORD node
    without a callback. Otherwise the callback's task ID, the inner chord's
    header group ID (``chord_group`` callbacks; a ``group:`` node ID is
    reduced to it) or, for a group body, the sorted body member IDs.
    """
    gid = chord_truth["group_id"]
    node = graph.nodes.get(f"{_PREFIX}{gid}")
    if node is None:
        return "no_node"
    if node.node_type != NodeType.CHORD:
        return "not_chord"
    if chord_truth["callback_kind"] == "group":
        body = getattr(graph, "_body_members", None)  # v0.4.2 drew a body node
        if body is not None:
            members = body.get(gid, [])
        else:
            members = graph.get_group_members(
                (node.chord_id or "").removeprefix(_PREFIX)
            )
        return "none" if not members else "members:" + ",".join(sorted(members))
    if node.chord_id is None:
        return "none"
    return node.chord_id.removeprefix(_PREFIX)


def expected_callback(chord_truth: dict[str, Any]) -> str | None:
    """The truth as ``drawn_callback`` writes it (None: the body never ran)."""
    if chord_truth["callback_kind"] == "group":
        return "members:" + ",".join(sorted(chord_truth["callback_member_ids"]))
    callback = chord_truth["callback_id"]
    return None if callback is None else str(callback)


def replay(events: list[TaskEvent]) -> TaskGraph:
    """The graph after adding ``events`` in the order given."""
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
    return graph


def classify(drawn: str, expected: str | None) -> str:
    """``ok``, ``none`` or ``wrong`` (``unresolved``: the body never ran and none is drawn)."""
    if expected is None:
        return "unresolved" if drawn in ("no_node", "none", "not_chord") else "wrong"
    if drawn == expected:
        return "ok"
    if drawn in ("no_node", "none", "not_chord"):
        return "none"
    return "wrong"


def body_class(chord_truth: dict[str, Any]) -> str:
    """The body kind; a chain body adds what it starts with (``chain>chord``)."""
    kind = chord_truth["body_kind"]
    if kind == "chain":
        return f"chain>{chord_truth['body_first_kind']}"
    return str(kind)


def flow_info(key: str) -> tuple[str, str]:
    """(backend + pool flavour, Celery version) of a capture key."""
    _, backend, version = key.split("@")
    return backend, version.removeprefix("celery")


def short(value: str | None) -> str:
    """First 4 characters of an ID (``members:`` lists shortened too)."""
    if value is None:
        return "None"
    if value.startswith("members:"):
        return "members:" + "+".join(v[:4] for v in value[8:].split(","))
    return value[:4]


def evaluate(
    captures: dict[str, Any], shuffles: int, seed: int
) -> list[dict[str, Any]]:
    """One row per (capture, chord): its truth, drawing and order sensitivity."""
    rows = []
    for key, capture in captures.items():
        events = [TaskEvent.model_validate(e) for e in capture["events"]]
        chords = capture["truth"]["chords"]
        orders = [events]
        for index in range(shuffles):
            shuffled = list(events)
            random.Random(f"{seed}:{key}:{index}").shuffle(shuffled)
            orders.append(shuffled)
        drawn: dict[str, list[str]] = defaultdict(list)
        for order in orders:
            try:
                graph = replay(order)
            except Exception as exc:  # noqa: BLE001  # report a crash, keep going
                for chord_truth in chords:
                    drawn[chord_truth["group_id"]].append(f"crash:{type(exc).__name__}")
                continue
            for chord_truth in chords:
                drawn[chord_truth["group_id"]].append(
                    drawn_callback(graph, chord_truth)
                )
        for chord_truth in chords:
            results = drawn[chord_truth["group_id"]]
            expected = expected_callback(chord_truth)
            classes = [classify(d, expected) for d in results]
            backend, version = flow_info(key)
            rows.append(
                {
                    "key": key,
                    "desc": capture["desc"],
                    "backend": backend.split("+")[0],
                    "wp": backend.endswith("+wp"),
                    "version": version,
                    "header": chord_truth["header_kind"],
                    "body": body_class(chord_truth),
                    "group_id": chord_truth["group_id"],
                    "seq": chord_truth["seq"],
                    "expected": expected,
                    "captured": results[0],
                    "captured_class": classes[0],
                    "any_wrong": "wrong" in classes,
                    "any_none": "none" in classes,
                    "any_not_ok": any(c not in ("ok", "unresolved") for c in classes),
                    "order_dependent": len(set(results)) > 1,
                    "distinct": sorted(set(results)),
                }
            )
    return rows


def print_tables(rows: list[dict[str, Any]], examples: int) -> None:
    """Totals, per shape class and examples of every class with a wrong drawing."""
    keys = ("backend", "version", "header", "body")
    by_class: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_class[tuple(row[k] for k in keys)].append(row)
    print(
        f"{'backend':8} {'celery':6} {'header':22} {'body':14} chords   ok none wrong  order-dep  any-shuffle-wrong"
    )
    for cls, members in sorted(by_class.items()):
        c = Counter(r["captured_class"] for r in members)
        print(
            f"{cls[0]:8} {cls[1]:6} {cls[2]:22} {cls[3]:14} {len(members):6} "
            f"{c['ok']:4} {c['none']:4} {c['wrong']:5} {sum(r['order_dependent'] for r in members):10} "
            f"{sum(r['any_wrong'] for r in members):10}"
        )
    total = Counter(r["captured_class"] for r in rows)
    print(
        f"TOTAL chords={len(rows)} ok={total['ok']} none={total['none']} "
        f"wrong={total['wrong']} unresolved={total['unresolved']} "
        f"order_dependent={sum(r['order_dependent'] for r in rows)} "
        f"any_shuffle_wrong={sum(r['any_wrong'] for r in rows)}"
    )
    for cls, members in sorted(by_class.items()):
        wrong = [r for r in members if r["captured_class"] == "wrong"]
        for row in wrong[:examples]:
            print(
                f"WRONG {cls} {row['key']} chord#{row['seq']} {row['desc']}: "
                f"drawn={short(row['captured'])} expected={short(row['expected'])}"
            )


def main() -> None:
    """Replay the captures and print the report."""
    assert ARGS is not None
    print("graph:", graph_module.__file__)
    captures = json.loads(ARGS.data.read_text())
    rows = evaluate(captures, ARGS.shuffles, ARGS.seed)
    print_tables(rows, ARGS.examples)
    if ARGS.json:
        ARGS.json.write_text(json.dumps(rows))


if __name__ == "__main__":
    main()
