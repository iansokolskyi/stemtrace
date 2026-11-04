"""Unit-test fixtures shared by every module under ``tests/unit``."""

from collections.abc import Iterator

import pytest

from stemtrace.server.api import routes
from stemtrace.server.inspect_probe import InspectProbe

PROBE_STOP_TIMEOUT_S = 2.0


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


@pytest.fixture(autouse=True)
def _stop_probes_a_test_left_running(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Stop every probe a test created before the next test starts.

    A probe whose authority is never stamped keeps retrying on the backoff
    ladder (D7b), so one left running would call the next test's fake inspector.
    """
    made: list[InspectProbe] = []
    original = InspectProbe.__init__

    def tracking(self: InspectProbe, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]  # forwards the caller's own arguments
        made.append(self)

    monkeypatch.setattr(InspectProbe, "__init__", tracking)
    yield
    for probe in made:
        probe.stop(timeout=PROBE_STOP_TIMEOUT_S)
