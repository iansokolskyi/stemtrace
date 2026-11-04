"""Row P6 (D4c with D4b): the fleet dies before anyone opens the UI, hours later one request.

A real extension, store, registry, probe and refresher; the Celery ``Inspect`` is the
fake from the probe rows (``FakeFleet``: Celery 5.6.3 ``stats()`` returns None when
nobody answers, and raises when the broker is unreachable). Two clocks are injected:
the registry's (``store._utcnow``) and the API's (``params.utcnow``); the probe's own
pacing clock is the fleet's fake one, so nothing waits out the 2 s interval.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server import inspect_probe
from stemtrace.server import store as store_module
from stemtrace.server.api import params
from tests.unit.test_inspect_probe_rows import (
    NODE_A,
    NODE_B,
    FakeFleet,
    extension_app,
    fleet,  # noqa: F401  (a fixture, used by name)
    got,
    take,
)

REAL_INSPECT_PROBE = True
JOINED = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)
HOURS_LATER = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


class Wall:
    """The registry's and the API's wall clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = JOINED
        monkeypatch.setattr(store_module, "_utcnow", lambda: self.now)
        monkeypatch.setattr(params, "utcnow", lambda: self.now)


def fleet_items(client: TestClient) -> list[tuple[str, str | None, str | None, int]]:
    body = client.get("/stemtrace/api/anomalies").json()
    return [
        (a["kind"], a["task_id"], a["name"], a["task_count"]) for a in body["anomalies"]
    ]


def run_outage(
    fake: FakeFleet, monkeypatch: pytest.MonkeyPatch, *, hears: bool
) -> list:
    """Workers join at 02:00, nobody requests, the fleet dies, one request at 09:00."""
    wall = Wall(monkeypatch)
    extension, app = extension_app()
    registry = extension.worker_registry
    extension.store.add_event(
        TaskEvent(
            task_id="t1",
            name="app.known",
            state=TaskState.PENDING,
            timestamp=JOINED + timedelta(minutes=10),
        )
    )
    with TestClient(app):
        take(fake.attempt_done)  # the startup probe: nobody there yet
        if hears:
            fake.nodes = {NODE_A: (1, ["app.known"]), NODE_B: (2, ["app.known"])}
        else:
            fake.mode = "silent"  # remote control off: nothing ever answers
        for node, pid in ((NODE_A, 1), (NODE_B, 2)):
            registry.register_worker(node, pid, ["app.known"], event_timestamp=JOINED)
        take(fake.attempt_done)  # the probe the joins triggered
        # Follow-ups until the probe is idle. With nobody answering, authority is
        # never stamped and the periodic keeps retrying (the fake clock runs freely).
        for _ in range(12):
            if not got(fake.attempt_done, 0.3):
                break
    # SIGKILL of the whole fleet; the first request comes hours later.
    fake.nodes = {}
    fake.mode = "silent"
    wall.now = HOURS_LATER
    fake.clock._now += 7 * 3600
    return fleet_items(TestClient(app))


def test_p6_a_fleet_killed_before_any_request_gives_the_item_hours_later(
    fleet: FakeFleet,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = run_outage(fleet, monkeypatch, hears=True)

    assert items == [("no_workers_online", None, None, 1)]


def test_p6_with_remote_control_off_the_same_outage_gives_no_item(
    fleet: FakeFleet,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = run_outage(fleet, monkeypatch, hears=False)

    assert items == []


def test_p6_without_the_probe_nobody_proved_the_channel_so_no_item(
    fleet: FakeFleet,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the same outage gave before D4c: the first request is the first inspect."""
    monkeypatch.setattr(inspect_probe.InspectProbe, "start", lambda _self: None)
    monkeypatch.setattr(
        inspect_probe.InspectProbe, "trigger", lambda _self, _nodename=None: None
    )
    wall = Wall(monkeypatch)
    extension, app = extension_app()
    extension.store.add_event(
        TaskEvent(
            task_id="t1",
            name="app.known",
            state=TaskState.PENDING,
            timestamp=JOINED + timedelta(minutes=10),
        )
    )
    for node, pid in ((NODE_A, 1), (NODE_B, 2)):
        extension.worker_registry.register_worker(
            node, pid, ["app.known"], event_timestamp=JOINED
        )
    fleet.mode = "silent"
    wall.now = HOURS_LATER
    fleet.clock._now += 7 * 3600

    assert fleet_items(TestClient(app)) == []
