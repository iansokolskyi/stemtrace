"""``member_count`` and ``member_name`` of ``flow_summary``: the flows list's "Chord of N fetch".

The rule mirrors the flow page's ``layoutFlow`` (``partsOf`` and ``rowFrom``). The matrix rows are
numbered as in ``.context/features/overview-p95-and-polish.md`` section 4. The captures are
compared with the page's rule in ``frontend/tests/layout-flow.spec.ts``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskNode, flow_summary
from stemtrace.server.store import GraphStore

_DATA = Path(__file__).parents[1] / "data" / "celery_flows.json"
_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(_DATA.read_text())


def _task(
    task_id: str,
    name: str = "app.step",
    *,
    group_id: str | None = None,
    children: tuple[str, ...] = (),
) -> TaskNode:
    return TaskNode(
        task_id=task_id,
        name=name,
        state=TaskState.SUCCESS,
        group_id=group_id,
        children=list(children),
    )


def _box(
    task_id: str,
    node_type: NodeType,
    children: tuple[str, ...] = (),
    *,
    chord_id: str | None = None,
) -> TaskNode:
    return TaskNode(
        task_id=task_id,
        name=node_type.value.lower(),
        state=TaskState.SUCCESS,
        node_type=node_type,
        children=list(children),
        chord_id=chord_id,
        group_id=task_id,
    )


def _flow(*nodes: TaskNode) -> dict[str, TaskNode]:
    return {node.task_id: node for node in nodes}


def _fields(nodes: dict[str, TaskNode], root: str) -> tuple[int | None, str | None]:
    summary = flow_summary(nodes, root)
    return summary.member_count, summary.member_name


def test_member_fields_of_a_group_of_three_same_task_name_the_task() -> None:
    """Matrix 1."""
    nodes = _flow(
        _box("g", NodeType.GROUP, ("a", "b", "c")),
        *(_task(i, "app.resize", group_id="g") for i in "abc"),
    )

    assert _fields(nodes, "g") == (3, "resize")


def test_member_fields_of_a_chord_do_not_count_the_callback() -> None:
    """Matrix 2: 12 headers of ``app.fetch`` and the callback ``app.merge``."""
    heads = tuple(f"h{i}" for i in range(12))
    nodes = _flow(
        _box("c", NodeType.CHORD, (*heads, "cb"), chord_id="cb"),
        *(_task(h, "app.fetch", group_id="c") for h in heads),
        _task("cb", "app.merge"),
    )

    assert _fields(nodes, "c") == (12, "fetch")


def test_member_fields_of_a_group_of_chains_follow_each_row_to_its_last_task() -> None:
    """Matrix 3: ``a -> b`` twice, and the row's last task gives the name."""
    nodes = _flow(
        _box("g", NodeType.GROUP, ("a1", "a2")),
        _task("a1", "app.a", children=("b1",)),
        _task("b1", "app.b", group_id="g"),
        _task("a2", "app.a", children=("b2",)),
        _task("b2", "app.b", group_id="g"),
    )

    assert _fields(nodes, "g") == (2, "b")


def test_member_fields_of_different_task_names_have_no_name() -> None:
    """Matrix 4."""
    nodes = _flow(
        _box("g", NodeType.GROUP, ("x", "y")),
        _task("x", "app.x", group_id="g"),
        _task("y", "app.y", group_id="g"),
    )

    assert _fields(nodes, "g") == (2, None)


def test_member_fields_same_last_segment_in_different_modules_have_no_name() -> None:
    """Names are compared whole; only the shared name is cut to its last segment."""
    nodes = _flow(
        _box("g", NodeType.GROUP, ("x", "y")),
        _task("x", "app.fetch", group_id="g"),
        _task("y", "other.fetch", group_id="g"),
    )

    assert _fields(nodes, "g") == (2, None)


def test_member_fields_of_a_group_in_a_chord_header_do_not_count_the_inner_group() -> (
    None
):
    """Matrix 5: the inner container is drawn as its own box, not as a row."""
    nodes = _flow(
        _box("c", NodeType.CHORD, ("t1", "t2", "inner", "cb"), chord_id="cb"),
        _task("t1", "app.fetch", group_id="c"),
        _task("t2", "app.fetch", group_id="c"),
        _box("inner", NodeType.GROUP, ("i1", "i2")),
        _task("i1", "app.other", group_id="inner"),
        _task("i2", "app.other", group_id="inner"),
        _task("cb", "app.merge"),
    )

    assert _fields(nodes, "c") == (2, "fetch")


def test_member_fields_mid_arrival_count_only_the_members_seen() -> None:
    """Matrix 6: 2 of 5 members seen, then all 5 on the next read."""
    ids = [f"m{i}" for i in range(5)]
    seen = _flow(
        _box("g", NodeType.GROUP, tuple(ids[:2])),
        *(_task(i, "app.fetch", group_id="g") for i in ids[:2]),
    )
    full = _flow(
        _box("g", NodeType.GROUP, tuple(ids)),
        *(_task(i, "app.fetch", group_id="g") for i in ids),
    )

    assert _fields(seen, "g") == (2, "fetch")
    assert _fields(full, "g") == (5, "fetch")


@pytest.mark.parametrize("node_type", [NodeType.GROUP, NodeType.CHORD])
def test_member_fields_of_a_container_without_children_are_zero_and_no_name(
    node_type: NodeType,
) -> None:
    """Matrix 7."""
    assert _fields(_flow(_box("g", node_type)), "g") == (0, None)


def test_member_fields_of_a_task_root_are_none() -> None:
    """Matrix 8: a task root keeps the task's own title."""
    nodes = _flow(
        _task("r", "app.root", children=("a",)),
        _task("a", "app.step"),
    )

    assert _fields(nodes, "r") == (None, None)


def test_member_fields_row_continues_only_through_single_task_children() -> None:
    """A task with two children ends its row; a container child is not part of it."""
    nodes = _flow(
        _box("g", NodeType.GROUP, ("a", "x")),
        _task("a", "app.a", children=("b1", "b2")),
        _task("b1", "app.b"),
        _task("b2", "app.b"),
        _task("x", "app.x", children=("inner",)),
        _box("inner", NodeType.GROUP, ("i",)),
        _task("i", "app.i", group_id="inner"),
    )

    # row a ends at a (two children), row x ends at x (its child is a container)
    assert _fields(nodes, "g") == (2, None)


def test_member_fields_ignore_a_self_link_and_a_child_that_is_not_in_the_graph() -> (
    None
):
    nodes = _flow(
        _box("g", NodeType.GROUP, ("g", "gone", "a")),
        _task("a", "app.fetch", group_id="g"),
    )

    assert _fields(nodes, "g") == (1, "fetch")


def test_member_fields_row_with_a_cycle_ends_without_looping() -> None:
    nodes = _flow(
        _box("g", NodeType.GROUP, ("a",)),
        _task("a", "app.a", children=("b",)),
        _task("b", "app.b", children=("a",)),
    )

    assert _fields(nodes, "g") == (1, "b")


def test_member_fields_name_without_a_dot_is_the_whole_name() -> None:
    nodes = _flow(
        _box("g", NodeType.GROUP, ("a",)),
        _task("a", "fetch", group_id="g"),
    )

    assert _fields(nodes, "g") == (1, "fetch")


def test_root_flows_summaries_carry_the_member_fields_of_a_capture() -> None:
    store = GraphStore()
    for raw in _CAPTURES["group"]:
        store.add_event(TaskEvent.model_validate(raw))
    page = store.get_root_flows()

    summary = page.summaries[page.roots[0].task_id]

    assert (summary.member_count, summary.member_name) == (3, "add")


def _fixture_graphs() -> dict[str, Any]:
    path = Path(__file__).parents[2] / "src/stemtrace/server/ui/frontend/tests/fixtures"
    return dict(json.loads((path / "flow-graphs.json").read_text()))


def test_member_fields_of_recorded_graphs_equal_the_fixture_fields() -> None:
    """The fixture's fields are what `layout-flow.spec.ts` compares with the page's container."""
    graphs = _fixture_graphs()
    checked = 0
    for name, graph in graphs.items():
        nodes = {
            task_id: TaskNode(
                task_id=task_id,
                name=raw["name"],
                state=raw["state"],
                node_type=raw["node_type"],
                group_id=raw["group_id"],
                chord_id=raw["chord_id"],
                children=raw["children"],
            )
            for task_id, raw in graph["nodes"].items()
        }
        expected = graph["member_fields"]

        assert _fields(nodes, graph["root_id"]) == (
            expected["member_count"],
            expected["member_name"],
        ), name
        checked += expected["member_count"] is not None

    # most captures root in a container; a count of zero would mean nothing was compared
    assert checked >= 40


def test_member_fields_use_the_name_function_when_given() -> None:
    """Matrix 9 at the core level: the names compared are the function's, not `node.name`."""
    nodes = _flow(
        _box("g", NodeType.GROUP, ("x", "y")),
        _task("x", "app.x", group_id="g"),
        _task("y", "app.y", group_id="g"),
    )

    same = flow_summary(nodes, "g", name_of=lambda node: "shop.eu")
    differ = flow_summary(nodes, "g", name_of=lambda node: node.task_id)

    assert (same.member_count, same.member_name) == (2, "eu")
    assert (differ.member_count, differ.member_name) == (2, None)
