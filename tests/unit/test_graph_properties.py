"""Randomized graph-linking tests: invariants hold after every event.

Seeds are fixed, so every run checks the same event sequences.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import TaskGraph
from stemtrace.server.store import GraphStore
from tests.graph_invariants import assert_graph_invariants

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_TASK_IDS = [f"t{i}" for i in range(10)]
_GROUP_IDS = ["g0", "g1", "g2"]
_STATES = list(TaskState)


def _make_celery_shaped(
    rng: random.Random,
    group_of: dict[str, str | None],
    parent_of: dict[str, str | None],
) -> dict[str, str | None]:
    """Make the plan Celery-shaped; returns each chord's callback.

    A group's members are sent together, by one earlier parent; each chord
    names its own callback, a later task outside the group that its last
    header sends.
    """
    callback_of: dict[str, str | None] = {}
    for g in _GROUP_IDS:
        members = [i for i, t in enumerate(_TASK_IDS) if group_of[t] == g]
        if not members:
            callback_of[g] = None
            continue
        shared = rng.choice([*_TASK_IDS[: min(members)], "ghost", None])
        for i in members:
            parent_of[_TASK_IDS[i]] = shared
        later = [
            t
            for t in _TASK_IDS[max(members) + 1 :]
            if group_of[t] is None and t not in callback_of.values()
        ]
        callback = rng.choice([*later, None]) if later else None
        callback_of[g] = callback
        if callback is not None:
            parent_of[callback] = _TASK_IDS[max(members)]
    return callback_of


def _random_events(seed: int, count: int, *, acyclic: bool = False) -> list[TaskEvent]:
    """Events for a small pool of tasks with groups, chords and odd parents.

    Each task mostly keeps one group and one parent, like real Celery tasks,
    but some events drop or change them, report a missing or the task's own
    ID as parent, or name a chord callback. With ``acyclic``, every reported
    parent is an earlier task (or missing), as in Celery, where a task's
    parent is sent before it; a group's members share one parent; chords
    name distinct callbacks; a task keeps its group.
    """
    rng = random.Random(seed)
    group_of = {t: rng.choice([*_GROUP_IDS, None, None]) for t in _TASK_IDS}
    parent_of = {
        t: rng.choice(
            [
                *(
                    # Celery-shaped: an earlier task, never one of the
                    # task's own group (a group's members share a parent
                    # outside it).
                    [
                        p
                        for p in _TASK_IDS[:i]
                        if group_of[t] is None or group_of[p] != group_of[t]
                    ]
                    if acyclic
                    else _TASK_IDS
                ),
                "ghost",
                None,
                None,
            ]
        )
        for i, t in enumerate(_TASK_IDS)
    }
    callback_of = {g: rng.choice([*_TASK_IDS, None]) for g in _GROUP_IDS}
    if acyclic:
        callback_of = _make_celery_shaped(rng, group_of, parent_of)
    events = []
    for i in range(count):
        task_id = rng.choice(_TASK_IDS)
        group_id = group_of[task_id]
        parent_id = parent_of[task_id]
        roll = rng.random()
        if roll < 0.3:
            parent_id = None  # e.g. the sender's PENDING
        elif roll < 0.4 and not acyclic:
            parent_id = rng.choice([*_TASK_IDS, "ghost"])
        if rng.random() < 0.1 and not acyclic:
            group_id = rng.choice([*_GROUP_IDS, None])
        callback = callback_of[group_id] if group_id and rng.random() < 0.5 else None
        events.append(
            TaskEvent(
                task_id=task_id,
                name="tests.task",
                state=rng.choice(_STATES),
                timestamp=_T0 + timedelta(seconds=rng.randint(0, count)),
                parent_id=parent_id,
                group_id=group_id,
                chord_callback_id=callback,
                retries=rng.choice([0, 0, 1]),
                args=[i],
            )
        )
    return events


@pytest.mark.parametrize("seed", range(150))
def test_add_event_random_sequence_keeps_graph_invariants(seed: int) -> None:
    graph = TaskGraph()
    rng = random.Random(10_000 + seed)
    for event in _random_events(seed, 60):
        graph.add_event(event)
        assert_graph_invariants(graph)
        if rng.random() < 0.15 and graph.nodes:
            graph.remove_node(rng.choice(sorted(graph.nodes)))
            assert_graph_invariants(graph)


@pytest.mark.parametrize("max_nodes", [3, 5, 8])
@pytest.mark.parametrize("seed", range(50))
def test_store_eviction_random_sequence_keeps_graph_invariants(
    seed: int, max_nodes: int
) -> None:
    store = GraphStore(max_nodes=max_nodes)
    for event in _random_events(seed, 60):
        store.add_event(event)
        assert_graph_invariants(store._graph)
        assert store.node_count <= max_nodes


def _shape(graph: TaskGraph) -> dict[str, tuple[str | None, list[str], str | None]]:
    return {
        node_id: (node.parent_id, sorted(node.children), node.chord_id)
        for node_id, node in graph.nodes.items()
    }


@pytest.mark.parametrize("seed", range(150))
def test_remove_node_matches_rebuild_from_remaining_events(seed: int) -> None:
    """Removing tasks leaves the graph a rebuild from the other tasks'
    events would give (placement, children, chord callbacks, roots).

    The plan is Celery-shaped (see ``_make_celery_shaped``). Steps where
    the data still closes a cycle through groups, chain links and chord
    bodies only check invariants: where a cycle breaks depends on which link
    came first. (About 6% of steps.)
    """
    rng = random.Random(20_000 + seed)
    events = _random_events(seed, 60, acyclic=True)
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
    removed: set[str] = set()
    for _ in range(3):
        tasks = sorted(
            n for n, node in graph.nodes.items() if not n.startswith("group:")
        )
        if not tasks:
            break
        victim = rng.choice(tasks)
        removed.add(victim)
        graph.remove_node(victim)
        assert_graph_invariants(graph)

        rebuilt = TaskGraph()
        for event in events:
            if event.task_id not in removed:
                rebuilt.add_event(event)
        assert_graph_invariants(rebuilt)
        if graph._cycles_refused or rebuilt._cycles_refused:
            # The data closed a cycle through groups, chain links and chord
            # bodies. Where it breaks depends on which link came first, so
            # only the invariants are comparable.
            continue
        assert _shape(graph) == _shape(rebuilt), (seed, removed)
        assert sorted(graph.root_ids) == sorted(rebuilt.root_ids)
