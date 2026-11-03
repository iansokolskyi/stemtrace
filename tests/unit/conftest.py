"""Unit-test fixtures shared by every module under ``tests/unit``."""

import pytest

from stemtrace.server.api import routes


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
        routes._InspectRefresher, "refresh_blocking", lambda _self: True
    )
