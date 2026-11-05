"""The flow list reads an index: clamped receive times, and only what the response needs.

P2 (a worker clock ahead of the server can't pin its flows on top) and P5 (the list
snapshots the roots, not every child) of the flow-time spec.
"""

import random
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskNode
from stemtrace.server import store as store_module
from stemtrace.server.store import GraphStore
from tests.unit.test_flow_time import _fuzz_events

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


class Clock:
    """The server's clock, set by the test."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock(T0)
    monkeypatch.setattr(store_module, "_utcnow", fake)
    return fake


def _ev(
    task_id: str,
    stamp: datetime,
    *,
    parent_id: str | None = None,
    group_id: str | None = None,
    state: TaskState = TaskState.SUCCESS,
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="t.task",
        state=state,
        timestamp=stamp,
        parent_id=parent_id,
        group_id=group_id,
    )


def _roots(store: GraphStore, **filters: object) -> list[str]:
    roots, _ = store.get_root_nodes(limit=1000, **filters)  # type: ignore[arg-type]
    return [r.task_id for r in roots]


def test_flow_from_a_clock_running_ahead_does_not_stay_on_top(clock: Clock) -> None:
    """A stamp 5 days ahead counts as the time it was received; later flows sort above."""
    store = GraphStore()
    store.add_event(_ev("fast", T0 + 5 * DAY))
    clock.now = T0 + HOUR
    store.add_event(_ev("real", T0 + HOUR - timedelta(minutes=1)))
    assert _roots(store) == ["real", "fast"]
    assert _roots(store, from_date=T0 + timedelta(minutes=30)) == ["real"]


def test_child_from_a_clock_running_ahead_does_not_pin_its_flow(clock: Clock) -> None:
    """The clamp applies to every node of a flow, not only the root."""
    store = GraphStore()
    store.add_event(_ev("r", T0 - HOUR))
    store.add_event(_ev("kid", T0 + 5 * DAY, parent_id="r"))
    clock.now = T0 + HOUR
    store.add_event(_ev("other", T0 + HOUR - timedelta(minutes=1)))
    assert _roots(store) == ["other", "r"]


def test_to_date_uses_the_clamped_first_time(clock: Clock) -> None:
    """A fast clock's first time can't put a flow started today after the day ends."""
    store = GraphStore()
    store.add_event(_ev("fast", T0 + 5 * DAY))
    assert _roots(store, to_date=datetime(2026, 10, 18)) == ["fast"]


def test_flow_from_a_clock_running_behind_sorts_lower(clock: Clock) -> None:
    """A stamp 3 hours behind is taken as it is."""
    store = GraphStore()
    store.add_event(_ev("slow", T0 - 3 * HOUR))
    store.add_event(_ev("real", T0 - timedelta(minutes=1)))
    assert _roots(store) == ["real", "slow"]
    assert _roots(store, from_date=T0 - HOUR) == ["real"]


def test_later_event_of_a_task_is_clamped_by_its_own_receive_time(clock: Clock) -> None:
    """The first event was on time; the next one, stamped ahead, counts as received."""
    store = GraphStore()
    store.add_event(_ev("t", T0 - HOUR, state=TaskState.STARTED))
    store.add_event(_ev("t", T0 + 2 * DAY))
    clock.now = T0 + 3 * HOUR
    store.add_event(_ev("y", T0 + 3 * HOUR - timedelta(minutes=1)))
    assert _roots(store) == ["y", "t"]
    assert _roots(store, from_date=T0 + HOUR) == ["y"]


def test_list_snapshots_the_roots_and_the_children_of_a_synthetic_root_only(
    clock: Clock,
) -> None:
    """A task root's children aren't copied: the response reads only their types."""
    store = GraphStore()
    store.add_event(_ev("root", T0 - HOUR))
    for i in range(5):
        store.add_event(_ev(f"kid{i}", T0 - HOUR, parent_id="root"))
    store.add_event(_ev("m1", T0 - HOUR, group_id="g"))
    store.add_event(_ev("m2", T0 - HOUR, group_id="g"))
    page = store.get_root_nodes_with_children(limit=10)
    assert sorted(page[1]) == ["group:g", "m1", "m2", "root"]
    assert page[1]["group:g"].node_type == NodeType.GROUP


def test_list_names_the_type_of_every_child_of_a_listed_root(clock: Clock) -> None:
    store = GraphStore()
    store.add_event(_ev("root", T0 - HOUR))
    store.add_event(_ev("a", T0 - HOUR, parent_id="root", group_id="g"))
    store.add_event(_ev("b", T0 - HOUR, parent_id="root", group_id="g"))
    store.add_event(_ev("c", T0 - HOUR, parent_id="root"))
    page = store.get_root_nodes_with_children(limit=10)
    assert page[2] == 1
    assert page[3] == {"group:g": NodeType.GROUP, "c": NodeType.TASK}


# ---- differential fuzz against the #278 one-pass computation --------------

NO_TIME = datetime.min.replace(tzinfo=timezone.utc)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _root_of_each(nodes: Mapping[str, TaskNode]) -> dict[str, str | None]:
    """#278's walker: each node's root, one walk per node (a parent loop has none)."""
    root_of: dict[str, str | None] = {}
    for start, node in nodes.items():
        parent = node.parent_id
        if parent is None or parent not in nodes:
            root_of[start] = start
            continue
        if parent in root_of and start not in root_of:
            root_of[start] = root_of[parent]
            continue
        if start in root_of:
            continue
        path: list[str] = []
        current = start
        while current not in root_of:
            root_of[current] = None
            path.append(current)
            parent = nodes[current].parent_id
            if parent is None or parent not in nodes:
                root_of[current] = current
                path.pop()
                break
            current = parent
        root = root_of[current]
        for node_id in path:
            root_of[node_id] = root
    return root_of


Received = dict[tuple[str, str], datetime]


def _key(event: TaskEvent) -> tuple[str, str]:
    return event.task_id, event.model_dump_json()


def _node_times(
    node: TaskNode, received: Received | None
) -> tuple[datetime | None, datetime | None]:
    """A node's (first, latest): #278's own times, or each event clamped to its receipt."""
    if received is None:
        return node.first_seen, node.last_updated
    if not node.events:
        return None, None
    first = min(min(e.timestamp, received[_key(e)]) for e in node.events)
    last = node.events[-1]
    return first, min(last.timestamp, received[_key(last)])


def _one_pass(
    store: GraphStore, received: Received | None
) -> dict[str, tuple[datetime, datetime]]:
    """#278's ``_flow_times_locked``: every root's (first, latest), in one pass."""
    nodes = store._graph.nodes
    firsts: dict[str, datetime] = {}
    lasts: dict[str, datetime] = {}
    for node_id, root in _root_of_each(nodes).items():
        first, last = _node_times(nodes[node_id], received)
        if root is None or last is None or first is None:
            continue
        if last > lasts.get(root, NO_TIME):
            lasts[root] = last
        if first < firsts.get(root, first):
            firsts[root] = first
        else:
            firsts.setdefault(root, first)
    return {
        root: (firsts.get(root, NO_TIME), lasts.get(root, NO_TIME))
        for root in store._graph.root_ids
    }


class CountingStore(GraphStore):
    """A store that counts which paths of the index an op took."""

    def __init__(self, max_nodes: int) -> None:
        super().__init__(max_nodes=max_nodes)
        self.hits: Counter[str] = Counter()

    def _walk_flow(self, root: str) -> None:
        top = self._graph.nodes.get(root)
        gone = top is None or top.parent_id is not None
        self.hits["walk_of_gone_root" if gone else "walk"] += 1
        super()._walk_flow(root)

    def _fold(self, root: str, times: tuple[datetime, datetime] | None) -> None:
        self.hits["fold"] += 1
        super()._fold(root, times)

    def _retime_flow(
        self,
        root: str,
        old: tuple[datetime, datetime] | None,
        new: tuple[datetime, datetime],
    ) -> None:
        walks = self.hits["walk"]
        super()._retime_flow(root, old, new)
        self.hits["retime_walk" if self.hits["walk"] > walks else "retime_fold"] += 1

    def _is_unindexed_leaf(self, task_id: str) -> bool:
        leaf = super()._is_unindexed_leaf(task_id)
        self.hits["new_leaf" if leaf else "lone_move_not_new_leaf"] += 1
        return leaf

    def _time_node(self, node: TaskNode, stored: TaskEvent, received: datetime) -> None:
        if stored.timestamp > received:
            self.hits["clamped_event"] += 1
        if node.events[-1] is not stored:
            self.hits["event_not_last"] += 1
        super()._time_node(node, stored, received)

    def _index_flows(self, stored: TaskEvent, received: datetime) -> None:
        before = len(self._graph.nodes)
        super()._index_flows(stored, received)
        self.hits["ops"] += 1
        if len(self._graph.nodes) < before + 1 and stored.task_id not in self._flow_of:
            self.hits["event_node_evicted"] += 1


def _check_index(
    store: GraphStore, received: Received | None, where: object
) -> None:
    """The index equals #278's one-pass computation, and holds nothing else."""
    graph = store._graph
    expected = _one_pass(store, received)
    for root in graph.root_ids:
        got = store._flow_times.get(root, (NO_TIME, NO_TIME))
        assert got == expected[root], (where, root)
    assert set(store._flow_times) <= set(graph.root_ids), where
    assert set(store._node_times) <= set(graph.nodes), where
    for node_id, root in _root_of_each(graph.nodes).items():
        if root is not None and root in expected:
            assert store._flow_of.get(node_id) == root, (where, node_id)
    assert set(store._flow_of) <= set(graph.nodes), where


def _ops(rng: random.Random) -> list[TaskEvent]:
    """Recorded flows in random order, with missed events and replays."""
    events = [e for e in _fuzz_events(rng) if rng.random() > 0.1]
    replays = [e for e in events if rng.random() < 0.05]
    events.extend(replays)
    rng.shuffle(events)
    return events


def _run_seed(seed: int, hits: Counter[str], *, clamped: bool) -> int:
    rng = random.Random(seed)
    events = _ops(rng)
    ids = len({e.task_id for e in events})
    store = CountingStore(max_nodes=rng.choice([100_000, max(8, int(ids * 0.6))]))
    received: Received = {}
    clock = store_module._utcnow
    try:
        for step, event in enumerate(events):
            if clamped:
                now = event.timestamp + timedelta(minutes=rng.randint(-180, 180))
            else:
                now = FAR_FUTURE
            store_module._utcnow = lambda now=now: now  # type: ignore[misc]
            node = store._graph.nodes.get(event.task_id)
            replay = node is not None and event in node.events  # ignored: keeps its receipt
            store.add_event(event)
            if not replay:
                received[_key(event)] = now  # an evicted event can come back later
            _check_index(store, received if clamped else None, (seed, step))
    finally:
        store_module._utcnow = clock
    hits.update(store.hits)
    return len(events)


@pytest.mark.parametrize("clamped", [False, True])
@pytest.mark.parametrize("chunk", range(5))
def test_index_equals_the_one_pass_computation_after_every_op(
    chunk: int, *, clamped: bool
) -> None:
    """Captured flows shuffled, thinned, replayed and evicting; 40 seeds per chunk."""
    hits: Counter[str] = Counter()
    ops = sum(
        _run_seed(seed, hits, clamped=clamped)
        for seed in range(chunk * 40, chunk * 40 + 40)
    )
    assert ops > 1000
    if chunk == 0:
        print("hits", clamped, dict(hits))
