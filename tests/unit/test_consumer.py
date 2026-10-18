"""Tests for EventConsumer and AsyncEventConsumer."""

import logging
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from stemtrace.server import consumer as consumer_module
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.api.schemas import WorkerStatus
from stemtrace.server.consumer import AsyncEventConsumer, EventConsumer
from stemtrace.server.store import GraphStore, WorkerRegistry
from stemtrace.transports.redis import RedisTransport


class FakeTransport:
    """Fake transport for testing."""

    def __init__(
        self,
        events: list[TaskEvent | WorkerEvent] | None = None,
        *,
        delay_per_event: float = 0.0,
    ) -> None:
        self._events: list[TaskEvent | WorkerEvent] = events or []
        # Models a consumer thread that is slow to get scheduled (a loaded
        # host): each event reaches the consumer this many seconds later.
        self._delay_per_event = delay_per_event
        self._publish_count = 0
        self._consume_started = False
        self.consumed = 0  # events the consumer has taken and come back for
        self._stop = False

    def publish(self, event: TaskEvent | WorkerEvent) -> None:
        self._events.append(event)
        self._publish_count += 1

    def consume(self) -> Iterator[TaskEvent | WorkerEvent]:
        self._consume_started = True
        for event in self._events:
            if self._stop:
                break
            time.sleep(self._delay_per_event)
            yield event
            self.consumed += 1
        # Block until stopped
        while not self._stop:
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop = True


@pytest.fixture
def store() -> GraphStore:
    """Create a fresh GraphStore for each test."""
    return GraphStore()


@pytest.fixture
def sample_events() -> list[TaskEvent]:
    """Create sample events for testing."""
    base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        TaskEvent(
            task_id=f"task-{i}",
            name="tests.sample",
            state=TaskState.STARTED,
            timestamp=base_time,
        )
        for i in range(5)
    ]


class TestEventConsumer:
    def test_initial_state(self, store: GraphStore) -> None:
        consumer = EventConsumer("memory://", store)
        assert not consumer.is_running

    def test_start_stops_gracefully(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()

            assert consumer.is_running
            _wait_until(lambda: fake._consume_started)

            # Stop should work
            fake.stop()
            consumer.stop(timeout=1.0)
            assert not consumer.is_running

    def test_start_idempotent(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()
            consumer.start()  # Second start should be no-op

            assert consumer.is_running

            fake.stop()
            consumer.stop(timeout=1.0)

    def test_stop_when_not_running(self, store: GraphStore) -> None:
        consumer = EventConsumer("memory://", store)
        # Should not raise
        consumer.stop()

    def test_consumes_events_into_store(
        self, store: GraphStore, sample_events: list[TaskEvent]
    ) -> None:
        fake = FakeTransport(sample_events.copy(), delay_per_event=0.05)

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()

            # Stopping first would drop the events still queued in the thread.
            _wait_until(lambda: store.node_count == 5)

            fake.stop()
            consumer.stop(timeout=1.0)

            assert store.node_count == 5

    def test_config_passed_to_transport(self, store: GraphStore) -> None:
        mock_get_transport = MagicMock(return_value=FakeTransport())

        with patch("stemtrace.server.consumer.get_transport", mock_get_transport):
            consumer = EventConsumer(
                "redis://localhost:6379",
                store,
                prefix="custom_prefix",
                ttl=3600,
            )
            consumer.start()
            _wait_until(lambda: mock_get_transport.return_value._consume_started)

            mock_get_transport.assert_called_once_with(
                "redis://localhost:6379",
                prefix="custom_prefix",
                ttl=3600,
            )

            mock_get_transport.return_value.stop()
            consumer.stop(timeout=1.0)


class TestAsyncEventConsumer:
    def test_initial_state(self, store: GraphStore) -> None:
        consumer = AsyncEventConsumer("memory://", store)
        assert not consumer.is_running

    @pytest.mark.asyncio
    async def test_async_context_manager(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            async with AsyncEventConsumer("memory://", store) as consumer:
                assert consumer.is_running
                fake.stop()

            assert not consumer.is_running

    def test_manual_start_stop(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = AsyncEventConsumer("memory://", store)
            consumer.start()
            assert consumer.is_running

            fake.stop()
            consumer.stop(timeout=1.0)
            assert not consumer.is_running


class TestWorkerEventHandling:
    """Tests for worker event consumption and routing."""

    def test_worker_ready_registers_worker(self, store: GraphStore) -> None:
        """Worker ready event should register worker in registry."""
        worker_registry = WorkerRegistry()
        # Use future time to ensure event is after consumer's _started_at
        future_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        worker_event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1",
            pid=12345,
            timestamp=future_time,
            registered_tasks=["tasks.add", "tasks.multiply"],
        )
        fake = FakeTransport([worker_event])

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer(
                "memory://", store, worker_registry=worker_registry
            )
            consumer.start()
            _wait_until(lambda: len(worker_registry.get_all_workers()) == 1)

            fake.stop()
            consumer.stop(timeout=1.0)

            # Worker should be registered
            workers = worker_registry.get_all_workers()
            assert len(workers) == 1
            assert workers[0].hostname == "worker-1"
            assert workers[0].pid == 12345
            assert workers[0].registered_tasks == ["tasks.add", "tasks.multiply"]
            assert workers[0].status == WorkerStatus.ONLINE

    @pytest.mark.parametrize(
        ("hostname", "refreshed"), [("celery@w1", True), (None, False)]
    )
    def test_task_event_refreshes_its_worker_last_seen(
        self, store: GraphStore, hostname: str | None, refreshed: bool
    ) -> None:
        """A busy worker's task events keep it online; an older worker's
        events (no hostname) change nothing."""
        worker_registry = WorkerRegistry()
        started = datetime.now(timezone.utc) - timedelta(minutes=10)
        worker_registry.register_worker(
            "celery@w1", 12345, ["tasks.add"], event_timestamp=started
        )
        recent = datetime.now(timezone.utc)
        fake = FakeTransport(
            [
                TaskEvent(
                    task_id="t1",
                    name="tasks.add",
                    state=TaskState.STARTED,
                    timestamp=recent,
                    hostname=hostname,
                )
            ]
        )

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer(
                "memory://", store, worker_registry=worker_registry
            )
            consumer.start()
            _wait_until(lambda: store.get_node("t1") is not None)
            fake.stop()
            consumer.stop(timeout=1.0)

        worker = worker_registry.get_worker("celery@w1", 12345)
        assert worker is not None
        assert worker.last_seen == (recent if refreshed else started)

    def test_worker_shutdown_marks_offline(self, store: GraphStore) -> None:
        """Worker shutdown event should mark worker offline."""
        worker_registry = WorkerRegistry()
        # First register the worker
        worker_registry.register_worker("worker-1", 12345, ["tasks.add"])

        # Use future time to ensure event is after consumer's _started_at
        future_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        shutdown_event = WorkerEvent(
            event_type=WorkerEventType.WORKER_SHUTDOWN,
            hostname="worker-1",
            pid=12345,
            timestamp=future_time,
            shutdown_time=future_time,
        )
        fake = FakeTransport([shutdown_event])

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer(
                "memory://", store, worker_registry=worker_registry
            )
            consumer.start()
            _wait_until(
                lambda: (
                    (w := worker_registry.get_worker("worker-1", 12345)) is not None
                    and w.status == WorkerStatus.OFFLINE
                )
            )

            fake.stop()
            consumer.stop(timeout=1.0)

            # Worker should be marked offline
            worker = worker_registry.get_worker("worker-1", 12345)
            assert worker is not None
            assert worker.status == WorkerStatus.OFFLINE

    def test_mixed_events_routed_correctly(self, store: GraphStore) -> None:
        """Task and worker events should be routed to correct handlers."""
        worker_registry = WorkerRegistry()
        # Use future time for worker event so it's after consumer's _started_at
        # Task events can use any time (they're not filtered)
        future_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        past_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

        events: list[TaskEvent | WorkerEvent] = [
            TaskEvent(
                task_id="task-1",
                name="tests.sample",
                state=TaskState.STARTED,
                timestamp=past_time,
            ),
            WorkerEvent(
                event_type=WorkerEventType.WORKER_READY,
                hostname="worker-1",
                pid=12345,
                timestamp=future_time,  # Future time for worker event
                registered_tasks=["tasks.add"],
            ),
            TaskEvent(
                task_id="task-2",
                name="tests.sample",
                state=TaskState.SUCCESS,
                timestamp=past_time,
            ),
        ]
        fake = FakeTransport(events)

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer(
                "memory://", store, worker_registry=worker_registry
            )
            consumer.start()
            _wait_until(
                lambda: (
                    store.node_count == 2
                    and len(worker_registry.get_all_workers()) == 1
                )
            )

            fake.stop()
            consumer.stop(timeout=1.0)

            # Both task events should be in store
            assert store.node_count == 2

            # Worker should be registered
            workers = worker_registry.get_all_workers()
            assert len(workers) == 1

    def test_worker_events_skipped_without_registry(self, store: GraphStore) -> None:
        """Worker events should be skipped if no registry provided."""
        future_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        worker_event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="worker-1",
            pid=12345,
            timestamp=future_time,
            registered_tasks=["tasks.add"],
        )
        fake = FakeTransport([worker_event])

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            # No worker_registry passed
            consumer = EventConsumer("memory://", store)
            consumer.start()
            # Nothing observable is stored for a skipped event: wait until the
            # consume loop has finished the transport's one event.
            _wait_until(lambda: fake.consumed == 1)

            fake.stop()
            consumer.stop(timeout=1.0)

    def test_historical_worker_events_are_processed(self, store: GraphStore) -> None:
        """Historical worker events should be processed to rebuild registry state.

        When server restarts but workers are still running, the historical
        WORKER_READY events in Redis are still valid and should be processed.
        This enables the registry to show "Registered Only" and "Never Executed"
        tasks even after server restart.

        If a worker has shut down, there will be a WORKER_SHUTDOWN event that
        marks it offline. Processing events in order gives correct state.
        """
        worker_registry = WorkerRegistry()
        # Create an event from 1 minute ago (before consumer starts but recent)
        one_min_ago = datetime.now(timezone.utc) - timedelta(minutes=1)
        old_event = WorkerEvent(
            event_type=WorkerEventType.WORKER_READY,
            hostname="still-running-worker",
            pid=12345,
            timestamp=one_min_ago,
            registered_tasks=["tasks.add", "tasks.multiply"],
        )
        fake = FakeTransport([old_event])

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            # Consumer is created NOW, but should still process historical events
            consumer = EventConsumer(
                "memory://", store, worker_registry=worker_registry
            )
            consumer.start()
            _wait_until(lambda: len(worker_registry.get_all_workers()) == 1)

            fake.stop()
            consumer.stop(timeout=1.0)

        # Historical WORKER_READY should register the worker
        workers = worker_registry.get_all_workers()
        assert len(workers) == 1
        assert workers[0].hostname == "still-running-worker"
        assert workers[0].pid == 12345
        assert workers[0].registered_tasks == ["tasks.add", "tasks.multiply"]

    def test_historical_shutdown_marks_worker_offline(self, store: GraphStore) -> None:
        """Historical WORKER_SHUTDOWN after WORKER_READY marks worker offline.

        When processing events from Redis, a worker that started and then
        shut down (before server restart) should end up marked offline.
        Stream ordering ensures correct final state.
        """
        worker_registry = WorkerRegistry()
        # Worker started at T1, shut down at T2 (both before consumer but recent)
        t1 = datetime.now(timezone.utc) - timedelta(minutes=2)
        t2 = datetime.now(timezone.utc) - timedelta(minutes=1)
        events: list[WorkerEvent] = [
            WorkerEvent(
                event_type=WorkerEventType.WORKER_READY,
                hostname="dead-worker",
                pid=99999,
                timestamp=t1,
                registered_tasks=["tasks.old"],
            ),
            WorkerEvent(
                event_type=WorkerEventType.WORKER_SHUTDOWN,
                hostname="dead-worker",
                pid=99999,
                timestamp=t2,
            ),
        ]
        fake = FakeTransport(events)

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer(
                "memory://", store, worker_registry=worker_registry
            )
            consumer.start()
            _wait_until(
                lambda: (
                    (w := worker_registry.get_worker("dead-worker", 99999)) is not None
                    and w.status == WorkerStatus.OFFLINE
                )
            )

            fake.stop()
            consumer.stop(timeout=1.0)

        # Worker should be registered but marked offline
        workers = worker_registry.get_all_workers()
        assert len(workers) == 1
        assert workers[0].hostname == "dead-worker"
        assert workers[0].status == WorkerStatus.OFFLINE

    def test_stale_worker_check_runs_periodically(self, store: GraphStore) -> None:
        """Consumer should periodically check for and mark stale workers.

        Workers that haven't been seen recently (no heartbeat/events)
        should be marked offline by the stale check mechanism.
        """
        worker_registry = WorkerRegistry()
        # Pre-register a worker last seen 20 minutes ago to trigger stale detection
        worker_registry.register_worker(
            "stale-worker",
            11111,
            ["tasks.stale"],
            event_timestamp=datetime.now(timezone.utc) - timedelta(minutes=20),
        )

        # Create a task event to trigger processing (stale check happens after events)
        task_event = TaskEvent(
            task_id="trigger-1",
            name="tests.trigger",
            state=TaskState.STARTED,
            timestamp=datetime.now(timezone.utc),
        )
        fake = FakeTransport([task_event])

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            # Use stale_check_interval=0 to trigger check immediately
            consumer = EventConsumer(
                "memory://",
                store,
                worker_registry=worker_registry,
                stale_check_interval=0,
            )
            consumer.start()
            _wait_until(
                lambda: (
                    store.node_count == 1
                    and (w := worker_registry.get_worker("stale-worker", 11111))
                    is not None
                    and w.status == WorkerStatus.OFFLINE
                )
            )

            fake.stop()
            consumer.stop(timeout=1.0)

        # Task event should be processed
        assert store.node_count == 1

        # Stale worker should now be marked offline
        worker = worker_registry.get_worker("stale-worker", 11111)
        assert worker is not None
        assert worker.status == WorkerStatus.OFFLINE


class FlakyTransport(FakeTransport):
    """Transport whose consume() raises for the first ``failures`` calls."""

    def __init__(
        self,
        events: list[TaskEvent | WorkerEvent],
        *,
        failures: int,
        connected: bool | None = None,
    ) -> None:
        super().__init__(events)
        self.failures_left = failures
        self.consume_calls = 0
        self.connected = connected

    def consume(self) -> Iterator[TaskEvent | WorkerEvent]:
        self.consume_calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise ConnectionError("Connection closed by server.")
        yield from super().consume()


def _wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


class TestConsumerRecovery:
    def test_consume_loop_after_errors_restarts_and_processes_events(
        self,
        store: GraphStore,
        sample_events: list[TaskEvent],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(consumer_module, "RESTART_INITIAL_BACKOFF", 0.01)
        fake = FlakyTransport(list(sample_events), failures=3)

        with (
            patch("stemtrace.server.consumer.get_transport", return_value=fake),
            caplog.at_level(logging.INFO, logger="stemtrace.server.consumer"),
        ):
            consumer = EventConsumer("memory://", store)
            consumer.start()
            _wait_until(lambda: store.node_count == len(sample_events))

            assert consumer.is_running
            fake.stop()
            consumer.stop(timeout=1.0)

        assert fake.consume_calls == 4
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 3
        assert warnings[0].exc_info is not None
        assert all(w.exc_info is None for w in warnings[1:])
        assert "still failing (attempt 3)" in warnings[-1].message
        assert "Consumer recovered after 3 restart(s)" in caplog.text

    def test_consume_loop_restart_backoff_doubles_up_to_cap(
        self, store: GraphStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []
        consumer = EventConsumer("memory://", store)
        fake = FlakyTransport([], failures=8)

        def fake_wait(timeout: float | None = None) -> bool:
            delays.append(timeout or 0.0)
            if len(delays) == 8:
                fake.stop()
            return False

        monkeypatch.setattr(consumer._stop_event, "wait", fake_wait)
        consumer._transport = fake
        consumer._consume_loop()

        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]

    def test_consume_loop_when_stopped_before_start_exits(
        self, store: GraphStore
    ) -> None:
        consumer = EventConsumer("memory://", store)
        fake = FlakyTransport([], failures=1)
        consumer._transport = fake
        consumer._stop_event.set()

        consumer._consume_loop()

        assert fake.consume_calls == 0

    def test_consume_loop_when_stopped_during_error_exits_without_warning(
        self, store: GraphStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        consumer = EventConsumer("memory://", store)

        class StopThenFail(FlakyTransport):
            def consume(self) -> Iterator[TaskEvent | WorkerEvent]:
                consumer._stop_event.set()
                return super().consume()

        fake = StopThenFail([], failures=1)
        consumer._transport = fake

        with caplog.at_level(logging.WARNING, logger="stemtrace.server.consumer"):
            consumer._consume_loop()

        assert fake.consume_calls == 1
        assert caplog.records == []

    def test_is_connected_reports_transport_connectivity(
        self, store: GraphStore
    ) -> None:
        fake = FlakyTransport([], failures=0, connected=False)

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = AsyncEventConsumer("memory://", store)
            assert consumer.is_connected is None
            consumer.start()
            _wait_until(lambda: fake._consume_started)

            assert consumer.is_connected is False
            fake.connected = True
            assert consumer.is_connected is True

            fake.stop()
            consumer.stop(timeout=1.0)

    def test_is_connected_without_connectivity_support_is_none(
        self, store: GraphStore
    ) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()
            _wait_until(lambda: fake._consume_started)

            assert consumer.is_connected is None

            fake.stop()
            consumer.stop(timeout=1.0)


class SwitchableTransport(FakeTransport):
    """Connection-aware transport whose reads fail while ``fail`` is True."""

    def __init__(self) -> None:
        super().__init__([])
        self.fail = True
        self.connected: bool | None = None

    def consume(self) -> Iterator[TaskEvent | WorkerEvent]:
        self._consume_started = True
        self.connected = None
        if self.fail:
            self.connected = False
            raise RuntimeError(
                "WRONGTYPE Operation against a key holding the wrong kind"
            )
        self.connected = True
        yield from super().consume()


class FiniteTransport:
    """Transport whose iterator ends after its events, like memory://."""

    def __init__(self, events: list[TaskEvent | WorkerEvent]) -> None:
        self.events = events

    def consume(self) -> Iterator[TaskEvent | WorkerEvent]:
        yield from self.events


class AlwaysDownRedis:
    """Redis client stand-in whose reads always fail to connect."""

    def xread(self, streams: dict[str, str], block: int, count: int) -> Any:
        del streams, block, count
        raise RedisConnectionError("Error 111 connecting to localhost:6379.")


def _health(consumer: AsyncEventConsumer, store: GraphStore) -> dict[str, Any]:
    app = FastAPI()
    app.include_router(create_api_router(store, consumer=consumer))
    body: dict[str, Any] = TestClient(app).get("/api/health").json()
    return body


class TestConsumerHealthState:
    def test_is_connected_while_restarting_on_errors_is_false_until_read(
        self,
        store: GraphStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(consumer_module, "RESTART_INITIAL_BACKOFF", 0.01)
        monkeypatch.setattr(consumer_module, "RESTART_MAX_BACKOFF", 0.01)
        fake = SwitchableTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = AsyncEventConsumer("redis://localhost", store)
            consumer.start()
            _wait_until(lambda: consumer._consumer._failures >= 2)

            assert consumer.is_running is True
            assert consumer.is_connected is False
            assert _health(consumer, store)["status"] == "degraded"

            fake.fail = False
            _wait_until(lambda: fake.connected is True)

            assert consumer.is_connected is True
            assert _health(consumer, store)["status"] == "ok"

            fake.stop()
            consumer.stop(timeout=1.0)

    def test_is_connected_while_failing_without_connectivity_support_is_false(
        self, store: GraphStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(consumer_module, "RESTART_INITIAL_BACKOFF", 0.01)
        fake = FlakyTransport([], failures=1_000_000)

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()
            _wait_until(lambda: consumer._failures >= 1)

            assert consumer.is_connected is False

            consumer.stop(timeout=1.0)

    def test_consumer_whose_source_finished_is_not_degraded(
        self, store: GraphStore, sample_events: list[TaskEvent]
    ) -> None:
        fake = FiniteTransport(list(sample_events))

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = AsyncEventConsumer("memory://", store)
            consumer.start()
            _wait_until(lambda: not consumer.is_running)

            assert consumer.is_finished is True
            assert store.node_count == len(sample_events)
            health = _health(consumer, store)
            assert health["status"] == "ok"
            assert health["consumer_running"] is False

            consumer.stop(timeout=1.0)

    def test_stop_during_transport_reconnect_backoff_is_prompt(
        self, store: GraphStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = RedisTransport(
            client=AlwaysDownRedis(),  # type: ignore[arg-type]  # structural fake
            prefix="test",
            ttl=60,
        )

        with (
            patch("stemtrace.server.consumer.get_transport", return_value=transport),
            caplog.at_level(logging.WARNING, logger="stemtrace.server.consumer"),
        ):
            consumer = EventConsumer("redis://localhost", store)
            consumer.start()
            _wait_until(lambda: transport.connected is False)
            started = time.monotonic()

            consumer.stop(timeout=2.0)

        assert time.monotonic() - started < 1.0
        assert "did not stop gracefully" not in caplog.text
        assert consumer.is_running is False


class TestGracefulStopHealth:
    def test_health_after_graceful_stop_is_not_degraded(
        self, store: GraphStore
    ) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = AsyncEventConsumer("redis://localhost", store)
            consumer.start()
            _wait_until(lambda: fake._consume_started)
            fake.stop()
            consumer.stop(timeout=1.0)

        assert consumer.is_running is False
        assert consumer.is_stopped is True
        health = _health(consumer, store)
        assert health["status"] == "ok"
        assert health["consumer_running"] is False
        assert health["consumer_connected"] is None

    def test_start_after_stop_clears_stopped_flag(self, store: GraphStore) -> None:
        fake = FakeTransport()

        with patch("stemtrace.server.consumer.get_transport", return_value=fake):
            consumer = EventConsumer("memory://", store)
            consumer.start()
            consumer.stop(timeout=0.1)
            assert consumer.is_stopped is True

            consumer.start()
            assert consumer.is_stopped is False

            fake.stop()
            consumer.stop(timeout=1.0)


class FailingStore(GraphStore):
    """GraphStore whose add_event raises for the given task ids."""

    def __init__(self, failing_ids: set[str]) -> None:
        super().__init__()
        self.failing_ids = failing_ids

    def add_event(self, event: TaskEvent) -> None:
        if event.task_id in self.failing_ids:
            raise RuntimeError(f"cannot store {event.task_id}")
        super().add_event(event)


class FailingStaleCheckRegistry(WorkerRegistry):
    """WorkerRegistry whose stale-worker sweep always raises."""

    def remove_stale_workers(
        self, stale_timeout_minutes: int = 3, cleanup_timeout_minutes: int = 30
    ) -> None:
        raise RuntimeError("sweep failed")


class TestConsumeLoopEventHandling:
    def test_consume_loop_without_transport_returns_quietly(
        self, store: GraphStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        consumer = EventConsumer("memory://", store)

        with caplog.at_level(logging.DEBUG, logger="stemtrace.server.consumer"):
            consumer._consume_loop()

        assert caplog.records == []
        assert consumer.is_finished is False

    def test_consume_loop_when_stopped_mid_stream_skips_remaining_events(
        self, store: GraphStore, sample_events: list[TaskEvent]
    ) -> None:
        consumer = EventConsumer("memory://", store)
        first, second = sample_events[0], sample_events[1]

        class StopAfterFirst:
            def consume(self) -> Iterator[TaskEvent | WorkerEvent]:
                yield first
                consumer._stop_event.set()
                yield second

        consumer._transport = StopAfterFirst()
        consumer._consume_loop()

        assert store.get_node(first.task_id) is not None
        assert store.get_node(second.task_id) is None
        assert consumer.is_finished is False

    def test_consume_loop_when_processing_fails_logs_and_continues(
        self, sample_events: list[TaskEvent], caplog: pytest.LogCaptureFixture
    ) -> None:
        store = FailingStore({sample_events[0].task_id})
        consumer = EventConsumer("memory://", store)
        consumer._transport = FiniteTransport(list(sample_events[:2]))

        with caplog.at_level(logging.WARNING, logger="stemtrace.server.consumer"):
            consumer._consume_loop()

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert [r.getMessage() for r in errors] == ["Error processing event"]
        assert errors[0].exc_info is not None
        assert str(errors[0].exc_info[1]) == f"cannot store {sample_events[0].task_id}"
        assert store.node_count == 1
        assert consumer._failures == 0
        assert consumer.is_finished is True

    def test_consume_loop_when_stale_check_fails_logs_and_continues(
        self,
        store: GraphStore,
        sample_events: list[TaskEvent],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        consumer = EventConsumer(
            "memory://",
            store,
            worker_registry=FailingStaleCheckRegistry(),
            stale_check_interval=0,
        )
        consumer._transport = FiniteTransport(list(sample_events[:2]))

        with caplog.at_level(logging.WARNING, logger="stemtrace.server.consumer"):
            consumer._consume_loop()

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert errors == ["Error processing event"] * 2
        assert store.node_count == 2
        assert consumer.is_finished is True
