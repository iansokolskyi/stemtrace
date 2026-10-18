"""A FAILURE Celery sends from the worker's main process carries the attempt's data.

Celery 5.6.3 (and 5.2.2) worker/request.py ``Request.on_failure`` sends
``task_failure`` for a hard time limit or a lost pool process from the main
process, with ``sender=self.task``; there ``task.request`` is an empty default
(app/task.py ``Task._get_request``), so the event had ``retries=0`` whatever the
attempt and no worker fields. stemtrace reads the failing ``Request`` from
``on_failure``'s frame instead. ``_on_failure`` below stands in for that method:
a frame whose ``self`` is a real ``celery.worker.request.Request``.
"""

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from celery import Celery
from celery.app.task import Context
from celery.contrib.testing.mocks import TaskMessage
from celery.signals import task_failure
from celery.worker.request import Request

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.config import StemtraceConfig, set_config
from stemtrace.library.signals import connect_signals, disconnect_signals
from stemtrace.transports.memory import MemoryTransport

app = Celery("main_process_failure", broker="memory://")


@app.task(name="app.encode")
def encode() -> None:
    """Do nothing."""


class MainProcessTask:
    """The task as the worker's main process sees it: an empty request."""

    name = "app.encode"
    request = Context()


@pytest.fixture(autouse=True)
def transport() -> Iterator[MemoryTransport]:
    """Connect the handlers to a fresh memory transport."""
    MemoryTransport.clear()
    set_config(StemtraceConfig(transport_url="memory://"))
    transport = MemoryTransport()
    connect_signals(transport)
    yield transport
    disconnect_signals()
    MemoryTransport.clear()


def _request(
    task_id: str,
    retries: int,
    hostname: str = "celery@w1",
    chord: dict[str, Any] | None = None,
    chain: list[dict[str, Any]] | None = None,
) -> Request:
    """A worker Request built by Celery from a protocol 2 message.

    ``chord`` and ``chain`` travel in the body's embed, not the headers (celery
    5.6.3 app/amqp.py ``as_task_v2``), as the real worker test shows.
    """
    message = TaskMessage(
        "app.encode",
        id=task_id,
        retries=retries,
        parent_id="parent-1",
        root_id="root-1",
        group="group-1",
        chain=chain,
    )
    args, kwargs, embed = message.payload
    message.payload = (args, kwargs, {**embed, "chord": chord})
    message.delivery_info = {"exchange": "", "routing_key": "media"}
    message.properties = {"priority": 0}
    return Request(message, app=app, hostname=hostname, task=encode)


def _send_failure(task_id: str, sender: Any = None) -> None:
    task_failure.send(
        sender=sender or MainProcessTask(),
        task_id=task_id,
        exception=TimeoutError("hard time limit"),
        args=(),
        kwargs={},
        traceback=None,
        einfo=None,
    )


def _on_failure(self: Any, task_id: str, sender: Any = None) -> None:
    """Like celery 5.6.3 worker/request.py Request.on_failure: sends the signal.

    ``mark_as_failure(request=self._context)`` runs first, and ``_context``
    merges the message's embed (chord, chain) into ``request_dict``.
    """
    _ = self._context
    _send_failure(task_id, sender)


def _failure() -> TaskEvent:
    [event] = [e for e in MemoryTransport.events if isinstance(e, TaskEvent)]
    assert event.state == TaskState.FAILURE
    return event


def test_main_process_failure_carries_its_request() -> None:
    _on_failure(_request("task-1", retries=2), "task-1")

    event = _failure()
    assert (event.retries, event.hostname, event.queue, event.routing_key) == (
        2,
        "celery@w1",
        "media",
        "media",
    )
    assert (event.parent_id, event.root_id, event.group_id) == (
        "parent-1",
        "root-1",
        "group-1",
    )


def test_attempt_zero_failing_while_attempt_one_is_known_is_attempt_zero() -> None:
    # self.retry(throw=False): attempt 1's Request (another worker here) is
    # alive, and even on the stack further up, when attempt 0's FAILURE is
    # sent; the nearest Request, attempt 0's own, is the one read.
    def outer(self: Any) -> None:
        _on_failure(_request("task-1", retries=0, hostname="celery@w0"), "task-1")

    outer(_request("task-1", retries=1, hostname="celery@w1"))

    event = _failure()
    assert (event.retries, event.hostname, event.queue) == (0, "celery@w0", "media")


def test_shallow_stack_without_a_request_keeps_the_defaults() -> None:
    # A failure sent from a fresh thread: the stack ends before the search
    # depth.
    thread = threading.Thread(target=_send_failure, args=("task-1",))
    thread.start()
    thread.join(5)

    assert (_failure().retries, _failure().hostname) == (0, None)


def test_request_for_another_task_is_ignored() -> None:
    _on_failure(_request("other", retries=3), "task-1")

    event = _failure()
    assert (event.retries, event.hostname, event.queue) == (0, None, None)


def test_no_request_on_the_stack_keeps_the_defaults() -> None:
    _send_failure("task-1")

    event = _failure()
    assert (event.retries, event.hostname, event.queue) == (0, None, None)


def test_request_beyond_the_search_depth_is_not_used() -> None:
    def deeper(depth: int) -> None:
        if depth:
            deeper(depth - 1)
        else:
            _send_failure("task-1")

    def on_failure(self: Any) -> None:
        deeper(20)

    on_failure(_request("task-1", retries=2))

    assert _failure().retries == 0


def test_failure_in_the_task_process_uses_its_own_request() -> None:
    class RunningTask:
        name = "app.encode"
        request = Context(
            id="task-1",
            retries=1,
            hostname="celery@w1",
            delivery_info={"exchange": "", "routing_key": "media"},
        )

    # A Request with other values on the stack must not win over the task's own.
    _on_failure(
        _request("task-1", retries=5, hostname="celery@other"), "task-1", RunningTask()
    )

    event = _failure()
    assert (event.retries, event.hostname) == (1, "celery@w1")


CHORD = {
    "task": "app.callback",
    "options": {"task_id": "callback-1", "group_id": "chord-1"},
}


def _step(step_id: str) -> dict[str, Any]:
    return {"task": "app.encode", "options": {"task_id": step_id}, "subtask_type": None}


def test_main_process_failure_carries_its_chord() -> None:
    _on_failure(_request("task-1", retries=0, chord=CHORD), "task-1")

    event = _failure()
    assert (event.chord_id, event.chord_callback_id) == ("chord-1", "callback-1")


def test_main_process_failure_publishes_the_chain_steps_it_leaves_unrun() -> None:
    # request.chain holds the steps left, the next one last.
    chain = [_step("step-3"), _step("step-2")]
    _on_failure(_request("task-1", retries=0, chain=chain), "task-1")

    events = [e for e in MemoryTransport.events if isinstance(e, TaskEvent)]
    assert [(e.task_id, e.state, e.parent_id, e.root_id) for e in events] == [
        ("task-1", TaskState.FAILURE, "parent-1", "root-1"),
        ("step-2", TaskState.FAILURE, "task-1", "root-1"),
        ("step-3", TaskState.FAILURE, "step-2", "root-1"),
    ]
    assert [e.exception_type for e in events] == ["TimeoutError"] * 3
    assert [e.hostname for e in events] == ["celery@w1", None, None]
