"""Replay events captured from real Celery canvas workflows.

``tests/data/celery_flows.json`` holds, per workflow, the events stemtrace's
signal handlers published from an in-process Celery 5.6 worker, in arrival
order (regenerate with ``tests/data/capture_celery_flows.py``). Each one is
replayed as captured and in seeded shuffles: the graph must keep its
invariants after every event and end in the same expected shape.

Task and group IDs are UUIDs, so expected graphs are written by structure:
names and topology (see ``_structure``), not IDs or arrival order. A
capture regenerated with the script must build the same structure; tests
that need a particular task pick it by its role in the events or the
final graph.
"""

import json
import random
from collections import Counter
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph, TaskNode
from stemtrace.server.store import GraphStore
from tests.graph_invariants import assert_graph_invariants

_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_G, _C, _T = NodeType.GROUP, NodeType.CHORD, NodeType.TASK


def _structure(graph: TaskGraph) -> str:
    """The graph's names and topology, independent of IDs and arrival order.

    A task is its name without the ``flows.`` prefix, followed by its
    children in parentheses; a GROUP or CHORD node is ``group{...}`` or
    ``chord{...}`` around its children. A chord's callback is marked ``*``
    (``*?`` if it isn't one of the chord's children). Siblings are sorted,
    graphs are joined with `` | ``.
    """

    def tree(node_id: str) -> str:
        node = graph.nodes[node_id]
        if node.node_type == _T:
            parts = sorted(tree(c) for c in node.children)
            head = node.name.removeprefix("flows.")
            return f"{head}({', '.join(parts)})" if parts else head
        callback = node.chord_id if node.node_type == _C else None
        parts = sorted(("*" if c == callback else "") + tree(c) for c in node.children)
        if callback is not None and callback not in node.children:
            parts.append("*?")
        return f"{node.node_type.value.lower()}{{{', '.join(parts)}}}"

    return " | ".join(sorted(tree(root) for root in graph.root_ids))


_EXPECTED: dict[str, str] = {
    "chain": "add(add(add))",
    "group": "group{add, add, add}",
    "chord": "chord{*tsum, add, add, add}",
    "chain_a_group_tsum_add": "add(chord{*tsum(add), add, add})",
    "chain_a_group_tsum_add_add": "add(chord{*tsum(add(add)), add, add})",
    "chain_group_nested": "add(group{add, add})",
    "group_spawned_in_task": "spawn_group(group{add, add, add})",
    "chain_group_tsum_add": "chord{*tsum(add), add, add}",
    "chord_retried_header": "chord{*tsum, add, flaky_add}",
    "chord_in_chord_body": "chord{*chord{*tsum, tsum, tsum}, add, add}",
    # group(chain(a1, b1), chain(a2, b2)): Celery stamps the group only on
    # b1 and b2; a1 and a2 are chain links drawn inside the group.
    "group_of_chains": "group{add(add), add(add)}",
    # A header's own subtask and the callback both hang from a header; only the callback the headers name is drawn after the chord.
    "chord_header_subtask": "chord{*tsum, add, add_with_subtask(add)}",
    "chord_header_fanout_group": "chord{*tsum, add, spawn_group(group{add, add})}",
    # The body never runs: nothing shows a callback, so none is drawn.
    "chord_failed_header": "chord{add, fail}",
    # A header's subtask is no evidence: with the body never run, no callback.
    "chord_failed_header_subtask": "chord{add_with_subtask(add), fail}",
    # The header's subtask must not become the callback, whichever of
    # the chain body's tasks arrives last.
    "chain_chord_header_subtask_add_add": "add(chord{*tsum(add(add)), add, add_with_subtask(add)})",
    # chain(a, group(chain(b1, c1), chain(b2, c2))): b1 and b2 are links.
    "chain_group_of_chains": "add(group{add(add), add(add)})",
    # add_with_subtask also spawned a task, so it isn't pulled into the
    # group as a chain link; the other chain's first task is.
    "group_of_chains_link_spawns": "add_with_subtask(add, group{add, add(add)})",
    # Result backends without chord support: Celery's chord_unlock task
    # fires the body, which reports it as its parent.
    "chord@chord_unlock": "chord{*tsum, add, add, add, celery.chord_unlock}",
    "chain_a_group_tsum_add@chord_unlock": "add(chord{*tsum(add), add, add, celery.chord_unlock})",
    "chord_with_chain_body@chord_unlock": "chord{*tsum(add), add, add, celery.chord_unlock}",
    # chain(a, chord([b], tsum)): the callback reports a, the chain's
    # previous task, as its parent; it carries the chord's group ID and is
    # the callback the header names.
    "chain_single_header_chord": "add(chord{*tsum, add})",
    "chain_chord": "add(chord{*tsum, add, add})",
    # group(chain(a, b), c): c has no parent (a top-level member) and b's
    # parent a has one child, so a is a chain link inside the group.
    "group_chain_and_task": "group{add, add(add)}",
    # chord([a, b], group(c, d)): the headers name the chord's own group ID
    # and both body tasks carry it; the body is drawn as a GROUP after the
    # chord. chain(group(a, b), group(c, d)) is the same canvas to Celery.
    "chord_group_body": "chord{*group{tsum, tsum}, add, add}",
    "chain_group_group": "chord{*group{tsum, tsum}, add, add}",
    # task.replace(): Celery gives the replacement's last task the replaced
    # task's ID, whose events then report a parent from the replacement.
    # Only the task's own events report its parent, so it stays the root.
    "replace_chain": "replace_with_chain(add)",
    # Replacing with a group makes a chord whose callback (accumulate)
    # reuses the replaced task's ID: it can't also be drawn after the chord.
    "replace_group": "replace_with_group(chord{add, add})",
    "chain_replace_group_tsum": "replace_with_group(chord{*tsum, add, add})",
    "group_of_chains_link_spawns@publish_headers": "add_with_subtask(add, group{add, add(add)})",
    # The same with the worker in its own process: it announces the
    # replacement's last task, under the replaced task's ID, with a second
    # attempt-0 PENDING (another name, a parent from the replacement).
    "replace_chain@worker_process": "replace_with_chain(add)",
    "replace_group@worker_process": "replace_with_group(chord{add, add})",
    "chain_replace_group_tsum@worker_process": "replace_with_group(chord{*tsum, add, add})",
    # Captured with Celery 5.2 (the lowest supported version). A group body
    # gets its own group ID there, which the headers name.
    "chord_group_body@celery5.2": "chord{*group{tsum, tsum}, add, add}",
    "chain_group_group@celery5.2": "chord{*group{tsum, tsum}, add, add}",
    "chain_chord@celery5.2": "add(chord{*tsum, add, add})",
    # On Celery 5.2 the headers of a chord with a chain body, or of
    # chain(group, task), carry no chord callback ID: nothing shows they are
    # a chord, so they stay a GROUP with the chain hanging from its last
    # member (as on main).
    "chain_group_tsum_add@celery5.2": "group{add, add(tsum(add))}",
    "chord_with_chain_body@celery5.2": "group{add, add(tsum(add))}",
    "chain_a_group_tsum_add@celery5.2": "add(group{add, add(tsum(add))})",
    # self.replace(add.s()): the single replacement task takes over the ID
    # and reports it as its own parent.
    "replace_task@worker_process": "replace_with_task",
    # paginate(0) replaces itself with chain(paginate(1), paginate(2)): the
    # same task name, so only the structure shows the replacement.
    "replace_same_name@worker_process": "paginate(paginate)",
    # chain(add, paginate(0)): the replaced task has a parent of its own,
    # and its replacement's events report another (paginate(1)).
    "chain_replace_same_name@worker_process": "add(paginate(paginate))",
    # paginate_to(0) replaces itself with a chain of 3 or 4 pages: the
    # replacement's last task reports a grandchild (or deeper) as parent.
    "chain_replace3@worker_process": "add(paginate_to(paginate_to(paginate_to)))",
    "chain_replace4@worker_process": (
        "add(paginate_to(paginate_to(paginate_to(paginate_to))))"
    ),
    # Replaced by a group inside a chain: celery.accumulate, the chord's
    # callback, reuses the ID and reports the last header as parent.
    "chain_replace_group@worker_process": "add(replace_with_group(chord{add, add}))",
    "chord_header_replace_same_name@worker_process": "chord{*tsum, add, paginate(paginate)}",
    # add.apply_async(parent_id=<an earlier add>) from a client: Celery
    # defaults root_id to the task's own ID there, so the task claims to be
    # a root with a parent.
    "client_explicit_parent": "add(add)",
    # The same from inside a task (send_with_parent): the root is the
    # sender's.
    "worker_explicit_parent": "add(add) | send_with_parent",
    "chord_with_chain_body": "chord{*tsum(add), add, add}",
}


def _replay(events: list[dict[str, Any]]) -> TaskGraph:
    graph = TaskGraph()
    for event in events:
        graph.add_event(TaskEvent.model_validate(event))
        assert_graph_invariants(graph)
    return graph


def _orders(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    orders = [list(events)]
    for seed in range(25):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        orders.append(shuffled)
    return orders


@pytest.mark.parametrize("flow", sorted(_EXPECTED))
def test_replay_celery_flow_in_any_order_builds_expected_graph(flow: str) -> None:
    for order in _orders(_CAPTURES[flow]):
        graph = _replay(order)

        assert _structure(graph) == _EXPECTED[flow]
        if flow in ("chord_failed_header", "chord_failed_header_subtask"):
            (chord_node,) = _nodes_of_type(graph, _C)
            assert chord_node.state == TaskState.FAILURE
        else:
            assert {node.state for node in graph.nodes.values()} == {TaskState.SUCCESS}


def _nodes_of_type(graph: TaskGraph, node_type: NodeType) -> list[TaskNode]:
    return [node for node in graph.nodes.values() if node.node_type == node_type]


@pytest.mark.parametrize("flow", sorted(_CAPTURES))
def test_replay_celery_flow_with_eviction_keeps_graph_invariants(flow: str) -> None:
    for order in _orders(_CAPTURES[flow])[:6]:
        store = GraphStore(max_nodes=4)
        for event in order:
            store.add_event(TaskEvent.model_validate(event))
            assert_graph_invariants(store._graph)


def test_replay_chain_body_with_middle_task_last_finds_callback() -> None:
    """The chain body's middle task arriving last completes the tail walk.

    Until then the walk from the stamped last task stops at the missing
    middle one, and the header's subtask must not be drawn as the callback.
    """
    events = _CAPTURES["chain_chord_header_subtask_add_add"]
    (final_chord,) = _nodes_of_type(_replay(events), _C)
    callback = final_chord.chord_id
    assert callback is not None
    (middle,) = _replay(events).nodes[callback].children
    rest = [e for e in events if e["task_id"] != middle]
    late = [e for e in events if e["task_id"] == middle]
    graph = _replay(rest)
    chord_node = graph.nodes[final_chord.task_id]
    assert chord_node.chord_id is None

    for event in late:
        graph.add_event(TaskEvent.model_validate(event))
        assert_graph_invariants(graph)

    assert graph.nodes[final_chord.task_id].chord_id == callback
    assert _structure(graph) == _EXPECTED["chain_chord_header_subtask_add_add"]


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
    spawner = next(
        e["task_id"] for e in events if e["name"] == "flows.add_with_subtask"
    )
    (subtask,) = {
        e["task_id"]
        for e in events
        if e["parent_id"] == spawner
        and not any(o["group_id"] for o in events if o["task_id"] == e["task_id"])
    }
    delayed = [e for e in events if e["task_id"] == subtask and e["state"] != "PENDING"]
    order = [e for e in events if e not in delayed] + delayed
    final = _replay(events)
    graph = TaskGraph()
    for event in order:
        graph.add_event(TaskEvent.model_validate(event))
        _assert_only_accepted_transients(graph, final)
    assert _structure(graph) == _EXPECTED["group_of_chains_link_spawns@publish_headers"]


_REPLACE_FLOWS = (
    "replace_chain@worker_process",
    "replace_group@worker_process",
    "chain_replace_group_tsum@worker_process",
    "replace_task@worker_process",
    "replace_same_name@worker_process",
    "chord_header_replace_same_name@worker_process",
    "chain_replace_same_name@worker_process",
    "chain_replace3@worker_process",
    "chain_replace4@worker_process",
    "chain_replace_group@worker_process",
)


def _replaced_task(events: list[dict[str, Any]]) -> tuple[str, str | None]:
    """The replaced task's ID and the parent its own events report.

    A worker in its own process announces the replacement's last task, which
    reuses the ID, with a second attempt-0 PENDING. The task's first event
    in a capture is its sender's PENDING.
    """
    pending = Counter(
        e["task_id"] for e in events if e["state"] == "PENDING" and e["retries"] == 0
    )
    (task_id,) = [t for t, count in pending.items() if count > 1]
    parent = next(e["parent_id"] for e in events if e["task_id"] == task_id)
    return task_id, parent


@pytest.mark.parametrize("flow", sorted(_REPLACE_FLOWS))
@pytest.mark.parametrize("skew_s", [-3600, -1, -0.05, -0.025, 0.025, 1, 3600])
def test_replay_replaced_task_with_skewed_worker_clock_keeps_identity(
    flow: str, skew_s: float
) -> None:
    """The replacement's events for the replaced task's ID come from another
    host: with its clock skewed either way, the replaced task keeps its name
    and place, in any order."""
    events = _CAPTURES[flow]
    original, first_parent = _replaced_task(events)
    name = next(e["name"] for e in events if e["task_id"] == original)
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
        assert _structure(graph) == _EXPECTED[flow]


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


def _without_own_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The capture as a server that started mid-flow sees it: the replaced
    task's own events are missing, only its replacement's remain."""
    original, first_parent = _replaced_task(events)
    return [
        e
        for e in events
        if e["task_id"] != original or _is_replacement_event(e, original, first_parent)
    ]


@pytest.mark.parametrize(
    ("flow", "expected"),
    [
        ("replace_chain@worker_process", "add(add)"),
        ("replace_same_name@worker_process", "paginate(paginate)"),
    ],
)
def test_replay_replaced_task_when_server_missed_its_start(
    flow: str, expected: str
) -> None:
    """A server that starts mid-flow sees only the replacement's events for
    a replaced root task: the node shows the replacement's name and stays
    the root, with the replacement's first task under it, in any order."""
    for order in _orders(_without_own_events(_CAPTURES[flow])):
        assert _structure(_replay(order)) == expected


def test_replay_non_root_replaced_task_when_server_missed_its_start() -> None:
    """chain(add, paginate(0)) without the replaced task's own events: nothing
    left links it to add, which is a graph of its own. Its replacement's
    events report the replacement's first task as parent, which reports it
    in turn (an ID reuse cycle with nothing to tell the original): the two
    make one graph, whose top is the task with the larger ID (it gives up
    its parent), in any order."""
    events = _without_own_events(_CAPTURES["chain_replace_same_name@worker_process"])
    original, _ = _replaced_task(_CAPTURES["chain_replace_same_name@worker_process"])
    (first,) = {e["task_id"] for e in events if e["parent_id"] == original}
    for order in _orders(events):
        graph = _replay(order)

        assert _structure(graph) == "add | paginate(paginate)"
        assert graph.nodes[min(original, first)].parent_id == max(original, first)
