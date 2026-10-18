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
    _green_spawn,
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
    registered: list[Callable[[], None]] = []
    monkeypatch.setattr(signals_module.atexit, "register", registered.append)
    yield
    disconnect_signals()
    MemoryTransport.clear()


def _module(name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class TestGreenSpawn:
    """Green sockets are detected from modules the worker already imported."""

    def test_neither_imported_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("gevent", "gevent.monkey", "eventlet", "eventlet.patcher"):
            monkeypatch.delitem(sys.modules, name, raising=False)

        assert _green_spawn() is None

    @pytest.mark.parametrize("patched", [True, False])
    def test_gevent(self, monkeypatch: pytest.MonkeyPatch, patched: bool) -> None:
        spawn = FakeSpawn()
        monkeypatch.setitem(
            sys.modules,
            "gevent.monkey",
            _module("gevent.monkey", is_module_patched=lambda name: patched),
        )
        monkeypatch.setitem(sys.modules, "gevent", _module("gevent", spawn=spawn))
        monkeypatch.delitem(sys.modules, "eventlet.patcher", raising=False)

        assert _green_spawn() is (spawn if patched else None)

    @pytest.mark.parametrize("patched", [True, False])
    def test_eventlet(self, monkeypatch: pytest.MonkeyPatch, patched: bool) -> None:
        spawn = FakeSpawn()
        monkeypatch.delitem(sys.modules, "gevent.monkey", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "eventlet.patcher",
            _module("eventlet.patcher", is_monkey_patched=lambda name: patched),
        )
        monkeypatch.setitem(sys.modules, "eventlet", _module("eventlet", spawn=spawn))

        assert _green_spawn() is (spawn if patched else None)


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
        monkeypatch.setattr(signals_module, "_green_spawn", lambda: spawn)

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
        monkeypatch.setattr(signals_module, "_green_spawn", lambda: spawn)

        _retry(Retry("Retry in 5s"))

        assert [e.task_id for e in MemoryTransport.events] == ["task-1"]
        assert spawn.spawned == []

    def test_cancel_retry_without_green_sockets_publishes_inline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signals_module, "_green_spawn", lambda: None)

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

        _defer_publish(spawn, event)
        spawn.spawned[0].dead = True
        _defer_publish(spawn, event)

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

        monkeypatch.setattr(time, "monotonic", lambda: now[0])
        monkeypatch.setattr(time, "sleep", sleep)
        stuck = FakeGreenlet(print)
        monkeypatch.setattr(signals_module, "_deferred_publishes", [stuck])
        caplog.set_level(logging.DEBUG, logger=LOGGER)

        _flush_deferred_publishes(timeout_s=2)

        assert 2 <= now[0] < 2.1
        assert signals_module._deferred_publishes == []
        assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
            "Gave up on 1 deferred event(s) at exit after 2s"
        ]

    def test_returns_once_all_finished(self, monkeypatch: pytest.MonkeyPatch) -> None:
        done = FakeGreenlet(print)
        running = FakeGreenlet(print)
        monkeypatch.setattr(signals_module, "_deferred_publishes", [done, running])
        done.dead = True

        def sleep(seconds: float) -> None:
            running.dead = True  # finishes while the flush waits

        monkeypatch.setattr(time, "sleep", sleep)

        _flush_deferred_publishes(timeout_s=2)

        assert signals_module._deferred_publishes == []
