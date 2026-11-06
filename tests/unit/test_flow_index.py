"""The flow list reads an index: clamped receive times, and only what the response needs.

P2 (a worker clock ahead of the server can't pin its flows on top) and P5 (the list
snapshots the roots, not every child) of the flow-time spec.
"""

import itertools
import random
from collections import Counter
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskNode
from stemtrace.server import store as store_module
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry
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


def test_retry_on_a_correct_clock_after_a_clock_ahead_still_moves_the_latest_time(
    clock: Clock,
) -> None:
    """X1: worker A (5 days ahead) sends STARTED and RETRY; worker B retries it correctly."""
    store = GraphStore()
    store.add_event(_ev("t", T0 + 5 * DAY, state=TaskState.STARTED))
    store.add_event(_ev("t", T0 + 5 * DAY, state=TaskState.RETRY))
    clock.now = T0 + 2 * HOUR
    store.add_event(
        TaskEvent(
            task_id="t",
            name="t.task",
            state=TaskState.STARTED,
            timestamp=T0 + 2 * HOUR,
            retries=1,
        )
    )
    store.add_event(
        TaskEvent(
            task_id="t",
            name="t.task",
            state=TaskState.SUCCESS,
            timestamp=T0 + 2 * HOUR,
            retries=1,
        )
    )
    assert store._flow_times["t"] == (T0, T0 + 2 * HOUR)
    assert _roots(store, from_date=T0 + HOUR) == ["t"]


def test_pending_stamped_after_the_start_does_not_count_once_the_task_ran(
    clock: Clock,
) -> None:
    """X2: a late PENDING (sender's clock ahead) is ignored for the latest time."""
    store = GraphStore()
    store.add_event(_ev("t", T0 - 2 * HOUR, state=TaskState.STARTED))
    store.add_event(_ev("t", T0 - HOUR, state=TaskState.PENDING))
    assert store._flow_times["t"] == (T0 - 2 * HOUR, T0 - 2 * HOUR)
    other = GraphStore()
    other.add_event(_ev("t", T0 - HOUR, state=TaskState.PENDING))
    assert other._flow_times["t"] == (T0 - HOUR, T0 - HOUR)
    other.add_event(_ev("t", T0 - 2 * HOUR, state=TaskState.STARTED))
    assert other._flow_times["t"] == (T0 - 2 * HOUR, T0 - 2 * HOUR)


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


def test_list_names_the_container_children_of_a_listed_root(clock: Clock) -> None:
    store = GraphStore()
    store.add_event(_ev("root", T0 - HOUR))
    store.add_event(_ev("a", T0 - HOUR, parent_id="root", group_id="g"))
    store.add_event(_ev("b", T0 - HOUR, parent_id="root", group_id="g"))
    store.add_event(_ev("c", T0 - HOUR, parent_id="root"))
    page = store.get_root_nodes_with_children(limit=10)
    assert page.total == 1
    assert page.containers == {"group:g"}


# ---- differential fuzz against the #278 one-pass computation --------------

_BEFORE_START = (TaskState.PENDING, TaskState.RECEIVED)
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
    """A node's (first, latest) by the spec's R1, written out from the events.

    Each event counts as ``min(stamp, receipt)`` (just the stamp without receipts).
    First is the oldest over all events. Latest is the newest over the events that
    count: once the task has an event past PENDING/RECEIVED, those two stop counting.
    """
    if not node.events:
        return None, None

    def clamp(event: TaskEvent) -> datetime:
        if received is None:
            return event.timestamp
        return min(event.timestamp, received[_key(event)])

    later = [e for e in node.events if e.retries > 0 or e.state not in _BEFORE_START]
    return min(clamp(e) for e in node.events), max(
        clamp(e) for e in later or node.events
    )


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
    """A store that counts which paths of the index an op took, and the nodes it visited."""

    def __init__(self, max_nodes: int) -> None:
        super().__init__(max_nodes=max_nodes)
        self.hits: Counter[str] = Counter()

    def _flow_nodes(self, root: str) -> Iterator[str]:
        for task_id in super()._flow_nodes(root):
            self.hits["visited"] += 1
            yield task_id

    def _walk_flow(self, root: str) -> None:
        top = self._graph.nodes.get(root)
        gone = top is None or top.parent_id is not None
        self.hits["walk_of_gone_root" if gone else "walk"] += 1
        super()._walk_flow(root)

    def _fold(self, root: str, span: tuple[datetime, datetime] | None) -> None:
        self.hits["fold"] += 1
        super()._fold(root, span)

    def _retime_flow(
        self,
        root: str,
        old: tuple[datetime, datetime] | None,
        new: tuple[datetime, datetime],
    ) -> None:
        walks = self.hits["walk"]
        super()._retime_flow(root, old, new)
        self.hits["retime_walk" if self.hits["walk"] > walks else "retime_fold"] += 1

    def _relink(self, moved: set[str]) -> tuple[set[str], set[str]]:
        merged = len(self._merged)
        result = super()._relink(moved)
        self.hits["merged_whole"] += len(self._merged) - merged
        self.hits["moved"] += len(moved)
        return result

    def _time_node(self, stored: TaskEvent, received: datetime) -> None:
        if stored.timestamp > received:
            self.hits["clamped_event"] += 1
        super()._time_node(stored, received)

    def _index_flows(self, stored: TaskEvent, received: datetime) -> None:
        super()._index_flows(stored, received)
        self.hits["ops"] += 1


def _check_index(store: GraphStore, received: Received | None, where: object) -> None:
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
            assert store._root_of(node_id) == root, (where, node_id)
    assert set(store._flow_of) == set(graph.nodes), where
    assert set(store._merged).isdisjoint(graph.root_ids), where
    assert store._containers == {
        n.task_id for n in graph.nodes.values() if n.node_type != NodeType.TASK
    }, where


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
            replay = (
                node is not None and event in node.events
            )  # ignored: keeps its receipt
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
    # Every path of the index is taken, or the sweep proves nothing about it.
    for path in (
        "walk",
        "walk_of_gone_root",
        "retime_fold",
        "retime_walk",
        "merged_whole",
    ):
        assert hits[path] > 0, path
    assert (hits["clamped_event"] > 0) == clamped


@pytest.mark.parametrize("length", [2, 3, 4])
def test_index_holds_for_every_arrival_order_of_a_chain(length: int) -> None:
    """A parent can arrive after its children: swept over every order of 2-4 links."""
    for order in itertools.permutations(range(length)):
        store = GraphStore()
        for step, i in enumerate(order):
            parent = f"c{i - 1}" if i else None
            store.add_event(
                _ev(f"c{i}", T0 - timedelta(minutes=10 * (i + 1)), parent_id=parent)
            )
            _check_index(store, None, (order, step))
        assert store._graph.root_ids == ["c0"]
        assert store._flow_times["c0"] == (
            T0 - timedelta(minutes=10 * length),
            T0 - timedelta(minutes=10),
        )


def test_index_holds_when_a_missing_parent_leaves_its_child_a_root() -> None:
    """A child whose parent never arrives is a flow of its own until the parent does."""
    store = GraphStore()
    store.add_event(_ev("kid", T0 - HOUR, parent_id="late"))
    _check_index(store, None, "orphan")
    assert store._flow_times == {"kid": (T0 - HOUR, T0 - HOUR)}
    store.add_event(_ev("late", T0 - 3 * HOUR))
    _check_index(store, None, "adopted")
    assert store._flow_times == {"late": (T0 - 3 * HOUR, T0 - HOUR)}


def test_index_drops_a_flow_when_its_root_is_evicted() -> None:
    """The oldest unit goes first; its children, still stored, become roots."""
    store = GraphStore(max_nodes=4)
    store.add_event(_ev("r", T0 - 9 * HOUR))
    for i in range(3):
        store.add_event(_ev(f"k{i}", T0 - HOUR, parent_id="r"))
    store.add_event(_ev("x", T0))
    _check_index(store, None, "evicted")
    assert "r" not in store._flow_times
    assert "r" not in store._flow_of


def _app(store: GraphStore) -> FastAPI:
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))
    return app


@pytest.mark.asyncio
async def test_graphs_endpoint_draws_tasks_before_containers() -> None:
    """The child order of a listed root is unchanged by the lighter snapshot."""
    store = GraphStore()
    store.add_event(_ev("root", T0 - HOUR))
    store.add_event(_ev("a", T0 - HOUR, parent_id="root", group_id="g"))
    store.add_event(_ev("b", T0 - HOUR, parent_id="root", group_id="g"))
    store.add_event(_ev("c", T0 - HOUR, parent_id="root"))
    assert store._graph.nodes["root"].children == ["group:g", "c"]
    transport = httpx.ASGITransport(app=_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/graphs")).json()
    assert body["graphs"][0]["children"] == ["c", "group:g"]


@pytest.mark.asyncio
async def test_graphs_endpoint_item_times_are_the_roots_own_not_clamped(
    clock: Clock,
) -> None:
    """Only the list's order and filters use the clamped time."""
    store = GraphStore()
    store.add_event(_ev("fast", T0 + 5 * DAY))
    transport = httpx.ASGITransport(app=_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        item = (await client.get("/api/graphs")).json()["graphs"][0]
    stamp = (T0 + 5 * DAY).isoformat().replace("+00:00", "Z")
    assert item["first_seen"] == stamp
    assert item["last_updated"] == stamp


def test_index_tolerates_a_graph_that_breaks_its_own_rules() -> None:
    """A parent that isn't stored, a parent loop, and a root left with no timed node."""
    store = GraphStore()
    store.add_event(_ev("a", T0 - HOUR))
    nodes = store._graph.nodes
    for task_id, parent in [("lost", "gone"), ("x", "y"), ("y", "x")]:
        nodes[task_id] = TaskNode(
            task_id=task_id, name="t.task", state=TaskState.PENDING, parent_id=parent
        )
    assert store._root_above("a") == "a"
    assert store._root_above("lost") is None
    assert store._root_above("x") is None
    assert store._relink({"lost"}) == (set(), set())  # no root to join
    nodes["group:g"] = TaskNode(
        task_id="group:g",
        name="group",
        state=TaskState.PENDING,
        node_type=NodeType.GROUP,
        group_id="g",
    )
    store._graph._roots["group:g"] = None
    assert store._relink({"group:g"}) == (set(), {"group:g"})  # no time to fold
    store._flow_times["group:g"] = (T0, T0)  # as if its timed members had all left
    store._walk_flow("group:g")
    assert "group:g" not in store._flow_times


def _visits_per_event(events: list[TaskEvent]) -> float:
    store = CountingStore(max_nodes=100_000)
    for event in events:
        store.add_event(event)
    _check_index(store, None, "visits")
    return store.hits["visited"] / len(events)


@pytest.mark.parametrize("groups", [100, 400])
def test_groups_under_one_root_cost_the_same_per_event_at_any_flow_size(
    groups: int,
) -> None:
    """R2: one root launching groups of 2; a group forming moves nodes inside one flow."""
    events = [_ev("root", T0 - HOUR)]
    for i in range(groups):
        events += [
            _ev(f"a{i}", T0 - HOUR, parent_id="root", group_id=f"g{i}"),
            _ev(f"b{i}", T0 - HOUR, parent_id="root", group_id=f"g{i}"),
        ]
    assert _visits_per_event(events) < 1


@pytest.mark.parametrize("length", [100, 400])
def test_chain_arriving_last_link_first_costs_the_same_per_event_at_any_length(
    length: int,
) -> None:
    """R2: each arrival puts the whole flow so far under a new root, which merges."""
    events = [
        _ev(f"c{k}", T0 - HOUR, parent_id=f"c{k - 1}" if k else None)
        for k in range(length, -1, -1)
    ]
    assert _visits_per_event(events) < 1


def test_receive_time_is_read_inside_the_store_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: so receipts are monotone in the order events apply."""
    store = GraphStore()
    held: list[bool] = []

    def reading() -> datetime:
        held.append(store._lock._is_owned())  # type: ignore[attr-defined]
        return T0

    monkeypatch.setattr(store_module, "_utcnow", reading)
    store.add_event(_ev("t", T0 - HOUR))
    assert held == [True]


@pytest.mark.asyncio
async def test_graphs_endpoint_draws_a_child_the_store_lacks_after_the_tasks() -> None:
    """A listed ID with no node sorts last, as it did before the lighter snapshot."""
    store = GraphStore()
    store.add_event(_ev("root", T0 - HOUR))
    store.add_event(_ev("c", T0 - HOUR, parent_id="root"))
    store._graph.nodes["root"].children.insert(0, "ghost")
    transport = httpx.ASGITransport(app=_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/graphs")).json()
    assert body["graphs"][0]["children"] == ["c", "ghost"]
