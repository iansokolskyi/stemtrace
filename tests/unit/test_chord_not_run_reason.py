"""Tests for ``chord_not_run_reasons``: why a chord's callback hasn't run."""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph, TaskNode, chord_not_run_reasons
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _orders(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    orders = [list(events)]
    for seed in range(10):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        orders.append(shuffled)
    return orders


def _reasons(events: list[dict[str, Any]]) -> dict[str, str | None]:
    graph = TaskGraph()
    for event in events:
        graph.add_event(TaskEvent.model_validate(event))
    return _chord_reasons(graph)


def _chord_reasons(graph: TaskGraph) -> dict[str, str | None]:
    """Every CHORD node's reason, None included."""
    reasons = chord_not_run_reasons(graph.nodes)
    return {
        node_id: reasons.get(node_id)
        for node_id, node in graph.nodes.items()
        if node.node_type == NodeType.CHORD
    }


# As events arrive a reason only appears or moves up this order, and clears
# only when the chord's callback is drawn.
_REASON_RANK = {None: 0, "header_ignored": 1, "header_revoked": 2, "header_failed": 3}


@pytest.mark.parametrize("flow", sorted(f for f in _CAPTURES if "chord" in f))
def test_chord_not_run_reasons_clear_only_when_a_callback_is_drawn(flow: str) -> None:
    """Celery decides a chord's fate at its first failed header (every chord
    path joins the header results with propagate=True: celery 5.6.3
    backends/redis.py and backends/base.py ``on_chord_part_return``,
    app/builtins.py ``unlock_chord``). Headers still queued have only a
    PENDING event, which names no chord, so a rule waiting for "every header"
    showed a reason, cleared it when the next header started, and showed it
    again. A reason clears only once the chord's callback is drawn (an
    ignored header that reported its result itself)."""
    for order in _orders(_CAPTURES[flow]):
        graph, seen = TaskGraph(), {}
        for event in order:
            graph.add_event(TaskEvent.model_validate(event))
            for node_id, reason in _chord_reasons(graph).items():
                before = seen.get(node_id)
                if reason is None and graph.nodes[node_id].chord_id is not None:
                    seen[node_id] = None
                    continue
                assert _REASON_RANK[reason] >= _REASON_RANK[before], (
                    flow,
                    node_id,
                    before,
                    reason,
                )
                seen[node_id] = reason


def test_chord_not_run_reason_ignored_header_that_reported_its_result() -> None:
    """Real capture: the header calls backend.mark_as_done, then raises
    Ignore. Celery counts its part (5.6.3 backends/base.py mark_as_done) and
    runs the callback, so the reason shows only until the callback appears."""
    graph = TaskGraph()
    reasons = []
    for event in _CAPTURES["chord_ignored_header_marked_done"]:
        graph.add_event(TaskEvent.model_validate(event))
        reasons.extend(_chord_reasons(graph).values())

    shown = [r for i, r in enumerate(reasons) if i == 0 or r != reasons[i - 1]]
    assert shown == [None, "header_ignored", None]
    (chord,) = [n for n in graph.nodes.values() if n.node_type == NodeType.CHORD]
    assert chord.chord_id is not None


_NOT_RUN = {
    # A header chain's first step fails: its unrun last step (the header) fails.
    "chord_chain_header_failed_head": "header_failed",
    "chord_failed_header": "header_failed",
    "chord_failed_header_subtask": "header_failed",
    "chord_rejected_header": "header_failed",
    "chord_ignored_header": "header_ignored",
}


@pytest.mark.parametrize(("flow", "expected"), sorted(_NOT_RUN.items()))
def test_chord_not_run_reason_real_header_outcome_in_any_order(
    flow: str, expected: str
) -> None:
    for order in _orders(_CAPTURES[flow]):
        assert set(_reasons(order).values()) == {expected}


@pytest.mark.parametrize(
    "flow",
    sorted(f for f in _CAPTURES if "chord" in f and f not in _NOT_RUN),
)
def test_chord_not_run_reason_real_chord_whose_callback_ran_has_none(flow: str) -> None:
    # Some Celery 5.2 captures show a chord as a plain group (no CHORD node).
    assert all(reason is None for reason in _reasons(_CAPTURES[flow]).values())


def _ev(task_id: str, state: TaskState, seconds: int = 0, **kwargs: Any) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.task",
        state=state,
        timestamp=_T0 + timedelta(seconds=seconds),
        **kwargs,
    )


def _header(task_id: str, state: TaskState) -> TaskEvent:
    """A header's event as a worker sends it. The sender's PENDING names no
    group or chord (``after_task_publish`` gets neither: see the PENDING
    events of ``chord_failed_header`` in celery_flows.json)."""
    if state == TaskState.PENDING:
        return _ev(task_id, state)
    return _ev(task_id, state, group_id="g", chord_callback_id="cb")


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ((TaskState.SUCCESS, TaskState.FAILURE), "header_failed"),
        ((TaskState.SUCCESS, TaskState.REJECTED), "header_failed"),
        ((TaskState.REVOKED, TaskState.FAILURE), "header_failed"),
        ((TaskState.FAILURE, TaskState.REVOKED), "header_failed"),
        ((TaskState.SUCCESS, TaskState.IGNORED), "header_ignored"),
        ((TaskState.IGNORED, TaskState.REVOKED), "header_revoked"),
        ((TaskState.REVOKED, TaskState.IGNORED), "header_revoked"),
        ((TaskState.IGNORED, TaskState.REJECTED), "header_failed"),
        ((TaskState.FAILURE, TaskState.IGNORED), "header_failed"),
        ((TaskState.SUCCESS, TaskState.REVOKED), "header_revoked"),
        ((TaskState.REVOKED, TaskState.REVOKED), "header_revoked"),
        # the other header hasn't finished, but the callback can't run any more
        ((TaskState.STARTED, TaskState.FAILURE), "header_failed"),
        ((TaskState.RETRY, TaskState.REVOKED), "header_revoked"),
        ((TaskState.STARTED, TaskState.SUCCESS), None),
        ((TaskState.RETRY, TaskState.SUCCESS), None),
        ((TaskState.SUCCESS, TaskState.SUCCESS), None),
    ],
)
def test_chord_not_run_reason_from_header_states(
    states: tuple[TaskState, TaskState], expected: str | None
) -> None:
    graph = TaskGraph()
    for i, state in enumerate(states):
        graph.add_event(_header(f"h{i}", state))

    assert chord_not_run_reasons(graph.nodes).get("group:g") == expected


def test_chord_not_run_reason_with_a_header_still_queued() -> None:
    """A queued header has only its PENDING, which doesn't make it a header yet."""
    graph = TaskGraph()
    for event in (
        _header("h0", TaskState.PENDING),
        _header("h1", TaskState.STARTED),
        _header("h1", TaskState.FAILURE),
    ):
        graph.add_event(event)

    assert chord_not_run_reasons(graph.nodes) == {"group:g": "header_failed"}


def test_chord_not_run_reason_none_when_callback_drawn() -> None:
    graph = TaskGraph()
    for event in (
        _header("h0", TaskState.FAILURE),
        _header("h1", TaskState.SUCCESS),
        _ev("cb", TaskState.SUCCESS, parent_id="h1"),
    ):
        graph.add_event(event)
    assert graph.nodes["group:g"].chord_id == "cb"

    assert chord_not_run_reasons(graph.nodes) == {}


def test_chord_not_run_reason_none_for_task_and_group_nodes() -> None:
    graph = TaskGraph()
    for event in (
        _ev("m0", TaskState.FAILURE, group_id="g"),
        _ev("m1", TaskState.SUCCESS, group_id="g"),
    ):
        graph.add_event(event)

    assert graph.nodes["group:g"].node_type == NodeType.GROUP
    assert chord_not_run_reasons(graph.nodes) == {}


def _client(store: GraphStore) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(store))
    return TestClient(app)


def test_get_graph_returns_not_run_reason_on_chord_node() -> None:
    store = GraphStore()
    for event in _CAPTURES["chord_failed_header"]:
        store.add_event(TaskEvent.model_validate(event))
    client = _client(store)
    (root,) = (g["task_id"] for g in client.get("/api/graphs").json()["graphs"])

    nodes = client.get(f"/api/graphs/{root}").json()["nodes"]

    reasons = {n["node_type"]: n["not_run_reason"] for n in nodes.values()}
    assert reasons["CHORD"] == "header_failed"
    assert reasons["TASK"] is None


def test_list_graphs_leaves_not_run_reason_empty() -> None:
    """The list holds only roots and their direct children, not every header."""
    store = GraphStore()
    for event in _CAPTURES["chord_failed_header"]:
        store.add_event(TaskEvent.model_validate(event))

    graphs = _client(store).get("/api/graphs").json()["graphs"]

    assert [g["not_run_reason"] for g in graphs] == [None]


def test_chord_not_run_reason_counts_only_headers() -> None:
    """Celery also stamps the chord's group ID on a chain body's last task; its
    failure is the body's, not a header's, so the callback did run."""
    graph = TaskGraph()
    for event in (
        _header("h0", TaskState.SUCCESS),
        _header("h1", TaskState.SUCCESS),
        _ev("tail", TaskState.FAILURE, parent_id="x", group_id="g"),
    ):
        graph.add_event(event)

    assert chord_not_run_reasons(graph.nodes) == {}


class _CountingNodes(dict[str, TaskNode]):
    """Node map that counts full scans."""

    scans = 0

    def values(self):  # type: ignore[no-untyped-def,override]  # counting wrapper
        self.scans += 1
        return super().values()

    def items(self):  # type: ignore[no-untyped-def,override]  # counting wrapper
        self.scans += 1
        return super().items()


def test_chord_not_run_reasons_scans_the_nodes_a_fixed_number_of_times() -> None:
    """One pass for any number of chords, not one per chord."""
    graph = TaskGraph()
    for c in range(50):
        for i, state in enumerate((TaskState.FAILURE, TaskState.SUCCESS)):
            graph.add_event(
                _ev(f"c{c}h{i}", state, group_id=f"g{c}", chord_callback_id=f"cb{c}")
            )
    nodes = _CountingNodes(graph.nodes)

    reasons = chord_not_run_reasons(nodes)

    assert len(reasons) == 50
    assert nodes.scans <= 2


@pytest.mark.parametrize(
    ("kept", "after"),
    [
        (TaskState.SUCCESS, None),
        (TaskState.IGNORED, "header_ignored"),
        (TaskState.REVOKED, "header_revoked"),
        (TaskState.FAILURE, "header_failed"),
    ],
)
def test_get_graph_reason_follows_the_headers_still_stored(
    kept: TaskState, after: str | None
) -> None:
    """The reason is worked out from the headers still stored. A chord that
    fits is evicted whole; one too big to keep (over 90% of max_nodes) loses
    its oldest headers, which lowers or clears the reason."""
    store = GraphStore(max_nodes=6)
    store.add_event(
        _ev("h0", TaskState.FAILURE, 1, group_id="g", chord_callback_id="cb")
    )
    for i in range(1, 5):
        store.add_event(
            _ev(f"h{i}", TaskState.SUCCESS, 1 + i, group_id="g", chord_callback_id="cb")
        )
    client = _client(store)
    assert (
        client.get("/api/graphs/group:g").json()["nodes"]["group:g"]["not_run_reason"]
        == "header_failed"
    )

    # 7 nodes in one unit, more than eviction keeps (5): the two oldest go.
    store.add_event(_ev("h5", kept, 6, group_id="g", chord_callback_id="cb"))

    nodes = client.get("/api/graphs/group:g").json()["nodes"]
    assert sorted(nodes) == ["group:g", "h2", "h3", "h4", "h5"]
    assert nodes["group:g"]["not_run_reason"] == after
