"""A flow's time on ``/api/graphs``: the newest (and oldest) time over the whole flow.

Rows F1-F7 of the flow-time spec, a depth and position sweep, an API check, and a
differential fuzz. The oracle shares only the nodes with the store: it walks every
flow with ``get_graph_from_root`` (the children, not the parent chains the store
walks) and writes each rule out itself. The fuzz re-ids and re-times the recorded
Celery flows of ``tests/data/celery_flows.json`` at random.
"""

import json
import random
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskNode
from stemtrace.server.api import params
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry, _root_of_each

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
NO_TIME = datetime.min.replace(tzinfo=timezone.utc)

_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_ID_FIELDS = (
    "task_id",
    "parent_id",
    "root_id",
    "group_id",
    "chord_id",
    "chord_callback_id",
)


def _time(raw: dict[str, Any], key: str = "timestamp") -> datetime:
    """A time field of a recorded event or a response (``Z`` spelled out for 3.10)."""
    return datetime.fromisoformat(raw[key].replace("Z", "+00:00"))


def _ev(
    task_id: str,
    ago: timedelta,
    *,
    state: TaskState = TaskState.SUCCESS,
    parent_id: str | None = None,
    group_id: str | None = None,
    name: str = "t.task",
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=state,
        timestamp=NOW - ago,
        parent_id=parent_id,
        group_id=group_id,
    )


def _roots(store: GraphStore, **filters: Any) -> list[str]:
    roots, _ = store.get_root_nodes(limit=1000, **filters)
    return [r.task_id for r in roots]


def _recorded(
    flow: str, prefix: str, shift: timedelta, jitter: random.Random | None = None
) -> list[TaskEvent]:
    """A recorded flow with ids prefixed and times moved by ``shift`` (some jittered)."""
    out = []
    for raw in _CAPTURES[flow]:
        moved = dict(raw)
        for key in _ID_FIELDS:
            if moved[key] is not None:
                moved[key] = prefix + moved[key]
        stamp = _time(raw) + shift
        if jitter is not None and jitter.random() < 0.3:
            stamp += timedelta(minutes=jitter.randint(-600, 600))
        moved["timestamp"] = stamp.isoformat()
        out.append(TaskEvent.model_validate(moved))
    return out


def _ending(flow: str, ago: timedelta) -> timedelta:
    """The shift that puts a recorded flow's last event ``ago`` before NOW."""
    end = max(_time(e) for e in _CAPTURES[flow])
    return NOW - end - ago


def test_flow_with_old_root_and_recent_failed_child_is_in_window_and_first() -> None:
    """F1."""
    store = GraphStore()
    store.add_event(_ev("a-root", 7 * HOUR))
    store.add_event(
        _ev(
            "a-child",
            timedelta(minutes=1),
            state=TaskState.FAILURE,
            parent_id="a-root",
        )
    )
    store.add_event(_ev("b-other", 3 * HOUR))
    assert _roots(store, from_date=NOW - 6 * HOUR) == ["a-root", "b-other"]
    assert _roots(store, from_date=NOW - 2 * HOUR) == ["a-root"]
    assert _roots(store) == ["a-root", "b-other"]


@pytest.mark.parametrize("length", [2, 3, 4, 5])
def test_chain_is_as_new_as_its_newest_link_at_any_position(length: int) -> None:
    """F2, swept over the chain length and which link is the recent one."""
    for recent in range(length):
        store = GraphStore()
        for i in range(length):
            age = timedelta(minutes=5) if i == recent else 10 * HOUR
            store.add_event(_ev(f"c{i}", age, parent_id=f"c{i - 1}" if i else None))
        store.add_event(_ev("x", 2 * HOUR))
        assert _roots(store) == ["c0", "x"], f"recent link {recent}"
        assert _roots(store, from_date=NOW - HOUR) == ["c0"], f"recent link {recent}"


def test_chord_with_old_header_and_recent_callback_is_as_new_as_the_callback() -> None:
    """F3."""
    store = GraphStore()
    for event in _recorded("chord", "k-", _ending("chord", 7 * HOUR)):
        store.add_event(event)
    callback = next(
        n.task_id for n in store._graph.nodes.values() if n.name == "flows.tsum"
    )
    store.add_event(_ev(callback, timedelta(minutes=1), name="flows.tsum"))
    store.add_event(_ev("x", 2 * HOUR))
    root = store._graph.root_ids[0]
    assert root.startswith("group:")
    assert _roots(store) == [root, "x"]
    assert _roots(store, from_date=NOW - HOUR) == [root]


def test_group_without_a_timed_node_sorts_last_and_fails_a_from_bound() -> None:
    """F3: no node with a time, so ``datetime.min``."""
    store = GraphStore()
    store.add_event(_ev("x", 40 * HOUR))
    with store._lock:
        store._graph.nodes["group:g"] = TaskNode(
            task_id="group:g",
            name="group",
            state=TaskState.PENDING,
            node_type=NodeType.GROUP,
            group_id="g",
        )
        store._graph._roots["group:g"] = None
    assert _roots(store) == ["x", "group:g"]
    assert _roots(store, from_date=NOW - 100 * HOUR) == ["x"]


def test_root_group_is_as_new_as_its_members() -> None:
    """A root GROUP has no events: its members' times are the flow's."""
    store = GraphStore()
    store.add_event(_ev("m1", 9 * HOUR, group_id="g"))
    store.add_event(_ev("m2", 8 * HOUR, group_id="g"))
    store.add_event(_ev("x", 2 * HOUR))
    store.add_event(_ev("m3", timedelta(minutes=2), group_id="g"))
    assert _roots(store) == ["group:g", "x"]


def test_group_nested_in_a_chain_counts_through_its_members() -> None:
    """F4: a chain root whose only recent task is a member of a nested group."""
    store = GraphStore()
    for event in _recorded(
        "chain_group_nested", "n-", _ending("chain_group_nested", 7 * HOUR)
    ):
        store.add_event(event)
    root = store._graph.root_ids[0]
    member = next(
        n.task_id
        for n in store._graph.nodes.values()
        if n.node_type == NodeType.TASK and n.task_id != root
    )
    store.add_event(_ev(member, timedelta(minutes=1)))
    store.add_event(_ev("x", 2 * HOUR))
    assert _roots(store) == [root, "x"]
    assert _roots(store, from_date=NOW - HOUR) == [root]


def test_to_date_includes_a_flow_started_by_that_day_and_still_running() -> None:
    """F5: it started on Oct 10 and its child ran until Oct 18."""
    store = GraphStore()
    store.add_event(_ev("r", NOW - datetime(2026, 10, 10, 10, tzinfo=timezone.utc)))
    store.add_event(_ev("late", timedelta(minutes=1), parent_id="r"))
    store.add_event(_ev("after", NOW - datetime(2026, 10, 12, 1, tzinfo=timezone.utc)))
    assert _roots(store, to_date=datetime(2026, 10, 10)) == ["r"]
    assert _roots(store, to_date=datetime(2026, 10, 9)) == []


def test_to_date_uses_the_flows_first_time_not_the_roots() -> None:
    """F5: a child that began earlier than its root puts the flow's start before the day ends."""
    store = GraphStore()
    store.add_event(_ev("r", NOW - datetime(2026, 10, 12, 10, tzinfo=timezone.utc)))
    store.add_event(
        _ev(
            "kid",
            NOW - datetime(2026, 10, 9, 23, tzinfo=timezone.utc),
            parent_id="r",
        )
    )
    assert _roots(store, to_date=datetime(2026, 10, 9)) == ["r"]


def test_equal_flow_times_sort_by_root_id_across_calls_and_pages() -> None:
    """F6: ties that arise from a child's time, and plain ties, in any insert order."""
    store = GraphStore()
    for task_id in ["c", "a", "b"]:
        store.add_event(_ev(task_id, 5 * HOUR))
    store.add_event(_ev("kid-b", timedelta(minutes=1), parent_id="b"))
    store.add_event(_ev("kid-c", timedelta(minutes=1), parent_id="c"))
    for _ in range(3):
        assert _roots(store) == ["b", "c", "a"]

    plain = GraphStore()
    for task_id in ["c", "a", "b"]:
        plain.add_event(_ev(task_id, 5 * HOUR))
    for _ in range(3):
        assert _roots(plain) == ["a", "b", "c"]
    page, _ = plain.get_root_nodes(limit=1, offset=1)
    assert [r.task_id for r in page] == ["b"]


def test_eviction_of_a_flows_newest_node_lowers_the_flows_time() -> None:
    """F7: the long-running child (earliest start, newest end) is evicted first."""
    store = GraphStore(max_nodes=20, flow_cache_interval_s=0)
    store.add_event(_ev("r", 25 * HOUR))
    store.add_event(
        TaskEvent(
            task_id="long",
            name="t.task",
            state=TaskState.STARTED,
            timestamp=NOW - 60 * HOUR,
            parent_id="r",
        )
    )
    store.add_event(_ev("long", timedelta(minutes=1), parent_id="r"))
    store.add_event(_ev("mid", 10 * HOUR))
    assert _roots(store)[:2] == ["r", "mid"]
    for i in range(18):
        store.add_event(_ev(f"f{i}", 30 * HOUR - timedelta(minutes=i)))
    assert store.get_node("long") is None
    assert store.get_node("r") is not None
    assert _roots(store)[:2] == ["mid", "r"]


@pytest.mark.asyncio
async def test_graphs_endpoint_within_lists_the_flow_with_a_recent_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 through ``GET /api/graphs?within=6h``."""
    monkeypatch.setattr(params, "utcnow", lambda: NOW)
    store = GraphStore()
    store.add_event(_ev("a-root", 7 * HOUR))
    store.add_event(
        _ev(
            "a-child",
            timedelta(minutes=1),
            state=TaskState.FAILURE,
            parent_id="a-root",
        )
    )
    store.add_event(_ev("b-other", 3 * HOUR))
    app = FastAPI()
    app.include_router(create_api_router(store, worker_registry=WorkerRegistry()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/graphs?within=6h")).json()
    assert [g["task_id"] for g in body["graphs"]] == ["a-root", "b-other"]
    assert body["total"] == 2
    # The item still describes the root's own time, not the flow's.
    assert _time(body["graphs"][0], "last_updated") == NOW - 7 * HOUR
    assert _time(body["graphs"][0], "first_seen") == NOW - 7 * HOUR


# ---- differential fuzz ----------------------------------------------------


def _fuzz_events(rng: random.Random) -> list[TaskEvent]:
    """Several recorded flows, re-id'd and moved to random times, some events jittered."""
    out: list[TaskEvent] = []
    for index in range(rng.randint(2, 9)):
        flow = rng.choice(sorted(_CAPTURES))
        shift = _ending(flow, timedelta(minutes=rng.randint(0, 3000)))
        out.extend(_recorded(flow, f"f{index}-", shift, rng))
    if rng.random() < 0.5:
        rng.shuffle(out)
    return out


def _oracle(
    store: GraphStore,
    from_date: datetime | None,
    to_date: datetime | None,
    names: set[str] | None,
) -> list[str]:
    """The roots the list must return, each flow walked in full."""
    rows: list[tuple[str, datetime, datetime]] = []
    for root_id in store._graph.root_ids:
        flow = store.get_graph_from_root(root_id).values()
        if names is not None and not any(
            n.name in names for n in flow if n.node_type == NodeType.TASK
        ):
            continue
        firsts = [n.first_seen for n in flow if n.first_seen is not None]
        lasts = [n.last_updated for n in flow if n.last_updated is not None]
        first = min(firsts, default=NO_TIME)
        last = max(lasts, default=NO_TIME)
        if from_date is not None and last < from_date:
            continue
        if to_date is not None:
            day_end = (
                to_date if to_date.tzinfo else to_date.replace(tzinfo=timezone.utc)
            )
            if day_end.time() == time.min:
                day_end = day_end.replace(
                    hour=23, minute=59, second=59, microsecond=999999
                )
            if first > day_end:
                continue
        rows.append((root_id, first, last))
    # Newest latest time first; equal times by root ID.
    rows.sort(key=lambda r: (EPOCH - r[2], r[0]))
    return [r[0] for r in rows]


@pytest.mark.parametrize("chunk", range(10))
def test_flow_list_equals_the_oracle_for_random_recorded_flows(chunk: int) -> None:
    """Order, ``from_date``, ``to_date``, name filter and paging; 40 seeds per chunk."""
    for seed in range(chunk * 40, chunk * 40 + 40):
        rng = random.Random(seed)
        events = _fuzz_events(rng)
        roomy = rng.random() < 0.5
        store = GraphStore(
            max_nodes=100_000
            if roomy
            else max(10, int(len({e.task_id for e in events}) * 0.7))
        )
        for event in events:
            store.add_event(event)
        stamps = sorted(e.timestamp for e in events)
        names = sorted(
            {
                n.name
                for n in store._graph.nodes.values()
                if n.node_type == NodeType.TASK
            }
        )
        for _ in range(6):
            from_date = rng.choice(
                [None, rng.choice(stamps), NOW - HOUR * rng.randint(1, 40)]
            )
            to_date = rng.choice(
                [None, rng.choice(stamps), datetime(2026, 10, rng.randint(14, 18))]
            )
            wanted = (
                None
                if rng.random() < 0.6
                else set(rng.sample(names, min(len(names), 2)))
            )
            expected = _oracle(store, from_date, to_date, wanted)
            listed = store.get_root_nodes_with_children(
                limit=1000, from_date=from_date, to_date=to_date, task_names=wanted
            )
            where = (seed, from_date, to_date, wanted)
            assert [r.task_id for r in listed.roots] == expected, where
            assert listed.total == len(expected), where
            page = store.get_root_nodes_with_children(
                limit=2,
                offset=1,
                from_date=from_date,
                to_date=to_date,
                task_names=wanted,
            )
            assert [r.task_id for r in page.roots] == expected[1:3], where


def _node(task_id: str, parent_id: str | None) -> TaskNode:
    return TaskNode(
        task_id=task_id, name="t.task", state=TaskState.PENDING, parent_id=parent_id
    )


def test_root_of_each_resolves_chains_in_any_order_and_loops_to_none() -> None:
    """Children before parents, a missing parent, and a parent loop (no root)."""
    ids = [("d", "c"), ("b", "a"), ("c", "b"), ("a", None), ("m", "gone")]
    ids += [("x", "y"), ("y", "x"), ("z", "x")]
    nodes = {task_id: _node(task_id, parent) for task_id, parent in ids}
    assert _root_of_each(nodes) == {
        "a": "a",
        "b": "a",
        "c": "a",
        "d": "a",
        "m": "m",
        "x": None,
        "y": None,
        "z": None,
    }
