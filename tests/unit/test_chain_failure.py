"""A failed chain step publishes FAILURE for the steps it leaves unrun."""

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery, chain, chord, group
from celery.canvas import Signature
from celery.contrib.testing.worker import start_worker

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import TaskGraph
from stemtrace.library.config import StemtraceConfig, set_config
from stemtrace.library.signals import (
    _on_task_failure,
    connect_signals,
    disconnect_signals,
)
from stemtrace.transports.memory import MemoryTransport


def _noop(*args: Any) -> None:
    """A task body; these tests only send messages."""


@pytest.fixture
def app() -> Iterator[Celery]:
    set_config(StemtraceConfig(transport_url="memory://"))
    MemoryTransport.clear()
    connect_signals(MemoryTransport())
    app = Celery("chain-failure", broker="memory://", backend="cache+memory://")
    for name in ("a", "b", "c", "d", "cb"):
        app.task(name=f"t.{name}")(_noop)
    yield app
    disconnect_signals()
    MemoryTransport.clear()


def _sent(app: Celery, sig: Signature) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Send ``sig``; return each task message's headers and embed, as a worker
    gets them (protocol 2: ``request.chain`` is ``embed["chain"]``)."""
    sig.apply_async()
    return _drain(app)


def _drain(app: Celery) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Take every task message off the memory broker's queue."""
    messages = []
    with app.connection_for_read() as conn:
        queue = conn.SimpleQueue("celery")
        while queue.qsize():
            message = queue.get(timeout=1)
            _, _, embed = message.decode()
            messages.append((message.headers, embed))
            message.ack()
    return messages


def _fail(
    headers: dict[str, Any], embed: dict[str, Any], chain_left: list[Any]
) -> None:
    """Run stemtrace's task_failure handler as Celery's tracer does for a task
    whose request carries ``chain_left`` (celery 5.6.3 app/trace.py)."""
    request = SimpleNamespace(
        id=headers["id"],
        parent_id=headers.get("parent_id"),
        root_id=headers.get("root_id"),
        group=headers.get("group"),
        chord=embed.get("chord"),
        chain=chain_left,
        retries=0,
        hostname="celery@w1",
        delivery_info={},
        is_eager=False,
    )
    sender = SimpleNamespace(name=headers["task"], request=request)
    _on_task_failure(
        task_id=headers["id"],
        exception=ValueError("boom"),
        args=(),
        kwargs={},
        traceback=None,
        einfo=None,
        sender=sender,
    )


def _failures() -> list[TaskEvent]:
    return [
        e
        for e in MemoryTransport.events
        if isinstance(e, TaskEvent) and e.state == TaskState.FAILURE
    ]


@pytest.mark.parametrize("length", [2, 3, 4])
def test_failed_chain_step_publishes_failure_for_each_unrun_step(
    app: Celery, length: int
) -> None:
    """Celery stores FAILURE for every step after the failed one (celery 5.6.3
    backends/base.py mark_as_failure) and runs none of them."""
    names = ["t.a", "t.b", "t.c", "t.d"][:length]
    ((headers, embed),) = _sent(app, chain(*(app.signature(n) for n in names)))
    steps = list(reversed(embed["chain"]))  # next step first
    ids = [headers["id"], *(s["options"]["task_id"] for s in steps)]

    for failed_at in range(length):
        MemoryTransport.clear()
        left = list(reversed(steps[failed_at:]))  # the failed step's request.chain
        step_headers = {**headers, "id": ids[failed_at], "task": names[failed_at]}
        _fail(step_headers, embed, left)

        events = _failures()
        assert [e.task_id for e in events] == ids[failed_at:], failed_at
        assert [e.name for e in events] == names[failed_at:]
        assert [e.parent_id for e in events[1:]] == ids[failed_at:-1]
        assert {e.root_id for e in events} == {headers["root_id"]}
        assert {e.exception for e in events} == {"ValueError: boom"}
        assert {e.exception_type for e in events} == {"ValueError"}
        # Never ran: no worker, no traceback, no output.
        assert [(e.hostname, e.traceback, e.output) for e in events[1:]] == [
            (None, None, None)
        ] * (length - 1 - failed_at)


def test_failed_head_of_chord_header_chain_fails_its_last_step_as_header(
    app: Celery,
) -> None:
    """Only a header chain's last step names the chord (celery 5.6.3 canvas.py
    prepare_steps), so its FAILURE is what marks the header failed."""
    sig = chord(
        [chain(app.signature("t.a"), app.signature("t.b")), app.signature("t.c")],
        app.signature("t.cb"),
    )
    sent = _sent(app, sig)
    ((headers, embed),) = [(h, e) for h, e in sent if e.get("chain")]

    _fail(headers, embed, embed["chain"])

    head, step = _failures()
    assert (head.group_id, head.chord_callback_id) == (None, None)
    (last_step,) = embed["chain"]
    callback_id = last_step["options"]["chord"]["options"]["task_id"]
    assert step.name == "t.b"
    assert step.group_id is not None
    assert step.chord_callback_id == callback_id
    assert step.parent_id == head.task_id


def test_failed_step_skips_unrun_steps_without_a_task(app: Celery) -> None:
    """A group step's ID is its group's, not a task's: no node is made for it."""
    ((headers, embed),) = _sent(
        app,
        chain(app.signature("t.a"), group(app.signature("t.b"), app.signature("t.c"))),
    )

    _fail(headers, embed, embed["chain"])

    assert [e.name for e in _failures()] == ["t.a"]


@pytest.mark.parametrize(
    "popped", [False, True], ids=["celery_5_6_3", "celery_5_2_to_5_6_2"]
)
def test_failed_step_whose_next_step_was_already_sent_publishes_no_more(
    app: Celery, popped: bool
) -> None:
    """Celery sends the next step before it stores the result, so a result it
    can't store (EncodeError) fails a task whose chain already moved on; the
    later steps run. celery 5.6.3 app/trace.py leaves the sent step in
    request.chain; 5.2.2 to 5.6.2 pop() it before sending."""
    names = ["t.a", "t.b", "t.c", "t.d"]
    ((headers, embed),) = _sent(app, chain(*(app.signature(n) for n in names)))
    steps = list(embed["chain"])  # next step last
    sent = steps.pop() if popped else steps[-1]
    app.signature(sent).apply_async(
        chain=steps if popped else steps[:-1], parent_id=headers["id"]
    )
    _drain(app)

    _fail(headers, embed, steps)

    assert [e.task_id for e in _failures()] == [headers["id"]]


def test_failed_step_whose_chain_another_task_sent_still_fails_its_steps(
    app: Celery,
) -> None:
    """Only the failed task's own sends count: this process having sent the
    same chain for another task (it started the workflow) doesn't."""
    names = ["t.a", "t.b", "t.c"]
    ((headers, embed),) = _sent(app, chain(*(app.signature(n) for n in names)))
    app.signature(embed["chain"][-1]).apply_async(parent_id="someone-else")
    _drain(app)

    _fail(headers, embed, embed["chain"])

    assert [e.name for e in _failures()] == names


def test_failed_step_with_a_malformed_next_step_does_not_raise(app: Celery) -> None:
    ((headers, embed),) = _sent(app, chain(app.signature("t.a"), app.signature("t.b")))
    malformed = {"task": "t.x", "options": {"task_id": ["x"]}}

    _fail(headers, embed, [*embed["chain"], malformed])

    assert [e.name for e in _failures()] == ["t.a", "t.b"]


def test_failed_step_skips_malformed_unrun_steps(app: Celery) -> None:
    """A hand-built or foreign chain entry without a task ID or name is
    skipped; the well-formed steps around it are still published, in order."""
    names = ["t.a", "t.b", "t.c"]
    ((headers, embed),) = _sent(app, chain(*(app.signature(n) for n in names)))
    last, following = embed["chain"]  # next step last
    malformed = [{"task": "t.x", "options": {}}, {"options": {"task_id": "x"}}, "junk"]

    _fail(headers, embed, [last, *malformed, following])

    events = _failures()
    assert [e.name for e in events] == names
    assert events[2].parent_id == events[1].task_id


def test_failed_task_outside_a_chain_publishes_one_failure(app: Celery) -> None:
    ((headers, embed),) = _sent(app, app.signature("t.a"))

    _fail(headers, embed, embed.get("chain") or [])

    assert [e.task_id for e in _failures()] == [headers["id"]]


_CAPTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parents[1] / "data" / "celery_flows.json").read_text()
)


def test_real_chain_whose_result_was_not_stored_shows_its_steps_succeeded() -> None:
    """Real worker capture: chain(unstorable, add, add); the head's result
    can't be encoded, so it fails after Celery already sent the next step."""
    graph = TaskGraph()
    for event in _CAPTURES["chain_head_result_not_stored"]:
        graph.add_event(TaskEvent.model_validate(event))

    states = sorted((n.name, n.state) for n in graph.nodes.values())
    assert states == [
        ("flows.add", TaskState.SUCCESS),
        ("flows.add", TaskState.SUCCESS),
        ("flows.unstorable", TaskState.FAILURE),
    ]
    assert not [
        e
        for e in _CAPTURES["chain_head_result_not_stored"]
        if e["state"] == "FAILURE" and e["name"] == "flows.add"
    ]


def test_real_failed_chain_shows_each_unrun_step_failed_under_the_last() -> None:
    """Real worker capture: chain(fail, add, add) with fail raising."""
    graph = TaskGraph()
    for event in _CAPTURES["chain_failed_head"]:
        graph.add_event(TaskEvent.model_validate(event))

    (root_id,) = graph.root_ids
    path = [graph.nodes[root_id]]
    while path[-1].children:
        (child_id,) = path[-1].children
        path.append(graph.nodes[child_id])
    assert [(n.name, n.state) for n in path] == [
        ("flows.fail", TaskState.FAILURE),
        ("flows.add", TaskState.FAILURE),
        ("flows.add", TaskState.FAILURE),
    ]


def _unstorable() -> object:
    """A result json can't encode: storing it fails after the chain moved on."""
    return object()


def _add(x: int, y: int) -> int:
    """Add two numbers."""
    return x + y


def test_real_worker_result_not_stored_leaves_the_sent_steps_running() -> None:
    """A real worker of whichever Celery is installed (CI runs the oldest
    supported one too): its chain step dispatch, request.chain shape and
    EncodeError path, end to end."""
    app = Celery("chain-worker", broker="memory://", backend="cache+memory://")
    unstorable = app.task(name="w.unstorable")(_unstorable)
    add = app.task(name="w.add")(_add)
    set_config(StemtraceConfig(transport_url="memory://"))
    MemoryTransport.clear()
    connect_signals(MemoryTransport())
    try:
        with start_worker(app, pool="solo", perform_ping_check=False):
            workflow = chain(unstorable.si(), add.si(1, 1), add.si(2, 2), add.si(3, 3))
            result = workflow.apply_async()
            # propagate=False: get() would also raise the head's EncodeError.
            result.get(timeout=30, propagate=False)
            assert result.result == 6
    finally:
        disconnect_signals()

    failures = [(e.name, e.hostname) for e in _failures()]
    assert [name for name, _ in failures] == ["w.unstorable"]
