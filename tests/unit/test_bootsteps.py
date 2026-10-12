"""Tests for bootsteps module."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from celery import Celery

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.bootsteps import (
    ReceivedEventStep,
    _set_publisher,
    register_bootsteps,
)
from stemtrace.library.config import StemtraceConfig, _reset_config, set_config
from stemtrace.library.signals import connect_signals, disconnect_signals

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_publisher() -> Iterator[None]:
    """Keep each test's publisher and config from leaking into the next."""
    _reset_config()
    yield
    _set_publisher(None)
    _reset_config()


def _received_via_real_message(
    events: list[TaskEvent], *, protocol: int, **message_options: object
) -> None:
    """Feed a message built by Celery itself through the bootstep.

    Called the way Celery 5.6.3's ``Consumer.create_task_handler``
    (celery/worker/consumer/consumer.py) calls strategies: protocol 2 passes
    no body (decoding is deferred to the pool, so only headers are readable);
    protocol 1 passes the decoded body, where canvas signatures are dicts.
    """
    app = Celery("bootstep_test", broker="memory://")
    build = app.amqp.as_task_v2 if protocol == 2 else app.amqp.as_task_v1
    message = build("header-1", "app.add", args=(1, 2), **message_options)
    body = None if protocol == 2 else json.loads(json.dumps(message.body))
    consumer = SimpleNamespace(strategies={"app.add": lambda *_a, **_k: None})
    _set_publisher(events.append)
    ReceivedEventStep(consumer).start(consumer)

    consumer.strategies["app.add"](
        SimpleNamespace(headers=message.headers), body, None, None, None
    )


class TestReceivedEventStep:
    """Tests for ReceivedEventStep bootstep."""

    def test_emits_received_event_dict_format(self) -> None:
        """Test RECEIVED event is emitted for dict body format."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        # Create mock consumer with strategies
        mock_consumer = MagicMock()
        original_strategy = MagicMock(return_value="result")
        mock_consumer.strategies = {"test_task": original_strategy}

        # Create and start bootstep
        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        # Get wrapped strategy and call it
        wrapped = mock_consumer.strategies["test_task"]
        body = {"id": "task-123", "parent_id": "parent-456", "root_id": "root-789"}
        message = MagicMock()
        message.headers = {}

        result = wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

        # Check RECEIVED event was emitted
        assert len(events) == 1
        assert events[0].task_id == "task-123"
        assert events[0].state == TaskState.RECEIVED
        assert events[0].name == "test_task"
        assert events[0].parent_id == "parent-456"
        assert events[0].root_id == "root-789"

        # Check original strategy was called
        assert result == "result"

    def test_emits_received_event_tuple_format(self) -> None:
        """Test RECEIVED event is emitted for tuple body format."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        mock_consumer = MagicMock()
        original_strategy = MagicMock()
        mock_consumer.strategies = {"test_task": original_strategy}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        # Old tuple format: (args, kwargs, embed)
        body = ([], {}, {"id": "task-tuple", "parent_id": None, "root_id": None})
        message = MagicMock()
        message.headers = {}

        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

        assert len(events) == 1
        assert events[0].task_id == "task-tuple"
        assert events[0].state == TaskState.RECEIVED

    def test_fallback_to_message_headers(self) -> None:
        """Test fallback to message headers when body doesn't have task_id."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        mock_consumer = MagicMock()
        mock_consumer.strategies = {"test_task": MagicMock()}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {}  # Empty body
        message = MagicMock()
        message.headers = {"id": "task-from-header"}

        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

        assert len(events) == 1
        assert events[0].task_id == "task-from-header"

    def test_no_event_when_no_task_id(self) -> None:
        """Test no event is emitted when task_id cannot be extracted."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        mock_consumer = MagicMock()
        mock_consumer.strategies = {"test_task": MagicMock()}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {}
        message = MagicMock()
        message.headers = {}

        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

        # No event should be emitted
        assert len(events) == 0

    def test_stop_restores_original_strategies(self) -> None:
        """Test stop() restores original strategies."""
        mock_consumer = MagicMock()
        original_strategy = MagicMock()
        mock_consumer.strategies = {"test_task": original_strategy}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        # Strategy should be wrapped
        assert mock_consumer.strategies["test_task"] != original_strategy

        step.stop(mock_consumer)

        # Strategy should be restored
        assert mock_consumer.strategies["test_task"] == original_strategy

    def test_no_crash_when_publisher_not_set(self) -> None:
        """Test no crash when publisher is None."""
        _set_publisher(None)

        mock_consumer = MagicMock()
        mock_consumer.strategies = {"test_task": MagicMock()}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {"id": "task-123"}
        message = MagicMock()
        message.headers = {}

        # Should not raise
        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())

    def test_skips_received_for_retries(self) -> None:
        """Retries should not emit RECEIVED (they already have RETRY + STARTED events)."""
        events: list[TaskEvent] = []

        def mock_publish(event: TaskEvent) -> None:
            events.append(event)

        _set_publisher(mock_publish)

        mock_consumer = MagicMock()
        mock_consumer.strategies = {"test_task": MagicMock()}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {"id": "task-123", "retries": 1}
        message = MagicMock()
        message.headers = {}

        wrapped(message, body, MagicMock(), MagicMock(), MagicMock())
        assert events == []

    def test_wrap_strategy_suppresses_emit_errors(self) -> None:
        """Errors during RECEIVED emission should be suppressed, and strategy still runs."""

        def broken_publish(event: TaskEvent) -> None:
            raise RuntimeError("publish failed")

        _set_publisher(broken_publish)

        mock_consumer = MagicMock()
        original_strategy = MagicMock(return_value="ok")
        mock_consumer.strategies = {"test_task": original_strategy}

        step = ReceivedEventStep(mock_consumer)
        step.start(mock_consumer)

        wrapped = mock_consumer.strategies["test_task"]
        body = {"id": "task-123"}
        message = MagicMock()
        message.headers = {}

        result = wrapped(message, body, MagicMock(), MagicMock(), MagicMock())
        assert result == "ok"
        original_strategy.assert_called_once()


class TestReceivedFromCeleryMessages:
    """RECEIVED built from messages exactly as Celery 5.6 delivers them."""

    def test_protocol2_chord_header_emits_received_from_headers(self) -> None:
        """Ids come from headers; chord ids are left to STARTED.

        The chord callback is only in the protocol 2 body, which the consumer
        doesn't decode.
        """
        app = Celery("bootstep_test", broker="memory://")
        callback = app.signature("app.tsum")
        callback.freeze(group_id="group-1")
        events: list[TaskEvent] = []

        _received_via_real_message(
            events,
            protocol=2,
            chord=callback,
            group_id="group-1",
            root_id="root-1",
            parent_id="parent-1",
        )

        assert [
            (e.task_id, e.name, e.state, e.group_id, e.root_id, e.parent_id)
            for e in events
        ] == [
            (
                "header-1",
                "app.add",
                TaskState.RECEIVED,
                "group-1",
                "root-1",
                "parent-1",
            )
        ]
        assert (events[0].chord_id, events[0].chord_callback_id) == (None, None)

    def test_protocol1_chord_header_emits_received_with_chord_ids(self) -> None:
        """The decoded body's chord signature yields the ids STARTED reports."""
        app = Celery("bootstep_test", broker="memory://")
        callback = app.signature("app.tsum")
        callback.freeze(group_id="group-1")
        events: list[TaskEvent] = []

        _received_via_real_message(
            events, protocol=1, chord=callback, group_id="group-1"
        )

        assert [
            (e.task_id, e.state, e.group_id, e.chord_id, e.chord_callback_id)
            for e in events
        ] == [
            (
                "header-1",
                TaskState.RECEIVED,
                "group-1",
                "group-1",
                callback.options["task_id"],
            )
        ]

    def test_retry_redelivery_message_emits_nothing(self) -> None:
        """A retry's message (headers retries=1) is skipped; RETRY covers it."""
        events: list[TaskEvent] = []

        _received_via_real_message(events, protocol=2, retries=1)

        assert events == []


class TestRegisterBootsteps:
    """Tests for register_bootsteps function."""

    def test_registers_bootstep(self) -> None:
        """Test bootstep is registered with the app."""
        mock_app = MagicMock()
        mock_app.steps = {"consumer": set()}

        register_bootsteps(mock_app, publish=lambda _event: None)

        assert ReceivedEventStep in mock_app.steps["consumer"]

    def test_without_publish_and_not_initialized_registers_nothing_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """v0.3.10 behavior: register_bootsteps(app) before init_worker is a no-op."""
        app = MagicMock()
        app.steps = {"consumer": set()}

        with caplog.at_level(logging.DEBUG):
            register_bootsteps(app)

        assert app.steps["consumer"] == set()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_without_publish_and_publish_received_off_registers_nothing(self) -> None:
        """An initialized worker that didn't opt in gets no RECEIVED step."""
        app = MagicMock()
        app.steps = {"consumer": set()}
        set_config(StemtraceConfig(transport_url="memory://"))

        register_bootsteps(app)

        assert app.steps["consumer"] == set()

    def test_without_publish_and_publish_received_on_uses_the_transport(self) -> None:
        """publish_received=True in the config publishes through the signals transport."""

        class _RecordingTransport:
            def __init__(self) -> None:
                self.events: list[TaskEvent] = []

            def publish(self, event: TaskEvent) -> None:
                self.events.append(event)

        transport = _RecordingTransport()
        app = Celery("bootstep_default", broker="memory://")
        set_config(StemtraceConfig(transport_url="memory://", publish_received=True))
        connect_signals(transport)  # type: ignore[arg-type]  # structural stub
        try:
            register_bootsteps(app)
            consumer = SimpleNamespace(strategies={"app.add": lambda *_a, **_k: None})
            step_cls = next(c for c in app.steps["consumer"] if c is ReceivedEventStep)
            step_cls(consumer).start(consumer)

            consumer.strategies["app.add"](
                SimpleNamespace(headers={"id": "task-1", "retries": 0}),
                None,
                None,
                None,
                None,
            )
        finally:
            disconnect_signals()

        assert [(e.task_id, e.name, e.state) for e in transport.events] == [
            ("task-1", "app.add", TaskState.RECEIVED)
        ]
