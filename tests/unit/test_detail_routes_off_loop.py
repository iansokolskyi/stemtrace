"""Detail routes build and encode their response off the event loop.

The handlers used to build every child response, and FastAPI to encode the body,
on the loop thread. Time spent in garbage collection is outside this budget.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.store import GraphStore
from tests.unit.test_summary_routes_off_loop import (
    MAX_LOOP_WORK,
    _app,
    _fixed_clock,  # noqa: F401  (autouse fixture: pins the clock)
    _get,
    _longest_loop_hold,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
GOLDEN = Path(__file__).parent.parent / "data" / "detail_routes_bodies.json"
SIZES = [1_000]
# Each detail route over a root with N direct children.
DETAIL_PATHS = [
    "/api/tasks/root",
    "/api/tasks/root/children",
    "/api/tasks/root/children?compact=true",
    "/api/graphs/root",
]
ROOT_PAYLOAD = "r" * 10_000  # root's args and result near max_data_size
CHILD_RESULT = "c" * 200
CHILD_TRACEBACK = "Traceback (most recent call last):\n  File x.py\n" * 10


def _add_child(store: GraphStore, i: int) -> None:
    """One child run: PENDING and a result or a failure."""
    failed = i % 10 == 0
    final = TaskState.FAILURE if failed else TaskState.SUCCESS
    base = {
        "task_id": f"c{i}",
        "name": f"app.task{i % 20}",
        "parent_id": "root",
        "root_id": "root",
        "hostname": f"worker{i % 5}@host{i % 5}",
        "queue": f"queue{i % 4}",
    }
    for n, state in enumerate((TaskState.PENDING, final)):
        is_final_failure = failed and state == final
        store.add_event(
            TaskEvent(
                state=state,
                args=[i, "arg"] if state == TaskState.PENDING else None,
                kwargs={"k": "v" * 20} if state == TaskState.PENDING else None,
                result=CHILD_RESULT if state == TaskState.SUCCESS else None,
                traceback=CHILD_TRACEBACK if is_final_failure else None,
                exception_type="ValueError" if is_final_failure else None,
                exception="boom" if is_final_failure else None,
                timestamp=NOW - timedelta(seconds=100 - n),
                **base,
            )
        )


def _store_with_children(count: int) -> GraphStore:
    store = GraphStore(max_nodes=count + 100)
    store.add_event(
        TaskEvent(
            task_id="root",
            name="app.root",
            state=TaskState.PENDING,
            args=[ROOT_PAYLOAD],
            kwargs={"data": ROOT_PAYLOAD},
            timestamp=NOW - timedelta(seconds=200),
        )
    )
    store.add_event(
        TaskEvent(
            task_id="root",
            name="app.root",
            state=TaskState.SUCCESS,
            result=ROOT_PAYLOAD,
            timestamp=NOW - timedelta(seconds=150),
        )
    )
    for i in range(count):
        _add_child(store, i)
    return store


@pytest.fixture(scope="module")
def stores() -> dict[int, GraphStore]:
    return {}


def _store(cache: dict[int, GraphStore], count: int) -> GraphStore:
    """The store with ``count`` children under ``root``, built once per module."""
    if count not in cache:
        cache[count] = _store_with_children(count)
    return cache[count]


def _encoded_on(
    thread_id: int, monkeypatch: pytest.MonkeyPatch, sink: list[int]
) -> None:
    """Record the size of every JSON body encoded on ``thread_id``.

    Encoding is C work: profile events count one ``dump_json`` as about one event.
    """
    real_dump_json = TypeAdapter.dump_json
    real_model_dump_json = BaseModel.model_dump_json

    def dump_json(self: TypeAdapter[Any], *args: Any, **kwargs: Any) -> bytes:
        out = real_dump_json(self, *args, **kwargs)
        if threading.get_ident() == thread_id:
            sink.append(len(out))
        return out

    def model_dump_json(self: BaseModel, *args: Any, **kwargs: Any) -> str:
        out = real_model_dump_json(self, *args, **kwargs)
        if threading.get_ident() == thread_id:
            sink.append(len(out))
        return out

    monkeypatch.setattr(TypeAdapter, "dump_json", dump_json)
    monkeypatch.setattr(BaseModel, "model_dump_json", model_dump_json)


@pytest.mark.slow
@pytest.mark.parametrize("count", SIZES)
@pytest.mark.parametrize("path", DETAIL_PATHS)
async def test_detail_route_over_many_children_keeps_the_loop_within_budget(
    path: str,
    count: int,
    stores: dict[int, GraphStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building and encoding the children runs in the worker thread, not the loop."""
    app = _app(_store(stores, count))
    assert (await _get(app, "/api/health")).status_code == 200
    on_loop: list[int] = []
    _encoded_on(threading.get_ident(), monkeypatch, on_loop)

    longest = await _longest_loop_hold(app, path)

    assert longest <= MAX_LOOP_WORK, (
        f"loop did {longest} events between turns (max {MAX_LOOP_WORK})"
    )
    assert [n for n in on_loop if n > 10_000] == []


@pytest.mark.parametrize("path", DETAIL_PATHS)
async def test_detail_route_over_children_returns_every_child(
    path: str, stores: dict[int, GraphStore]
) -> None:
    """The big-body case has the children, so the budget test builds a real body."""
    body = (await _get(_app(_store(stores, 1_000)), path)).json()
    if path == "/api/tasks/root":
        assert len(body["children"]) == 1_000
    elif path.startswith("/api/graphs"):
        assert len(body["nodes"]) == 1_001
    else:
        assert len(body) == 1_000


async def test_detail_routes_for_unknown_id_return_404(
    stores: dict[int, GraphStore],
) -> None:
    """The 404 path is unchanged: FastAPI's error body, not a built response."""
    app = _app(_store(stores, 1_000))
    expected = {
        "/api/tasks/nope": "Task nope not found",
        "/api/tasks/nope/children": "Task nope not found",
        "/api/graphs/nope": "Graph nope not found",
    }
    for path, detail in expected.items():
        response = await _get(app, path)
        assert response.status_code == 404
        assert response.json() == {"detail": detail}


GOLDEN_PATHS = [
    *DETAIL_PATHS,
    "/api/tasks/g",
    "/api/tasks/g/children",
    "/api/tasks/g/children?compact=true",
    "/api/graphs/g",
]


def golden_store() -> GraphStore:
    """60 children under ``root`` plus a root ``g`` whose children share a group."""
    store = _store_with_children(60)
    store.add_event(
        TaskEvent(
            task_id="g",
            name="app.fan",
            state=TaskState.SUCCESS,
            timestamp=NOW - timedelta(seconds=90),
        )
    )
    for i in range(3):
        store.add_event(
            TaskEvent(
                task_id=f"m{i}",
                name="app.member",
                state=TaskState.SUCCESS,
                parent_id="g",
                root_id="g",
                group_id="grp1",
                timestamp=NOW - timedelta(seconds=80 - i),
            )
        )
    return store


async def golden_digests() -> dict[str, str]:
    """SHA-256 of each route's body bytes over ``golden_store``."""
    app = _app(golden_store())
    return {
        path: hashlib.sha256((await _get(app, path)).content).hexdigest()
        for path in GOLDEN_PATHS
    }


async def test_detail_bodies_are_byte_identical_to_the_ones_recorded_on_main() -> None:
    """Moving the build into the thread changes no byte of any detail body.

    The digests were recorded on ``origin/redesign`` (commit 2817025), before the change;
    they differ from main's only in ``flow_root_id`` and the graph nodes' ``flow``.
    """
    assert await golden_digests() == json.loads(GOLDEN.read_text())
