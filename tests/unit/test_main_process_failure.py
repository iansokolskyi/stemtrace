"""A FAILURE Celery sends from the worker's main process carries the attempt's data.

Celery 5.6.3 (and 5.2.2) worker/request.py ``Request.on_failure`` sends
``task_failure`` for a hard time limit or a lost pool process from the main
process, with ``sender=self.task``; there ``task.request`` is an empty default
(app/task.py ``Task._get_request``), so the event had ``retries=0`` whatever the
attempt and no worker fields. The main process did see the request when the
message arrived: worker/strategy.py sends ``task_received`` with it.
``ReceivedRequest`` stands in for celery.worker.request.Request: the
attributes it has are the ones that class exposes (``request_dict``,
``hostname``, ``delivery_info``), with its values.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from celery.app.task import Context
from celery.signals import task_failure, task_received

import stemtrace.library.signals as signals_module
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.config import StemtraceConfig, set_config
from stemtrace.library.signals import connect_signals, disconnect_signals
from stemtrace.transports.memory import MemoryTransport


class ReceivedRequest:
    """The parts of celery.worker.request.Request the receiver reads."""

    def __init__(self, task_id: str, retries: int, hostname: str) -> None:
        """A request for attempt ``retries`` of ``task_id`` on ``hostname``."""
        self.request_dict: dict[str, Any] = {
            "id": task_id,
            "retries": retries,
            "parent_id": "parent-1",
            "root_id": "root-1",
            "group": "group-1",
            "args": ["big"] * 3,
            "kwargs": {},
        }
        self.hostname = hostname
        self.delivery_info = {"exchange": "", "routing_key": "media", "priority": 0}


class Task:
    """A task in the worker's main process: its request is an empty default."""

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


def _receive(task_id: str, retries: int, hostname: str = "celery@w1") -> None:
    # celery 5.6.3 worker/strategy.py: task_received.send(sender=consumer, request=req)
    task_received.send(
        sender=object(), request=ReceivedRequest(task_id, retries, hostname)
    )


def _fail_from_main_process(task_id: str) -> TaskEvent:
    # celery 5.6.3 worker/request.py Request.on_failure
    task_failure.send(
        sender=Task(),
        task_id=task_id,
        exception=TimeoutError("hard time limit"),
        args=(),
        kwargs={},
        traceback=None,
        einfo=None,
    )
    [event] = [e for e in MemoryTransport.events if isinstance(e, TaskEvent)]
    assert event.state == TaskState.FAILURE
    return event


def test_main_process_failure_carries_the_received_attempt() -> None:
    _receive("task-1", retries=2)

    event = _fail_from_main_process("task-1")

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


def test_latest_delivery_wins() -> None:
    # A retry is a new message: received again with the new count.
    _receive("task-1", retries=0, hostname="celery@w1")
    _receive("task-1", retries=1, hostname="celery@w2")

    event = _fail_from_main_process("task-1")

    assert (event.retries, event.hostname) == (1, "celery@w2")


def test_main_process_failure_never_received_keeps_the_defaults() -> None:
    event = _fail_from_main_process("unknown")

    assert (event.retries, event.hostname, event.queue) == (0, None, None)


def test_failure_in_the_task_process_uses_its_own_request() -> None:
    _receive("task-1", retries=5, hostname="celery@other")

    class RunningTask:
        name = "app.encode"
        request = Context(
            id="task-1",
            retries=1,
            hostname="celery@w1",
            delivery_info={"exchange": "", "routing_key": "media"},
        )

    task_failure.send(
        sender=RunningTask(),
        task_id="task-1",
        exception=ValueError("boom"),
        args=(),
        kwargs={},
        traceback=None,
        einfo=None,
    )

    [event] = [e for e in MemoryTransport.events if isinstance(e, TaskEvent)]
    assert (event.retries, event.hostname) == (1, "celery@w1")


def test_remembered_requests_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signals_module, "_RECEIVED_REQUESTS_SIZE", 2, raising=False)
    for n in range(3):
        _receive(f"task-{n}", retries=n + 4)

    oldest = _fail_from_main_process("task-0")

    assert oldest.retries == 0  # forgotten (it was 4): the defaults
    MemoryTransport.clear()
    assert _fail_from_main_process("task-2").retries == 6


def test_remembered_request_holds_no_arguments() -> None:
    _receive("task-1", retries=0)

    remembered = signals_module._received_requests.get("task-1")

    assert remembered is not None
    assert getattr(remembered, "args", None) is None


@pytest.mark.parametrize(
    "request_dict",
    [None, "not-a-dict", {"retries": 1}, {"id": 7, "retries": 1}],
    ids=["missing", "not-a-dict", "no-id", "non-string-id"],
)
def test_request_without_a_usable_id_is_not_remembered(request_dict: object) -> None:
    # A receiver of task_received from another producer's request shape.
    request = ReceivedRequest("task-1", retries=3, hostname="celery@w1")
    request.request_dict = request_dict  # type: ignore[assignment]  # malformed on purpose
    task_received.send(sender=object(), request=request)

    assert dict(signals_module._received_requests) == {}
    assert _fail_from_main_process("task-1").retries == 0
