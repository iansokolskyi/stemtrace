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


def _random_events(seed: int, count: int) -> list[TaskEvent]:
    """Events for a small pool of tasks with groups, chords and odd parents.

    Each task mostly keeps one group and one parent, like real Celery tasks,
    but some events drop or change them, report a missing or the task's own
    ID as parent, or name a chord callback.
    """
    rng = random.Random(seed)
    group_of = {t: rng.choice([*_GROUP_IDS, None, None]) for t in _TASK_IDS}
    parent_of = {t: rng.choice([*_TASK_IDS, "ghost", None, None]) for t in _TASK_IDS}
    callback_of = {g: rng.choice([*_TASK_IDS, None]) for g in _GROUP_IDS}
    events = []
    for i in range(count):
        task_id = rng.choice(_TASK_IDS)
        group_id = group_of[task_id]
        parent_id = parent_of[task_id]
        roll = rng.random()
        if roll < 0.3:
            parent_id = None  # e.g. the sender's PENDING
        elif roll < 0.4:
            parent_id = rng.choice([*_TASK_IDS, "ghost"])
        if rng.random() < 0.1:
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
