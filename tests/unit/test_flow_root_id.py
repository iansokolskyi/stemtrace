"""The flow root of a task: the node ``/api/graphs`` lists for the graph that holds it.

Expected roots are read off the store a second way (which listed root's subgraph holds the
node), so they don't share the code under test. Captures are real Celery events
(``tests/data/celery_flows.json``).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType
from stemtrace.server.store import GraphStore

_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)

# Top-level group, chord, chain, nested group, replace and a group spawned inside a task
_FLOWS = [
    "group",
    "chord",
    "chain",
    "chain_group_nested",
    "group_spawned_in_task",
    "group_of_chains",
    "chord_in_chord_body",
    "nested_replace_header@worker_process@field",
]


def _store(flow: str) -> GraphStore:
    store = GraphStore()
    for raw in _CAPTURES[flow]:
        store.add_event(TaskEvent.model_validate(raw))
    return store


def _listed_root_of(store: GraphStore, node_id: str) -> str:
    """The one root ``/api/graphs`` lists whose subgraph holds ``node_id``."""
    roots, _ = store.get_root_nodes()
    holders = [
        r.task_id for r in roots if node_id in store.get_graph_from_root(r.task_id)
    ]
    assert len(holders) == 1, f"{node_id} is in {len(holders)} listed graphs"
    return holders[0]


def _event(task_id: str, **fields: Any) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.sample",
        state=TaskState.STARTED,
        timestamp=_NOW,
        **fields,
    )


@pytest.mark.parametrize("flow", _FLOWS)
def test_flow_root_id_of_every_node_is_the_listed_graph_root(flow: str) -> None:
    store = _store(flow)
    ids = {raw["task_id"] for raw in _CAPTURES[flow]}
    # Synthetic GROUP and CHORD nodes are in the graph too
    roots, _ = store.get_root_nodes()
    for root in roots:
        ids.update(store.get_graph_from_root(root.task_id))

    for node_id in sorted(ids):
        assert store.get_flow_root_id(node_id) == _listed_root_of(store, node_id)


def test_top_level_group_members_have_the_group_node_as_flow_root() -> None:
    """Celery stamps each member's ``root_id`` with its own ID; the flow root is the group."""
    store = _store("group")
    members = {raw["task_id"]: raw for raw in _CAPTURES["group"] if raw.get("group_id")}
    group_node_ids = {
        n.task_id
        for root in store.get_root_nodes()[0]
        for n in store.get_graph_from_root(root.task_id).values()
        if n.node_type == NodeType.GROUP
    }
    assert members
    assert len(group_node_ids) == 1
    for task_id, raw in members.items():
        assert raw["root_id"] == task_id  # what the events say
        assert store.get_flow_root_id(task_id) in group_node_ids  # what the graph says


def test_top_level_chord_header_flow_root_is_the_chord_node() -> None:
    store = _store("chord")
    node_types = {
        nid: n.node_type
        for root in store.get_root_nodes()[0]
        for nid, n in store.get_graph_from_root(root.task_id).items()
    }
    roots = {store.get_flow_root_id(raw["task_id"]) for raw in _CAPTURES["chord"]}
    assert len(roots) == 1
    (root,) = roots
    assert root is not None
    assert node_types[root] in (NodeType.CHORD, NodeType.GROUP)


def test_chain_steps_share_the_first_task_as_flow_root() -> None:
    store = _store("chain")
    first = next(r["task_id"] for r in _CAPTURES["chain"] if not r.get("parent_id"))
    for raw in _CAPTURES["chain"]:
        assert store.get_flow_root_id(raw["task_id"]) == first


def test_a_task_sent_without_a_parent_is_its_own_flow_root() -> None:
    """Celery's default: ``root_id == task_id`` and no parent."""
    store = GraphStore()
    store.add_event(_event("solo", root_id="solo"))
    assert store.get_flow_root_id("solo") == "solo"


def test_a_child_reports_the_root_of_its_parent() -> None:
    store = GraphStore()
    store.add_event(_event("top", root_id="top"))
    store.add_event(_event("mid", parent_id="top", root_id="top"))
    store.add_event(_event("leaf", parent_id="mid", root_id="top"))
    assert [store.get_flow_root_id(t) for t in ("top", "mid", "leaf")] == ["top"] * 3


def test_flow_root_id_ignores_what_events_say_the_root_is() -> None:
    """A wrong ``root_id`` on every event doesn't move the flow root."""
    store = GraphStore()
    store.add_event(_event("top", root_id="elsewhere"))
    store.add_event(_event("kid", parent_id="top", root_id="elsewhere"))
    assert store.get_flow_root_id("kid") == "top"


def test_unknown_task_has_no_flow_root() -> None:
    assert GraphStore().get_flow_root_id("missing") is None


def test_a_hand_made_parent_cycle_ends_with_none() -> None:
    """Corrupt links (a store restored from bad data): the walk must not loop."""
    store = GraphStore()
    store.add_event(_event("a"))
    store.add_event(_event("b"))
    graph = store._graph  # pin the guard against links the graph never makes
    graph.nodes["a"].parent_id = "b"
    graph.nodes["b"].parent_id = "a"
    assert store.get_flow_root_id("a") is None
    assert store.get_flow_root_id("b") is None


def test_a_parent_missing_from_the_store_gives_no_flow_root() -> None:
    store = GraphStore()
    store.add_event(_event("a"))
    store._graph.nodes["a"].parent_id = "gone"  # an evicted parent
    assert store.get_flow_root_id("a") is None
