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
from datetime import datetime, timedelta
from itertools import combinations
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
    # chord([a, b], group(c, d)): the headers name the chord's own group ID
    # and both body tasks carry it; the body is drawn as a GROUP after the
    # chord. chain(group(a, b), group(c, d)) is the same canvas to Celery.
    "chord_group_body": (
        {
            "group:T1": (_C, None, ["T0", "T2", "group:T1:body"]),
            "T0": (_T, "group:T1", []),
            "T2": (_T, "group:T1", []),
            "group:T1:body": (_G, "group:T1", ["T3", "T4"]),
            "T3": (_T, "group:T1:body", []),
            "T4": (_T, "group:T1:body", []),
        },
        {"group:T1": "group:T1:body"},
    ),
    "chain_group_group": (
        {
            "group:T1": (_C, None, ["T0", "T2", "group:T1:body"]),
            "T0": (_T, "group:T1", []),
            "T2": (_T, "group:T1", []),
            "group:T1:body": (_G, "group:T1", ["T3", "T4"]),
            "T3": (_T, "group:T1:body", []),
            "T4": (_T, "group:T1:body", []),
        },
        {"group:T1": "group:T1:body"},
    ),
    # task.replace(): Celery gives the replacement's last task the replaced
    # task's ID (T0), whose events then report a parent from the
    # replacement. Only events under the task's first name report its
    # parent, so T0 stays the root.
    "replace_chain": (
        {"T0": (_T, None, ["T1"]), "T1": (_T, "T0", [])},
        {},
    ),
    # Replacing with a group makes a chord whose callback (accumulate)
    # reuses T0's ID: it can't also be drawn after the chord.
    "replace_group": (
        {
            "T0": (_T, None, ["group:T2"]),
            "group:T2": (_C, "T0", ["T1", "T3"]),
            "T1": (_T, "group:T2", []),
            "T3": (_T, "group:T2", []),
        },
        {"group:T2": None},
    ),
    "chain_replace_group_tsum": (
        {
            "T0": (_T, None, ["group:T2"]),
            "group:T2": (_C, "T0", ["T1", "T3", "T4"]),
            "T1": (_T, "group:T2", []),
            "T3": (_T, "group:T2", []),
            "T4": (_T, "group:T2", []),
        },
        {"group:T2": "T4"},
    ),
    "group_of_chains_link_spawns@publish_headers": (
        {
            "T0": (_T, None, ["T2", "group:T4"]),
            "T2": (_T, "T0", []),
            "group:T4": (_G, "T0", ["T1", "T3"]),
            "T3": (_T, "group:T4", []),
            "T1": (_T, "group:T4", ["T5"]),
            "T5": (_T, "T1", []),
        },
        {},
    ),
    # The same with the worker in its own process: it announces the
    # replacement's last task, under the replaced task's ID, with a second
    # attempt-0 PENDING (another name, a parent from the replacement).
    "replace_chain@worker_process": (
        {"T0": (_T, None, ["T1"]), "T1": (_T, "T0", [])},
        {},
    ),
    "replace_group@worker_process": (
        {
            "T0": (_T, None, ["group:T2"]),
            "group:T2": (_C, "T0", ["T1", "T3"]),
            "T1": (_T, "group:T2", []),
            "T3": (_T, "group:T2", []),
        },
        {"group:T2": None},
    ),
    "chain_replace_group_tsum@worker_process": (
        {
            "T0": (_T, None, ["group:T2"]),
            "group:T2": (_C, "T0", ["T1", "T3", "T4"]),
            "T1": (_T, "group:T2", []),
            "T3": (_T, "group:T2", []),
            "T4": (_T, "group:T2", []),
        },
        {"group:T2": "T4"},
    ),
    # Captured with Celery 5.2 (the lowest supported version). A group body
    # gets its own group ID there, which the headers name.
    "chord_group_body@celery5.2": (
        {
            "group:T1": (_C, None, ["T0", "T2", "group:T3"]),
            "T0": (_T, "group:T1", []),
            "T2": (_T, "group:T1", []),
            "group:T3": (_G, "group:T1", ["T4", "T5"]),
            "T4": (_T, "group:T3", []),
            "T5": (_T, "group:T3", []),
        },
        {"group:T1": "group:T3"},
    ),
    "chain_group_group@celery5.2": (
        {
            "group:T1": (_C, None, ["T0", "T2", "group:T3"]),
            "T0": (_T, "group:T1", []),
            "T2": (_T, "group:T1", []),
            "group:T3": (_G, "group:T1", ["T4", "T5"]),
            "T4": (_T, "group:T3", []),
            "T5": (_T, "group:T3", []),
        },
        {"group:T1": "group:T3"},
    ),
    "chain_chord@celery5.2": (
        {
            "T0": (_T, None, ["group:T2"]),
            "group:T2": (_C, "T0", ["T1", "T3", "T4"]),
            "T1": (_T, "group:T2", []),
            "T3": (_T, "group:T2", []),
            "T4": (_T, "group:T2", []),
        },
        {"group:T2": "T4"},
    ),
    # On Celery 5.2 the headers of a chord with a chain body, or of
    # chain(group, task), carry no chord callback ID: nothing shows they are
    # a chord, so they stay a GROUP with the chain hanging from its last
    # member (as on main).
    "chain_group_tsum_add@celery5.2": (
        {
            "group:T1": (_G, None, ["T0", "T2"]),
            "T0": (_T, "group:T1", []),
            "T2": (_T, "group:T1", ["T3"]),
            "T3": (_T, "T2", ["T4"]),
            "T4": (_T, "T3", []),
        },
        {},
    ),
    "chord_with_chain_body@celery5.2": (
        {
            "group:T1": (_G, None, ["T0", "T2"]),
            "T0": (_T, "group:T1", []),
            "T2": (_T, "group:T1", ["T3"]),
            "T3": (_T, "T2", ["T4"]),
            "T4": (_T, "T3", []),
        },
        {},
    ),
    "chain_a_group_tsum_add@celery5.2": (
        {
            "T0": (_T, None, ["group:T2"]),
            "group:T2": (_G, "T0", ["T1", "T3"]),
            "T1": (_T, "group:T2", []),
            "T3": (_T, "group:T2", ["T4"]),
            "T4": (_T, "T3", ["T5"]),
            "T5": (_T, "T4", []),
        },
        {},
    ),
    # self.replace(add.s()): the single replacement task takes over the ID
    # and reports it as its own parent.
    "replace_task@worker_process": ({"T0": (_T, None, [])}, {}),
    # paginate(0) replaces itself with chain(paginate(1), paginate(2)): the
    # same task name, so only the structure shows the replacement.
    "replace_same_name@worker_process": (
        {"T0": (_T, None, ["T1"]), "T1": (_T, "T0", [])},
        {},
    ),
    # chain(add, paginate(0)): the replaced task (T1) has a parent of its
    # own, and its replacement's events report another (T2).
    "chain_replace_same_name@worker_process": (
        {"T0": (_T, None, ["T1"]), "T1": (_T, "T0", ["T2"]), "T2": (_T, "T1", [])},
        {},
    ),
    "chord_header_replace_same_name@worker_process": (
        {
            "group:T1": (_C, None, ["T0", "T2", "T3"]),
            "T0": (_T, "group:T1", ["T4"]),
            "T2": (_T, "group:T1", []),
            "T3": (_T, "group:T1", []),
            "T4": (_T, "T0", []),
        },
        {"group:T1": "T3"},
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
    if node_id.startswith("group:") and node_id.endswith(":body"):
        return (
            f"group:{labels[node_id.removeprefix('group:').removesuffix(':body')]}:body"
        )
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


def _assert_only_accepted_transients(graph: TaskGraph, final: TaskGraph) -> None:
    """Accepted transients: a node is a root, under its reported parent (a
    group's: one its members report), or under a node it ends up below; a
    GROUP may still turn into a CHORD; a chord's callback is not drawn yet,
    or is (part of) its final callback. Never a node in a container it
    doesn't end up in, never a wrong callback."""
    for node_id, node in graph.nodes.items():
        end = final.nodes.get(node_id)
        if end is None:
            continue
        ancestors = _final_ancestors(final, node_id)
        if node.parent_id is not None:
            reported = {node.reported_parent_id}
            if node.node_type != _T:
                reported |= {
                    graph.nodes[c].reported_parent_id
                    for c in node.children
                    if c in graph.nodes
                }
            allowed = ancestors | reported
            assert node.parent_id in allowed, (node_id, node.parent_id, end.parent_id)
        if node.node_type != end.node_type:
            assert (node.node_type, end.node_type) == (_G, _C), (
                node_id,
                node.node_type,
            )
        if node.chord_id is not None and node.chord_id != end.chord_id:
            callback = node.chord_id
            assert end.chord_id is not None, (node_id, callback)
            assert callback in final.nodes, (node_id, callback)
            assert end.chord_id in _final_ancestors(final, callback), (
                node_id,
                callback,
            )


def _from_this_release(events: list[dict[str, Any]]) -> bool:
    """Whether a capture's senders put parent and group IDs on PENDING.

    Older stemtrace senders (task_sent) never did; captures made with them
    stay, as servers receive events from such workers too.
    """
    return any(
        e["state"] == "PENDING" and (e["parent_id"] or e["group_id"]) for e in events
    )


def _causal_orders(
    events: list[dict[str, Any]], count: int
) -> list[list[dict[str, Any]]]:
    """Random arrival orders concurrent workers can produce.

    Each process's events keep their order: a task's worker events, with
    the PENDINGs of the tasks it sends; the client's PENDINGs. A task runs
    once its sender published it, so its worker events come after what the
    sender emitted before that - but possibly before its own PENDING
    (after_task_publish fires once the broker accepted the message, and a
    fast worker can report first). Otherwise processes interleave freely.
    """
    task_ids = {e["task_id"] for e in events}

    def producer(event: dict[str, Any]) -> str:
        if event["state"] != "PENDING":
            return event["task_id"]
        parent = event["parent_id"]
        return parent if parent in task_ids else "client"

    # A task's worker stream waits until its sender emitted this many events.
    gate: dict[str, tuple[str, int]] = {}
    position: dict[str, int] = {}
    for event in events:
        name = producer(event)
        if event["state"] == "PENDING" and event["task_id"] not in gate:
            gate[event["task_id"]] = (name, position.get(name, 0))
        position[name] = position.get(name, 0) + 1

    orders = []
    for seed in range(count):
        rng = random.Random(seed)
        streams: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            streams.setdefault(producer(event), []).append(event)
        emitted = dict.fromkeys(streams, 0)
        order: list[dict[str, Any]] = []
        while any(streams.values()):
            ready = [
                name
                for name, stream in streams.items()
                if stream
                and (
                    stream[0]["state"] == "PENDING"
                    or name not in gate
                    or emitted[gate[name][0]] >= gate[name][1]
                )
            ]
            name = rng.choice(ready)
            order.append(streams[name].pop(0))
            emitted[name] += 1
        orders.append(order)
    return orders


@pytest.mark.parametrize("flow", sorted(_EXPECTED))
def test_replay_celery_flow_shows_only_accepted_transients(flow: str) -> None:
    """After every event the graph only differs from its final shape in
    accepted ways (see above): in arrival order, and for captures from this
    release's senders also in causal orders concurrent workers can produce.
    (With older senders, whose PENDING carries no parent, a parent's other
    children can show up late enough for it to look like a chain link.)"""
    events = _CAPTURES[flow]
    final = _replay(events)
    orders = [events]
    if _from_this_release(events):
        orders += _causal_orders(events, 8)
    for order in orders:
        graph = TaskGraph()
        for event in order:
            graph.add_event(TaskEvent.model_validate(event))
            _assert_only_accepted_transients(graph, final)


def test_replay_chain_link_spawning_task_with_delayed_subtask() -> None:
    """group(chain(a, b), chain(c, d)) where a also sends a subtask whose
    worker events arrive last: the PENDING of a's subtask carries a as its
    parent, so a never looks like a one-child chain link."""
    events = _CAPTURES["group_of_chains_link_spawns@publish_headers"]
    labels = _labels(events)
    subtask = _id(labels, "T2")
    delayed = [e for e in events if e["task_id"] == subtask and e["state"] != "PENDING"]
    order = [e for e in events if e not in delayed] + delayed
    final = _replay(events)
    graph = TaskGraph()
    for event in order:
        graph.add_event(TaskEvent.model_validate(event))
        _assert_only_accepted_transients(graph, final)
    assert (
        _labelled_shape(graph, labels)
        == _EXPECTED["group_of_chains_link_spawns@publish_headers"][0]
    )


# Flow -> label of the replaced task.
_REPLACE_FLOWS = {
    "replace_chain@worker_process": "T0",
    "replace_group@worker_process": "T0",
    "chain_replace_group_tsum@worker_process": "T0",
    "replace_task@worker_process": "T0",
    "replace_same_name@worker_process": "T0",
    "chord_header_replace_same_name@worker_process": "T0",
    "chain_replace_same_name@worker_process": "T1",
}


@pytest.mark.parametrize("flow", sorted(_REPLACE_FLOWS))
@pytest.mark.parametrize("skew_s", [-3600, -1, -0.025, 0.025, 1, 3600])
def test_replay_replaced_task_with_skewed_worker_clock_keeps_identity(
    flow: str, skew_s: float
) -> None:
    """The replacement's events for the replaced task's ID come from another
    host: with its clock skewed either way, the replaced task keeps its name
    and place, in any order."""
    events = _CAPTURES[flow]
    labels = _labels(events)
    original = _id(labels, _REPLACE_FLOWS[flow])
    name = next(e["name"] for e in events if e["task_id"] == original)
    first_parent = next(e["parent_id"] for e in events if e["task_id"] == original)
    skewed = []
    for event in events:
        if _is_replacement_event(event, original, first_parent):
            shifted = datetime.fromisoformat(
                event["timestamp"].replace("Z", "+00:00")
            ) + timedelta(seconds=skew_s)
            event = {**event, "timestamp": shifted.isoformat()}
        skewed.append(event)
    for order in _orders(skewed):
        graph = _replay(order)

        assert graph.nodes[original].name == name
        assert _labelled_shape(graph, labels) == _EXPECTED[flow][0]


def _graph_shape(
    graph: TaskGraph,
) -> dict[str, tuple[str | None, list[str], str | None]]:
    return {
        node_id: (node.parent_id, sorted(node.children), node.chord_id)
        for node_id, node in graph.nodes.items()
    }


@pytest.mark.parametrize("flow", sorted(_CAPTURES))
def test_remove_tasks_matches_rebuild_without_them(flow: str) -> None:
    """Removing any one or two tasks of a real capture leaves the graph that
    the remaining events build, in arrival order and two shuffles."""
    events = _CAPTURES[flow]
    task_ids = sorted({e["task_id"] for e in events})
    removals = [(t,) for t in task_ids] + list(combinations(task_ids, 2))
    for order in _orders(events)[:3]:
        for removed in removals:
            graph = _replay(order)
            for task_id in removed:
                graph.remove_node(task_id)
            assert_graph_invariants(graph)
            rebuilt = _replay([e for e in order if e["task_id"] not in removed])
            if graph._cycle_broken or rebuilt._cycle_broken:
                continue  # a cycle is still open: where it breaks depends on order
            assert _graph_shape(graph) == _graph_shape(rebuilt), (flow, removed)
            assert sorted(graph.root_ids) == sorted(rebuilt.root_ids), (flow, removed)


def _is_replacement_event(
    event: dict[str, Any], task_id: str, first_parent: str | None = None
) -> bool:
    """A captured event of ``task_id`` sent for its replacement: it reports
    another parent than the task's first event."""
    return event["task_id"] == task_id and event["parent_id"] != first_parent


@pytest.mark.parametrize(
    "flow", ["replace_chain@worker_process", "replace_same_name@worker_process"]
)
def test_replay_replaced_task_when_server_missed_its_start(flow: str) -> None:
    """A server that starts mid-flow sees only the replacement's events for
    the replaced task: the graph is the same, in any order."""
    events = _CAPTURES[flow]
    labels = _labels(events)
    original = _id(labels, "T0")
    late = [
        e
        for e in events
        if e["task_id"] != original or _is_replacement_event(e, original)
    ]
    for order in _orders(late):
        assert _labelled_shape(_replay(order), labels) == _EXPECTED[flow][0]


def test_replay_non_root_replaced_task_when_server_missed_its_start() -> None:
    """Without the replaced task's own events, its replacement's report the
    replacement's previous task as parent, which reports it in turn: an ID
    reuse cycle with nothing to tell the original. The result is stable in
    any order (the larger task ID gives up its parent)."""
    events = _CAPTURES["chain_replace_same_name@worker_process"]
    labels = _labels(events)
    original = _id(labels, "T1")
    late = [
        e
        for e in events
        if e["task_id"] != original
        or _is_replacement_event(e, original, _id(labels, "T0"))
    ]
    shapes = {
        repr(sorted(_labelled_shape(_replay(o), labels).items())) for o in _orders(late)
    }
    assert len(shapes) == 1
