"""Tests for ``flow_summary``: the totals the flows list shows for one flow.

Expectations for the captures (real Celery events, ``tests/data/celery_flows.json``)
are read off each capture by hand and written down in ``_CAPTURED``.
"""

import itertools
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import (
    FLOW_STATE_ORDER,
    FlowSummary,
    NodeType,
    TaskGraph,
    TaskNode,
    flow_summary,
)
from stemtrace.server.store import GraphStore

_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

S = TaskState

# The spec, written out by hand: a state -> its place in FLOW_STATE_ORDER.
_CLASS_OF = {
    S.FAILURE: S.FAILURE,
    S.REJECTED: S.FAILURE,
    S.REVOKED: S.REVOKED,
    S.RETRY: S.RETRY,
    S.PENDING: S.STARTED,
    S.RECEIVED: S.STARTED,
    S.STARTED: S.STARTED,
    S.IGNORED: S.IGNORED,
    S.SUCCESS: S.SUCCESS,
}


def _node(task_id: str, state: TaskState, **fields: Any) -> TaskNode:
    return TaskNode(task_id=task_id, name="tests.task", state=state, **fields)


def _nodes(*states: TaskState) -> dict[str, TaskNode]:
    return {f"t{i}": _node(f"t{i}", state) for i, state in enumerate(states)}


def _ev(task_id: str, state: TaskState, seconds: int = 0, **kwargs: Any) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.task",
        state=state,
        timestamp=_T0 + timedelta(seconds=seconds),
        **kwargs,
    )


def _header(task_id: str, state: TaskState, group_id: str = "g") -> TaskEvent:
    """A header as a worker reports it: its events name the chord's callback."""
    return _ev(task_id, state, group_id=group_id, chord_callback_id=f"cb-{group_id}")


def _graph(*events: TaskEvent) -> TaskGraph:
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
    return graph


def _summary(graph: TaskGraph, root_id: str) -> FlowSummary:
    return flow_summary(graph.nodes, root_id)


def test_flow_state_order_is_failure_revoked_retry_started_ignored_success() -> None:
    assert FLOW_STATE_ORDER == (
        S.FAILURE,
        S.REVOKED,
        S.RETRY,
        S.STARTED,
        S.IGNORED,
        S.SUCCESS,
    )


def test_flow_state_classes_cover_every_task_state_and_land_in_the_order() -> None:
    assert set(_CLASS_OF) == set(TaskState)
    assert set(_CLASS_OF.values()) == set(FLOW_STATE_ORDER)


@pytest.mark.parametrize("state", list(TaskState))
def test_flow_summary_of_one_task_has_the_class_of_its_state(state: TaskState) -> None:
    summary = flow_summary(_nodes(state), "t0")

    assert summary.state == _CLASS_OF[state]
    assert summary.state_counts == {state.value: 1}


@pytest.mark.parametrize(
    ("first", "second"), list(itertools.product(TaskState, TaskState))
)
def test_flow_summary_worst_state_wins_for_every_ordered_pair(
    first: TaskState, second: TaskState
) -> None:
    ranks = [FLOW_STATE_ORDER.index(_CLASS_OF[s]) for s in (first, second)]
    expected = FLOW_STATE_ORDER[min(ranks)]

    assert flow_summary(_nodes(first, second), "t0").state == expected


def test_flow_summary_state_ignores_how_many_tasks_are_better() -> None:
    """One failure outweighs any number of successes, and a run of tasks never adds up."""
    states = [S.SUCCESS] * 50 + [S.REVOKED] * 40 + [S.FAILURE]

    assert flow_summary(_nodes(*states), "t0").state == S.FAILURE
    assert flow_summary(_nodes(*states[:-1]), "t0").state == S.REVOKED


@pytest.mark.parametrize(
    "states",
    [
        [S.SUCCESS],
        [S.FAILURE, S.FAILURE, S.SUCCESS, S.STARTED, S.PENDING],
        [S.REJECTED, S.RECEIVED, S.RETRY, S.IGNORED, S.REVOKED, S.SUCCESS],
        list(TaskState) * 3,
    ],
)
def test_flow_summary_counts_sum_to_task_count(states: list[TaskState]) -> None:
    summary = flow_summary(_nodes(*states), "t0")

    assert summary.task_count == len(states)
    assert sum(summary.state_counts.values()) == summary.task_count
    assert summary.state_counts == {
        s.value: states.count(s) for s in TaskState if s in states
    }


def test_flow_summary_state_counts_leave_out_states_with_no_task() -> None:
    summary = flow_summary(_nodes(S.SUCCESS, S.SUCCESS, S.FAILURE), "t0")

    assert summary.state_counts == {"SUCCESS": 2, "FAILURE": 1}


def test_flow_summary_excludes_synthetic_nodes_from_counts_and_state() -> None:
    nodes = _nodes(S.SUCCESS, S.SUCCESS)
    nodes["group:g"] = _node("group:g", S.FAILURE, node_type=NodeType.GROUP)
    nodes["group:c"] = _node("group:c", S.REVOKED, node_type=NodeType.CHORD)

    summary = flow_summary(nodes, "group:g")

    assert summary.task_count == 2
    assert summary.state_counts == {"SUCCESS": 2}
    assert summary.state == S.SUCCESS


def test_flow_summary_without_tasks_takes_the_roots_own_state() -> None:
    nodes = {"group:g": _node("group:g", S.REVOKED, node_type=NodeType.GROUP)}

    summary = flow_summary(nodes, "group:g")

    assert summary == FlowSummary(
        task_count=0, state_counts={}, not_run_count=0, state=S.REVOKED
    )


def test_flow_summary_root_success_with_failed_child_is_failure() -> None:
    graph = _graph(
        _ev("root", S.SUCCESS),
        _ev("child", S.FAILURE, 1, parent_id="root"),
    )

    summary = _summary(graph, "root")

    assert graph.nodes["root"].state == S.SUCCESS
    assert summary.state == S.FAILURE
    assert summary.state_counts == {"SUCCESS": 1, "FAILURE": 1}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ((S.SUCCESS, S.FAILURE), 1),
        ((S.SUCCESS, S.REJECTED), 1),
        ((S.SUCCESS, S.REVOKED), 1),
        ((S.SUCCESS, S.IGNORED), 1),
        ((S.STARTED, S.FAILURE), 1),
        ((S.SUCCESS, S.SUCCESS), 0),
        ((S.STARTED, S.SUCCESS), 0),
        ((S.RETRY, S.SUCCESS), 0),
    ],
)
def test_flow_summary_not_run_count_of_one_chord_from_its_header_states(
    headers: tuple[TaskState, TaskState], expected: int
) -> None:
    graph = _graph(*(_header(f"h{i}", s) for i, s in enumerate(headers)))

    assert _summary(graph, "group:g").not_run_count == expected


def test_flow_summary_not_run_count_is_zero_when_a_callback_ran() -> None:
    graph = _graph(
        _header("h0", S.FAILURE),
        _header("h1", S.SUCCESS),
        _ev("cb-g", S.SUCCESS, 2, parent_id="h1"),
    )

    assert graph.nodes["group:g"].chord_id == "cb-g"
    assert _summary(graph, "group:g").not_run_count == 0


def test_flow_summary_not_run_count_counts_each_chord_once() -> None:
    """Two chords with a failed header each, and a third whose headers all succeeded."""
    graph = _graph(
        _header("a0", S.FAILURE, "a"),
        _header("a1", S.FAILURE, "a"),
        _header("b0", S.REVOKED, "b"),
        _header("b1", S.SUCCESS, "b"),
        _header("c0", S.SUCCESS, "c"),
    )

    assert {
        n.task_id for n in graph.nodes.values() if n.node_type == NodeType.CHORD
    } == {
        "group:a",
        "group:b",
        "group:c",
    }
    assert _summary(graph, "group:a").not_run_count == 2


# What each capture's flow looks like, by hand:
#   (tasks, tasks per state, chords whose callback hasn't run, flow state)
_CAPTURED: dict[str, tuple[int, dict[str, int], int, TaskState]] = {
    # three adds in a chain
    "chain": (3, {"SUCCESS": 3}, 0, S.SUCCESS),
    # three adds in a group: the GROUP node isn't a task
    "group": (3, {"SUCCESS": 3}, 0, S.SUCCESS),
    # three headers and the tsum callback: the CHORD node isn't a task
    "chord": (4, {"SUCCESS": 4}, 0, S.SUCCESS),
    # flows.fail failed and flows.add succeeded; the chord's CHORD node is FAILURE too
    "chord_failed_header": (2, {"SUCCESS": 1, "FAILURE": 1}, 1, S.FAILURE),
    # a header chain whose first step failed: it and the unrun header fail, the
    # other header chain succeeded in full
    "chord_chain_header_failed_head": (4, {"SUCCESS": 2, "FAILURE": 2}, 1, S.FAILURE),
    "chord_failed_header_subtask": (3, {"SUCCESS": 2, "FAILURE": 1}, 1, S.FAILURE),
    # flows.reject was REJECTED, flows.add succeeded
    "chord_rejected_header": (2, {"SUCCESS": 1, "REJECTED": 1}, 1, S.FAILURE),
    # flows.ignore raised Ignore: worse than SUCCESS, and no callback ran
    "chord_ignored_header": (2, {"SUCCESS": 1, "IGNORED": 1}, 1, S.IGNORED),
    # the ignored header reported its result itself, so the callback ran
    "chord_ignored_header_marked_done@field": (
        3,
        {"SUCCESS": 2, "IGNORED": 1},
        0,
        S.IGNORED,
    ),
    # two headers, one retried and then succeeded (only its last state counts), and the tsum body
    "chord_retried_header": (3, {"SUCCESS": 3}, 0, S.SUCCESS),
    # two chords, one inside the other's body: five tasks, both callbacks ran
    "chord_in_chord_body": (5, {"SUCCESS": 5}, 0, S.SUCCESS),
    # the whole chain fails from its head
    "chain_failed_head": (3, {"FAILURE": 3}, 0, S.FAILURE),
    # replace(): the replacing task and the chain it was replaced with
    "replace_chain": (2, {"SUCCESS": 2}, 0, S.SUCCESS),
    # replace() with a group: the replacer, two members, one CHORD node
    "replace_group": (3, {"SUCCESS": 3}, 0, S.SUCCESS),
    # a task that spawns a group of three
    "group_spawned_in_task": (4, {"SUCCESS": 4}, 0, S.SUCCESS),
    # a header replaced twice over: outer, inner, two adds and the tsum body
    "nested_replace_header@worker_process@field": (5, {"SUCCESS": 5}, 0, S.SUCCESS),
    # RETRY events, then SUCCESS
    "countdown_retry_countdown": (1, {"SUCCESS": 1}, 0, S.SUCCESS),
}


def _listed_flows(capture: str) -> dict[str, FlowSummary]:
    """Each root ``/api/graphs`` would list for a capture, with its summary."""
    store = GraphStore()
    for raw in _CAPTURES[capture]:
        store.add_event(TaskEvent.model_validate(raw))
    roots, _, summaries, _ = store.get_root_flows(limit=100)
    return {r.task_id: summaries[r.task_id] for r in roots}


@pytest.mark.parametrize("capture", sorted(_CAPTURED))
def test_flow_summary_of_real_capture_matches_the_hand_derived_flow(
    capture: str,
) -> None:
    tasks, per_state, not_run, state = _CAPTURED[capture]

    ((_, summary),) = _listed_flows(capture).items()

    assert summary == FlowSummary(
        task_count=tasks, state_counts=per_state, not_run_count=not_run, state=state
    )


@pytest.mark.parametrize("capture", sorted(_CAPTURES))
def test_flow_summary_of_every_capture_agrees_with_a_second_reading_of_its_events(
    capture: str,
) -> None:
    """Counts and state read from the raw events, not the graph: a task's final
    state is its latest event's. The flows together hold every task once."""
    flows = _listed_flows(capture)
    last: dict[str, tuple[datetime, TaskState]] = {}
    for raw in _CAPTURES[capture]:
        event = TaskEvent.model_validate(raw)
        if event.task_id not in last or event.timestamp >= last[event.task_id][0]:
            last[event.task_id] = (event.timestamp, event.state)
    final_counts = Counter(state.value for _, state in last.values())

    merged: Counter[str] = Counter()
    for flow in flows.values():
        merged.update(flow.state_counts)
    assert merged == final_counts
    assert sum(f.task_count for f in flows.values()) == len(last)
    worst = min(FLOW_STATE_ORDER.index(_CLASS_OF[state]) for _, state in last.values())
    # The worst task is in some flow, and no flow is worse than the worst task
    assert min(FLOW_STATE_ORDER.index(f.state) for f in flows.values()) == worst
    if len(flows) == 1:
        (flow,) = flows.values()
        assert flow.state == FLOW_STATE_ORDER[worst]
