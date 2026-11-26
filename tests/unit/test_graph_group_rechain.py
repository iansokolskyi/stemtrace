"""Group chain evidence: the gate, idempotence, equivalence with main and cost.

The group-wide gate on chain links (``_GroupTally.chains_allowed``) flips as
members arrive. These tests pin what it draws, check that a full rechain of
every member changes nothing, compare every prefix with ``tests/graph_oracle.py``
(a frozen copy of ``graph.py`` before the flip rechain was bounded), and count
``_rechain`` calls for serial arrival.
"""

import json
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import TaskGraph
from tests import graph_oracle
from tests.graph_invariants import assert_graph_invariants, graph_state

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_P, _S, _OK, _RETRY = (
    TaskState.PENDING,
    TaskState.STARTED,
    TaskState.SUCCESS,
    TaskState.RETRY,
)
_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)


class _Clock:
    """Strictly increasing timestamps, one second apart."""

    def __init__(self) -> None:
        self.now = 0

    def tick(self) -> datetime:
        self.now += 1
        return _T0 + timedelta(seconds=self.now)


def _event(
    clock: _Clock,
    task_id: str,
    state: TaskState,
    parent_id: str | None = None,
    group_id: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name=f"tests.{task_id}",
        state=state,
        timestamp=clock.tick(),
        parent_id=parent_id,
        group_id=group_id,
    )


# -- the gate -------------------------------------------------------------


def _group_view(graph: TaskGraph) -> dict[str, tuple[str | None, list[str]]]:
    return {k: (n.parent_id, sorted(n.children)) for k, n in graph.nodes.items()}


def test_add_event_pending_only_member_blocks_chain_link_until_it_starts() -> None:
    clock = _Clock()
    graph = TaskGraph()
    for state in (_P, _S):
        graph.add_event(_event(clock, "x", state, None, "g"))
    graph.add_event(_event(clock, "L", _OK))
    graph.add_event(_event(clock, "y", _S, "L", "g"))

    # x reported no parent, so y's parent L is a chain link inside the group.
    assert _group_view(graph)["group:g"] == (None, ["L", "x"])
    assert graph.nodes["L"].parent_id == "group:g"
    assert graph.nodes["y"].parent_id == "L"

    # z's PENDING carries the group but nothing about its parent: L may be
    # the group's own parent, so it leaves the group until z reports.
    graph.add_event(_event(clock, "z", _P, None, "g"))
    assert _group_view(graph)["group:g"] == ("L", ["x", "y", "z"])
    assert graph.nodes["L"].parent_id is None
    assert graph.nodes["y"].parent_id == "group:g"

    graph.add_event(_event(clock, "z", _S, None, "g"))
    assert _group_view(graph)["group:g"] == (None, ["L", "x", "z"])
    assert graph.nodes["y"].parent_id == "L"


# -- rechain idempotence (I2) ------------------------------------------------


def _chains(graph: Any) -> dict[str, list[str]]:
    return {m: list(c) for m, c in graph._chain_of.items()}


def assert_rechain_idempotent(graph: TaskGraph) -> None:
    """Recomputing every counted member's chain changes nothing."""
    before = (_normal(graph), _chains(graph))
    votes = {g: dict(t.vote_of) for g, t in graph._groups.items()}
    for tally in list(graph._groups.values()):
        for member_id in list(tally.counted()):
            graph._rechain(graph.nodes[member_id])
    assert (_normal(graph), _chains(graph)) == before
    assert {g: dict(t.vote_of) for g, t in graph._groups.items()} == votes


def _normal(graph: Any) -> dict[str, object]:
    """``graph_state`` with enums as values, so two ``graph`` modules compare."""
    state = graph_state(graph)
    nodes: dict[str, tuple[object, ...]] = state["nodes"]  # type: ignore[assignment]
    state["nodes"] = {k: (v[0], v[1].value, *v[2:]) for k, v in nodes.items()}
    return state


# -- streams -----------------------------------------------------------------

# An operation is an event to add or a task ID to remove.
_Op = TaskEvent | str


def _flow_ops(events: list[dict[str, Any]], order: str, seed: int = 0) -> list[_Op]:
    """A capture in its own order, shuffled, or one task's events at a time."""
    parsed = [TaskEvent.model_validate(e) for e in events]
    if order == "shuffle":
        random.Random(seed).shuffle(parsed)
    elif order == "serial":
        firsts = list(dict.fromkeys(e.task_id for e in parsed))
        parsed = [e for t in firsts for e in parsed if e.task_id == t]
    return list(parsed)


def _scaled_ops(capture: str, prefixes: list[str], n: int, order: str) -> list[_Op]:
    """One member of a capture (and its link) cloned ``n`` times, other events kept."""
    events = _CAPTURES[capture]
    templates = [
        next(e["task_id"] for e in events if e["task_id"].startswith(p))
        for p in prefixes
    ]
    rng = random.Random(7)
    clones = [{t: (t if i == 0 else f"{t}-{i}") for t in templates} for i in range(n)]

    def remap(event: dict[str, Any], ids: dict[str, str]) -> TaskEvent:
        copy = dict(event)
        for field in ("task_id", "parent_id", "root_id"):
            if copy.get(field) in ids:
                copy[field] = ids[copy[field]]
        return TaskEvent.model_validate(copy)

    own = [e for e in events if e["task_id"] in templates]
    ops: list[_Op] = []
    placed = False
    for event in events:
        if event["task_id"] not in templates:
            ops.append(TaskEvent.model_validate(event))
        elif order == "burst":
            ops.extend(remap(event, ids) for ids in clones)
        elif not placed:
            placed = True
            ops.extend(remap(e, ids) for ids in clones for e in own)
    if order == "shuffle":
        per: dict[str, list[_Op]] = {}
        for op in ops:
            per.setdefault(op.task_id, []).append(op)  # type: ignore[union-attr]
        keys = [op.task_id for op in ops]  # type: ignore[union-attr]
        rng.shuffle(keys)
        ops = [per[k].pop(0) for k in keys]
    return ops


_SCALED = {
    "plain_group": ("chain_group_tsum_add@celery5.2", ["a8d276"]),
    "group_of_chains": ("group_of_chains", ["0b99083c", "d6490971"]),
    "chord": ("chord", ["6b8e8b05"]),
    "chord_field": ("chord@field", ["a2a414"]),
    "group_in_task": ("group_spawned_in_task", ["85f111b3"]),
    "group": ("group", ["2a63f4db"]),
    "replace_into_group": (
        "group_chain_replace3@worker_process",
        ["3483b5"],
    ),
}


_SEEDS = 800
_MIN_FLIPS = {"event": 20, "remove": 5}


def _group_ops(seed: int) -> list[_Op]:
    """A small group arriving in a seeded order, with link tasks and removals.

    Members carry the group ID on every event and no parent on PENDING. Each
    member is top-level, reports a shared parent, a one-child link task or a
    missing task. Orders: serial, burst, random interleave, publisher lead.
    Some streams stamp a chord, some remove a task part way.
    """
    rng = random.Random(seed)
    clock = _Clock()
    members = [f"m{i}" for i in range(rng.randint(2, 6))]
    parent_of: dict[str, str | None] = {}
    links: dict[str, list[TaskEvent]] = {}
    for member in members:
        kind = rng.choice(["top", "top", "shared", "link", "link", "ghost"])
        if kind == "link":
            link = f"L{member}"
            parent_of[member] = link
            links[link] = [
                _event(clock, link, s, rng.choice([None, "shared"]), None)
                for s in rng.sample([_P, _S, _OK], rng.randint(1, 3))
            ]
        else:
            parent_of[member] = {"top": None, "shared": "shared", "ghost": "ghost"}[
                kind
            ]
    chains: dict[str, list[TaskEvent]] = {}
    for member in members:
        states = rng.choice(
            [[_P, _S, _OK], [_P, _S], [_P], [_S, _OK], [_P, _S, _RETRY]]
        )
        chains[member] = [
            _event(
                clock,
                member,
                s,
                None if s == _P and rng.random() < 0.85 else parent_of[member],
                "g",
            )
            for s in states
        ]
    streams = [*chains.values(), *links.values()]
    mode = rng.choice(["serial", "burst", "random", "lead"])
    queues = [list(s) for s in streams]
    ordered: list[TaskEvent] = []
    if mode == "serial":
        ordered = [e for q in queues for e in q]
    elif mode == "burst":
        for depth in range(3):
            ordered += [q[depth] for q in queues if depth < len(q)]
    else:
        lead = rng.randint(0, 3) if mode == "lead" else 0
        ordered = [q.pop(0) for q in queues[:lead] if q]
        while any(queues):
            queue = rng.choice([q for q in queues if q])
            ordered.append(queue.pop(0))
    if rng.random() < 0.15:  # a chord stamp on one member's first event
        index = rng.randrange(len(ordered))
        ordered[index] = ordered[index].model_copy(update={"chord_callback_id": "cb"})
    ops: list[_Op] = list(ordered)
    for _ in range(rng.choice([0, 1, 2, 3])):
        victim = rng.choice([*members, *links])
        ops.insert(rng.randrange(len(ops) + 1), victim)
    return ops


# -- differential against main (I1) ------------------------------------------


class _FlipCells:
    """Counts of gate flips by direction and whether a chained member took part."""

    def __init__(self) -> None:
        self.cells: Counter[str] = Counter()

    def record(
        self, before: dict[str, tuple[bool, bool]], graph: Any, via: str
    ) -> None:
        for group_id, tally in graph._groups.items():
            if group_id not in before:
                continue
            was_allowed, had_chain = before[group_id]
            allowed = tally.chains_allowed()
            if allowed == was_allowed:
                continue
            has_chain = any(m in graph._chain_of for m in tally.counted())
            direction = "allowed->blocked" if was_allowed else "blocked->allowed"
            chained = had_chain if was_allowed else has_chain
            kind = "chained member" if chained else "no chained member"
            self.cells[f"{direction}, {kind}, via {via}"] += 1

    @staticmethod
    def snapshot(graph: Any) -> dict[str, tuple[bool, bool]]:
        return {
            g: (
                t.chains_allowed(),
                any(m in graph._chain_of for m in t.counted()),
            )
            for g, t in graph._groups.items()
        }


def _apply(graph: Any, op: _Op) -> str:
    if isinstance(op, str):
        graph.remove_node(op)
        return "remove"
    graph.add_event(op)
    return "event"


def _run_differential(ops: list[_Op], cells: _FlipCells | None = None) -> None:
    """Feed ops to head and to main's algorithm; compare after each one."""
    head, base = TaskGraph(), graph_oracle.TaskGraph()
    for op in ops:
        before = _FlipCells.snapshot(base)
        via = _apply(base, op)
        if cells is not None:
            cells.record(before, base, via)
        _apply(head, op)
        assert _normal(head) == _normal(base), op
        assert _chains(head) == _chains(base), op
        assert_graph_invariants(head)
        assert_rechain_idempotent(head)


_FLOWS = sorted(_CAPTURES)


@pytest.mark.parametrize("flow", _FLOWS)
def test_add_event_capture_matches_main_after_every_event(flow: str) -> None:
    for _label, ops in _flow_streams_of(flow):
        _run_differential(ops)


def _flow_streams_of(flow: str) -> list[tuple[str, list[_Op]]]:
    events = _CAPTURES[flow]
    return [
        (f"{flow}/capture", _flow_ops(events, "capture")),
        (f"{flow}/serial", _flow_ops(events, "serial")),
        *((f"{flow}/shuffle{s}", _flow_ops(events, "shuffle", s)) for s in range(20)),
    ]


@pytest.mark.parametrize("order", ["serial", "burst", "shuffle"])
@pytest.mark.parametrize("shape", sorted(_SCALED))
def test_add_event_scaled_group_matches_main_after_every_event(
    shape: str, order: str
) -> None:
    capture, prefixes = _SCALED[shape]
    _run_differential(_scaled_ops(capture, prefixes, 50, order))


@pytest.mark.parametrize("seed", range(_SEEDS))
def test_add_event_random_group_matches_main_after_every_event(seed: int) -> None:
    _run_differential(_group_ops(seed))


def test_group_generator_reaches_every_flip_cell() -> None:
    cells = _FlipCells()
    for seed in range(_SEEDS):
        _run_differential(_group_ops(seed), cells)
    wanted = [
        f"{direction}, {kind}, via {via}"
        for direction in ("allowed->blocked", "blocked->allowed")
        for kind in ("chained member", "no chained member")
        for via in ("event", "remove")
    ]
    missing = {
        c: cells.cells[c]
        for c in wanted
        if cells.cells[c] < _MIN_FLIPS[c.rsplit(" ", 1)[1]]
    }
    assert not missing, (missing, dict(cells.cells))


# -- cost ---------------------------------------------------------------------


def _serial_group_events(n: int, order: str) -> list[TaskEvent]:
    """N top-level members of one group, PENDING carrying the group ID."""
    clock = _Clock()
    per_member = [
        [_event(clock, f"m{i}", s, None, "g") for s in (_P, _S, _OK)] for i in range(n)
    ]
    if order == "burst":
        return [per_member[i][d] for d in range(3) for i in range(n)]
    return [e for member in per_member for e in member]


class _RechainCounter:
    """Counts ``TaskGraph._rechain`` calls from when it is installed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = TaskGraph._rechain
        counter = self

        def counting(self: TaskGraph, *args: Any, **kwargs: Any) -> None:
            counter.calls += 1
            real(self, *args, **kwargs)

        monkeypatch.setattr(TaskGraph, "_rechain", counting)


@pytest.mark.parametrize("order", ["serial", "burst"])
def test_add_event_group_members_arriving_one_by_one_rechain_linearly(
    monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    counter = _RechainCounter(monkeypatch)
    calls = {}
    for n in (50, 100, 200, 400):
        counter.calls = 0
        graph = TaskGraph()
        for event in _serial_group_events(n, order):
            graph.add_event(event)
        calls[n] = counter.calls
        assert len(graph.nodes["group:g"].children) == n
        assert calls[n] <= 3 * n + 10, (n, calls)
    assert calls[400] <= 10 * calls[50], calls  # 8x the members, not 64x


def test_remove_node_of_pending_only_member_from_big_group_rechains_few(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = TaskGraph()
    for event in _serial_group_events(300, "serial"):
        graph.add_event(event)
    graph.add_event(_event(_Clock(), "late", _P, None, "g"))
    counter = _RechainCounter(monkeypatch)
    graph.remove_node("late")
    assert counter.calls <= 5, counter.calls
