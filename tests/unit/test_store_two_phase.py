"""``GraphStore`` list scans filter copied run rows outside the lock, then snapshot the page.

The oracle is the scan this replaced: filter the live nodes under the lock.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskNode
from stemtrace.server.store import GraphStore, _ensure_end_of_day, ensure_tz_aware
from tests.conftest import InterleavingLock

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

    from stemtrace.server.stats import RunRecord

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _old_get_nodes(
    store: GraphStore,
    *,
    limit: int = 100,
    offset: int = 0,
    state: TaskState | None = None,
    name_contains: str | None = None,
    name_exact: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    hostname: str | None = None,
    queue: str | None = None,
    exception_type: str | None = None,
    task_names: Collection[str] | None = None,
    sort_key: Callable[[RunRecord], int | None] | None = None,
) -> tuple[list[TaskNode], int]:
    """The scan before the change: every filter over the live nodes, under the lock."""
    name_lower = None if name_contains is None else name_contains.lower()
    to_dt = None if to_date is None else _ensure_end_of_day(to_date)
    from_dt = None if from_date is None else ensure_tz_aware(from_date)

    def matches(n: TaskNode) -> bool:
        return (
            n.node_type == NodeType.TASK
            and (state is None or n.state == state)
            and (name_lower is None or name_lower in n.name.lower())
            and (name_exact is None or n.name == name_exact)
            and (task_names is None or n.name in task_names)
            and (
                from_dt is None
                or (n.last_updated is not None and n.last_updated >= from_dt)
            )
            and (to_dt is None or (n.first_seen is not None and n.first_seen <= to_dt))
            and (hostname is None or n.hostname == hostname)
            and (queue is None or n.queue == queue)
            and (exception_type is None or n.exception_type == exception_type)
        )

    with store._lock:
        nodes = [n for n in store._graph.nodes.values() if matches(n)]
        if sort_key is None:
            nodes.sort(key=lambda n: n.last_updated or _MIN, reverse=True)
            return [n.snapshot() for n in nodes[offset : offset + limit]], len(nodes)
        ranked = [
            (store._runs[n.task_id], n.last_updated or _MIN, n.task_id, n)
            for n in nodes
        ]
    ranked.sort(key=lambda r: r[2])
    ranked.sort(key=lambda r: r[1], reverse=True)
    keyed = [r for r in ranked if sort_key(r[0]) is not None]
    keyed.sort(key=lambda r: sort_key(r[0]), reverse=True)  # type: ignore[arg-type,return-value]
    unkeyed = [r for r in ranked if sort_key(r[0]) is None]
    ordered = [r[3] for r in keyed + unkeyed]
    return [n.snapshot() for n in ordered[offset : offset + limit]], len(ordered)


def _dump(page: list[TaskNode], total: int) -> str:
    return json.dumps([[n.model_dump_json() for n in page], total])


def _ev(
    task_id: str,
    state: TaskState = TaskState.STARTED,
    seconds: int = 0,
    **fields: Any,
) -> TaskEvent:
    fields.setdefault("name", "app.work")
    return TaskEvent(
        task_id=task_id,
        state=state,
        timestamp=T0 + timedelta(seconds=seconds),
        **fields,
    )


# -- the differential fuzz ---------------------------------------------------

_NAMES = ["app.work", "app.Work2", "pay.charge", "pay.refund"]
_HOSTS = [None, "w1@a", "w2@a", "w3@b"]
_QUEUES = [None, "q1", "q2"]
_EXC = [None, "ValueError", "KeyError", "TimeoutError"]
_STATES = list(TaskState)


class _Hits:
    """What the generator reached, so a sweep can show it covers the rules."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def hit(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1


def _random_event(rng: random.Random, pool: int, hits: _Hits) -> TaskEvent:
    task = f"t{rng.randrange(pool)}"
    state = rng.choice(_STATES)
    name = rng.choice(_NAMES[:2]) if rng.random() < 0.9 else rng.choice(_NAMES)
    parent = f"t{rng.randrange(pool)}" if rng.random() < 0.5 else None
    group = f"g{rng.randrange(3)}" if rng.random() < 0.15 else None
    typed = state in (TaskState.RETRY, TaskState.FAILURE) or rng.random() < 0.1
    if state == TaskState.RETRY:
        hits.hit("retry")
    return TaskEvent(
        task_id=task,
        name=name,
        state=state,
        timestamp=T0 + timedelta(seconds=rng.randrange(-300, 300)),  # skew both ways
        retries=rng.randrange(4),
        hostname=rng.choice(_HOSTS),
        queue=rng.choice(_QUEUES),
        exception_type=rng.choice(_EXC) if typed else None,
        exception="boom" if typed else None,
        parent_id=parent,
        root_id=parent,
        group_id=group,
    )


def _random_filters(rng: random.Random) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "limit": rng.choice([1, 3, 10, 100]),
        "offset": rng.choice([0, 0, 1, 4]),
    }
    options: list[tuple[str, Callable[[], Any]]] = [
        ("state", lambda: rng.choice(_STATES)),
        ("name_contains", lambda: rng.choice(["work", "PAY", "x", "2"])),
        ("name_exact", lambda: rng.choice(_NAMES)),
        ("from_date", lambda: T0 + timedelta(seconds=rng.randrange(-300, 300))),
        ("to_date", lambda: T0 + timedelta(seconds=rng.randrange(-300, 300))),
        ("hostname", lambda: rng.choice(_HOSTS[1:])),
        ("queue", lambda: rng.choice(_QUEUES[1:])),
        ("exception_type", lambda: rng.choice(_EXC[1:])),
        ("task_names", lambda: set(rng.sample(_NAMES, 2))),
        ("sort_key", lambda: rng.choice(_SORT_KEYS)),
    ]
    for key, make in options:
        if rng.random() < 0.25:
            filters[key] = make()
    return filters


_SORT_KEYS: list[Callable[[RunRecord], int | None]] = [
    lambda r: r.runtime_ms,
    lambda r: r.queue_wait_ms,
]


def _scan_both(store: GraphStore, filters: dict[str, Any]) -> tuple[str, str]:
    return (
        _dump(*store.get_nodes(**filters)),
        _dump(*_old_get_nodes(store, **filters)),
    )


def _fuzz_store(seed: int, hits: _Hits) -> Iterator[GraphStore]:
    """Yield a store after each batch of random events (small, so it evicts)."""
    rng = random.Random(seed)
    pool = rng.choice([6, 12, 30])
    store = GraphStore(max_nodes=rng.choice([8, 15, 40]))
    seen: set[str] = set()
    for _ in range(rng.choice([4, 6])):
        for _ in range(25):
            event = _random_event(rng, pool, hits)
            seen.add(event.task_id)
            store.add_event(event)
        if seen - set(store._graph.nodes):
            hits.hit("evicted")
        yield store


@pytest.mark.parametrize("seed", range(25))
def test_scan_pages_and_totals_equal_the_old_scan_on_a_quiet_store(seed: int) -> None:
    rng = random.Random(seed * 7919)
    for store in _fuzz_store(seed, _Hits()):
        for _ in range(12):
            new, old = _scan_both(store, _random_filters(rng))
            assert new == old


def test_fuzz_generator_reaches_eviction_retries_and_reuse() -> None:
    hits = _Hits()
    reused = 0
    for seed in range(25):
        for store in _fuzz_store(seed, hits):
            nodes = list(store._graph.nodes.values())
            reused += sum(len({e.name for e in n.events}) > 1 for n in nodes)
    assert hits.counts["retry"] > 250
    assert reused > 8
    assert hits.counts["evicted"] > 8


def _index_facts(store: GraphStore) -> tuple[list[Any], list[Any]]:
    with store._lock:
        nodes = {
            n.task_id: n
            for n in store._graph.nodes.values()
            if n.node_type == NodeType.TASK
        }
        records = dict(store._runs)
    got = [
        (
            r.task_id,
            r.name,
            r.hostname,
            r.queue,
            r.latest_exception_type,
            r.first_seen,
        )
        for r in records.values()
    ]
    want = [
        (n.task_id, n.name, n.hostname, n.queue, n.exception_type, n.first_seen)
        for n in nodes.values()
    ]
    return got, want


@pytest.mark.parametrize("seed", range(25))
def test_run_rows_carry_the_nodes_latest_facts_after_every_event(seed: int) -> None:
    rng = random.Random(seed)
    store = GraphStore(max_nodes=rng.choice([8, 40]))
    hits = _Hits()
    for step in range(120):
        store.add_event(_random_event(rng, rng.choice([5, 20]), hits))
        if step % 6 == 0:
            got, want = _index_facts(store)
            assert got == want


@pytest.mark.parametrize("hosts", [("w1@a", "w2@a"), ("w2@a", "w1@a")])
def test_run_row_hostname_on_an_exact_rank_tie_is_the_scans_whatever_the_arrival_order(
    hosts: tuple[str, str],
) -> None:
    """Two events with one state, attempt and stamp rank alike; the first in lifecycle order wins."""
    store = GraphStore()
    for host in hosts:
        store.add_event(_ev("t", TaskState.STARTED, 5, hostname=host))

    node = store.get_node("t")
    assert node is not None
    (record,) = store.run_records()
    assert record.hostname == node.hostname
    page, _ = store.get_nodes(hostname=node.hostname)
    assert [n.task_id for n in page] == ["t"]


# -- phase 2 under concurrent ingestion --------------------------------------


class _FiringNames(set[str]):
    """A ``task_names`` set that runs ``action`` once, when the filter first asks it."""

    def __init__(self, names: Collection[str], action: Callable[[], None]) -> None:
        super().__init__(names)
        self._action: Callable[[], None] | None = action

    def __contains__(self, item: object) -> bool:
        action, self._action = self._action, None
        if action is not None:
            action()
        return super().__contains__(item)


def _scan_with(
    store: GraphStore, action: Callable[[], None], **filters: Any
) -> tuple[list[TaskNode], int]:
    """A ``get_nodes`` whose filter phase runs ``action`` on the store (as ingestion would)."""
    return store.get_nodes(task_names=_FiringNames(_NAMES, action), **filters)


def _seed_three(store: GraphStore) -> None:
    for i, tid in enumerate(["a", "b", "c"]):
        store.add_event(
            _ev(
                tid,
                TaskState.FAILURE,
                i,
                hostname="w1@a",
                queue="q1",
                exception_type="ValueError",
            )
        )


def _move_b(store: GraphStore, **fields: Any) -> Callable[[], None]:
    def action() -> None:
        base: dict[str, Any] = {
            "hostname": "w1@a",
            "queue": "q1",
            "exception_type": "ValueError",
        }
        store.add_event(_ev("b", TaskState.FAILURE, 600, **{**base, **fields}))

    return action


@pytest.mark.parametrize(
    ("filters", "change"),
    [
        ({"hostname": "w1@a"}, {"hostname": "w2@a"}),
        ({"queue": "q1"}, {"queue": "q2"}),
        ({"exception_type": "ValueError"}, {"exception_type": "KeyError"}),
    ],
    ids=["hostname", "queue", "exception_type"],
)
@pytest.mark.parametrize("sort_key", [None, _SORT_KEYS[0]], ids=["newest", "sorted"])
def test_a_row_that_stopped_matching_meanwhile_stays_with_its_current_snapshot(
    filters: dict[str, Any],
    change: dict[str, str],
    sort_key: Callable[[RunRecord], int | None] | None,
) -> None:
    store = GraphStore()
    _seed_three(store)

    page, total = _scan_with(
        store, _move_b(store, **change), sort_key=sort_key, **filters
    )

    by_id = {n.task_id: n for n in page}
    assert sorted(by_id) == ["a", "b", "c"]
    assert total == 3
    key = next(iter(change))
    assert getattr(by_id["b"], key) == change[key]


def test_a_row_that_left_the_state_filter_meanwhile_shows_its_new_state() -> None:
    store = GraphStore()
    for i, tid in enumerate(["a", "b", "c"]):
        store.add_event(_ev(tid, TaskState.STARTED, i))

    def finish() -> None:
        store.add_event(_ev("b", TaskState.SUCCESS, 60))

    page, total = _scan_with(store, finish, state=TaskState.STARTED)

    assert {n.task_id: n.state for n in page} == {
        "a": TaskState.STARTED,
        "b": TaskState.SUCCESS,
        "c": TaskState.STARTED,
    }
    assert total == 3


@pytest.mark.parametrize("sort_key", [None, _SORT_KEYS[0]], ids=["newest", "sorted"])
def test_a_row_evicted_meanwhile_is_dropped_and_total_is_the_filter_instants(
    sort_key: Callable[[RunRecord], int | None] | None,
) -> None:
    store = GraphStore(max_nodes=10)
    for i in range(9):
        store.add_event(_ev(f"old{i}", TaskState.STARTED, i))

    def flood() -> None:
        for i in range(4):
            store.add_event(_ev(f"new{i}", TaskState.STARTED, 100 + i))

    page, total = _scan_with(store, flood, limit=100, sort_key=sort_key)

    live = set(store._graph.nodes)
    assert len(live) < 13  # eviction ran
    assert {n.task_id for n in page} == live & {f"old{i}" for i in range(9)}
    assert total == 9


def test_a_row_added_meanwhile_is_not_listed() -> None:
    store = GraphStore()
    _seed_three(store)

    page, total = _scan_with(store, lambda: store.add_event(_ev("late", seconds=900)))

    assert sorted(n.task_id for n in page) == ["a", "b", "c"]
    assert total == 3


def test_an_offset_past_the_surviving_rows_gives_an_empty_page_with_the_old_total() -> (
    None
):
    store = GraphStore(max_nodes=10)
    for i in range(9):
        store.add_event(_ev(f"old{i}", TaskState.STARTED, i))

    def flood() -> None:
        for i in range(4):
            store.add_event(_ev(f"new{i}", TaskState.STARTED, 100 + i))

    # Newest first: the oldest run, old0, is last (offset 8) and is evicted first.
    page, total = _scan_with(store, flood, offset=8, limit=5)

    assert [n.task_id for n in page] == []
    assert total == 9


def test_a_rows_sort_position_stays_the_filter_instants() -> None:
    store = GraphStore()
    for i, tid in enumerate(["a", "b", "c"]):
        store.add_event(_ev(tid, TaskState.STARTED, i))

    page, _ = _scan_with(store, lambda: store.add_event(_ev("a", seconds=900)))

    assert [n.task_id for n in page] == ["c", "b", "a"]


@pytest.mark.parametrize("seed", range(25))
def test_page_under_concurrent_ingestion_is_the_filter_instants_rows_as_they_are_now(
    seed: int,
) -> None:
    rng = random.Random(seed * 104729)
    hits = _Hits()
    for store in _fuzz_store(seed, hits):
        filters = _random_filters(rng)
        filters.pop("task_names", None)
        sort_key = filters.pop("sort_key", None)
        before, total = _old_get_nodes(
            store, **{**filters, "limit": 10_000, "offset": 0}, sort_key=sort_key
        )
        want_ids = [n.task_id for n in before][
            filters["offset"] : filters["offset"] + filters["limit"]
        ]

        same_run = dict(store._graph.nodes)

        def ingest(store: GraphStore = store) -> None:
            for _ in range(rng.choice([1, 5, 30])):
                store.add_event(_random_event(rng, 40, hits))

        page, got_total = _scan_with(store, ingest, sort_key=sort_key, **filters)

        live = store._graph.nodes
        # An ID evicted and re-created meanwhile is another run: dropped too.
        assert [n.task_id for n in page] == [
            i for i in want_ids if live.get(i) is same_run[i]
        ]
        assert got_total == total
        for node in page:
            assert (
                node.model_dump_json()
                == live[node.task_id].snapshot().model_dump_json()
            )


# -- identity between phases (an evicted ID that came back is a different run) --


def _evict_and_recreate(store: GraphStore, task_id: str) -> Callable[[], None]:
    """Flood the store so ``task_id`` is evicted, then let a late event re-create it."""

    def action() -> None:
        for i in range(6):
            store.add_event(_ev(f"flood{i}", TaskState.STARTED, 500 + i))
        assert store.get_node(task_id) is None  # evicted
        store.add_event(_ev(task_id, TaskState.STARTED, 900, hostname="h2@x"))

    return action


@pytest.mark.parametrize("sort_key", [None, _SORT_KEYS[0]], ids=["newest", "sorted"])
def test_an_id_evicted_and_recreated_meanwhile_is_dropped_from_the_task_page(
    sort_key: Callable[[RunRecord], int | None] | None,
) -> None:
    store = GraphStore(max_nodes=8)
    for i in range(8):
        store.add_event(_ev(f"t{i}", TaskState.STARTED, i, hostname="h1@x"))

    page, total = _scan_with(
        store, _evict_and_recreate(store, "t0"), limit=100, sort_key=sort_key
    )

    assert "t0" not in {n.task_id for n in page}
    assert total == 8
    assert store.get_node("t0") is not None  # the new run exists, it is just not listed


def _flows(store: GraphStore, count: int) -> None:
    for i in range(count):
        store.add_event(_ev(f"r{i}", TaskState.STARTED, i))
        store.add_event(
            _ev(f"c{i}", TaskState.STARTED, i, parent_id=f"r{i}", root_id=f"r{i}")
        )


class _FiringDate(datetime):
    """A ``from_date`` that runs ``action`` once, when the root filter first compares it."""

    action: Callable[[], None] | None = None

    def __le__(self, other: datetime) -> bool:
        action, type(self).action = type(self).action, None
        if action is not None:
            action()
        return datetime.__le__(self, other)


def _firing_date(action: Callable[[], None]) -> datetime:
    _FiringDate.action = action
    return _FiringDate(2000, 1, 1, tzinfo=timezone.utc)


def _flow_page(store: GraphStore, action: Callable[[], None], **kw: Any) -> Any:
    return store.get_root_nodes_with_children(from_date=_firing_date(action), **kw)


def test_filtered_graph_page_drops_a_root_that_got_a_parent_meanwhile() -> None:
    store = GraphStore()
    _flows(store, 5)
    page = _flow_page(
        store,
        lambda: store.add_event(
            _ev("r4", TaskState.STARTED, 50, parent_id="r0", root_id="r0")
        ),
        limit=3,
    )

    assert [n.task_id for n in page.roots] == ["r3", "r2"]  # newest first, r4 left
    assert page.total == 5


def test_filtered_graph_page_drops_a_root_evicted_meanwhile() -> None:
    store = GraphStore(max_nodes=10)
    _flows(store, 5)

    def flood() -> None:
        for i in range(4):
            store.add_event(_ev(f"f{i}", TaskState.STARTED, 100 + i))

    page = _flow_page(store, flood, limit=10)

    live = set(store._graph.nodes)
    assert {n.task_id for n in page.roots} == {f"r{i}" for i in range(5)} & live
    assert len(live) < 14  # eviction ran
    assert page.total == 5


def test_filtered_graph_page_does_not_list_a_root_added_meanwhile() -> None:
    store = GraphStore()
    _flows(store, 3)

    page = _flow_page(
        store, lambda: store.add_event(_ev("late", seconds=900)), limit=10
    )

    assert [n.task_id for n in page.roots] == ["r2", "r1", "r0"]
    assert page.total == 3


def test_filtered_graph_page_snapshots_a_roots_children_with_it() -> None:
    store = GraphStore()
    _flows(store, 2)

    def child_arrives() -> None:
        store.add_event(
            _ev("late-child", TaskState.STARTED, 60, parent_id="r1", root_id="r1")
        )

    page = _flow_page(store, child_arrives, limit=10)

    by_id = {n.task_id: n for n in page.roots}
    assert by_id["r1"].children == ["c1", "late-child"]


def test_filtered_graph_page_drops_a_root_evicted_and_recreated_meanwhile() -> None:
    store = GraphStore(max_nodes=10)
    _flows(store, 5)

    def churn() -> None:
        for i in range(4):
            store.add_event(_ev(f"f{i}", TaskState.STARTED, 100 + i))
        assert store.get_node("r0") is None
        store.add_event(_ev("r0", TaskState.STARTED, 900, hostname="h2@x"))

    page = _flow_page(store, churn, limit=10)

    assert "r0" not in {n.task_id for n in page.roots}
    assert page.total == 5


def test_unfiltered_graph_page_is_one_hold_so_a_parent_cannot_arrive_between() -> None:
    store = GraphStore()
    _flows(store, 11)
    store.add_event(_ev("anchor", TaskState.STARTED, 650))
    order = [r.task_id for r in store.get_root_nodes_with_children(limit=12).roots]
    victims = iter(o for o in order if o != "anchor")

    def top_root_gets_a_parent() -> None:
        store.add_event(
            _ev(
                next(victims),
                TaskState.STARTED,
                700,
                parent_id="anchor",
                root_id="anchor",
            )
        )

    lock = InterleavingLock(store._lock, top_root_gets_a_parent)
    store._lock = lock  # type: ignore[assignment]  # same protocol

    page = store.get_root_nodes_with_children(limit=3)

    assert lock.interleavings == 1  # one acquisition: no point between two holds
    assert len(page.roots) == 3
    assert page.total == len(store._graph.root_ids)


# -- roots that came or went since the flow times were computed -------------------


def test_root_delta_is_the_net_change_since_the_mark() -> None:
    store = GraphStore()
    _flows(store, 3)
    graph = store._graph
    assert graph.mark_roots() == frozenset({"r0", "r1", "r2"})

    store.add_event(_ev("r3", TaskState.STARTED, 10))  # a new root
    store.add_event(_ev("r1", TaskState.STARTED, 11, parent_id="r0", root_id="r0"))
    store.add_event(_ev("r2", TaskState.STARTED, 12, parent_id="r0", root_id="r0"))

    added, removed = graph.root_delta()
    assert added == ("r3",)
    assert set(removed) == {"r1", "r2"}
    assert graph.root_count == len(graph.root_ids)


def _warm(store: GraphStore) -> None:
    store.get_root_nodes_with_children(limit=1)  # computes and publishes the times


def test_graph_page_inside_the_gate_lists_a_new_root_in_its_time_order() -> None:
    store = GraphStore(flow_cache_interval_s=3600)
    _flows(store, 3)
    _warm(store)
    store.add_event(_ev("mid", TaskState.STARTED, 1))  # newer than r0, older than r2

    for kwargs in ({}, {"from_date": T0 - timedelta(days=1)}):
        page = store.get_root_nodes_with_children(limit=10, **kwargs)
        assert [n.task_id for n in page.roots] == ["r2", "mid", "r1", "r0"], kwargs
        assert page.total == 4


def test_graph_page_inside_the_gate_drops_a_root_that_got_a_parent() -> None:
    store = GraphStore(flow_cache_interval_s=3600)
    _flows(store, 3)
    _warm(store)
    store.add_event(_ev("r1", TaskState.STARTED, 50, parent_id="r0", root_id="r0"))

    for kwargs in ({}, {"from_date": T0 - timedelta(days=1)}):
        page = store.get_root_nodes_with_children(limit=10, **kwargs)
        assert [n.task_id for n in page.roots] == ["r2", "r0"], kwargs
        assert page.total == 2


def test_root_delta_is_empty_for_a_root_that_left_and_came_back() -> None:
    store = GraphStore()
    store.add_event(_ev("solo", TaskState.STARTED, 0))
    store.add_event(_ev("other", TaskState.STARTED, 1))
    graph = store._graph
    graph.mark_roots()

    graph.remove_node("solo")
    assert graph.root_delta() == ((), ("solo",))
    store.add_event(_ev("solo", TaskState.STARTED, 20))

    assert graph.root_delta() == ((), ())
    assert graph.root_count == 2
