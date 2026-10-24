"""The rules that pick a chord's callback, one input shape at a time.

Each case is built by hand from the IDs Celery's events carry (see
``docs/graphs.md``) and replayed in many orders: the callback drawn must not
depend on arrival order. The recorded Celery flows are checked in
``test_chord_callback_attribution.py``.
"""

import random
import uuid
from datetime import datetime, timezone
from itertools import permutations
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph, chord_not_run_reasons
from tests.graph_invariants import assert_graph_invariants

_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNLOCK = "celery.chord_unlock"


def uid(n: int) -> str:
    """A UUID-shaped ID (``celery.chord_unlock`` arguments must look like one)."""
    return str(uuid.UUID(int=n))


G = uid(1)  # the chord's header group
G2 = uid(2)  # another chord's header group
H1, H2 = uid(11), uid(12)  # headers
CB = uid(21)  # a callback
CB2 = uid(22)
U = uid(31)  # a chord_unlock task
U2 = uid(32)
S = uid(41)  # another group (a body group)


def ev(
    task_id: str,
    state: TaskState = TaskState.SUCCESS,
    /,
    *,
    group: str | None = None,
    stamp: str | None = None,
    first: str | None = None,
    parent: str | None = None,
    name: str = "app.task",
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> TaskEvent:
    """One event; a header names its group and a stamp or first task."""
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=state,
        timestamp=_TIME,
        parent_id=parent,
        group_id=group,
        chord_callback_id=stamp,
        chord_body_first_id=first,
        args=args,
        kwargs=kwargs,
    )


def header(
    task_id: str,
    group: str = G,
    *,
    stamp: str | None = None,
    first: str | None = None,
    state: TaskState = TaskState.SUCCESS,
) -> TaskEvent:
    """A worker event of a chord header."""
    return ev(task_id, state, group=group, stamp=stamp, first=first)


def unlock(
    task_id: str,
    group: Any,
    body: Any = None,
    result: Any = None,
    state: TaskState = TaskState.SUCCESS,
) -> TaskEvent:
    """A ``celery.chord_unlock`` event with the arguments Celery gives it."""
    args: list[Any] = [group] if body is None else [group, body]
    kwargs = {} if result is None else {"result": result}
    return ev(task_id, state, name=UNLOCK, args=args, kwargs=kwargs)


def task_body(task_id: str) -> dict[str, Any]:
    """A task signature, as in ``chord_unlock``'s second argument."""
    return {"task": "app.cb", "args": [], "options": {"task_id": task_id}}


def chain_body(first: str, last: str) -> dict[str, Any]:
    """A chain signature: its own ID is the last step's."""
    steps = [task_body(first), task_body(last)]
    return {
        "task": "celery.chain",
        "kwargs": {"tasks": steps},
        "options": {"task_id": last},
    }


def result_of(*task_ids: str) -> list[Any]:
    """A ``chord_unlock`` ``result``: ``[[[id, parent], None], ...]``."""
    return [[[task_id, None], None] for task_id in task_ids]


def orders(events: list[TaskEvent]) -> list[list[TaskEvent]]:
    """The given order, its reverse and 30 seeded shuffles."""
    found = [events, events[::-1]]
    for index in range(30):
        found.append(random.Random(f"rules:{index}").sample(events, len(events)))
    return found


def build(events: list[TaskEvent]) -> TaskGraph:
    """Replay ``events`` in order, checking the graph's invariants throughout."""
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
        assert_graph_invariants(graph)
    return graph


def callback_of(graph: TaskGraph, group: str = G) -> str | None:
    """The node the chord's CHORD node draws as its callback."""
    node = graph.nodes[f"group:{group}"]
    assert node.node_type == NodeType.CHORD
    return node.chord_id


def draws(events: list[TaskEvent], expected: str | None, group: str = G) -> None:
    """Assert every arrival order draws ``expected`` as the chord's callback."""
    for order in orders(events):
        graph = build(order)
        assert callback_of(graph, group) == expected, [e.task_id for e in order]


# --- header evidence: chord_body_first_id ---------------------------------


def test_first_task_id_of_a_task_body_is_drawn_when_it_has_events() -> None:
    draws([header(H1, first=CB), header(H2, first=CB), ev(CB)], CB)


def test_first_task_id_is_drawn_when_the_callback_carries_the_group_id() -> None:
    # Celery 5.6 gives a task body's callback the header group ID too.
    draws(
        [
            header(H1, stamp=CB, first=CB),
            header(H2, stamp=CB, first=CB),
            ev(CB, group=G),
        ],
        CB,
    )


def test_first_task_id_of_a_chain_body_wins_over_the_stamped_last_task() -> None:
    last = uid(23)
    events = [
        header(H1, stamp=last, first=CB),
        header(H2, stamp=last, first=CB),
        ev(CB),
        ev(last, group=G),
    ]

    draws(events, CB)


def test_first_task_id_without_events_draws_nothing_until_the_task_runs() -> None:
    draws([header(H1, first=CB)], None)
    draws([header(H1, first=CB), ev(CB, TaskState.PENDING)], CB)


def test_first_task_id_naming_a_header_draws_nothing() -> None:
    draws([header(H1, first=H2), header(H2, first=H2)], None)


def test_first_task_id_naming_the_group_draws_nothing() -> None:
    draws([header(H1, first=G), ev(G)], None)


def test_headers_naming_different_first_task_ids_draw_nothing() -> None:
    draws([header(H1, first=CB), header(H2, first=CB2), ev(CB), ev(CB2)], None)


def test_a_header_carrying_only_the_first_task_id_is_a_header() -> None:
    # Celery 5.2 chain bodies carry no stamp: the field alone makes a chord.
    graph = build([header(H1, first=CB), header(H2, first=CB), ev(CB)])

    assert graph.nodes[f"group:{G}"].node_type == NodeType.CHORD
    assert graph.get_group_members(G) == [H1, H2]
    assert graph.nodes[f"group:{G}"].chord_callback_id is None


# --- header evidence: chord_callback_id (the stamp) ------------------------


def test_stamp_of_a_task_outside_the_group_is_drawn() -> None:
    draws([header(H1, stamp=CB), header(H2, stamp=CB), ev(CB)], CB)


def test_stamp_of_a_task_carrying_the_group_id_draws_nothing() -> None:
    # Celery 5.6 stamps a callback and a chain body's last task alike, and both
    # carry the group ID: the stamp alone can't tell which one it is.
    draws([header(H1, stamp=CB), ev(CB, group=G)], None)


def test_stamp_naming_a_header_draws_nothing() -> None:
    draws([header(H1, stamp=H2), header(H2, stamp=H2)], None)


def test_stamp_equal_to_the_group_id_draws_nothing() -> None:
    # Celery 5.6 stamps a group body, and a chain starting with a chord or
    # group, with the header group's own ID.
    draws([header(H1, stamp=G), ev(CB, group=G), ev(CB2, group=G)], None)


def test_headers_naming_different_stamps_draw_nothing_even_with_a_first_id() -> None:
    draws(
        [
            header(H1, stamp=CB, first=CB),
            header(H2, stamp=CB2, first=CB),
            ev(CB),
            ev(CB2),
        ],
        None,
    )


def test_stamped_task_with_only_a_pending_event_is_not_drawn_yet() -> None:
    # Workers before 0.4.0 sent PENDING without the group ID: until a worker
    # event shows whether the task carries it, it may be a chain body's last task.
    draws([header(H1, stamp=CB), ev(CB, TaskState.PENDING)], None)
    draws(
        [header(H1, stamp=CB), ev(CB, TaskState.PENDING), ev(CB, TaskState.STARTED)], CB
    )


def test_stamp_naming_a_task_without_events_draws_nothing() -> None:
    draws([header(H1, stamp=CB)], None)


def test_stamp_naming_a_group_draws_that_group_when_it_has_members() -> None:
    members = [ev(uid(51), group=S), ev(uid(52), group=S)]

    draws([header(H1, stamp=S), *members], f"group:{S}")


def test_stamp_naming_a_group_with_one_member_draws_nothing() -> None:
    draws([header(H1, stamp=S), ev(uid(51), group=S)], None)


def test_stamp_naming_a_group_of_only_the_headers_draws_nothing() -> None:
    draws([header(H1, stamp=S), header(H2, stamp=S, first=None)], None)


def test_stamp_naming_an_inner_chord_draws_that_chord() -> None:
    inner = [header(uid(51), group=S, stamp=CB), header(uid(52), group=S, stamp=CB)]

    draws([header(H1, stamp=S), *inner], f"group:{S}")


@pytest.mark.parametrize("second_stamp", [CB, CB2])
def test_two_chords_naming_one_stamp_draw_nothing(second_stamp: str) -> None:
    events = [
        header(H1, G, stamp=CB),
        header(H2, G2, stamp=second_stamp),
        ev(CB),
        ev(CB2),
    ]

    for order in orders(events):
        graph = build(order)
        shared = second_stamp == CB
        assert callback_of(graph, G) == (None if shared else CB)
        assert callback_of(graph, G2) == (None if shared else CB2)


def test_a_chord_draws_its_stamp_again_once_the_other_chord_changes_it() -> None:
    graph = build(
        [header(H1, G, stamp=CB), header(H2, G2, stamp=CB), ev(CB)],
    )
    assert callback_of(graph, G) is None

    graph.add_event(header(H2, G2, stamp=CB2, state=TaskState.STARTED))

    # The second header now names two callbacks: that chord draws nothing and
    # the first no longer shares its stamp.
    assert callback_of(graph, G) == CB
    assert callback_of(graph, G2) is None


# --- ties: a node two chords point to ---------------------------------------


@pytest.mark.parametrize("first", [True, False])
def test_two_chords_pointing_to_one_node_draw_it_after_neither(first: bool) -> None:
    def named(group: str, task_id: str) -> TaskEvent:
        return (
            header(task_id, group, first=CB)
            if first
            else header(task_id, group, stamp=CB)
        )

    draws_a = [named(G, H1), named(G2, H2), ev(CB)]

    for order in orders(draws_a):
        graph = build(order)
        assert callback_of(graph, G) is None
        assert callback_of(graph, G2) is None
        assert graph._callback_of == {}


def test_node_drawn_by_one_chord_is_released_when_a_second_points_to_it() -> None:
    graph = build([header(H1, G, first=CB), ev(CB)])
    assert callback_of(graph) == CB

    graph.add_event(header(H2, G2, first=CB))

    assert callback_of(graph, G) is None
    assert callback_of(graph, G2) is None
    assert graph.nodes[CB].parent_id is None

    graph.remove_node(H2)

    assert callback_of(graph, G) == CB


# --- chord_unlock -----------------------------------------------------------


def test_unlock_body_signature_gives_the_callback_without_a_field() -> None:
    events = [
        header(H1, stamp=G),
        header(H2, stamp=G),
        unlock(U, G, task_body(CB)),
        ev(CB, group=G),
    ]

    draws(events, CB)


def test_unlock_chain_body_gives_the_first_step_not_the_last() -> None:
    last = uid(23)
    events = [
        header(H1, stamp=G),
        unlock(U, G, chain_body(CB, last)),
        ev(CB),
        ev(last, group=G),
    ]

    draws(events, CB)


def test_unlock_task_is_drawn_under_the_chord_with_its_callback() -> None:
    graph = build([header(H1, stamp=G), unlock(U, G, task_body(CB)), ev(CB)])

    assert graph.nodes[U].parent_id == f"group:{G}"
    assert graph._unlock_by_chord == {G: U}
    assert callback_of(graph) == CB


def test_unlock_without_a_callback_is_not_drawn_under_the_chord() -> None:
    graph = build([header(H1, stamp=G), unlock(U, G, task_body(CB))])

    assert callback_of(graph) is None
    assert graph.nodes[U].parent_id is None


@pytest.mark.parametrize("group_arg", ["[Truncated: 20480 bytes > 10240 max]", 7, None])
def test_unlock_whose_group_argument_is_unreadable_is_ignored(group_arg: Any) -> None:
    events = [
        header(H1, stamp=G),
        unlock(U, group_arg, task_body(CB), result_of(H1)),
        ev(CB),
    ]

    draws(events, None)


def test_unlock_arguments_that_are_not_a_list_are_ignored() -> None:
    event = ev(U, TaskState.SUCCESS, name=UNLOCK)

    draws([header(H1, stamp=G), event, ev(CB)], None)


def test_unlock_naming_a_header_set_the_chord_lacks_is_ignored() -> None:
    # Its result lists header IDs that aren't all of this chord's: not its.
    events = [
        header(H1, stamp=G),
        header(H2, stamp=G),
        unlock(U, G, task_body(CB), result_of(H1, uid(13))),
        ev(CB),
    ]

    draws(events, None)


def test_unlock_listing_every_header_is_the_chords() -> None:
    events = [
        header(H1, stamp=G),
        header(H2, stamp=G),
        unlock(U, G, task_body(CB), result_of(H1, H2, uid(13))),
        ev(CB),
    ]

    draws(events, CB)


@pytest.mark.parametrize("result", ["not a list", [[]], [["x"]], [[[7, None], None]]])
def test_unlock_with_an_unreadable_result_still_belongs_to_its_chord(
    result: Any,
) -> None:
    events = [header(H1, stamp=G), unlock(U, G, task_body(CB), result), ev(CB)]

    draws(events, CB)


def test_unlock_and_header_naming_different_first_tasks_draw_nothing() -> None:
    events = [
        header(H1, stamp=CB, first=CB),
        unlock(U, G, task_body(CB2)),
        ev(CB),
        ev(CB2),
    ]

    draws(events, None)


def test_unlock_and_header_naming_the_same_first_task_draw_it() -> None:
    events = [
        header(H1, stamp=CB, first=CB),
        unlock(U, G, task_body(CB)),
        ev(CB),
    ]

    draws(events, CB)


def test_two_unlock_tasks_naming_the_same_first_task_draw_it_under_the_smaller() -> (
    None
):
    events = [
        header(H1, stamp=G),
        unlock(U2, G, task_body(CB)),
        unlock(U, G, task_body(CB)),
        ev(CB),
    ]

    for order in orders(events):
        graph = build(order)
        assert callback_of(graph) == CB
        assert graph._unlock_by_chord == {G: U}


def test_two_unlock_tasks_naming_different_first_tasks_draw_nothing() -> None:
    events = [
        header(H1, stamp=G),
        unlock(U, G, task_body(CB)),
        unlock(U2, G, task_body(CB2)),
        ev(CB),
        ev(CB2),
    ]

    draws(events, None)


def test_unlock_without_a_body_signature_leaves_the_header_evidence() -> None:
    events = [header(H1, stamp=CB, first=CB), unlock(U, G), ev(CB)]

    draws(events, CB)


def test_unlock_naming_a_chord_without_headers_makes_no_chord() -> None:
    graph = build([unlock(U, G, task_body(CB)), ev(CB)])

    assert f"group:{G}" not in graph.nodes
    assert graph._unlocks[U].group_id == G


def test_unlock_arguments_arriving_later_are_read() -> None:
    graph = build(
        [
            header(H1, stamp=G),
            ev(CB),
            ev(U, TaskState.RECEIVED, name=UNLOCK),
        ]
    )
    assert callback_of(graph) is None

    graph.add_event(unlock(U, G, task_body(CB), state=TaskState.STARTED))

    assert callback_of(graph) == CB


def test_unlock_reads_the_arguments_of_its_earliest_event_in_any_order() -> None:
    # Events are kept in lifecycle order: whichever arrives, the PENDING
    # event's arguments are the task's.
    pending = unlock(U, G, task_body(CB), state=TaskState.PENDING)
    started = unlock(U, G2, task_body(CB2), state=TaskState.STARTED)
    events = [header(H1, G, stamp=G), header(H2, G2, stamp=G2), pending, started]
    events += [ev(CB), ev(CB2)]

    for order in orders(events):
        graph = build(order)
        assert graph._unlocks_by_group == {G: {U: None}}
        assert callback_of(graph, G) == CB
        assert callback_of(graph, G2) is None


# --- a task's name decides whether it is a chord_unlock task -----------------


def _every_order(events: list[TaskEvent]) -> list[TaskGraph]:
    return [build(list(order)) for order in permutations(events)]


def test_task_named_chord_unlock_by_its_first_event_names_the_body() -> None:
    # The task's own PENDING (first in lifecycle order) names it chord_unlock;
    # a later event of the same task has another name.
    body = [G, task_body(CB)]
    events = [
        header(H1, stamp=G),
        ev(CB),
        ev(U, TaskState.STARTED, name="app.other", args=body),
        ev(U, TaskState.PENDING, name=UNLOCK, args=body),
    ]

    for graph in _every_order(events):
        assert graph.nodes[U].name == UNLOCK
        assert callback_of(graph) == CB
        assert graph._unlock_of == {U: G}
        assert graph.nodes[U].parent_id == f"group:{G}"


def test_task_named_something_else_by_its_first_event_names_no_body() -> None:
    body = [G, task_body(CB)]
    events = [
        header(H1, stamp=G),
        ev(CB),
        ev(U, TaskState.STARTED, name=UNLOCK, args=body),
        ev(U, TaskState.PENDING, name="app.other", args=body),
    ]

    for graph in _every_order(events):
        assert graph.nodes[U].name == "app.other"
        assert callback_of(graph) is None
        assert graph._unlocks == {}
        assert graph._unlock_of == {}


def test_task_renamed_by_a_replacement_check_stops_naming_the_body() -> None:
    """A rename found by re-checking a task, not by one of its own events.

    Once ``d`` names ``u`` as its root, ``u``'s PENDING (reporting ``d``, below
    ``u``) reads as a replacement's, so ``u`` takes the name of its other event.
    """
    body = [G, task_body(CB)]
    d = uid(33)
    pending = ev(U, TaskState.PENDING, name=UNLOCK, parent=d, args=body)
    events = [
        header(H1, stamp=G),
        ev(CB),
        pending.model_copy(update={"root_id": U}),
        ev(U, TaskState.STARTED, name="app.other", parent=d, args=body),
        ev(d, TaskState.STARTED).model_copy(update={"root_id": U}),
    ]

    for graph in _every_order(events):
        assert graph.nodes[U].name == "app.other"
        assert callback_of(graph) is None
        assert graph._unlock_of == {}


# --- never evidence ---------------------------------------------------------


def test_parent_pointers_are_not_evidence() -> None:
    # The old rule walked parents: a task reporting a header as its parent was
    # the callback. Without a stamp or first ID naming it, it is not drawn.
    events = [
        header(H1, stamp=G),
        header(H2, stamp=G),
        ev(CB, parent=H2, group=G),
        ev(CB2, parent=U),
        unlock(U, G),
    ]

    draws(events, None)


def test_task_names_are_not_evidence() -> None:
    events = [
        header(H1, stamp=CB),
        ev(CB2, name="app.aggregate"),
        ev(CB, name=UNLOCK),
    ]

    draws(events, CB)


def test_timestamps_and_order_are_not_evidence() -> None:
    late = TaskEvent(
        task_id=CB,
        name="app.cb",
        state=TaskState.SUCCESS,
        timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    draws([header(H1, stamp=CB), late], CB)


def test_chord_id_of_an_event_is_not_evidence() -> None:
    other = TaskEvent(
        task_id=CB,
        name="app.cb",
        state=TaskState.SUCCESS,
        timestamp=_TIME,
        chord_id=G,
    )

    draws([header(H1, stamp=G), other], None)


# --- removal ----------------------------------------------------------------


def test_removing_the_callback_stops_drawing_it_and_a_new_event_restores_it() -> None:
    graph = build([header(H1, first=CB), ev(CB)])

    graph.remove_node(CB)

    assert_graph_invariants(graph)
    assert callback_of(graph) is None
    assert graph.nodes[f"group:{G}"].children == [H1]

    graph.add_event(ev(CB))

    assert callback_of(graph) == CB


def test_removing_the_only_header_with_the_first_id_forgets_it() -> None:
    graph = build([header(H1, first=CB), header(H2, stamp=CB), ev(CB)])
    assert callback_of(graph) == CB

    graph.remove_node(H1)

    assert_graph_invariants(graph)
    assert graph._groups[G].firsts == {}
    assert callback_of(graph) == CB  # the stamp names it now


def test_removing_a_header_that_made_stamps_conflict_restores_the_callback() -> None:
    graph = build([header(H1, stamp=CB), header(H2, stamp=CB2), ev(CB), ev(CB2)])
    assert callback_of(graph) is None

    graph.remove_node(H2)

    assert_graph_invariants(graph)
    assert callback_of(graph) == CB


def test_removing_the_unlock_task_forgets_what_it_named() -> None:
    graph = build([header(H1, stamp=G), unlock(U, G, task_body(CB)), ev(CB)])
    assert callback_of(graph) == CB

    graph.remove_node(U)

    assert_graph_invariants(graph)
    assert callback_of(graph) is None
    assert graph._unlocks == {}
    assert graph._unlocks_by_group == {}


def test_removing_the_chord_node_and_adding_a_header_draws_the_callback_again() -> None:
    graph = build([header(H1, first=CB), ev(CB)])

    graph.remove_node(f"group:{G}")
    assert callback_of_missing(graph)

    graph.add_event(header(H1, first=CB, state=TaskState.FAILURE))

    assert callback_of(graph) == CB


def callback_of_missing(graph: TaskGraph) -> bool:
    """Whether the chord's node is gone (and the callback no longer listed)."""
    return f"group:{G}" not in graph.nodes and graph._callback_of == {}


def test_removing_a_chord_releases_the_nodes_it_pointed_to_for_another_chord() -> None:
    graph = build([header(H1, G, first=CB), header(H2, G2, first=CB), ev(CB)])
    assert callback_of(graph, G) is None

    graph.remove_node(f"group:{G}")

    assert_graph_invariants(graph)
    assert callback_of(graph, G2) == CB


def test_last_header_leaving_turns_the_chord_back_into_a_group() -> None:
    graph = build([header(H1, first=CB), ev(H2, group=G), ev(CB)])

    graph.remove_node(H1)

    assert_graph_invariants(graph)
    assert f"group:{G}" not in graph.nodes  # one member left
    assert graph._candidate == {}
    assert graph._claims == {}


# --- placement --------------------------------------------------------------


def test_callback_that_is_the_chords_own_ancestor_is_not_drawn() -> None:
    # task.replace() reuses an ID: the callback is also the task that sent the
    # headers. A node can't hang below itself.
    events = [
        ev(CB),
        ev(H1, TaskState.STARTED, parent=CB, group=G, first=CB),
        ev(H2, TaskState.STARTED, parent=CB, group=G, first=CB),
    ]

    for order in orders(events):
        graph = build(order)
        assert callback_of(graph) is None
        assert graph._cycles_refused >= 1


def test_chord_callback_arriving_before_the_headers_is_drawn_once_they_do() -> None:
    graph = build([ev(CB, parent=H1)])

    graph.add_event(header(H1, first=CB))

    assert_graph_invariants(graph)
    assert graph.nodes[f"group:{G}"].children == [H1, CB]
    assert graph.nodes[CB].parent_id == f"group:{G}"


# --- chord_not_run_reasons --------------------------------------------------


@pytest.mark.parametrize("field", ["stamp", "first"])
def test_chord_not_run_reason_counts_a_header_with_either_field(field: str) -> None:
    value: dict[str, Any] = {field: CB}
    graph = build(
        [
            ev(H1, TaskState.FAILURE, group=G, **value),
            ev(H2, TaskState.SUCCESS, group=G, **value),
        ]
    )

    assert chord_not_run_reasons(graph.nodes) == {f"group:{G}": "header_failed"}


def test_chord_not_run_reason_ignores_a_group_task_without_chord_fields() -> None:
    graph = build(
        [
            ev(H1, TaskState.FAILURE, group=G),
            header(H2, first=CB),
        ]
    )

    assert chord_not_run_reasons(graph.nodes) == {}


# --- plumbing ---------------------------------------------------------------


def test_chord_callback_id_of_the_node_is_the_one_stamp_or_none() -> None:
    graph = build([header(H1, stamp=CB), header(H2, stamp=CB)])
    assert graph.nodes[f"group:{G}"].chord_callback_id == CB

    graph.add_event(header(H2, stamp=CB2, state=TaskState.STARTED))

    assert graph.nodes[f"group:{G}"].chord_callback_id is None


def test_graph_rebuilt_from_its_nodes_keeps_the_drawn_callback() -> None:
    graph = build([header(H1, first=CB), header(H2, first=CB), ev(CB)])

    rebuilt = TaskGraph.model_validate(graph.model_dump())

    assert callback_of(rebuilt) == CB
