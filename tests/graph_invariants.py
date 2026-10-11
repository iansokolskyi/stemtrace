"""Structural invariants of a TaskGraph, shared by graph and store tests."""

from stemtrace.core.graph import NodeType, TaskGraph, TaskNode


def _is_callback_edge(parent: TaskNode, child_id: str) -> bool:
    """A CHORD lists its callback to draw its edge, whatever the callback's parent."""
    return parent.node_type == NodeType.CHORD and parent.chord_callback_id == child_id


def _check_roots(graph: TaskGraph) -> set[str]:
    """Roots are unique, live and parentless; returns them as a set."""
    assert len(graph.root_ids) == len(set(graph.root_ids)), graph.root_ids
    for root_id in graph.root_ids:
        assert root_id in graph.nodes, f"dangling root {root_id}"
        assert graph.nodes[root_id].parent_id is None, f"root {root_id} has a parent"
    return set(graph.root_ids)


def _check_children(graph: TaskGraph) -> dict[str, list[str]]:
    """Children lists are clean and agree with ``parent_id``.

    Returns, per node, the nodes whose children list it (callback edges aside).
    """
    nodes = graph.nodes
    listed_in: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        assert len(node.children) == len(set(node.children)), (node_id, node.children)
        for child_id in node.children:
            assert child_id != node_id, f"{node_id} is its own child"
            assert child_id in nodes, f"{node_id} lists missing child {child_id}"
            child_parent = nodes[child_id].parent_id
            if child_parent != node_id and _is_callback_edge(node, child_id):
                continue
            assert child_parent == node_id, (
                f"{node_id} lists {child_id}, whose parent is {child_parent}"
            )
            listed_in[child_id].append(node_id)
    return listed_in


def _check_no_cycle(graph: TaskGraph, node_id: str) -> None:
    """Walking up from a node never comes back to a node already seen."""
    seen = {node_id}
    ancestor = graph.nodes[node_id].parent_id
    while ancestor is not None and ancestor in graph.nodes:
        assert ancestor not in seen, f"{node_id} is its own ancestor"
        seen.add(ancestor)
        ancestor = graph.nodes[ancestor].parent_id


def _check_reachable(graph: TaskGraph, starts: set[str]) -> None:
    """Every node is reachable through children from one of ``starts``."""
    reached: set[str] = set()
    to_visit = list(starts)
    while to_visit:
        current = to_visit.pop()
        if current not in reached:
            reached.add(current)
            to_visit.extend(graph.nodes[current].children)
    assert reached == set(graph.nodes), f"unreachable: {set(graph.nodes) - reached}"


def assert_graph_invariants(graph: TaskGraph) -> None:
    """Fail if the graph's links are inconsistent.

    Checks: roots are unique, live and parentless, and every parentless node
    is a root; ``children`` lists have no duplicates, no dangling IDs and no
    self-links, and agree with ``parent_id`` (except a CHORD's callback
    edge); no node is its own ancestor; each node is a root or listed in
    exactly its parent's children; every node is reachable from a root.
    """
    roots = _check_roots(graph)
    listed_in = _check_children(graph)
    for node_id, node in graph.nodes.items():
        assert node.parent_id != node_id, f"{node_id} is its own parent"
        if node.parent_id is None:
            assert node_id in roots, f"parentless {node_id} is not a root"
            assert listed_in[node_id] == [], (node_id, listed_in[node_id])
        else:
            assert node_id not in roots, f"{node_id} is a root with a parent"
            assert listed_in[node_id] == [node.parent_id], (node_id, listed_in)
        _check_no_cycle(graph, node_id)
    _check_reachable(graph, roots)
