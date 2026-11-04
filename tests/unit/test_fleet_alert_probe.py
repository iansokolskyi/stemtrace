"""Row P6 (D4c with D4b): the fleet dies before anyone opens the UI, hours later one request.

A real extension, store, registry, probe and refresher; the Celery ``Inspect`` is the
fake from the probe rows (``FakeFleet``: Celery 5.6.3 ``stats()`` returns None when
nobody answers, and raises when the broker is unreachable). Two clocks are injected:
the registry's (``store._utcnow``) and the API's (``params.utcnow``); the probe's own
pacing clock is the fleet's fake one, so nothing waits out the 2 s interval.
"""

import time
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
    """The registry's and the API's wall clock; the registry's can tick on every read."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = JOINED
        self.registry_step = timedelta(0)
        monkeypatch.setattr(store_module, "_utcnow", self._registry_now)
        monkeypatch.setattr(params, "utcnow", lambda: self.now)

    def _registry_now(self) -> datetime:
        at = self.now
        self.now += self.registry_step
        return at


def fleet_items(
    client: TestClient, query: str = ""
) -> list[tuple[str, str | None, str | None, int]]:
    body = client.get(f"/stemtrace/api/anomalies{query}").json()
    return [
        (a["kind"], a["task_id"], a["name"], a["task_count"]) for a in body["anomalies"]
    ]


def run_outage(
    fake: FakeFleet, monkeypatch: pytest.MonkeyPatch, *, hears: bool
) -> list:
    """Workers join at 02:00, nobody requests, the fleet dies, one request at 09:00.

    D4d: that request is the first miss; the probe's confirming attempt (a few
    seconds later on the registry's clock) is the second. Returns what a read
    sees once the confirm has run, with no second request.
    """
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
    with TestClient(app) as client:
        take(fake.attempt_done)  # the startup probe: nobody there yet
        if hears:
            fake.nodes = {NODE_A: (1, ["app.known"]), NODE_B: (2, ["app.known"])}
        else:
            fake.mode = "silent"  # remote control off: nothing ever answers
        for node, pid in ((NODE_A, 1), (NODE_B, 2)):
            registry.register_worker(node, pid, ["app.known"], event_timestamp=JOINED)
        take(fake.attempt_done)  # the probe the joins triggered
        while got(fake.attempt_done, 0.3):  # follow-ups, until the probe is idle
            pass
        # SIGKILL of the whole fleet; the first request comes hours later.
        fake.nodes = {}
        fake.mode = "silent"
        wall.now = HOURS_LATER
        fake.clock._now += 7 * 3600
        wall.registry_step = timedelta(seconds=3)
        before = fake.attempts
        assert fleet_items(client) == []  # one miss is not evidence
        take(fake.attempt_done)  # the request's own attempt
        if hears:
            take(fake.attempt_done)  # the confirm the first miss owed
        items = poll_items(client, until_found=hears)
        assert not got(fake.attempt_done, 0.5)  # nothing is owed after the confirm
        fake.outage_attempts = fake.attempts - before  # type: ignore[attr-defined]  # test-only note
        return items


def poll_items(client: TestClient, *, until_found: bool) -> list:
    """Read the anomalies without a refresh until the item shows (or the bound passes)."""
    items: list = []
    for _ in range(50):
        items = fleet_items(client, "?refresh=false")
        if items or not until_found:
            break
        time.sleep(0.1)
    return items


def test_row28p_a_killed_fleet_gives_the_item_from_one_request_plus_one_confirm(
    fleet: FakeFleet,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = run_outage(fleet, monkeypatch, hears=True)

    assert items == [("no_workers_online", None, None, 1)]
    # The request's attempt and the one confirm, then nothing: no loop.
    assert fleet.outage_attempts == 2  # type: ignore[attr-defined]  # set by run_outage
    # The confirm waits out the shared interval after the request's attempt.
    first, confirm = fleet.stats_times[-2:]
    assert confirm - first >= inspect_probe.INSPECT_REFRESH_MIN_INTERVAL_SECONDS


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
