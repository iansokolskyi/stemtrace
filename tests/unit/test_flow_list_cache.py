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

from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import (
    FLOW_TIMES_REFRESH_SECONDS,
    GraphStore,
    WorkerRegistry,
)
from tests.unit.test_flow_time import (
    HOUR,
    NOW,
    _ev,
    _fuzz_events,
    _oracle,
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

    def _flow_times_locked(self) -> dict[str, tuple[datetime, datetime]]:
        self.computes += 1
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
    store.add_event(_ev("c", HOUR))  # a change inside the second: not listed yet
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
    _roots(store)
    clock.advance(0.5)
    store.add_event(_ev("b", HOUR))
    assert _roots(store) == ["a"]
    clock.advance(0.6)
    assert _roots(store) == ["b", "a"]
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


def test_a_root_that_left_the_store_is_not_listed_before_the_refresh() -> None:
    """Inside the second the order is the cache's, but every listed root exists."""
    clock = FakeClock()
    store = _store(clock, max_nodes=10)
    for i in range(10):
        store.add_event(_ev(f"f{i}", (30 - i) * HOUR))
    _roots(store)
    store.add_event(_ev("new", HOUR))  # evicts f0 and f1
    listed = _roots(store)
    assert store.computes == 1
    assert listed == [f"f{i}" for i in range(9, 1, -1)]
    page, total = store.get_root_nodes(limit=3, offset=1)
    assert [n.task_id for n in page] == ["f8", "f7", "f6"]
    assert total == 8


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
    """Random recorded flows, with eviction, fed in bursts; the clock moves at random."""
    fresh_checks = 0
    stale_checks = 0
    for seed in range(chunk * 12, chunk * 12 + 12):
        rng = random.Random(seed)
        events = _fuzz_events(rng)
        clock = FakeClock()
        distinct = len({e.task_id for e in events})
        capacity = max(10, int(distinct * rng.choice([0.5, 0.8, 5])))
        store = _store(clock, max_nodes=capacity)
        stamps = sorted(e.timestamp for e in events)
        position = 0
        while position < len(events):
            for event in events[position : position + rng.randint(1, 6)]:
                store.add_event(event)
            position += 6
            clock.advance(rng.choice([0, 0.3, 0.6, 1.0, 2.5]))
            from_date = rng.choice(
                [None, rng.choice(stamps), NOW - HOUR * rng.randint(1, 40)]
            )
            to_date = rng.choice(
                [None, datetime(2026, 10, rng.randint(14, 18), tzinfo=timezone.utc)]
            )
            listed = store.get_root_nodes_with_children(
                limit=1000, from_date=from_date, to_date=to_date
            )
            cache = store._flow_cache
            assert cache is not None
            if cache.changes != store._changes:
                stale_checks += 1
                continue  # inside the second after a change: allowed to be older
            fresh_checks += 1
            expected = _oracle(store, from_date, to_date, None)
            where = (seed, position, from_date, to_date)
            assert [r.task_id for r in listed.roots] == expected, where
            assert listed.total == len(expected), where
    assert fresh_checks > 20
    assert stale_checks > 5
