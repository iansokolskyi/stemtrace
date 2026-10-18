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
from contextvars import ContextVar, Token, copy_context
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
    monkeypatch.setattr(signals_module, "_reentry_last", "", raising=False)


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


class _RaiseAfterSet:
    """Stands in for ``_publishing``: raises right after the flag is set.

    As a signal handler that raises (billiard's soft time limit handler
    raises SoftTimeLimitExceeded) can, at the first bytecode after the set.
    """

    def __init__(self, var: ContextVar[bool]) -> None:
        """Wrap the real flag."""
        self._var = var
        self.raised = False

    def get(self) -> bool:
        """The real flag's value."""
        return self._var.get()

    def set(self, value: bool) -> Token[bool]:
        """Set the real flag, then raise once."""
        token = self._var.set(value)
        if value and not self.raised:
            self.raised = True
            raise RuntimeError("signal handler raised right after the set")
        return token

    def reset(self, token: Token[bool]) -> None:
        """Reset the real flag."""
        self._var.reset(token)


def test_exception_right_after_the_flag_is_set_does_not_leave_it_set(
    transport: LockedTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = _RaiseAfterSet(signals_module._publishing)
    monkeypatch.setattr(signals_module, "_publishing", flag)

    def scenario() -> None:
        _publish_event(_event("interrupted"))
        _publish_event(_event("next"))

    # Its own context: a flag left set must not leak into other tests.
    copy_context().run(scenario)

    assert flag.raised is True
    assert [e.task_id for e in transport.events if isinstance(e, TaskEvent)] == ["next"]


def test_drops_are_counted_in_the_handler_and_logged_by_a_later_publish(
    transport: LockedTransport,
    on_sigusr1: Callable[[Callable[[], None]], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing is logged in signal context (a logging handler's own plain lock,
    # e.g. QueueHandler's queue mutex, could be held by the interrupted code);
    # the next publish outside a handler logs the count, at most once per
    # interval.
    now = [100.0]
    in_handler = [False]
    logged_in_handler: list[str] = []
    monkeypatch.setattr(signals_module, "_monotonic", lambda: now[0])

    def handler() -> None:
        in_handler[0] = True
        try:
            _publish_event(_event("cancelled", TaskState.RETRY))
        finally:
            in_handler[0] = False

    class Spy(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if in_handler[0]:
                logged_in_handler.append(record.getMessage())

    on_sigusr1(handler)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    spy = Spy()
    logging.getLogger(LOGGER).addHandler(spy)

    def publish(at: float, *, interrupted: bool) -> None:
        now[0] = at
        if interrupted:
            transport.during_publish = lambda: signal.raise_signal(signal.SIGUSR1)
        _publish_event(_event(f"outer-{at}"))

    try:
        publish(100.0, interrupted=True)  # drop 1
        publish(101.0, interrupted=False)  # logs 1
        publish(110.0, interrupted=True)  # drop
        publish(120.0, interrupted=True)  # drop, within the interval
        publish(130.0, interrupted=False)  # within the interval: no log
        publish(101.0 + signals_module._REENTRY_LOG_INTERVAL_S, interrupted=False)
    finally:
        logging.getLogger(LOGGER).removeHandler(spy)

    drops = [
        r.getMessage() for r in caplog.records if "signal handler" in r.getMessage()
    ]
    assert logged_in_handler == []
    assert [d.split(" event(s)")[0] for d in drops] == ["Dropped 1", "Dropped 2"]
    assert all(d.endswith("the last was cancelled") for d in drops)
    assert {
        r.levelno for r in caplog.records if "signal handler" in r.getMessage()
    } == {logging.DEBUG}
