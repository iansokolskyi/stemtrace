"""Replay events captured from real Celery canvas workflows.

``tests/data/celery_flows.json`` holds, per workflow, the events stemtrace's
signal handlers published from an in-process Celery 5.6 worker, in arrival
order (regenerate with ``tests/data/capture_celery_flows.py``). Each one is
replayed as captured and in seeded shuffles: the graph must keep its
invariants after every event and end in the same expected shape.

Task and group IDs are UUIDs, so expected graphs are written by structure:
names, topology and roles (see ``_structure``), not IDs or arrival order. A
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
    """The graph's names, topology and roles, independent of IDs and order.

    A task is its name without the ``flows.`` prefix, followed by its
    children in parentheses; a GROUP or CHORD node is ``group{...}`` or
    ``chord{...}`` around its children. Siblings are sorted, graphs are
    joined with `` | ``. Roles taken from the task's own events pin where
    same-named tasks sit:

    * ``^``: a replaced task (its ID announced twice, see ``_is_replaced``);
    * ``+``: a member of the container it sits in (it carries that group's
      ID; a chain link pulled into the group doesn't);
    * ``*``: the chord's callback, when it is the task its headers name, or
      one carrying the chord's group ID without naming a callback (Celery
      stamps it on a chain body's last task); ``*>`` when such a task is
      further down the callback's chain (a chain body, drawn from its first
      task); ``*!`` for any other; ``*?`` when the callback isn't among the
      chord's children.
    """

    def tree(node_id: str) -> str:
        node = graph.nodes[node_id]
        if node.node_type == _T:
            parts = sorted(tree(c) for c in node.children)
            head = ("^" if _is_replaced(node) else "") + node.name.removeprefix(
                "flows."
            )
            return f"{head}({', '.join(parts)})" if parts else head
        callback = node.chord_id if node.node_type == _C else None
        named = _named_callbacks(graph, node)
        parts = []
        for child_id in node.children:
            if child_id == callback:
                mark = _callback_mark(graph, child_id, named)
            elif graph.nodes[child_id].group_id == node.group_id:
                mark = "+"
            else:
                mark = ""
            parts.append(mark + tree(child_id))
        if callback is not None and callback not in node.children:
            parts.append("*?")
        return f"{node.node_type.value.lower()}{{{', '.join(sorted(parts))}}}"

    return " | ".join(sorted(tree(root) for root in graph.root_ids))


def _is_replaced(node: TaskNode) -> bool:
    """Whether the ID carries a replaced task and its replacement's last one.

    A worker in its own process announces the reused ID again (a second
    attempt-0 PENDING), and a replacement under another name shows a second
    name.
    """
    pending = [e for e in node.events if e.state == "PENDING" and e.retries == 0]
    return len(pending) > 1 or len({e.name for e in node.events}) > 1


def _named_callbacks(graph: TaskGraph, container: TaskNode) -> set[str]:
    """Tasks the events point out as the container's callback (see above)."""
    group_id = container.group_id
    named = {
        e.chord_callback_id
        for child_id in container.children
        for e in graph.nodes[child_id].events
        if e.chord_callback_id is not None and e.group_id == group_id
    }
    for node_id, node in graph.nodes.items():
        events = node.events
        if any(e.group_id == group_id for e in events) and not any(
            e.chord_callback_id for e in events
        ):
            named.add(node_id)
    return named


def _callback_mark(graph: TaskGraph, node_id: str, named: set[str]) -> str:
    """How a drawn callback relates to the ones its headers name."""

    def is_named(candidate: str) -> bool:
        return any(candidate in (n, f"group:{n}", f"group:{n}:body") for n in named)

    if is_named(node_id):
        return "*"
    below = list(graph.nodes[node_id].children)
    while below:
        current = below.pop()
        if is_named(current):
            return "*>"
        below.extend(graph.nodes[current].children)
    return "*!"


_EXPECTED: dict[str, str] = {
    "chain": "add(add(add))",
    "group": "group{+add, +add, +add}",
    "chord": "chord{*tsum, +add, +add, +add}",
    "chain_a_group_tsum_add": "add(chord{*>tsum(add), +add, +add})",
    "chain_a_group_tsum_add_add": "add(chord{*>tsum(add(add)), +add, +add})",
    "chain_group_nested": "add(group{+add, +add})",
    "group_spawned_in_task": "spawn_group(group{+add, +add, +add})",
    "chain_group_tsum_add": "chord{*>tsum(add), +add, +add}",
    "chord_retried_header": "chord{*tsum, +add, +flaky_add}",
    "chord_in_chord_body": "chord{*chord{*tsum, +tsum, +tsum}, +add, +add}",
    # group(chain(a1, b1), chain(a2, b2)): Celery stamps the group only on
    # b1 and b2; a1 and a2 are chain links drawn inside the group.
    "group_of_chains": "group{add(add), add(add)}",
    # A header's own subtask and the callback both hang from a header; only the callback the headers name is drawn after the chord.
    "chord_header_subtask": "chord{*tsum, +add, +add_with_subtask(add)}",
    "chord_header_fanout_group": "chord{*tsum, +add, +spawn_group(group{+add, +add})}",
    # The body never runs: nothing shows a callback, so none is drawn.
    "chord_failed_header": "chord{+add, +fail}",
    # A header's subtask is no evidence: with the body never run, no callback.
    "chord_failed_header_subtask": "chord{+add_with_subtask(add), +fail}",
    # The header's subtask must not become the callback, whichever of
    # the chain body's tasks arrives last.
    "chain_chord_header_subtask_add_add": "add(chord{*>tsum(add(add)), +add, +add_with_subtask(add)})",
    # chain(a, group(chain(b1, c1), chain(b2, c2))): b1 and b2 are links.
    "chain_group_of_chains": "add(group{add(add), add(add)})",
    # add_with_subtask also spawned a task, so it isn't pulled into the
    # group as a chain link; the other chain's first task is.
    "group_of_chains_link_spawns": "add_with_subtask(add, group{+add, add(add)})",
    # Result backends without chord support: Celery's chord_unlock task
    # fires the body, which reports it as its parent.
    "chord@chord_unlock": "chord{*tsum, +add, +add, +add, celery.chord_unlock}",
    "chain_a_group_tsum_add@chord_unlock": "add(chord{*>tsum(add), +add, +add, celery.chord_unlock})",
    "chord_with_chain_body@chord_unlock": "chord{*>tsum(add), +add, +add, celery.chord_unlock}",
    # chain(a, chord([b], tsum)): the callback reports a, the chain's
    # previous task, as its parent; it carries the chord's group ID and is
    # the callback the header names.
    "chain_single_header_chord": "add(chord{*tsum, +add})",
    "chain_chord": "add(chord{*tsum, +add, +add})",
    # group(chain(a, b), c): c has no parent (a top-level member) and b's
    # parent a has one child, so a is a chain link inside the group.
    "group_chain_and_task": "group{+add, add(add)}",
    # chord([a, b], group(c, d)): the headers name the chord's own group ID
    # and both body tasks carry it; the body is drawn as a GROUP after the
    # chord. chain(group(a, b), group(c, d)) is the same canvas to Celery.
    "chord_group_body": "chord{*group{+tsum, +tsum}, +add, +add}",
    "chain_group_group": "chord{*group{+tsum, +tsum}, +add, +add}",
    # task.replace(): Celery gives the replacement's last task the replaced
    # task's ID, whose events then report a parent from the replacement.
    # Only the task's own events report its parent, so it stays the root.
    "replace_chain": "^replace_with_chain(add)",
    # Replacing with a group makes a chord whose callback (accumulate)
    # reuses the replaced task's ID: it can't also be drawn after the chord.
    "replace_group": "^replace_with_group(chord{+add, +add})",
    "chain_replace_group_tsum": "^replace_with_group(chord{*tsum, +add, +add})",
    "group_of_chains_link_spawns@publish_headers": "add_with_subtask(add, group{+add, add(add)})",
    # The same with the worker in its own process: it announces the
    # replacement's last task, under the replaced task's ID, with a second
    # attempt-0 PENDING (another name, a parent from the replacement).
    "replace_chain@worker_process": "^replace_with_chain(add)",
    "replace_group@worker_process": "^replace_with_group(chord{+add, +add})",
    "chain_replace_group_tsum@worker_process": "^replace_with_group(chord{*tsum, +add, +add})",
    # Captured with Celery 5.2 (the lowest supported version). A group body
    # gets its own group ID there, which the headers name.
    "chord_group_body@celery5.2": "chord{*group{+tsum, +tsum}, +add, +add}",
    "chain_group_group@celery5.2": "chord{*group{+tsum, +tsum}, +add, +add}",
    "chain_chord@celery5.2": "add(chord{*tsum, +add, +add})",
    # On Celery 5.2 the headers of a chord with a chain body, or of
    # chain(group, task), carry no chord callback ID: nothing shows they are
    # a chord, so they stay a GROUP with the chain hanging from its last
    # member (as on main).
    "chain_group_tsum_add@celery5.2": "group{+add, +add(tsum(add))}",
    "chord_with_chain_body@celery5.2": "group{+add, +add(tsum(add))}",
    "chain_a_group_tsum_add@celery5.2": "add(group{+add, +add(tsum(add))})",
    # self.replace(add.s()): the single replacement task takes over the ID
    # and reports it as its own parent.
    "replace_task@worker_process": "^replace_with_task",
    # paginate(0) replaces itself with chain(paginate(1), paginate(2)): the
    # same task name, so only the structure shows the replacement.
    "replace_same_name@worker_process": "^paginate(paginate)",
    # chain(add, paginate(0)): the replaced task has a parent of its own,
    # and its replacement's events report another (paginate(1)).
    "chain_replace_same_name@worker_process": "add(^paginate(paginate))",
    # paginate_to(0) replaces itself with a chain of 3 or 4 pages: the
    # replacement's last task reports a grandchild (or deeper) as parent.
    "chain_replace3@worker_process": "add(^paginate_to(paginate_to(paginate_to)))",
    "chain_replace4@worker_process": (
        "add(^paginate_to(paginate_to(paginate_to(paginate_to))))"
    ),
    # Replaced by a group inside a chain: celery.accumulate, the chord's
    # callback, reuses the ID and reports the last header as parent.
    "chain_replace_group@worker_process": "add(^replace_with_group(chord{+add, +add}))",
    "chord_header_replace_same_name@worker_process": "chord{*tsum, +^paginate(paginate), +add}",
    # add.apply_async(parent_id=<an earlier add>) from a client: Celery
    # defaults root_id to the task's own ID there, so the task claims to be
    # a root with a parent.
    "client_explicit_parent": "add(add)",
    # The same from inside a task (send_with_parent): the root is the
    # sender's.
    "worker_explicit_parent": "add(add) | send_with_parent",
    "chord_with_chain_body": "chord{*>tsum(add), +add, +add}",
    # outer replaces itself with chain(inner, add) and inner with chain(add,
    # add): both IDs are reused, and outer's replacement reports inner (its
    # own child) as parent while inner's own events report outer.
    "nested_replace@worker_process": "add(^outer(^inner(add)))",
    "nested_replace_root@worker_process": "^outer(^inner(add))",
    "nested_replace_header@worker_process": "chord{*tsum, +^outer(^inner(add)), +add}",
    "nested_replace_group@worker_process": (
        "add(^outer_g(^replace_with_group(chord{+add, +add})))"
    ),
    "nested_replace_group_root@worker_process": (
        "^outer_g(^replace_with_group(chord{+add, +add}))"
    ),
    # The replacement's last task replaces itself again, under the same ID.
    "nested_replace_last@worker_process": "add(^outer_last(add, add))",
    # prec(n) replaces itself with chain(fetch(n), prec(n + 1)): one ID
    # carries every page's prec, each reporting the previous fetch.
    "recursive_replace@worker_process": "add(^prec(fetch, fetch, fetch))",
    "recursive_replace_root@worker_process": "^prec(fetch, fetch, fetch)",
    "two_replacers@worker_process": (
        "add(^replace_with_chain(^replace_with_chain(add), add))"
    ),
    "two_replacers_root@worker_process": (
        "^replace_with_chain(^replace_with_chain(add), add)"
    ),
    "mid_chain_replace3@worker_process": "add(^paginate_to(add, paginate_to(paginate_to)))",
    "mid_chain_replace_names@worker_process": "add(^mid_replacer(add, add(tsum)))",
    "chord_header_replace3@worker_process": (
        "chord{*tsum, +^paginate_to(paginate_to(paginate_to)), +add}"
    ),
    "chord_body_replace3@worker_process": (
        "chord{*^paginate_to(paginate_to(paginate_to)), +add, +add}"
    ),
    "group_chain_replace3@worker_process": (
        "group{+add, add(^paginate_to(paginate_to(paginate_to)))}"
    ),
    "chain_replace12@worker_process": (
        "add(^paginate_to(paginate_to(paginate_to(paginate_to(paginate_to("
        "paginate_to(paginate_to(paginate_to(paginate_to(paginate_to("
        "paginate_to(paginate_to))))))))))))"
    ),
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

    An ID ``task.replace()`` reuses carries one task per announcement (a
    new attempt-0 PENDING after the previous task's worker events): each
    runs in its own process, once its own sender published it.
    """
    task_ids = {e["task_id"] for e in events}
    runs: Counter[str] = Counter()
    started: set[str] = set()
    producers: list[str] = []
    # A task's worker stream waits until its sender emitted this many events.
    gate: dict[str, tuple[str, int]] = {}
    position: dict[str, int] = {}
    for event in events:
        task_id = event["task_id"]
        pending = event["state"] == "PENDING"
        if pending and event["retries"] == 0 and task_id in started:
            runs[task_id] += 1  # the next task under a reused ID
            started.discard(task_id)
        elif not pending:
            started.add(task_id)
        parent = event["parent_id"]
        if not pending:
            name = f"{task_id}#{runs[task_id]}"
        elif parent in task_ids:
            name = f"{parent}#{runs[parent]}"
        else:
            name = "client"
        own = f"{task_id}#{runs[task_id]}"
        if pending and own not in gate:
            gate[own] = (name, position.get(name, 0))
        position[name] = position.get(name, 0) + 1
        producers.append(name)

    orders = []
    for seed in range(count):
        rng = random.Random(seed)
        streams: dict[str, list[dict[str, Any]]] = {}
        for event, name in zip(events, producers, strict=True):
            streams.setdefault(name, []).append(event)
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
    "nested_replace@worker_process",
    "nested_replace_root@worker_process",
    "nested_replace_header@worker_process",
    "nested_replace_group@worker_process",
    "nested_replace_group_root@worker_process",
    "nested_replace_last@worker_process",
    "recursive_replace@worker_process",
    "recursive_replace_root@worker_process",
    "two_replacers@worker_process",
    "two_replacers_root@worker_process",
    "mid_chain_replace3@worker_process",
    "mid_chain_replace_names@worker_process",
    "chord_header_replace3@worker_process",
    "chord_body_replace3@worker_process",
    "group_chain_replace3@worker_process",
    "chain_replace12@worker_process",
)


def _replaced_tasks(events: list[dict[str, Any]]) -> dict[str, str | None]:
    """The replaced tasks' IDs and the parent each one's own events report.

    A worker in its own process announces the replacement's last task, which
    reuses the ID, with a second attempt-0 PENDING. The task's first event
    in a capture is its sender's PENDING.
    """
    pending = Counter(
        e["task_id"] for e in events if e["state"] == "PENDING" and e["retries"] == 0
    )
    return {
        task_id: next(e["parent_id"] for e in events if e["task_id"] == task_id)
        for task_id, count in pending.items()
        if count > 1
    }


def _replaced_task(events: list[dict[str, Any]]) -> tuple[str, str | None]:
    """The ID of a capture's only replaced task, and its own parent."""
    ((task_id, parent),) = _replaced_tasks(events).items()
    return task_id, parent


@pytest.mark.parametrize("flow", sorted(_REPLACE_FLOWS))
@pytest.mark.parametrize("skew_s", [-3600, -1, -0.05, -0.025, 0.025, 1, 3600])
def test_replay_replaced_task_with_skewed_worker_clock_keeps_identity(
    flow: str, skew_s: float
) -> None:
    """The replacement's events for a replaced task's ID come from another
    host: with its clock skewed either way, every replaced task keeps its
    name and place, in any order."""
    events = _CAPTURES[flow]
    replaced = _replaced_tasks(events)
    names = {
        task_id: next(e["name"] for e in events if e["task_id"] == task_id)
        for task_id in replaced
    }
    skewed = []
    for event in events:
        task_id = event["task_id"]
        if task_id in replaced and _is_replacement_event(
            event, task_id, replaced[task_id]
        ):
            shifted = datetime.fromisoformat(
                event["timestamp"].replace("Z", "+00:00")
            ) + timedelta(seconds=skew_s)
            event = {**event, "timestamp": shifted.isoformat()}
        skewed.append(event)
    for order in _orders(skewed):
        graph = _replay(order)

        assert {t: graph.nodes[t].name for t in replaced} == names
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


def _only_node(graph: TaskGraph, node_type: NodeType, suffix: str = "") -> TaskNode:
    (node,) = (
        n for n in _nodes_of_type(graph, node_type) if n.task_id.endswith(suffix)
    )
    return node


def test_remove_group_body_node_of_real_chord_rebuilds_the_body() -> None:
    """The chord of a real capture whose body is a group still draws it, with
    the same tasks inside, after the body's synthetic node is removed."""
    events = _CAPTURES["chord_group_body"]
    graph = _replay(events)
    chord = _only_node(graph, _C)
    body = _only_node(graph, _G, ":body")
    body_tasks = list(body.children)
    nodes_before = set(graph.nodes)

    graph.remove_node(body.task_id)

    assert_graph_invariants(graph)
    assert set(graph.nodes) == nodes_before
    assert chord.chord_id == body.task_id
    assert chord.children[-1] == body.task_id
    assert graph.nodes[body.task_id].children == body_tasks
    assert {graph.nodes[t].parent_id for t in body_tasks} == {body.task_id}
    assert set(graph._body_members) == {chord.group_id}
    assert graph._callback_of == {body.task_id: chord.group_id}
    assert graph.nodes[body.task_id].parent_id == chord.task_id
    assert graph.root_ids == [chord.task_id]


def test_remove_chord_node_of_real_capture_releases_headers_and_unlock_task() -> None:
    """Removing a chord's synthetic node leaves its headers, the chord_unlock
    task and the callback as plain tasks, and forgets the unlock task's role."""
    graph = _replay(_CAPTURES["chord@chord_unlock"])
    assert graph._unlock_of
    chord = _only_node(graph, _C)

    graph.remove_node(chord.task_id)

    assert_graph_invariants(graph)
    assert _structure(graph) == "add | add | add | celery.chord_unlock(tsum)"
    assert graph._unlock_of == {}
    assert graph._unlock_by_chord == {}
    assert graph._callback_of == {}


def test_remove_task_from_three_task_group_body_shrinks_the_body() -> None:
    """A body of three tasks losing one is a body of two, as a rebuild gives.

    The capture's group body has two tasks; one of them is repeated under
    another task ID (same events, so same parent and group) to get three.
    """
    events = _CAPTURES["chord_group_body"]
    body_task = next(e["task_id"] for e in events if e["name"] == "flows.tsum")
    extra = "extra-" + body_task
    with_extra = events + [
        {**e, "task_id": extra} for e in events if e["task_id"] == body_task
    ]
    graph = _replay(with_extra)
    assert _structure(graph) == "chord{*group{+tsum, +tsum, +tsum}, +add, +add}"
    assert extra in graph._body_of

    graph.remove_node(extra)

    assert_graph_invariants(graph)
    assert _structure(graph) == "chord{*group{+tsum, +tsum}, +add, +add}"
    assert extra not in graph._body_of
    rebuilt = _replay(events)
    assert _graph_shape(graph) == _graph_shape(rebuilt)
    assert sorted(graph.root_ids) == sorted(rebuilt.root_ids)


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


@pytest.mark.parametrize(
    "flow", ["chain_replace3@worker_process", "chain_replace4@worker_process"]
)
def test_replay_longer_replacement_when_server_missed_its_start(flow: str) -> None:
    """Without the replaced task's own events, a replacement of 3+ tasks
    leaves the reused ID and the replacement's tasks naming each other as
    parents in a loop: they still make one graph of their own (apart from
    the workflow's first task), whatever the order; which of them is on top
    depends on the order (the loop is broken where it closes)."""
    events = _without_own_events(_CAPTURES[flow])
    chain = flow.removeprefix("chain_replace").removesuffix("@worker_process")
    expected = (
        "add | " + "(".join(["paginate_to"] * int(chain)) + ")" * (int(chain) - 1)
    )
    for order in _orders(events):
        assert _structure(_replay(order)) == expected


def _pages_like_capture(
    flow: str, pages: int, *, skew_s: float = 0
) -> tuple[list[dict[str, Any]], str, str]:
    """A capture's replaced task with its replacement chain stretched.

    ``chain_replace4@worker_process`` (chain(add, paginate_to(0, 4))): the
    replaced task reports the workflow's first task as parent; page 1
    reports the replaced task, each next page the one before, and the last
    page's events, under the replaced task's ID, report the page before it
    (``skew_s`` shifts their timestamps). Each page sends a PENDING, then
    runs (STARTED, SUCCESS), as in the capture.

    Returns:
        The events in arrival order, the replaced task's ID and its parent.
    """
    captured = _CAPTURES[flow]
    replaced, first = _replaced_task(captured)
    assert first is not None
    page = next(e for e in captured if e["parent_id"] == replaced)
    start = datetime.fromisoformat(captured[0]["timestamp"].replace("Z", "+00:00"))
    events = [e for e in captured if e["task_id"] in (first, replaced)][:5]
    clock = iter(range(1, 10 * pages))

    def stamp(event: dict[str, Any], shift: float = 0) -> dict[str, Any]:
        at = start + timedelta(milliseconds=next(clock), seconds=shift)
        return {**event, "timestamp": at.isoformat()}

    events = [stamp(e) for e in events]
    parent = replaced
    for number in range(1, pages):
        task_id = f"page-{number:05d}"
        for state in ("PENDING", "STARTED", "SUCCESS"):
            events.append(
                stamp({**page, "task_id": task_id, "state": state, "parent_id": parent})
            )
        parent = task_id
    last = {**page, "task_id": replaced, "parent_id": parent}
    for state in ("PENDING", "STARTED", "SUCCESS"):
        events.append(stamp({**last, "state": state}, skew_s))
    return events, replaced, first


@pytest.mark.parametrize("skew_s", [-3600, 0, 3600])
def test_replay_long_replacement_chain_keeps_replaced_task_in_place(
    skew_s: float,
) -> None:
    """A replacement chain of 300 tasks: the last one's events report a task
    300 links below the replaced task, which still keeps its own parent and
    name, with the chain under it, in shuffled orders."""
    events, replaced, first = _pages_like_capture(
        "chain_replace4@worker_process", 300, skew_s=skew_s
    )
    for order in _orders(events)[:4]:
        graph = TaskGraph()
        for event in order:
            graph.add_event(TaskEvent.model_validate(event))
        assert_graph_invariants(graph)

        node = graph.nodes[replaced]
        assert (node.name, node.parent_id) == ("flows.paginate_to", first)
        depth, below = 0, node.children
        while below:
            (only,) = below
            depth, below = depth + 1, graph.nodes[only].children
        assert depth == 299


def _recursive_pages(pages: int) -> list[dict[str, Any]]:
    """``recursive_replace@worker_process`` with ``pages`` pages.

    prec(n) replaces itself with chain(fetch(n), prec(n + 1)): every page's
    fetch reports the replaced task, and the next prec's events, under the
    replaced task's ID, report that fetch, as in the capture.
    """
    captured = _CAPTURES["recursive_replace@worker_process"]
    replaced, first = _replaced_task(captured)
    fetch = next(e for e in captured if e["name"] == "flows.fetch")
    prec = next(e for e in captured if e["task_id"] == replaced)
    start = datetime.fromisoformat(captured[0]["timestamp"].replace("Z", "+00:00"))
    events = [e for e in captured if e["task_id"] == first]
    events += [e for e in captured if e["task_id"] == replaced][:2]
    for number in range(pages):
        task_id = f"fetch-{number:05d}"
        for state in ("PENDING", "STARTED", "SUCCESS"):
            events.append({**fetch, "task_id": task_id, "state": state})
        for state in ("PENDING", "STARTED"):
            events.append({**prec, "state": state, "parent_id": task_id})
    events.append({**prec, "state": "SUCCESS", "parent_id": task_id})
    return [
        {**e, "timestamp": (start + timedelta(milliseconds=i)).isoformat()}
        for i, e in enumerate(events)
    ]


def test_recursive_replacement_checks_grow_linearly_with_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recursive pagination with 500 pages: a new page runs a bounded number
    of replacement checks (no recheck of every earlier page), so the work
    under the store's lock grows linearly.

    That holds for events arriving in (roughly) the order they happened,
    which is what this feeds. Fully shuffled events are not linear: 2000
    shuffled pages took 17.5 s (worst event 238 ms), quadratic, though far
    cheaper than before.
    """
    checks = 0
    replacement_check = TaskGraph._replacement_check

    def counted(graph: TaskGraph, node: TaskNode) -> Any:
        check = replacement_check(graph, node)

        def count(event: TaskEvent) -> bool:
            nonlocal checks
            checks += 1
            return check(event)

        return count

    monkeypatch.setattr(TaskGraph, "_replacement_check", counted)
    graph = TaskGraph()
    for event in _recursive_pages(500):
        graph.add_event(TaskEvent.model_validate(event))

    replaced, _ = _replaced_task(_CAPTURES["recursive_replace@worker_process"])
    assert len(graph.nodes[replaced].children) == 500
    assert checks <= 20 * 500


def _nested_replacements(depth: int, *, skew_s: float = 0) -> list[dict[str, Any]]:
    """``nested_replace_root@worker_process`` nested ``depth`` levels deep.

    As in the capture, ``replaced-0`` (outer) replaces itself with a chain
    whose first task replaces itself in turn, and so on: each
    ``replaced-<n>`` reports the one above as parent, the innermost one's
    replacement starts with ``leaf``, and each replacement's last task, under
    the replaced task's ID, reports the task below as parent (``skew_s``
    shifts the timestamps of those events).
    """
    captured = _CAPTURES["nested_replace_root@worker_process"]
    base = {**captured[0], "root_id": "replaced-0", "parent_id": None}
    start = datetime.fromisoformat(base["timestamp"].replace("Z", "+00:00"))
    events: list[dict[str, Any]] = []

    def emit(
        task_id: str, name: str, parent: str | None, states: str, shift: float = 0
    ):
        for state in states.split():
            at = start + timedelta(milliseconds=len(events), seconds=shift)
            events.append(
                {
                    **base,
                    "task_id": task_id,
                    "name": name,
                    "state": state,
                    "parent_id": parent,
                    "timestamp": at.isoformat(),
                }
            )

    emit("replaced-0", "flows.outer", None, "PENDING STARTED")
    for level in range(1, depth + 1):
        emit(
            f"replaced-{level}",
            "flows.inner",
            f"replaced-{level - 1}",
            "PENDING STARTED",
        )
    emit("leaf", "flows.add", f"replaced-{depth}", "PENDING STARTED SUCCESS")
    below = "leaf"
    for level in range(depth, -1, -1):
        emit(f"replaced-{level}", "flows.add", below, "PENDING STARTED SUCCESS", skew_s)
        below = f"replaced-{level}"
    return events


@pytest.mark.parametrize("depth", [3, 16])
@pytest.mark.parametrize("skew_s", [-3600, 0, 3600])
def test_replay_deeply_nested_replacements_keep_each_task_in_place(
    depth: int, skew_s: float
) -> None:
    """replace() nested 3 levels deep, and 16 (``_NESTING_LIMIT``): every
    replaced task keeps its own name and parent, in shuffled orders and
    with the replacements' clocks skewed."""
    events = _nested_replacements(depth, skew_s=skew_s)
    for order in _orders(events)[:4]:
        graph = TaskGraph()
        for event in order:
            graph.add_event(TaskEvent.model_validate(event))
        assert_graph_invariants(graph)

        assert graph.root_ids == ["replaced-0"]
        assert graph.nodes["leaf"].parent_id == f"replaced-{depth}"
        for level in range(1, depth + 1):
            node = graph.nodes[f"replaced-{level}"]
            assert (node.name, node.parent_id) == (
                "flows.inner",
                f"replaced-{level - 1}",
            )
        assert graph.nodes["replaced-0"].name == "flows.outer"


def test_replay_replacements_nested_past_the_limit_keeps_graph_invariants() -> None:
    """replace() nested 40 levels deep, past what a check works out at once
    (``_NESTING_LIMIT``): the graph keeps its invariants (the nesting is
    then no longer told apart in every order)."""
    graph = TaskGraph()
    for event in _nested_replacements(40):
        graph.add_event(TaskEvent.model_validate(event))
    assert_graph_invariants(graph)
    assert len(graph.nodes) == 42


def _as_older_sender(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The capture as an older stemtrace worker sends it: its PENDING (from
    Celery's ``task_sent``) carries no parent, root or group IDs."""
    ids = ("parent_id", "root_id", "group_id", "chord_id", "chord_callback_id")
    return [
        {**e, **dict.fromkeys(ids)} if e["state"] == "PENDING" else e for e in events
    ]


@pytest.mark.parametrize("flow", sorted(_REPLACE_FLOWS))
def test_replay_replaced_tasks_from_older_senders_builds_expected_graph(
    flow: str,
) -> None:
    """From older workers, whose PENDING names no parent, a task's parent
    only shows with its worker events: replaced tasks still end where they
    were sent, in any order."""
    for order in _orders(_as_older_sender(_CAPTURES[flow])):
        assert _structure(_replay(order)) == _EXPECTED[flow]


def test_remove_tasks_with_skewed_replacement_matches_rebuild() -> None:
    """Nested replace() with the replacements' clocks an hour behind: after
    removing any one task, every replaced task's name and parent are the
    ones the remaining events give (e.g. without the inner replacement's
    first task, nothing shows the outer task's replacement any more)."""
    events = _CAPTURES["nested_replace@worker_process"]
    replaced = _replaced_tasks(events)
    skewed = [
        {
            **e,
            "timestamp": (
                datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                - timedelta(hours=1)
            ).isoformat(),
        }
        if e["task_id"] in replaced
        and _is_replacement_event(e, e["task_id"], replaced[e["task_id"]])
        else e
        for e in events
    ]
    for removed in sorted({e["task_id"] for e in events}):
        graph = _replay(skewed)
        graph.remove_node(removed)
        assert_graph_invariants(graph)
        rebuilt = _replay([e for e in skewed if e["task_id"] != removed])

        assert {t: (n.name, n.reported_parent_id) for t, n in graph.nodes.items()} == {
            t: (n.name, n.reported_parent_id) for t, n in rebuilt.nodes.items()
        }
        assert _graph_shape(graph) == _graph_shape(rebuilt)
