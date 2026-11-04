"""Unregistered end to end: a real consumer, store, registry and refresher.

The fakes are the Celery ``Inspect`` the refresher builds (the one thing that
needs a live broker and workers) and the transport that hands events over; one
test stops a worker through the registry, as the consumer does.
Celery 5.6.3 ``Inspect.stats()`` and ``registered()`` return ``{nodename: ...}``
or None when nothing answers.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.server.api import routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.consumer import EventConsumer
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.store import GraphStore, WorkerRegistry, WorkerTimeline
from tests.unit.anomaly_api import ReplayTransport

pytestmark = pytest.mark.usefixtures("stop_probes")
DEADLINE_S = 10
NODE = "celery@w1"
BROKER = "redis://broker:6379/0"


def waiting(task_id: str, name: str, *, minutes: float = 20) -> TaskEvent:
    """A PENDING task that has waited ``minutes`` (the grace is 5, the clock is real)."""
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=TaskState.PENDING,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes),
    )


def ready(*tasks: str) -> WorkerEvent:
    """A ``worker_ready`` for ``NODE`` registering ``tasks``."""
    return WorkerEvent(
        event_type=WorkerEventType.WORKER_READY,
        hostname=NODE,
        pid=11,
        timestamp=datetime.now(timezone.utc),
        registered_tasks=list(tasks),
    )


class _Inspect:
    def __init__(
        self,
        stats: dict[str, Any] | None,
        registered: dict[str, Any] | None,
        ping: dict[str, Any] | None,
    ) -> None:
        self._stats, self._registered, self._ping = stats, registered, ping

    def stats(self) -> dict[str, Any] | None:
        return self._stats

    def registered(self) -> dict[str, Any] | None:
        return self._registered

    def ping(self) -> dict[str, Any] | None:
        return self._ping


@contextmanager
def fed(
    consumer: EventConsumer, events: list[TaskEvent | WorkerEvent]
) -> Iterator[None]:
    """Run ``consumer`` until it has handled every one of ``events``."""
    transport = ReplayTransport(events)
    with patch("stemtrace.server.consumer.get_transport", return_value=transport):
        consumer.start()
        try:
            deadline = time.monotonic() + DEADLINE_S
            while transport.consumed < len(events) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert transport.consumed == len(events)
            yield
        finally:
            transport.stop()
            consumer.stop(timeout=2.0)


def _items(client: TestClient) -> list[tuple[str, str | None, int]]:
    body = client.get("/api/anomalies").json()
    return [(a["kind"], a["name"], a["task_count"]) for a in body["anomalies"]]


def _extension_client(extension: StemtraceExtension) -> TestClient:
    app = FastAPI()
    app.include_router(extension.router)
    return TestClient(app)


def _run_extension(
    monkeypatch: pytest.MonkeyPatch, inspector: _Inspect | None
) -> list[tuple[str, str | None, int]]:
    monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: inspector)
    extension = StemtraceExtension(broker_url=BROKER, serve_ui=False)
    consumer = extension.consumer
    assert consumer is not None
    stream: list[TaskEvent | WorkerEvent] = [
        ready("app.known"),
        waiting("a", "app.missing"),
        waiting("b", "app.missing", minutes=30),
        waiting("c", "app.known"),
        waiting("celery", "celery.chord_unlock"),
    ]
    with fed(consumer, stream):
        return _items(_extension_client(extension))


def test_route_after_a_restart_without_replay_flags_nothing_until_inspect_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Row 26: RabbitMQ doesn't replay worker_ready, so the registry starts empty.
    extension = StemtraceExtension(broker_url=BROKER, serve_ui=False)
    consumer = extension.consumer
    assert consumer is not None
    monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: None)

    with fed(consumer, [waiting("a", "app.missing")]):
        before = _items(_extension_client(extension))
        answered = _Inspect(
            stats={NODE: {"pid": 11}}, registered={NODE: ["app.known"]}, ping=None
        )
        monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: answered)
        # The unreachable attempt backed off (D7c reading 3): let the wait pass.
        later = routes._monotonic() + routes._INSPECT_BACKOFF_START_SECONDS + 1
        monkeypatch.setattr(routes, "_monotonic", lambda: later)
        after = _items(_extension_client(extension))

    assert before == []
    assert after == [("unregistered", "app.missing", 1)]


def test_route_flags_a_name_no_online_worker_registers_after_a_complete_inspect_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = _Inspect(
        stats={NODE: {"pid": 11}},
        registered={NODE: ["app.known", "celery.backend_cleanup"]},
        ping={NODE: {"ok": "pong"}},
    )

    assert _run_extension(monkeypatch, inspector) == [
        ("unregistered", "app.missing", 2)
    ]


@pytest.mark.parametrize(
    "inspector",
    [
        pytest.param(None, id="inspect-unreachable"),
        pytest.param(
            _Inspect(stats={}, registered=None, ping={NODE: {"ok": "pong"}}),
            id="ping-only",
        ),
        pytest.param(
            _Inspect(stats={NODE: {"pid": 11}}, registered=None, ping=None),
            id="registered-unanswered",
        ),
    ],
)
def test_route_flags_nothing_with_a_broker_and_no_complete_inspect_reply(
    monkeypatch: pytest.MonkeyPatch, inspector: _Inspect | None
) -> None:
    assert _run_extension(monkeypatch, inspector) == []


def test_route_with_every_worker_stopped_gives_only_the_fleet_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answered = _Inspect(
        stats={NODE: {"pid": 11}}, registered={NODE: ["app.known"]}, ping=None
    )
    monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: answered)
    extension = StemtraceExtension(broker_url=BROKER, serve_ui=False)
    consumer = extension.consumer
    assert consumer is not None

    with fed(consumer, [ready("app.known"), waiting("a", "app.known")]):
        assert _items(_extension_client(extension)) == []
        # The worker reports a stop; the last complete reply is still recent.
        extension.worker_registry.mark_shutdown(NODE, 11)
        monkeypatch.setattr(
            routes, "_get_inspector", lambda _url, **_: _Inspect(None, None, None)
        )

        assert _items(_extension_client(extension)) == [("no_workers_online", None, 1)]


def test_route_without_a_broker_trusts_worker_ready_events() -> None:
    store, registry, timeline = GraphStore(), WorkerRegistry(), WorkerTimeline()
    consumer = EventConsumer(
        "memory://", store, worker_registry=registry, worker_timeline=timeline
    )
    app = FastAPI()
    app.include_router(
        create_api_router(
            store, worker_registry=registry, broker_url=None, worker_timeline=timeline
        )
    )
    stream: list[TaskEvent | WorkerEvent] = [
        ready("app.known"),
        waiting("a", "app.missing"),
        waiting("b", "app.known"),
    ]

    with fed(consumer, stream):
        client = TestClient(app)
        listed = _items(client)
        task = client.get("/api/tasks/a").json()["task"]

    assert listed == [("unregistered", "app.missing", 1)]
    assert task["anomaly"] == "unregistered"
