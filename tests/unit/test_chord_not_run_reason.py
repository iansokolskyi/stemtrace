"""Tests for ``chord_not_run_reason``: why a chord's callback never ran."""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph, chord_not_run_reason
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
    return {
        node_id: chord_not_run_reason(node, graph.nodes)
        for node_id, node in graph.nodes.items()
        if node.node_type == NodeType.CHORD
    }


@pytest.mark.parametrize("flow", ["chord_failed_header", "chord_failed_header_subtask"])
def test_chord_not_run_reason_real_failed_header_in_any_order(flow: str) -> None:
    for order in _orders(_CAPTURES[flow]):
        assert set(_reasons(order).values()) == {"header_failed"}


@pytest.mark.parametrize(
    "flow",
    sorted(f for f in _CAPTURES if "chord" in f and "failed_header" not in f),
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
    return _ev(task_id, state, group_id="g", chord_callback_id="cb")


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ((TaskState.SUCCESS, TaskState.FAILURE), "header_failed"),
        ((TaskState.SUCCESS, TaskState.REJECTED), "header_failed"),
        ((TaskState.REVOKED, TaskState.FAILURE), "header_failed"),
        ((TaskState.SUCCESS, TaskState.REVOKED), "header_revoked"),
        # not over yet: the callback may still run
        ((TaskState.STARTED, TaskState.FAILURE), None),
        ((TaskState.PENDING, TaskState.REVOKED), None),
        ((TaskState.SUCCESS, TaskState.SUCCESS), None),
    ],
)
def test_chord_not_run_reason_from_header_states(
    states: tuple[TaskState, TaskState], expected: str | None
) -> None:
    graph = TaskGraph()
    for i, state in enumerate(states):
        graph.add_event(_header(f"h{i}", state))

    assert chord_not_run_reason(graph.nodes["group:g"], graph.nodes) == expected


def test_chord_not_run_reason_none_when_callback_drawn() -> None:
    graph = TaskGraph()
    for event in (
        _header("h0", TaskState.FAILURE),
        _header("h1", TaskState.SUCCESS),
        _ev("cb", TaskState.SUCCESS, parent_id="h1"),
    ):
        graph.add_event(event)
    assert graph.nodes["group:g"].chord_id == "cb"

    assert chord_not_run_reason(graph.nodes["group:g"], graph.nodes) is None


def test_chord_not_run_reason_none_for_task_and_group_nodes() -> None:
    graph = TaskGraph()
    for event in (
        _ev("m0", TaskState.FAILURE, group_id="g"),
        _ev("m1", TaskState.SUCCESS, group_id="g"),
    ):
        graph.add_event(event)

    assert graph.nodes["group:g"].node_type == NodeType.GROUP
    assert chord_not_run_reason(graph.nodes["group:g"], graph.nodes) is None
    assert chord_not_run_reason(graph.nodes["m0"], graph.nodes) is None


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

    assert chord_not_run_reason(graph.nodes["group:g"], graph.nodes) is None
