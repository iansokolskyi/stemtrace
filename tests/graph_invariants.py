"""Structural invariants of a TaskGraph, shared by graph and store tests."""

from stemtrace.core.graph import (
    NodeType,
    TaskGraph,
    TaskNode,
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
    """Chord callbacks, claims and unlock tasks agree with the nodes."""
    nodes = graph.nodes
    for node_id, node in nodes.items():
        if node.node_type == NodeType.CHORD and node.chord_id is not None:
            assert node.chord_id in node.children, f"{node_id} hides its callback"
            assert graph._callback_of.get(node.chord_id) == node.group_id, node_id
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
    _check_claims(graph)
    _check_unlock_indexes(graph)


def _check_claims(graph: TaskGraph) -> None:
    """A node two chords point to is drawn after neither."""
    for candidate, claimants in graph._claims.items():
        assert claimants, (candidate, "empty claim")
        for group_id in claimants:
            assert graph._candidate.get(group_id) == candidate, (group_id, candidate)
        if len(claimants) > 1:
            assert candidate not in graph._callback_of, (candidate, claimants)
    for group_id, candidate in graph._candidate.items():
        assert group_id in graph._claims.get(candidate, {}), (group_id, candidate)
        tally = graph._groups.get(group_id)
        assert tally is not None, (group_id, "candidate of no group")
        assert tally.headers, (group_id, "candidate of no chord")
    for callback_id, group_id in graph._callback_of.items():
        assert graph._candidate.get(group_id) == callback_id, (callback_id, group_id)


def _check_unlock_indexes(graph: TaskGraph) -> None:
    """``chord_unlock`` tasks are indexed by the chord they name, and exist."""
    for unlock_id, info in graph._unlocks.items():
        assert unlock_id in graph.nodes, (unlock_id, "unlock of no node")
        if info.group_id is not None:
            assert unlock_id in graph._unlocks_by_group[info.group_id], unlock_id
    for group_id, unlock_ids in graph._unlocks_by_group.items():
        assert unlock_ids, group_id
        for unlock_id in unlock_ids:
            assert graph._unlocks[unlock_id].group_id == group_id, unlock_id


def _check_named_callbacks(graph: TaskGraph) -> None:
    """Chord evidence: the tally owns it, the node and the stamp index agree."""
    for group_id, tally in graph._groups.items():
        node = graph.nodes.get(_group_node_id(group_id))
        if node is not None and node.node_type == NodeType.CHORD:
            assert node.chord_callback_id == graph._named_stamp(tally), group_id
        for evidence in (tally.stamps, tally.firsts):
            for value, task_ids in evidence.items():
                assert task_ids, (group_id, value, "empty evidence")
                assert set(task_ids) <= set(tally.headers), (group_id, value)
        stamp = graph._named_stamp(tally)
        expected = stamp if stamp != group_id else None
        assert graph._stamp_of.get(group_id) == expected, (group_id, stamp)
    for stamp, group_ids in graph._chords_by_stamp.items():
        assert group_ids, (stamp, "empty stamp index")
        for group_id in group_ids:
            assert graph._stamp_of.get(group_id) == stamp, (stamp, group_id)


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
    missing-parent waits, replacement walks) name live nodes and
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
    _check_named_callbacks(graph)
    _check_missing_parent_indexes(graph)
    _check_dependency_indexes(graph)
    _check_reported_children(graph)


def graph_state(graph: TaskGraph) -> dict[str, object]:
    """What a graph shows and the chord roles it decided, for comparing two graphs.

    Per node: name, type, place (parent and children), chord callback (drawn
    and named), reported parent, state and group. Also the roots and the
    chord indexes (stamps, drawn callbacks, chord_unlock tasks), so a stale
    entry in one of them counts as a difference. Lists
    are sorted: two graphs built in different orders may list the same
    children or roots in another order.
    """
    return {
        "nodes": {
            node_id: (
                node.name,
                node.node_type,
                node.parent_id,
                sorted(node.children),
                node.chord_id,
                node.chord_callback_id,
                node.reported_parent_id,
                node.state,
                node.group_id,
            )
            for node_id, node in graph.nodes.items()
        },
        "roots": sorted(graph.root_ids),
        "stamp_of": dict(graph._stamp_of),
        "callback_of": dict(graph._callback_of),
        "unlock_of": dict(graph._unlock_of),
    }
