"""The ETA a task was sent with (``countdown`` or ``eta``) on the events the handlers publish.

Celery puts it in the message as an ISO 8601 string: in the headers with protocol 2 (the
default) and in the body with protocol 1 (celery 5.6.3 and 5.2.2 app/amqp.py ``as_task_v2``
and ``as_task_v1``). The worker parses it with ``maybe_iso8601`` and treats a naive value as
UTC (worker/request.py ``Request.__init__``); ``task.request.eta`` is the string itself.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery
from celery.app.task import Context

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.bootsteps import ReceivedEventStep, _set_publisher
from stemtrace.library.config import StemtraceConfig, set_config
from stemtrace.library.signals import (
    _on_after_task_publish,
    _on_task_prerun,
    _parse_eta,
    connect_signals,
    disconnect_signals,
)
from stemtrace.transports.memory import MemoryTransport

ETA = datetime(2026, 10, 18, 9, 30, 15, 250000, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def transport() -> Iterator[MemoryTransport]:
    """Connect the handlers to a fresh memory transport."""
    MemoryTransport.clear()
    set_config(StemtraceConfig(transport_url="memory://"))
    transport = MemoryTransport()
    connect_signals(transport)
    yield transport
    disconnect_signals()
    _set_publisher(None)
    MemoryTransport.clear()


@pytest.fixture
def app() -> Celery:
    app = Celery("eta-tests", broker="memory://")

    @app.task(name="app.encode")
    def encode() -> None:
        """Do nothing."""

    return app


def _task_events() -> list[TaskEvent]:
    return [e for e in MemoryTransport.events if isinstance(e, TaskEvent)]


def _only(state: TaskState) -> TaskEvent:
    [event] = [e for e in _task_events() if e.state == state]
    return event


class TestParseEta:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-10-18T09:30:15.250000+00:00", ETA),
            ("2026-10-18T12:30:15.250000+03:00", ETA),
            # Naive: UTC, as the worker reads it (maybe_make_aware, naive_as_utc).
            ("2026-10-18T09:30:15.250000", ETA),
            (ETA, ETA),
            (ETA.replace(tzinfo=None), ETA),
            (None, None),
            ("", None),
            ("not-a-date", None),
            # Past the ends of the date range once converted to UTC (Celery's worker
            # catches OverflowError for the same conversion, worker/strategy.py).
            ("9999-12-31T23:00:00-05:00", None),
            ("0001-01-01T00:30:00+01:00", None),
            (12345, None),
        ],
        ids=[
            "utc",
            "offset",
            "naive-is-utc",
            "datetime",
            "naive-datetime",
            "none",
            "empty",
            "garbage",
            "overflow-max",
            "overflow-min",
            "number",
        ],
    )
    def test_parse_eta(self, value: Any, expected: datetime | None) -> None:
        parsed = _parse_eta(value)
        assert parsed == expected
        if parsed is not None:
            assert parsed.utcoffset() == timedelta(0)


class TestPending:
    """The sender's PENDING carries the ETA Celery wrote into the message."""

    def test_countdown(self, app: Celery) -> None:
        before = datetime.now(timezone.utc)
        app.tasks["app.encode"].apply_async(countdown=3)
        after = datetime.now(timezone.utc)

        eta = _only(TaskState.PENDING).eta
        assert eta is not None
        assert before + timedelta(seconds=3) <= eta <= after + timedelta(seconds=3)

    def test_explicit_eta(self, app: Celery) -> None:
        app.tasks["app.encode"].apply_async(eta=ETA)

        assert _only(TaskState.PENDING).eta == ETA

    def test_naive_explicit_eta_is_utc(self, app: Celery) -> None:
        app.tasks["app.encode"].apply_async(eta=ETA.replace(tzinfo=None))

        assert _only(TaskState.PENDING).eta == ETA

    def test_protocol_1_body(self, app: Celery) -> None:
        app.conf.task_protocol = 1
        app.tasks["app.encode"].apply_async(eta=ETA)

        assert _only(TaskState.PENDING).eta == ETA

    def test_no_eta(self, app: Celery) -> None:
        app.tasks["app.encode"].delay()

        assert _only(TaskState.PENDING).eta is None

    def test_unparsable_eta_is_dropped_not_raised(self) -> None:
        _on_after_task_publish(
            sender="app.encode",
            body=((), {}, {}),
            headers={"id": "task-1", "task": "app.encode", "eta": "soon"},
        )

        event = _only(TaskState.PENDING)
        assert (event.task_id, event.eta) == ("task-1", None)


class TestWorkerEvents:
    """Worker events read it from ``task.request.eta`` (the message's string)."""

    def test_started_carries_eta(self) -> None:
        request = Context(
            id="task-1", task="app.encode", retries=0, eta=ETA.isoformat()
        )
        _on_task_prerun(
            task_id="task-1",
            task=SimpleNamespace(name="app.encode", request=request),
            args=(),
            kwargs={},
        )

        assert _only(TaskState.STARTED).eta == ETA

    def test_started_without_eta(self) -> None:
        request = Context(id="task-1", task="app.encode", retries=0)
        _on_task_prerun(
            task_id="task-1",
            task=SimpleNamespace(name="app.encode", request=request),
            args=(),
            kwargs={},
        )

        assert _only(TaskState.STARTED).eta is None

    def test_eager_apply_has_no_eta(self, app: Celery) -> None:
        app.tasks["app.encode"].apply()

        assert [e.eta for e in _task_events()] == [None, None]

    @pytest.mark.parametrize(
        ("headers", "body"),
        [
            ({"id": "task-1", "task": "app.encode", "eta": ETA.isoformat()}, None),
            (None, {"id": "task-1", "task": "app.encode", "eta": ETA.isoformat()}),
        ],
        ids=["protocol-2-headers", "protocol-1-body"],
    )
    def test_received_carries_eta(
        self, headers: dict[str, Any] | None, body: dict[str, Any] | None
    ) -> None:
        events: list[TaskEvent] = []
        _set_publisher(events.append)
        consumer = SimpleNamespace(
            hostname="celery@w1", strategies={"app.encode": lambda *_a, **_k: None}
        )
        ReceivedEventStep(consumer).start(consumer)
        message = SimpleNamespace(headers=headers, delivery_info=None)
        consumer.strategies["app.encode"](message, body, None, None, None)

        [event] = events
        assert (event.state, event.eta) == (TaskState.RECEIVED, ETA)
