"""The graph list reuses its flow times for up to a second (rows L1-L4 of the flow-time spec).

The store counts changes (a stored event or an eviction); the list recomputes the flow times only
for a changed store and once ``FLOW_TIMES_REFRESH_SECONDS`` passed since the last compute. The
clock is injected, so nothing sleeps. The differential test compares a cached list with the
brute-force oracle of ``test_flow_time`` whenever the cache is fresh.
"""

import random
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import (
    FLOW_TIMES_REFRESH_SECONDS,
    GraphStore,
    WorkerRegistry,
)
from tests.unit.test_flow_time import (
    _CAPTURES,
    HOUR,
    NOW,
    _ending,
    _ev,
    _fuzz_events,
    _oracle,
    _recorded,
    _roots,
)


class FakeClock:
    """A monotonic reading that moves only when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingStore(GraphStore):
    """A store that counts how often the flow times are worked out."""

    computes = 0
    cost = 0.0  # seconds a compute takes on ``ticker``
    ticker: "FakeClock | None" = None

    def _flow_times_locked(self) -> dict[str, tuple[datetime, datetime]]:
        self.computes += 1
        if self.ticker is not None:
            self.ticker.advance(self.cost)
        return super()._flow_times_locked()


def _store(clock: FakeClock, max_nodes: int = 10_000) -> CountingStore:
    return CountingStore(max_nodes=max_nodes, clock=clock)


def test_two_lists_within_a_second_compute_once() -> None:
    """L1, also with a change between them."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("a", 3 * HOUR))
    store.add_event(_ev("b", 2 * HOUR))
    first = _roots(store)
    # A change inside the second: it moves "a" up only after the second.
    store.add_event(_ev("a-kid", timedelta(minutes=1), parent_id="a"))
    clock.advance(FLOW_TIMES_REFRESH_SECONDS - 0.001)
    second = _roots(store)
    assert store.computes == 1
    assert first == ["b", "a"]
    assert second == ["b", "a"]


def test_a_change_is_listed_once_a_second_has_passed() -> None:
    """L2: the second a change waits, and not before."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("a", 3 * HOUR))
    assert _roots(store) == ["a"]
    store.add_event(_ev("b", HOUR))
    clock.advance(FLOW_TIMES_REFRESH_SECONDS)
    assert _roots(store) == ["b", "a"]
    assert store.computes == 2


def test_a_new_event_of_a_known_flow_moves_it_after_the_second() -> None:
    """L2 for a flow's time, not only for a new root."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("a", 5 * HOUR))
    store.add_event(_ev("b", 3 * HOUR))
    assert _roots(store, from_date=NOW - 2 * HOUR) == []
    store.add_event(_ev("a-child", timedelta(minutes=1), parent_id="a"))
    assert _roots(store, from_date=NOW - 2 * HOUR) == []
    clock.advance(FLOW_TIMES_REFRESH_SECONDS)
    assert _roots(store, from_date=NOW - 2 * HOUR) == ["a"]


def test_a_store_that_did_not_change_never_recomputes() -> None:
    """L3: any amount of time, any filter."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("a", 3 * HOUR))
    store.add_event(_ev("b", HOUR))
    for hours in (0, 1, 24, 24 * 30):
        clock.advance(hours * 3600)
        _roots(store)
        _roots(store, from_date=NOW - 2 * HOUR)
        store.get_root_nodes_with_children(limit=1, to_date=NOW)
    assert store.computes == 1


def test_a_duplicate_event_is_not_a_change() -> None:
    """The counter moves for stored events only."""
    clock = FakeClock()
    store = _store(clock)
    event = _ev("a", HOUR)
    store.add_event(event)
    _roots(store)
    clock.advance(5)
    store.add_event(event)
    _roots(store)
    assert store.computes == 1


def test_a_change_inside_the_second_is_not_forgotten() -> None:
    """The cache stays marked stale: the first list after the second sees it."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("a", 3 * HOUR))
    store.add_event(_ev("b", 2 * HOUR))
    assert _roots(store) == ["b", "a"]
    clock.advance(0.5)
    store.add_event(_ev("a-kid", timedelta(minutes=1), parent_id="a"))
    assert _roots(store) == ["b", "a"]
    clock.advance(0.6)
    assert _roots(store) == ["a", "b"]
    assert store.computes == 2


def test_evicting_outside_an_event_invalidates() -> None:
    """L4 at the eviction itself: ``_maybe_evict`` marks the store changed."""
    clock = FakeClock()
    store = _store(clock, max_nodes=100)
    for i in range(10):
        store.add_event(_ev(f"f{i}", (10 - i) * HOUR))
    assert len(_roots(store)) == 10
    store._max_nodes = 5
    store._maybe_evict()
    clock.advance(FLOW_TIMES_REFRESH_SECONDS)
    assert _roots(store) == ["f9", "f8", "f7", "f6"]
    assert store.computes == 2


def test_evicting_inside_add_event_invalidates() -> None:
    """L4 through ingest: evicted flows leave the list after the second."""
    clock = FakeClock()
    store = _store(clock, max_nodes=10)
    for i in range(10):
        store.add_event(_ev(f"f{i}", (30 - i) * HOUR))
    assert len(_roots(store)) == 10
    store.add_event(_ev("new", HOUR))
    assert store.node_count == 9
    clock.advance(FLOW_TIMES_REFRESH_SECONDS)
    assert _roots(store) == ["new", *(f"f{i}" for i in range(9, 1, -1))]


def test_roots_that_left_or_joined_the_store_are_live_inside_the_second() -> None:
    """Eviction drops roots at once, a new root is listed, and the total is the live count."""
    clock = FakeClock()
    store = _store(clock, max_nodes=10)
    for i in range(10):
        store.add_event(_ev(f"f{i}", (30 - i) * HOUR))
    _roots(store)
    store.add_event(_ev("new", HOUR))  # evicts f0 and f1
    listed = _roots(store)
    assert store.computes == 1
    assert listed == ["new", *(f"f{i}" for i in range(9, 1, -1))]
    page, total = store.get_root_nodes(limit=3, offset=1)
    assert [n.task_id for n in page] == ["f9", "f8", "f7"]
    assert total == 9


def test_a_flow_stamped_ahead_of_the_server_stays_first_until_it_is_evicted() -> None:
    """C4: the clock is not clamped, so a fast worker keeps its flow on top."""
    clock = FakeClock()
    store = _store(clock, max_nodes=20)
    store.add_event(_ev("fast", 2 * HOUR))
    store.add_event(_ev("fast-child", -5 * 24 * HOUR, parent_id="fast"))
    for i in range(5):
        store.add_event(_ev(f"real{i}", timedelta(minutes=i)))
    clock.advance(FLOW_TIMES_REFRESH_SECONDS)
    assert _roots(store)[0] == "fast"
    assert _roots(store, from_date=NOW + 4 * 24 * HOUR) == ["fast"]
    for i in range(40):
        store.add_event(_ev(f"later{i}", timedelta(seconds=i)))
    assert store.get_node("fast") is None
    clock.advance(FLOW_TIMES_REFRESH_SECONDS)
    assert "fast" not in _roots(store)


def test_nodes_on_a_parent_loop_belong_to_no_flow() -> None:
    """A loop has no root: its times count for no flow and the list still works."""
    store = GraphStore()
    store.add_event(_ev("a", 3 * HOUR))
    store.add_event(_ev("l1", HOUR, parent_id="l2"))
    store.add_event(_ev("l2", HOUR, parent_id="l1"))
    with (
        store._lock
    ):  # the graph never builds a loop from events; replace() can leave one
        store._graph.nodes["l2"].parent_id = "l1"
    assert store._graph.nodes["l1"].parent_id == "l2"
    assert _roots(store) == ["a", "l2"]
    assert _roots(store, from_date=NOW - 2 * HOUR) == []


def test_listing_snapshots_the_roots_and_not_their_task_children() -> None:
    """The response reads the roots, and only a root with no events needs its children."""
    store = GraphStore()
    store.add_event(_ev("root", 3 * HOUR))
    for i in range(30):
        store.add_event(_ev(f"child{i}", HOUR, parent_id="root"))
    page = store.get_root_nodes_with_children(limit=10)
    assert set(page.related) == {"root"}
    assert page.containers == set()
    assert page.total == 1


def test_a_root_group_is_snapshotted_with_its_members() -> None:
    """A GROUP root has no events: its timing comes from the members."""
    store = GraphStore()
    for member in ("m1", "m2"):
        store.add_event(_ev(member, HOUR, group_id="g1"))
    page = store.get_root_nodes_with_children(limit=10)
    assert [r.task_id for r in page.roots] == ["group:g1"]
    assert set(page.related) == {"group:g1", "m1", "m2"}
    assert page.containers == set()


@pytest.mark.asyncio
async def test_graph_response_lists_task_children_before_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route orders children without holding them: a container goes last."""
    monkeypatch.setattr(params, "utcnow", lambda: NOW)
    store = GraphStore()
    store.add_event(_ev("root", 3 * HOUR))
    for member in ("m1", "m2"):
        store.add_event(_ev(member, HOUR, parent_id="root", group_id="g1"))
    store.add_event(_ev("plain", HOUR, parent_id="root"))
    live = store._graph.nodes["root"]
    assert live.children.index("group:g1") < live.children.index("plain")
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/graphs")).json()
    assert body["graphs"][0]["children"] == ["plain", "group:g1"]


def test_lists_and_events_from_threads_neither_fail_nor_tear() -> None:
    """Both sides take the store lock: no exception, and the final list is exact."""
    clock = FakeClock()
    store = _store(clock, max_nodes=200)
    errors: list[BaseException] = []
    barrier = threading.Barrier(5)

    def writer(worker: int) -> None:
        try:
            barrier.wait(timeout=30)
            for i in range(150):
                store.add_event(_ev(f"w{worker}-{i}", timedelta(minutes=i)))
                clock.advance(0.01)
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            errors.append(exc)

    def reader() -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(150):
                _roots(store)
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
    threads.append(threading.Thread(target=reader))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    clock.advance(FLOW_TIMES_REFRESH_SECONDS)
    assert _roots(store) == _oracle(store, None, None, None)


@pytest.mark.parametrize("chunk", range(5))
def test_cached_list_equals_the_oracle_whenever_the_cache_is_fresh(chunk: int) -> None:
    """Random recorded flows, with eviction, fed in bursts; the clock moves at random.

    The reference is the same events in an ungated store."""
    fresh_checks = 0
    stale_checks = 0
    for seed in range(chunk * 12, chunk * 12 + 12):
        rng = random.Random(seed)
        events = _fuzz_events(rng)
        clock = FakeClock()
        distinct = len({e.task_id for e in events})
        capacity = max(10, int(distinct * rng.choice([0.5, 0.8, 5])))
        store = _store(clock, max_nodes=capacity)
        twin = GraphStore(max_nodes=capacity, flow_cache_interval_s=0)
        stamps = sorted(e.timestamp for e in events)
        position = 0
        while position < len(events):
            batch = events[position : position + rng.randint(1, 6)]
            for event in batch:
                store.add_event(event)
                twin.add_event(event)
            position += len(batch)
            clock.advance(rng.choice([0, 0.3, 0.6, 1.0, 2.5]))
            from_date = rng.choice(
                [None, rng.choice(stamps), NOW - HOUR * rng.randint(1, 40)]
            )
            to_date = rng.choice(
                [None, datetime(2026, 10, rng.randint(14, 18), tzinfo=timezone.utc)]
            )
            # Membership is live even when the times are not.
            everyone, everyone_total = store.get_root_nodes(limit=1000)
            assert sorted(r.task_id for r in everyone) == sorted(
                store._graph.root_ids
            ), (seed, position)
            assert everyone_total == len(store._graph.root_ids), (seed, position)
            listed = store.get_root_nodes_with_children(
                limit=1000, from_date=from_date, to_date=to_date
            )
            cache = store._flow_cache
            assert cache is not None
            if cache.changes != store._changes:
                stale_checks += 1
                continue  # inside the second after a change: allowed to be older
            fresh_checks += 1
            # The uncached list (the same store with no gate), not the oracle:
            # the oracle walks children, the store walks parent chains, and
            # they differ while a flow's events are still arriving.
            plain = twin.get_root_nodes_with_children(
                limit=1000, from_date=from_date, to_date=to_date
            )
            where = (seed, position, from_date, to_date)
            assert [r.task_id for r in listed.roots] == [
                r.task_id for r in plain.roots
            ], where
            assert listed.total == plain.total, where
        clock.advance(FLOW_TIMES_REFRESH_SECONDS)
        assert _roots(store) == _oracle(store, None, None, None), seed
    assert fresh_checks > 20
    assert stale_checks > 5


@pytest.mark.parametrize("flow", sorted(_CAPTURES))
def test_every_flow_is_listed_after_each_event_of_its_capture(flow: str) -> None:
    """A GET after each event lists exactly the live roots, inside the gated second."""
    clock = FakeClock()
    store = _store(clock)
    other = _ev("other", 40 * HOUR)
    store.add_event(other)
    _roots(store)  # the cache is warm; nothing below moves the clock
    for event in _recorded(flow, "x-", _ending(flow, timedelta(minutes=5))):
        store.add_event(event)
        listed, total = store.get_root_nodes(limit=1000)
        live = sorted(store._graph.root_ids)
        assert sorted(r.task_id for r in listed) == live, (flow, event.task_id)
        assert total == len(live)
    assert store.computes == 1


def test_a_parent_that_arrives_inside_the_second_replaces_its_child_in_the_list() -> (
    None
):
    """The reviewer's PoC: the new root is listed, placed by its own flow's time."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("other", 3 * HOUR))
    store.add_event(_ev("child", HOUR, parent_id="parent"))
    assert _roots(store) == ["child", "other"]
    store.add_event(_ev("parent", 5 * HOUR))
    assert _roots(store) == ["parent", "other"]
    assert _roots(store, from_date=NOW - 2 * HOUR) == ["parent"]
    assert store.computes == 1


def test_a_group_that_forms_inside_the_second_is_listed() -> None:
    """Two members of one group make a GROUP root; the members stop being roots."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("other", 3 * HOUR))
    store.add_event(_ev("m1", HOUR, group_id="g1"))
    assert _roots(store) == ["m1", "other"]
    store.add_event(_ev("m2", 2 * HOUR, group_id="g1"))
    assert _roots(store) == ["group:g1", "other"]
    assert store.computes == 1


def test_a_root_missing_from_the_cache_is_placed_by_its_whole_flow() -> None:
    """On-demand time: the new root's latest is its newest node, not its own."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("other", 3 * HOUR))
    _roots(store)
    store.add_event(_ev("new", 9 * HOUR))
    store.add_event(_ev("new-child", HOUR, parent_id="new"))
    assert _roots(store) == ["new", "other"]
    assert _roots(store, from_date=NOW - 2 * HOUR) == ["new"]
    assert _roots(store, to_date=NOW - 8 * HOUR) == ["new"]
    assert store.get_root_nodes(limit=1, offset=1)[1] == 2


def test_an_unseen_root_with_a_dangling_or_repeated_child_is_timed_once_per_node() -> (
    None
):
    """The on-demand walk skips a missing child and a child listed twice."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("other", 3 * HOUR))
    _roots(store)
    store.add_event(_ev("new", 9 * HOUR))
    store.add_event(_ev("kid", HOUR, parent_id="new"))
    with store._lock:
        store._graph.nodes["new"].children.extend(["ghost", "kid"])
    assert _roots(store) == ["new", "other"]
    assert _roots(store, from_date=NOW - 2 * HOUR) == ["new"]


def test_the_gate_runs_from_the_end_of_a_compute() -> None:
    """S3: a compute of 1.2 s keeps the next one at least 1.2 s after it ended."""
    clock = FakeClock()
    store = _store(clock)
    store.cost, store.ticker = 1.2, clock
    store.add_event(_ev("a", 3 * HOUR))
    store.add_event(_ev("b", 2 * HOUR))
    _roots(store)  # computes from t0 to t0 + 1.2
    assert store.computes == 1
    store.add_event(_ev("a-kid", HOUR, parent_id="a"))
    clock.advance(1.19)
    assert _roots(store) == ["b", "a"]
    assert store.computes == 1
    clock.advance(0.02)
    assert _roots(store) == ["a", "b"]
    assert store.computes == 2


def test_the_gap_after_a_fast_compute_is_still_the_interval() -> None:
    """S3: the gap is max(interval, duration)."""
    clock = FakeClock()
    store = _store(clock)
    store.cost, store.ticker = 0.1, clock
    store.add_event(_ev("a", 3 * HOUR))
    store.add_event(_ev("b", 2 * HOUR))
    _roots(store)
    store.add_event(_ev("a-kid", HOUR, parent_id="a"))
    clock.advance(FLOW_TIMES_REFRESH_SECONDS - 0.01)
    assert _roots(store) == ["b", "a"]
    clock.advance(0.02)
    assert _roots(store) == ["a", "b"]


def test_a_root_with_a_missing_child_lists_it_as_a_container() -> None:
    """A child ID with no node sorts after tasks, as an unknown node always did."""
    store = GraphStore()
    store.add_event(_ev("root", HOUR))
    store.add_event(_ev("kid", HOUR, parent_id="root"))
    with store._lock:
        store._graph.nodes["root"].children.append("ghost")
    page = store.get_root_nodes_with_children(limit=10)
    assert page.containers == {"ghost"}


def test_an_eviction_inside_the_gap_keeps_the_flows_old_times_until_the_next_compute() -> (
    None
):
    """S4: the evicted newest node still counts, for up to the gap."""
    clock = FakeClock()
    store = _store(clock, max_nodes=20)
    store.add_event(_ev("r", 25 * HOUR))
    # The earliest start and the newest end: the oldest node, evicted first.
    started = TaskEvent(
        task_id="long",
        name="t.task",
        state=TaskState.STARTED,
        timestamp=NOW - 60 * HOUR,
        parent_id="r",
    )
    store.add_event(started)
    store.add_event(_ev("long", timedelta(minutes=1), parent_id="r"))
    store.add_event(_ev("mid", 10 * HOUR))
    assert _roots(store, from_date=NOW - HOUR) == ["r"]
    for i in range(18):
        store.add_event(_ev(f"f{i}", 30 * HOUR - timedelta(minutes=i)))
    assert store.get_node("long") is None
    assert _roots(store, from_date=NOW - HOUR) == ["r"]
    clock.advance(FLOW_TIMES_REFRESH_SECONDS)
    assert _roots(store, from_date=NOW - HOUR) == []


def test_the_task_name_filter_reads_live_nodes_inside_the_gap() -> None:
    """S5: the filter is not part of the cache: a task added inside the gap matches."""
    clock = FakeClock()
    store = _store(clock)
    store.add_event(_ev("a", 3 * HOUR, name="t.one"))
    store.add_event(_ev("b", 2 * HOUR, name="t.one"))
    assert store.get_root_nodes_with_children(task_names={"t.two"}).total == 0
    store.add_event(_ev("a-kid", HOUR, parent_id="a", name="t.two"))
    page = store.get_root_nodes_with_children(task_names={"t.two"})
    assert [r.task_id for r in page.roots] == ["a"]
    assert store.computes == 1
