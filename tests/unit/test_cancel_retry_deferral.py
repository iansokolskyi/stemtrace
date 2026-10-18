"""In-process tests of the cancel-path RETRY deferral (see test_cancel_retry_green.py).

The subprocess tests prove the behaviour under real gevent/eventlet; these pin
the pieces with fakes: green-socket detection, which retries are deferred, and
the exit flush's bound.
"""

import logging
import sys
import time
from collections.abc import Callable, Iterator
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from celery.app.task import Context
from celery.exceptions import Retry

import stemtrace.library.signals as signals_module
from stemtrace.core.events import TaskEvent
from stemtrace.library.config import StemtraceConfig, set_config
from stemtrace.library.signals import (
    _defer_publish,
    _flush_deferred_publishes,
    _Green,
    _green,
    _on_task_retry,
    connect_signals,
    disconnect_signals,
)
from stemtrace.transports.memory import MemoryTransport

LOGGER = "stemtrace.library.signals"


class FakeGreenlet:
    """A spawned greenlet that never runs: ``dead`` until told otherwise."""

    def __init__(self, fn: Callable[..., Any], *args: Any) -> None:
        """Remember what it would run."""
        self.fn, self.args = fn, args
        self.dead = False


class FakeSpawn:
    """Stands in for gevent.spawn / eventlet.spawn: schedules, never runs."""

    def __init__(self) -> None:
        """Nothing spawned yet."""
        self.spawned: list[FakeGreenlet] = []

    def __call__(self, fn: Callable[..., Any], *args: Any) -> FakeGreenlet:
        """Spawn a greenlet that doesn't run."""
        greenlet = FakeGreenlet(fn, *args)
        self.spawned.append(greenlet)
        return greenlet


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh transport and deferral state; no real atexit registration."""
    MemoryTransport.clear()
    set_config(StemtraceConfig(transport_url="memory://"))
    connect_signals(MemoryTransport())
    monkeypatch.setattr(signals_module, "_deferred_publishes", [])
    monkeypatch.setattr(signals_module, "_flush_at_exit_registered", False)
    monkeypatch.setattr(signals_module, "_deferred_sleep", _real_sleep_forbidden)
    # The flush must never wait with time.sleep (see TestFlush): signals sees a
    # time module whose sleep fails fast instead of hanging. Only signals' view
    # is replaced, so other code's threads keep the real time.sleep.
    _set_clock(monkeypatch, time.monotonic)
    registered: list[Callable[[], None]] = []
    monkeypatch.setattr(signals_module.atexit, "register", registered.append)
    yield
    disconnect_signals()
    MemoryTransport.clear()


def _set_clock(monkeypatch: pytest.MonkeyPatch, monotonic: Callable[[], float]) -> None:
    fake_time = SimpleNamespace(monotonic=monotonic, sleep=_real_sleep_forbidden)
    monkeypatch.setattr(signals_module, "time", fake_time)


def _real_sleep_forbidden(seconds: float) -> None:
    raise AssertionError("the flush must wait with the green library's sleep")


def _green_library_sleep(seconds: float) -> None:
    """Stands in for gevent.sleep / eventlet.sleep."""


def _green_with(spawn: "FakeSpawn") -> _Green:
    return _Green(spawn, _real_sleep_forbidden)


def _module(name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class TestGreen:
    """Green sockets are detected from modules the worker already imported."""

    def test_neither_imported_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("gevent", "gevent.monkey", "eventlet", "eventlet.patcher"):
            monkeypatch.delitem(sys.modules, name, raising=False)

        assert _green() is None

    @pytest.mark.parametrize("patched", [True, False])
    def test_gevent(self, monkeypatch: pytest.MonkeyPatch, patched: bool) -> None:
        spawn, sleep = FakeSpawn(), _green_library_sleep
        monkeypatch.setitem(
            sys.modules,
            "gevent.monkey",
            _module("gevent.monkey", is_module_patched=lambda name: patched),
        )
        monkeypatch.setitem(
            sys.modules, "gevent", _module("gevent", spawn=spawn, sleep=sleep)
        )
        monkeypatch.delitem(sys.modules, "eventlet.patcher", raising=False)

        green = _green()
        assert green == (_Green(spawn, sleep) if patched else None)
        # The library's own sleep, never time.sleep (green or not).
        assert green is None or green.sleep is _green_library_sleep

    @pytest.mark.parametrize("patched", [True, False])
    def test_eventlet(self, monkeypatch: pytest.MonkeyPatch, patched: bool) -> None:
        spawn, sleep = FakeSpawn(), _green_library_sleep
        monkeypatch.delitem(sys.modules, "gevent.monkey", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "eventlet.patcher",
            _module("eventlet.patcher", is_monkey_patched=lambda name: patched),
        )
        monkeypatch.setitem(
            sys.modules, "eventlet", _module("eventlet", spawn=spawn, sleep=sleep)
        )

        green = _green()
        assert green == (_Green(spawn, sleep) if patched else None)
        # The library's own sleep, never time.sleep (green or not).
        assert green is None or green.sleep is _green_library_sleep


def _retry(reason: Any) -> None:
    request = Context(id="task-1", task="app.busy", retries=0, delivery_info={})
    task = SimpleNamespace(name="app.busy", request=request)
    _on_task_retry(sender=task, request=request, reason=reason, einfo=None)


class TestWhichRetriesAreDeferred:
    """Only Celery's cancel path (no reason, no einfo) on green sockets."""

    def test_cancel_retry_on_green_sockets_is_spawned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn = FakeSpawn()
        monkeypatch.setattr(signals_module, "_green", lambda: _green_with(spawn))

        _retry(None)

        assert MemoryTransport.events == []
        [greenlet] = spawn.spawned
        assert greenlet.fn is signals_module._publish_event
        assert isinstance(greenlet.args[0], TaskEvent)
        assert greenlet.args[0].task_id == "task-1"

    def test_own_retry_on_green_sockets_publishes_inline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn = FakeSpawn()
        monkeypatch.setattr(signals_module, "_green", lambda: _green_with(spawn))

        _retry(Retry("Retry in 5s"))

        assert [e.task_id for e in MemoryTransport.events] == ["task-1"]
        assert spawn.spawned == []

    def test_cancel_retry_without_green_sockets_publishes_inline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signals_module, "_green", lambda: None)

        _retry(None)

        assert [e.task_id for e in MemoryTransport.events] == ["task-1"]


class TestDeferPublish:
    """Spawned publishes are tracked; the exit flush is registered once."""

    def test_finished_greenlets_are_forgotten_and_flush_registered_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered: list[Callable[[], None]] = []
        monkeypatch.setattr(signals_module.atexit, "register", registered.append)
        spawn = FakeSpawn()
        event = TaskEvent.model_validate(
            {
                "task_id": "t",
                "name": "n",
                "state": "RETRY",
                "timestamp": "2026-10-18T00:00:00Z",
            }
        )

        _defer_publish(_green_with(spawn), event)
        spawn.spawned[0].dead = True
        _defer_publish(_green_with(spawn), event)

        assert signals_module._deferred_publishes == [spawn.spawned[1]]
        assert registered == [_flush_deferred_publishes]


class TestFlush:
    """The exit flush waits for deferred publishes, but never past its bound."""

    def test_stops_at_the_bound_and_logs_what_it_dropped(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            # Bound the loop: a flush without a deadline fails, not hangs.
            assert len(sleeps) < 10_000, "flush never gave up"
            now[0] += seconds

        _set_clock(monkeypatch, lambda: now[0])
        monkeypatch.setattr(signals_module, "_deferred_sleep", sleep)
        stuck = FakeGreenlet(print)
        monkeypatch.setattr(signals_module, "_deferred_publishes", [stuck])
        caplog.set_level(logging.DEBUG, logger=LOGGER)

        _flush_deferred_publishes(timeout_s=2)

        assert 2 <= now[0] < 2.1
        assert signals_module._deferred_publishes == []
        assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
            "Gave up on 1 deferred event(s) at exit after 2s"
        ]

    def test_returns_as_soon_as_all_finished(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = [0.0]
        sleeps: list[float] = []
        done = FakeGreenlet(print)
        running = FakeGreenlet(print)
        done.dead = True

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds
            running.dead = True  # finishes during the first wait

        _set_clock(monkeypatch, lambda: now[0])
        monkeypatch.setattr(signals_module, "_deferred_sleep", sleep)
        monkeypatch.setattr(signals_module, "_deferred_publishes", [done, running])
        caplog.set_level(logging.DEBUG, logger=LOGGER)

        _flush_deferred_publishes(timeout_s=2)

        assert len(sleeps) == 1
        assert now[0] < 0.1
        assert signals_module._deferred_publishes == []
        assert [r for r in caplog.records if r.name == LOGGER] == []

    def test_waits_with_the_green_librarys_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sockets patched but not time (patch_socket() only): time.sleep
        # would block the hub, so the deferred publish would never run.
        green_sleeps: list[float] = []
        running = FakeGreenlet(print)

        def green_sleep(seconds: float) -> None:
            green_sleeps.append(seconds)
            running.dead = True

        def spawn(fn: Callable[..., Any], *args: Any) -> FakeGreenlet:
            return running

        _defer_publish(
            _Green(spawn, green_sleep),
            TaskEvent.model_validate(
                {
                    "task_id": "t",
                    "name": "n",
                    "state": "RETRY",
                    "timestamp": "2026-10-18T00:00:00Z",
                }
            ),
        )

        _flush_deferred_publishes(timeout_s=2)

        assert green_sleeps == [0.01]
