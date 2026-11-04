"""Unit-test fixtures shared by every module under ``tests/unit``."""

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from stemtrace.server import inspect_probe
from stemtrace.server.api import routes
from stemtrace.server.inspect_probe import InspectProbe

PROBE_THREAD_NAME = inspect_probe._THREAD_NAME
# Bound on each cleanup stop() of a leaked probe.
LEAK_GRACE_S = 2.0


@pytest.fixture(autouse=True)
def _no_real_inspect_from_the_probe(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the inspect probe from running a real Celery inspect in other tests.

    Any test that starts an extension or registers a worker on a router with a
    broker URL would start one, and against an unreachable broker one attempt
    takes seconds. A module sets ``REAL_INSPECT_PROBE = True`` to opt out.
    """
    if getattr(request.module, "REAL_INSPECT_PROBE", False):
        return
    monkeypatch.setattr(
        routes._InspectRefresher, "refresh_blocking", lambda _self: frozenset()
    )


def _live_probe_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == PROBE_THREAD_NAME]


def _only(before: set[threading.Thread]) -> list[threading.Thread]:
    return [t for t in _live_probe_threads() if t not in before]


@pytest.fixture(autouse=True)
def inspect_probes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[InspectProbe]]:
    """Record every probe a test creates, and fail the test if its thread outlives it.

    A probe thread resolves ``routes._get_inspector``, ``routes._monotonic`` and
    ``WorkerRegistry.apply_ping_reply`` when it runs, so one that outlives its
    test consumes the next test's fakes. A test that starts one stops it, or
    requests ``stop_probes``. A leaked probe is stopped here, then the test fails.
    """
    probes: list[InspectProbe] = []
    original_init = InspectProbe.__init__

    def recording_init(self: InspectProbe, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        probes.append(self)

    monkeypatch.setattr(InspectProbe, "__init__", recording_init)
    before = set(_live_probe_threads())
    yield probes
    # A stopped probe's thread ends once its refresh returns; allow it a moment.
    deadline = time.monotonic() + LEAK_GRACE_S
    while not _only(before) and time.monotonic() < deadline:
        time.sleep(0.01)
    leaked = _only(before)
    for probe in probes:
        probe.stop(LEAK_GRACE_S)
    assert not leaked, f"inspect probe thread(s) outlived the test: {leaked}"


@pytest.fixture
def stop_probes(inspect_probes: list[InspectProbe]) -> Iterator[None]:
    """Stop every probe the test created when it ends, before the leak check."""
    yield
    for probe in inspect_probes:
        probe.stop(LEAK_GRACE_S)
