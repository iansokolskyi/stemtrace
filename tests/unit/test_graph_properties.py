"""Randomized graph-linking tests: invariants hold after every event.

Seeds are fixed, so every run checks the same event sequences.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import TaskGraph
from stemtrace.server.store import GraphStore
from tests.graph_invariants import assert_graph_invariants, graph_state

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_TASK_IDS = [f"t{i}" for i in range(10)]
_GROUP_IDS = ["g0", "g1", "g2"]
_STATES = list(TaskState)
_UNLOCK = "celery.chord_unlock"
_NAMES = ["tests.task", "tests.other", _UNLOCK]


def _make_celery_shaped(
    rng: random.Random,
    group_of: dict[str, str | None],
    parent_of: dict[str, str | None],
) -> tuple[dict[str, str | None], set[str]]:
    """Make the plan Celery-shaped; returns each chord's named callback and
    the headers (the only tasks that name it).

    A group's members are sent together, by one earlier parent. A chord's
    last header sends its body (see ``_plan_chord_body``).
    """
    members_of = {
        g: [i for i, t in enumerate(_TASK_IDS) if group_of[t] == g] for g in _GROUP_IDS
    }
    for members in members_of.values():
        if members:
            shared = rng.choice([*_TASK_IDS[: min(members)], "ghost", None])
            for i in members:
                parent_of[_TASK_IDS[i]] = shared
    headers = {_TASK_IDS[i] for members in members_of.values() for i in members}
    callback_of: dict[str, str | None] = dict.fromkeys(_GROUP_IDS)
    used: set[str] = set()
    for g, members in members_of.items():
        if members:
            callback_of[g] = _plan_chord_body(
                rng,
                g,
                members,
                members_of=members_of,
                group_of=group_of,
                parent_of=parent_of,
                used=used,
            )
    return callback_of, headers


def _plan_chord_body(
    rng: random.Random,
    g: str,
    members: list[int],
    *,
    members_of: dict[str, list[int]],
    group_of: dict[str, str | None],
    parent_of: dict[str, str | None],
    used: set[str],
) -> str | None:
    """Plan what chord ``g``'s last header sends; returns the named callback.

    A later task outside any group (named), two later tasks stamped with
    the chord's group ID (a group body: the headers name the chord's own
    ID), or a later group (a chord or group body: they name that group).
    """
    last = _TASK_IDS[max(members)]
    loose = [
        t
        for t in _TASK_IDS[max(members) + 1 :]
        if group_of[t] is None and t not in used
    ]
    later_groups = [
        g2
        for g2, m2 in members_of.items()
        if m2 and g2 != g and g2 not in used and min(m2) > max(members)
    ]
    mode = rng.choice(["task", "task", "own", "group"])
    if mode == "own" and len(loose) >= 2:
        for t in rng.sample(loose, 2):
            group_of[t] = g
            parent_of[t] = last
            used.add(t)
        return g
    if mode == "group" and later_groups:
        body = rng.choice(later_groups)
        used.add(body)
        for i in members_of[body]:
            parent_of[_TASK_IDS[i]] = last
        return body
    callback = rng.choice([*loose, None]) if loose else None
    if callback is not None:
        used.add(callback)
        parent_of[callback] = last
    return callback


def _hang_callbacks_from_unlocks(
    rng: random.Random,
    callback_of: dict[str, str | None],
    parent_of: dict[str, str | None],
    name_of: dict[str, str],
    group_of: dict[str, str | None],
) -> None:
    """Make some chord callbacks children of ``celery.chord_unlock`` tasks.

    Celery's chord_unlock task fires the body, so the body's tasks report it
    as their parent. Callbacks of different chords may pick the same unlock
    task. Only earlier, group-less tasks qualify, so parents stay acyclic.
    """
    for callback in callback_of.values():
        if callback is None or callback not in _TASK_IDS or rng.random() < 0.4:
            continue
        index = _TASK_IDS.index(callback)
        unlocks = [
            t
            for t in _TASK_IDS[:index]
            if group_of[t] is None and t != parent_of[callback]
        ]
        if unlocks:
            unlock = rng.choice(unlocks)
            name_of[unlock] = _UNLOCK
            parent_of[callback] = unlock


def _random_events(
    seed: int, count: int, *, acyclic: bool = False, odd_names: bool = True
) -> list[TaskEvent]:
    """Events for a small pool of tasks with groups, chords and odd parents.

    Each task mostly keeps one group and one parent, like real Celery tasks,
    but some events drop or change them, report a missing or the task's own
    ID as parent, or name a chord callback. With ``acyclic``, every reported
    parent is an earlier task (or missing), as in Celery, where a task's
    parent is sent before it; a group's members share one parent; chords
    name distinct callbacks; a task keeps its group.

    Events carry a ``root_id`` and a callback may name a group ID (the
    chord's own included). With ``odd_names``, some tasks are named
    ``celery.chord_unlock`` (the parent of a chord callback, shared between
    chords) and some events report another name for their task ID, as a
    reused ID (``task.replace()``) does.
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
    callback_of = {g: rng.choice([*_TASK_IDS, *_GROUP_IDS, None]) for g in _GROUP_IDS}
    headers = set(_TASK_IDS)
    if acyclic:
        callback_of, headers = _make_celery_shaped(rng, group_of, parent_of)
    # A separate stream, so the plan above keeps its draws.
    extra = random.Random(seed ^ 0x5EED)
    names = ["tests.task", "tests.task", _UNLOCK] if odd_names else ["tests.task"]
    name_of = {t: extra.choice(names) for t in _TASK_IDS}
    root_of = {
        t: extra.choice(
            [None, None, t, "ghost", *(_TASK_IDS[:i] if acyclic else _TASK_IDS)]
        )
        for i, t in enumerate(_TASK_IDS)
    }
    if acyclic and odd_names:
        _hang_callbacks_from_unlocks(extra, callback_of, parent_of, name_of, group_of)
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
        callback = (
            callback_of[group_id]
            if group_id and task_id in headers and rng.random() < 0.5
            else None
        )
        events.append(
            TaskEvent(
                task_id=task_id,
                name=(
                    extra.choice(_NAMES)
                    if odd_names and extra.random() < 0.08
                    else name_of[task_id]
                ),
                root_id=None if extra.random() < 0.15 else root_of[task_id],
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


@pytest.mark.parametrize("seed", range(150))
def test_remove_node_mid_stream_matches_rebuild_from_remaining_events(
    seed: int,
) -> None:
    """Removing nodes partway through, then receiving the remaining events,
    leaves the graph a rebuild from the events the graph kept would give.

    The plan is Celery-shaped (see ``_make_celery_shaped``), with
    ``celery.chord_unlock`` tasks and IDs that report two names (as
    ``task.replace()`` reuses them). Victims are tasks, whose earlier events
    go with them, and GROUP/CHORD nodes, which keep their members: one is
    compared once a later member event recreated it, or if a rebuild has
    none either. A step that ends with a cycle still open only checks
    invariants (where it breaks depends on which link came first).
    """
    rng = random.Random(20_000 + seed)
    events = _random_events(seed, 60, acyclic=True)
    cuts = sorted(rng.sample(range(1, len(events) + 1), 3))
    graph = TaskGraph()
    kept: list[TaskEvent] = []
    position = 0
    for cut in cuts:
        for event in events[position:cut]:
            graph.add_event(event)
            kept.append(event)
        position = cut
        if not graph.nodes:
            continue
        victim = rng.choice(sorted(graph.nodes))
        graph.remove_node(victim)
        assert_graph_invariants(graph)
        kept = [e for e in kept if e.task_id != victim]
    for event in events[position:]:
        graph.add_event(event)
        kept.append(event)
    assert_graph_invariants(graph)

    rebuilt = TaskGraph()
    for event in kept:
        rebuilt.add_event(event)
    assert_graph_invariants(rebuilt)
    missing = set(rebuilt.nodes) - set(graph.nodes)
    if missing or graph._cycle_broken or rebuilt._cycle_broken:
        # A removed GROUP/CHORD no later event recreated, or a cycle still
        # open: only the invariants are comparable.
        assert all(m.startswith("group:") for m in missing), missing
        return
    assert graph_state(graph) == graph_state(rebuilt), (seed, cuts)


@pytest.mark.parametrize("seed", range(200))
def test_remove_one_task_then_continue_matches_rebuild(seed: int) -> None:
    """Removing one task (after all events, then at three random points),
    then receiving the rest of the events, leaves what the events the graph
    kept build (200 plans). These plans reach a task that a reused ID renames
    to or from ``celery.chord_unlock`` after a chord read its name.
    """
    rng = random.Random(seed)
    events = _random_events(seed, 60, acyclic=True)
    for attempt in range(4):
        cut = len(events) if attempt == 0 else rng.randint(1, len(events))
        prefix = TaskGraph()
        for event in events[:cut]:
            prefix.add_event(event)
        tasks = sorted(n for n in prefix.nodes if not n.startswith("group:"))
        if not tasks:
            continue
        victim = rng.choice(tasks)
        prefix.remove_node(victim)
        assert_graph_invariants(prefix)
        for event in events[cut:]:
            prefix.add_event(event)
        assert_graph_invariants(prefix)
        rebuilt = TaskGraph()
        for event in events[:cut]:
            if event.task_id != victim:
                rebuilt.add_event(event)
        for event in events[cut:]:
            rebuilt.add_event(event)
        if prefix._cycle_broken or rebuilt._cycle_broken:
            continue  # a cycle is still open: where it breaks depends on order
        assert graph_state(prefix) == graph_state(rebuilt), (seed, cut, victim)
