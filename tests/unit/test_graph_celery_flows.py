"""Replay events captured from real Celery canvas workflows.

``tests/data/celery_flows.json`` holds, per workflow, the events stemtrace's
signal handlers published from an in-process Celery 5.6 worker, in arrival
order (regenerate with ``tests/data/capture_celery_flows.py``). Each one is
replayed as captured and in seeded shuffles: the graph must keep its
invariants after every event and end in the same expected shape.

Task and group IDs are UUIDs; shapes are written with labels ``T0``,
``T1``, ... given in order of first appearance in the capture (task, parent,
root, group, chord callback and chord IDs of each event in turn).
"""

import json
import random
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph
from stemtrace.server.store import GraphStore
from tests.graph_invariants import assert_graph_invariants

_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_LABELLED_FIELDS = (
    "task_id",
    "parent_id",
    "root_id",
    "group_id",
    "chord_callback_id",
    "chord_id",
)

_G, _C, _T = NodeType.GROUP, NodeType.CHORD, NodeType.TASK
# label -> (node type, parent label, sorted children labels)
_Shape = dict[str, tuple[NodeType, str | None, list[str]]]


def _chord_shape(header: list[str], body: str, tail: list[str], gid: str) -> _Shape:
    """A root chord whose body is a chain ``body -> tail[0] -> tail[1] ...``."""
    group = f"group:{gid}"
    shape: _Shape = {h: (_T, group, []) for h in header}
    shape[group] = (_C, None, sorted([*header, body]))
    chain = [body, *tail]
    for i, task in enumerate(chain):
        parent = group if i == 0 else chain[i - 1]
        shape[task] = (_T, parent, chain[i + 1 : i + 2])
    return shape


_EXPECTED: dict[str, tuple[_Shape, dict[str, str]]] = {
    "chain": (
        {"T0": (_T, None, ["T1"]), "T1": (_T, "T0", ["T2"]), "T2": (_T, "T1", [])},
        {},
    ),
    "group": (
        {
            "group:T3": (_G, None, ["T0", "T1", "T2"]),
            **{t: (_T, "group:T3", []) for t in ("T0", "T1", "T2")},
        },
        {},
    ),
    "chord": (_chord_shape(["T0", "T1", "T2"], "T4", [], "T3"), {"group:T3": "T4"}),
    "chain_a_group_tsum_add": (
        {
            "T0": (_T, None, ["group:T3"]),
            **_chord_shape(["T1", "T2"], "T4", ["T5"], "T3"),
            "group:T3": (_C, "T0", ["T1", "T2", "T4"]),
        },
        {"group:T3": "T4"},
    ),
    "chain_a_group_tsum_add_add": (
        {
            "T0": (_T, None, ["group:T3"]),
            **_chord_shape(["T1", "T2"], "T4", ["T5", "T6"], "T3"),
            "group:T3": (_C, "T0", ["T1", "T2", "T4"]),
        },
        {"group:T3": "T4"},
    ),
    "chain_group_nested": (
        {
            "T0": (_T, None, ["group:T3"]),
            "group:T3": (_G, "T0", ["T1", "T2"]),
            "T1": (_T, "group:T3", []),
            "T2": (_T, "group:T3", []),
        },
        {},
    ),
    "group_spawned_in_task": (
        {
            "T0": (_T, None, ["group:T4"]),
            "group:T4": (_G, "T0", ["T1", "T2", "T3"]),
            **{t: (_T, "group:T4", []) for t in ("T1", "T2", "T3")},
        },
        {},
    ),
    "chain_group_tsum_add": (
        _chord_shape(["T0", "T1"], "T4", ["T3"], "T2"),
        {"group:T2": "T4"},
    ),
    "chord_retried_header": (
        _chord_shape(["T0", "T1"], "T3", [], "T2"),
        {"group:T2": "T3"},
    ),
    "chord_in_chord_body": (
        {
            **_chord_shape(["T0", "T1"], "group:T3", [], "T2"),
            "group:T3": (_C, "group:T2", ["T4", "T5", "T6"]),
            "T4": (_T, "group:T3", []),
            "T5": (_T, "group:T3", []),
            "T6": (_T, "group:T3", []),
        },
        {"group:T2": "group:T3", "group:T3": "T6"},
    ),
    # group(chain(a1, b1), chain(a2, b2)): Celery stamps the group only on
    # b1 and b2; a1 and a2 are chain links drawn inside the group.
    "group_of_chains": (
        {
            "group:T4": (_G, None, ["T0", "T1"]),
            "T0": (_T, "group:T4", ["T2"]),
            "T1": (_T, "group:T4", ["T3"]),
            "T2": (_T, "T0", []),
            "T3": (_T, "T1", []),
        },
        {},
    ),
    # A header's own subtask (T4) and the callback (T3) both hang from a
    # header; only the callback the headers name is drawn after the chord.
    "chord_header_subtask": (
        {
            **_chord_shape(["T0", "T1"], "T3", [], "T2"),
            "T0": (_T, "group:T2", ["T4"]),
            "T4": (_T, "T0", []),
        },
        {"group:T2": "T3"},
    ),
    "chord_header_fanout_group": (
        {
            **_chord_shape(["T0", "T1"], "T3", [], "T2"),
            "T0": (_T, "group:T2", ["group:T6"]),
            "group:T6": (_G, "T0", ["T4", "T5"]),
            "T4": (_T, "group:T6", []),
            "T5": (_T, "group:T6", []),
        },
        {"group:T2": "T3"},
    ),
    # The body never runs: nothing shows a callback, so none is drawn.
    "chord_failed_header": (
        {
            "group:T2": (_C, None, ["T0", "T1"]),
            "T0": (_T, "group:T2", []),
            "T1": (_T, "group:T2", []),
        },
        {"group:T2": None},
    ),
    # A header's subtask is no evidence: with the body never run, no callback.
    "chord_failed_header_subtask": (
        {
            "group:T2": (_C, None, ["T0", "T1"]),
            "T0": (_T, "group:T2", ["T4"]),
            "T1": (_T, "group:T2", []),
            "T4": (_T, "T0", []),
        },
        {"group:T2": None},
    ),
    # The header's subtask (T4) must not become the callback, whichever of
    # the chain body's tasks arrives last.
    "chain_chord_header_subtask_add_add": (
        {
            "T0": (_T, None, ["group:T3"]),
            **_chord_shape(["T1", "T2"], "T5", ["T6", "T7"], "T3"),
            "group:T3": (_C, "T0", ["T1", "T2", "T5"]),
            "T1": (_T, "group:T3", ["T4"]),
            "T4": (_T, "T1", []),
        },
        {"group:T3": "T5"},
    ),
    # chain(a, group(chain(b1, c1), chain(b2, c2))): b1 and b2 are links.
    "chain_group_of_chains": (
        {
            "T0": (_T, None, ["group:T5"]),
            "group:T5": (_G, "T0", ["T1", "T2"]),
            "T1": (_T, "group:T5", ["T3"]),
            "T2": (_T, "group:T5", ["T4"]),
            "T3": (_T, "T1", []),
            "T4": (_T, "T2", []),
        },
        {},
    ),
    # T0 also spawned T2, so it isn't pulled into the group; T1 is.
    "group_of_chains_link_spawns": (
        {
            "T0": (_T, None, ["T2", "group:T5"]),
            "T2": (_T, "T0", []),
            "group:T5": (_G, "T0", ["T1", "T3"]),
            "T3": (_T, "group:T5", []),
            "T1": (_T, "group:T5", ["T4"]),
            "T4": (_T, "T1", []),
        },
        {},
    ),
    # Result backends without chord support: Celery's chord_unlock task
    # (T0 here) fires the body, which reports it as its parent.
    "chord@chord_unlock": (
        {
            **_chord_shape(["T1", "T2", "T3"], "T5", [], "T4"),
            "group:T4": (_C, None, ["T0", "T1", "T2", "T3", "T5"]),
            "T0": (_T, "group:T4", []),
        },
        {"group:T4": "T5"},
    ),
    "chain_a_group_tsum_add@chord_unlock": (
        {
            "T0": (_T, None, ["group:T4"]),
            **_chord_shape(["T2", "T3"], "T5", ["T6"], "T4"),
            "group:T4": (_C, "T0", ["T1", "T2", "T3", "T5"]),
            "T1": (_T, "group:T4", []),
        },
        {"group:T4": "T5"},
    ),
    "chord_with_chain_body@chord_unlock": (
        {
            **_chord_shape(["T1", "T2"], "T5", ["T4"], "T3"),
            "group:T3": (_C, None, ["T0", "T1", "T2", "T5"]),
            "T0": (_T, "group:T3", []),
        },
        {"group:T3": "T5"},
    ),
    # chain(a, chord([b], tsum)): the callback reports a, the chain's
    # previous task, as its parent; it carries the chord's group ID and is
    # the callback the header names.
    "chain_single_header_chord": (
        {
            "T0": (_T, None, ["group:T2"]),
            "group:T2": (_C, "T0", ["T1", "T3"]),
            "T1": (_T, "group:T2", []),
            "T3": (_T, "group:T2", []),
        },
        {"group:T2": "T3"},
    ),
    "chain_chord": (
        {
            "T0": (_T, None, ["group:T3"]),
            "group:T3": (_C, "T0", ["T1", "T2", "T4"]),
            "T1": (_T, "group:T3", []),
            "T2": (_T, "group:T3", []),
            "T4": (_T, "group:T3", []),
        },
        {"group:T3": "T4"},
    ),
    # group(chain(a, b), c): c has no parent (a top-level member) and b's
    # parent a has one child, so a is a chain link inside the group.
    "group_chain_and_task": (
        {
            "group:T3": (_G, None, ["T0", "T1"]),
            "T0": (_T, "group:T3", ["T2"]),
            "T1": (_T, "group:T3", []),
            "T2": (_T, "T0", []),
        },
        {},
    ),
    "chord_with_chain_body": (
        _chord_shape(["T0", "T1"], "T4", ["T3"], "T2"),
        {"group:T2": "T4"},
    ),
}


def _labels(events: list[dict[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for event in events:
        for field in _LABELLED_FIELDS:
            value = event[field]
            if value is not None and value not in labels:
                labels[value] = f"T{len(labels)}"
    return labels


def _label(labels: dict[str, str], node_id: str | None) -> str | None:
    if node_id is None:
        return None
    if node_id.startswith("group:"):
        return f"group:{labels[node_id.removeprefix('group:')]}"
    return labels[node_id]


def _replay(events: list[dict[str, Any]]) -> TaskGraph:
    graph = TaskGraph()
    for event in events:
        graph.add_event(TaskEvent.model_validate(event))
        assert_graph_invariants(graph)
    return graph


def _labelled_shape(graph: TaskGraph, labels: dict[str, str]) -> _Shape:
    shape: _Shape = {}
    for node_id, node in graph.nodes.items():
        children = sorted(str(_label(labels, c)) for c in node.children)
        shape[str(_label(labels, node_id))] = (
            node.node_type,
            _label(labels, node.parent_id),
            children,
        )
    return shape


def _orders(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    orders = [list(events)]
    for seed in range(25):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        orders.append(shuffled)
    return orders


@pytest.mark.parametrize("flow", sorted(_EXPECTED))
def test_replay_celery_flow_in_any_order_builds_expected_graph(flow: str) -> None:
    events = _CAPTURES[flow]
    labels = _labels(events)
    expected_shape, expected_callbacks = _EXPECTED[flow]
    for order in _orders(events):
        graph = _replay(order)

        assert _labelled_shape(graph, labels) == expected_shape
        callbacks = {
            str(_label(labels, node_id)): _label(labels, node.chord_id)
            for node_id, node in graph.nodes.items()
            if node.node_type == NodeType.CHORD
        }
        assert callbacks == expected_callbacks
        assert len(graph.root_ids) == 1
        if flow in ("chord_failed_header", "chord_failed_header_subtask"):
            assert graph.nodes[f"group:{_id(labels, 'T2')}"].state == TaskState.FAILURE
        else:
            assert {node.state for node in graph.nodes.values()} == {TaskState.SUCCESS}


def _id(labels: dict[str, str], label: str) -> str:
    return next(node_id for node_id, lab in labels.items() if lab == label)


@pytest.mark.parametrize("flow", sorted(_CAPTURES))
def test_replay_celery_flow_with_eviction_keeps_graph_invariants(flow: str) -> None:
    for order in _orders(_CAPTURES[flow])[:6]:
        store = GraphStore(max_nodes=4)
        for event in order:
            store.add_event(TaskEvent.model_validate(event))
            assert_graph_invariants(store._graph)


def test_replay_chain_body_with_middle_task_last_finds_callback() -> None:
    """The chain body's middle task arriving last completes the tail walk.

    Until then the walk from the stamped last task (T7) stops at the missing
    T6, and the header's subtask (T4) must not be drawn as the callback.
    """
    events = _CAPTURES["chain_chord_header_subtask_add_add"]
    labels = _labels(events)
    middle = _id(labels, "T6")
    rest = [e for e in events if e["task_id"] != middle]
    late = [e for e in events if e["task_id"] == middle]
    graph = _replay(rest)
    chord_node = graph.nodes[f"group:{_id(labels, 'T3')}"]
    assert chord_node.chord_id is None

    for event in late:
        graph.add_event(TaskEvent.model_validate(event))
        assert_graph_invariants(graph)

    assert _label(labels, chord_node.chord_id) == "T5"
    assert (
        _labelled_shape(graph, labels)
        == _EXPECTED["chain_chord_header_subtask_add_add"][0]
    )


def _final_ancestors(graph: TaskGraph, node_id: str) -> set[str]:
    ancestors: set[str] = set()
    parent = graph.nodes[node_id].parent_id
    while parent is not None and parent not in ancestors:
        ancestors.add(parent)
        parent = graph.nodes[parent].parent_id
    return ancestors


@pytest.mark.parametrize("flow", sorted(_EXPECTED))
def test_replay_celery_flow_never_shows_a_node_off_its_final_path(flow: str) -> None:
    """After every event (in arrival order), each node is a root, sits under
    the parent its events reported, or under a node it ends up below: no
    transient detours, such as a chain's first task drawn inside the group
    it started for a while."""
    events = _CAPTURES[flow]
    final = _replay(events)
    graph = TaskGraph()
    for event in events:
        graph.add_event(TaskEvent.model_validate(event))
        for node_id, node in graph.nodes.items():
            if node.parent_id is None or node_id not in final.nodes:
                continue
            allowed = _final_ancestors(final, node_id) | {node.reported_parent_id}
            assert node.parent_id in allowed, (
                node_id,
                node.parent_id,
                final.nodes[node_id].parent_id,
            )
