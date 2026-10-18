"""A publish re-entered from a signal handler is dropped instead of deadlocking.

Celery 5.6.3's cold shutdown (SIGQUIT) cancels running tasks from inside its
signal handler (celery/apps/worker.py ``on_cold_shutdown`` →
``Consumer.cancel_active_requests`` → ``Request._announce_cancelled``), which
sends ``task_retry`` and so publishes a RETRY event on the main thread. If the
main thread was itself in the middle of a publish, holding a lock on the
publish path (the breaker's plain ``threading.Lock``), the nested publish
waited for that lock forever. ``LockedTransport`` models that lock.
"""

import logging
import signal
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import stemtrace.library.signals as signals_module
from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent
from stemtrace.library.signals import (
    _publish_event,
    connect_signals,
    disconnect_signals,
    on_worker_ready,
    on_worker_shutdown,
)

LOGGER = "stemtrace.library.signals"


def _event(task_id: str, state: TaskState = TaskState.STARTED) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="app.encode",
        state=state,
        timestamp=datetime(2026, 10, 18, tzinfo=timezone.utc),
    )


class LockedTransport:
    """Publishes under a plain lock, as the publish breaker's slow paths do.

    ``during_publish`` runs while the lock is held (e.g. raising a signal).
    A publish that finds the lock taken by its own thread records the
    re-entry and raises instead of waiting forever.
    """

    def __init__(self) -> None:
        """Start with no events and nothing to run mid-publish."""
        self.events: list[TaskEvent | WorkerEvent] = []
        self.lock = threading.Lock()
        self.holder: int | None = None
        self.reentered = False
        self.during_publish: Callable[[], None] | None = None
        self.fail_next = False

    def publish(self, event: TaskEvent | WorkerEvent) -> None:
        """Record the event while holding the lock."""
        if self.holder == threading.get_ident():
            self.reentered = True
            raise RuntimeError("publish re-entered while its lock is held")
        with self.lock:
            self.holder = threading.get_ident()
            try:
                if self.fail_next:
                    self.fail_next = False
                    raise ConnectionError("broker down")
                hook, self.during_publish = self.during_publish, None
                if hook is not None:
                    hook()
                self.events.append(event)
            finally:
                self.holder = None


@pytest.fixture
def transport() -> Iterator[LockedTransport]:
    """Connect the handlers to a LockedTransport."""
    transport = LockedTransport()
    connect_signals(transport)
    yield transport
    disconnect_signals()


@pytest.fixture
def on_sigusr1() -> Iterator[Callable[[Callable[[], None]], None]]:
    """Install a SIGUSR1 handler for the test; restore the old one after."""
    previous = signal.getsignal(signal.SIGUSR1)

    def install(handler: Callable[[], None]) -> None:
        signal.signal(signal.SIGUSR1, lambda *_: handler())

    yield install
    signal.signal(signal.SIGUSR1, previous)


@pytest.fixture(autouse=True)
def _reset_drop_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no drops counted and no drop logged."""
    monkeypatch.setattr(signals_module, "_reentry_dropped", 0, raising=False)
    monkeypatch.setattr(signals_module, "_reentry_logged_at", None, raising=False)


def test_publish_from_signal_handler_mid_publish_is_dropped(
    transport: LockedTransport,
    on_sigusr1: Callable[[Callable[[], None]], None],
) -> None:
    on_sigusr1(lambda: _publish_event(_event("cancelled", TaskState.RETRY)))
    transport.during_publish = lambda: signal.raise_signal(signal.SIGUSR1)

    _publish_event(_event("outer"))

    assert transport.reentered is False
    assert [e.task_id for e in transport.events if isinstance(e, TaskEvent)] == [
        "outer"
    ]


def test_publish_from_signal_handler_between_publishes_goes_through(
    transport: LockedTransport,
    on_sigusr1: Callable[[Callable[[], None]], None],
) -> None:
    on_sigusr1(lambda: _publish_event(_event("cancelled", TaskState.RETRY)))

    _publish_event(_event("outer"))
    signal.raise_signal(signal.SIGUSR1)

    assert [e.task_id for e in transport.events if isinstance(e, TaskEvent)] == [
        "outer",
        "cancelled",
    ]


def test_worker_event_from_signal_handler_mid_publish_is_dropped(
    transport: LockedTransport,
    on_sigusr1: Callable[[Callable[[], None]], None],
) -> None:
    on_sigusr1(lambda: on_worker_shutdown(sender=SimpleNamespace(hostname="celery@w1")))
    transport.during_publish = lambda: signal.raise_signal(signal.SIGUSR1)

    _publish_event(_event("outer"))

    assert transport.reentered is False
    assert [type(e) for e in transport.events] == [TaskEvent]


def test_worker_ready_from_signal_handler_mid_publish_is_dropped(
    transport: LockedTransport,
    on_sigusr1: Callable[[Callable[[], None]], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = SimpleNamespace(hostname="celery@w1", app=SimpleNamespace(tasks={}))
    on_sigusr1(lambda: on_worker_ready(sender=worker))
    transport.during_publish = lambda: signal.raise_signal(signal.SIGUSR1)
    caplog.set_level(logging.INFO, logger=LOGGER)

    _publish_event(_event("outer"))

    assert transport.reentered is False
    assert [type(e) for e in transport.events] == [TaskEvent]
    assert not [
        r for r in caplog.records if "Worker ready event sent" in r.getMessage()
    ]


def test_other_threads_publish_while_one_is_publishing(
    transport: LockedTransport,
) -> None:
    # The flag is per thread (per greenlet under gevent/eventlet): another
    # thread's publish waits for the lock and goes through.
    inside, release = threading.Event(), threading.Event()

    def hold() -> None:
        inside.set()
        assert release.wait(5)

    transport.during_publish = hold
    first = threading.Thread(target=_publish_event, args=(_event("first"),))
    first.start()
    assert inside.wait(5)
    second = threading.Thread(target=_publish_event, args=(_event("second"),))
    second.start()
    release.set()
    first.join(5)
    second.join(5)

    assert sorted(e.task_id for e in transport.events if isinstance(e, TaskEvent)) == [
        "first",
        "second",
    ]


def test_failed_publish_does_not_block_later_ones(transport: LockedTransport) -> None:
    transport.fail_next = True

    _publish_event(_event("lost"))
    _publish_event(_event("next"))

    assert [e.task_id for e in transport.events if isinstance(e, TaskEvent)] == ["next"]


def test_drops_are_logged_at_most_once_per_interval(
    transport: LockedTransport,
    on_sigusr1: Callable[[Callable[[], None]], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [100.0]
    monkeypatch.setattr(signals_module, "_monotonic", lambda: now[0])
    on_sigusr1(lambda: _publish_event(_event("cancelled", TaskState.RETRY)))
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    def publish_interrupted(at: float) -> None:
        now[0] = at
        transport.during_publish = lambda: signal.raise_signal(signal.SIGUSR1)
        _publish_event(_event(f"outer-{at}"))

    for at in (100.0, 110.0, 120.0):  # within one interval
        publish_interrupted(at)
    publish_interrupted(100.0 + signals_module._REENTRY_LOG_INTERVAL_S)

    drops = [
        r.getMessage() for r in caplog.records if "signal handler" in r.getMessage()
    ]
    assert len(drops) == 2
    assert drops[0].startswith("Dropped 1 event(s)")
    assert drops[1].startswith("Dropped 3 event(s)")
    assert {
        r.levelno for r in caplog.records if "signal handler" in r.getMessage()
    } == {logging.DEBUG}
