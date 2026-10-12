"""Structural invariants of a TaskGraph, shared by graph and store tests."""

from stemtrace.core.graph import (
    NodeType,
    TaskGraph,
    TaskNode,
    _body_node_id,
    _group_node_id,
)


def _is_callback_edge(parent: TaskNode, child_id: str) -> bool:
    """A CHORD lists its callback to draw its edge, whatever the callback's parent."""
    return parent.node_type == NodeType.CHORD and parent.chord_id == child_id


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


def _check_group_indexes(graph: TaskGraph) -> None:
    """Group tallies, group nodes and chains agree with the nodes."""
    nodes = graph.nodes
    for group_id, tally in graph._groups.items():
        assert set(tally.members) <= set(nodes), (group_id, "member not in nodes")
        assert set(tally.headers) <= set(tally.members), (group_id, "stray header")
    for node_id, node in nodes.items():
        if node.group_id is None:
            continue
        tally = graph._groups.get(node.group_id)
        assert tally is not None, f"{node_id} is in a group without a tally"
        if node.node_type == NodeType.TASK:
            assert node_id in tally.members, f"{node_id} is not its group's member"
    for member_id, links in graph._chain_of.items():
        assert member_id in nodes, f"chain of missing member {member_id}"
        for position, link_id in enumerate(links):
            assert link_id in nodes, f"missing chain link {link_id}"
            assert graph._link_owner.get(link_id) == member_id, link_id
            assert graph._link_pos.get(link_id) == position, link_id
    for link_id, member_id in graph._link_owner.items():
        assert link_id in graph._chain_of.get(member_id, []), link_id
        member = nodes[member_id]
        assert member.group_id is not None, member_id
        assert member_id in graph._groups[member.group_id].counted(), member_id
        assert _group_node_id(member.group_id) in nodes, member_id


def _check_chord_indexes(graph: TaskGraph) -> None:
    """Chord callbacks, unlock tasks and group bodies agree with the nodes."""
    nodes = graph.nodes
    for node_id, node in nodes.items():
        if node.node_type == NodeType.CHORD and node.chord_id is not None:
            assert node.chord_id in node.children, f"{node_id} hides its callback"
            assert graph._callback_of.get(node.chord_id) == node.group_id, node_id
        if node.node_type == NodeType.CHORD and node.chord_callback_id is not None:
            named = graph._chord_by_callback.get(node.chord_callback_id)
            assert named == node.group_id, (node_id, node.chord_callback_id, named)
    for callback_id, group_id in graph._callback_of.items():
        chord = nodes.get(_group_node_id(group_id))
        assert chord is not None, (callback_id, group_id, "callback of no chord")
        assert chord.chord_id == callback_id, (callback_id, group_id)
    # One owner each way: a chord_unlock task is drawn under one chord.
    assert len(set(graph._unlock_by_chord.values())) == len(graph._unlock_by_chord), (
        graph._unlock_by_chord
    )
    for group_id, unlock_id in graph._unlock_by_chord.items():
        assert graph._unlock_of.get(unlock_id) == group_id, (group_id, unlock_id)
    for unlock_id, group_id in graph._unlock_of.items():
        assert graph._unlock_by_chord.get(group_id) == unlock_id, (group_id, unlock_id)
    for group_id, members in graph._body_members.items():
        assert _body_node_id(group_id) in nodes, f"{group_id} lost its body node"
        assert len(set(members)) == len(members), (group_id, members)
        for member_id in members:
            # A task is in one chord's group body only.
            assert graph._body_of.get(member_id) == group_id, member_id
    for member_id, group_id in graph._body_of.items():
        assert member_id in graph._body_members[group_id], member_id


def _check_missing_parent_indexes(graph: TaskGraph) -> None:
    """Nodes waiting for a missing parent are listed both ways and exist."""
    assert set(graph._awaited_by) <= set(graph.nodes), "waiting node is gone"
    for parent_id, waiting in graph._awaiting_parent.items():
        for child_id in waiting:
            assert graph._awaited_by.get(child_id) == parent_id, child_id
    for child_id, parent_id in graph._awaited_by.items():
        assert child_id in graph._awaiting_parent[parent_id], child_id
    for walked, walkers in graph._walk_deps.items():
        for walker in walkers:
            node = graph.nodes.get(walker)
            assert node is not None, (walked, walker, "walker is gone")
            assert node.node_type == NodeType.TASK, (walked, walker)


def _check_dependency_indexes(graph: TaskGraph) -> None:
    """Chord and replacement-walk dependencies agree with their reverse maps."""
    for node_id, group_ids in graph._chord_deps.items():
        for group_id in group_ids:
            assert node_id in graph._deps_of_chord.get(group_id, ()), (
                node_id,
                group_id,
            )
    for group_id, node_ids in graph._deps_of_chord.items():
        for node_id in node_ids:
            assert group_id in graph._chord_deps.get(node_id, {}), (node_id, group_id)
    for owner, walked_ids in graph._walks_of.items():
        for walked in walked_ids:
            assert owner in graph._walk_deps.get(walked, {}), (walked, owner)
    for walked, walkers in graph._walk_deps.items():
        for walker in walkers:
            assert walked in graph._walks_of.get(walker, ()), (walked, walker)


def _check_reported_children(graph: TaskGraph) -> None:
    """Each task listed under a reported parent exists and reports it."""
    for parent_id, child_ids in graph._reported_children.items():
        for child_id in child_ids:
            child = graph.nodes.get(child_id)
            assert child is not None, (parent_id, child_id, "reporter is gone")
            assert child.reported_parent_id == parent_id, (parent_id, child_id)


def assert_graph_invariants(graph: TaskGraph) -> None:
    """Fail if the graph's links are inconsistent.

    Checks: roots are unique, live and parentless, and every parentless node
    is a root; ``children`` lists have no duplicates, no dangling IDs and no
    self-links, and agree with ``parent_id`` (except a CHORD's callback
    edge); no node is its own ancestor; each node is a root or listed in
    exactly its parent's children; every node is reachable from a root. The
    internal indexes (group tallies, chains, chord callbacks and unlock tasks,
    group bodies, missing-parent waits, replacement walks) name live nodes and
    agree with each other.
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
    _check_group_indexes(graph)
    _check_chord_indexes(graph)
    _check_missing_parent_indexes(graph)
    _check_dependency_indexes(graph)
    _check_reported_children(graph)
