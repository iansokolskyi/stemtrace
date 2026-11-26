"""Pool type and slot count from ``inspect.stats()`` (B4a).

The fixtures in ``tests/data/worker_stats/`` are real ``stats()`` replies, each
the reply of one real worker started with the pool named by its file, captured
over a Docker Redis broker: ``celery-5.6.3`` (the version the tests run on) and
``celery-5.2.2`` (``.github/lowest-requirements.txt``). Celery 5.6.3
``concurrency/base.py`` ``BasePool._get_info`` adds ``implementation`` to the
pool dict; 5.2.2 doesn't send it.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.server.api import routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore, WorkerRegistry
from stemtrace.server.worker_pool import NO_POOL_INFO, PoolInfo, pool_from_stats_payload

DATA = Path(__file__).parent.parent / "data" / "worker_stats"
NODE = "celery@a"
T = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def section(payload: dict[str, object], key: str) -> dict[str, object]:
    """The ``pool`` or ``autoscaler`` dict of a payload, narrowed for the type checker."""
    value = payload[key]
    assert isinstance(value, dict)
    return value


def capture(version: str, pool: str) -> dict[str, object]:
    reply: dict[str, object] = json.loads(
        (DATA / f"celery-{version}" / f"{pool}.json").read_text()
    )
    return reply


# autoscale was captured as `--autoscale=10,3` on prefork.
CAPTURED_5_6_3 = [
    ("prefork", PoolInfo("prefork", 4)),
    ("solo", PoolInfo("solo", 1)),
    ("threads", PoolInfo("threads", 3)),
    ("gevent", PoolInfo("gevent", 5)),
    ("eventlet", PoolInfo("eventlet", 6)),
    ("autoscale", PoolInfo("prefork", 10)),
]
# 5.2.2 sends no `implementation`, so the pool name is unknown there.
CAPTURED_5_2_2 = [
    ("prefork", PoolInfo(None, 4)),
    ("solo", PoolInfo(None, 1)),
    ("threads", PoolInfo(None, 3)),
    ("gevent", PoolInfo(None, 5)),
    ("eventlet", PoolInfo(None, 6)),
    ("autoscale", PoolInfo(None, 10)),
]


@pytest.mark.parametrize(("pool", "expected"), CAPTURED_5_6_3)
def test_pool_from_stats_on_celery_5_6_3_capture_returns_pool_and_slots(
    pool: str, expected: PoolInfo
) -> None:
    assert pool_from_stats_payload(capture("5.6.3", pool)) == expected


@pytest.mark.parametrize(("pool", "expected"), CAPTURED_5_2_2)
def test_pool_from_stats_on_celery_5_2_2_capture_returns_slots_without_pool_name(
    pool: str, expected: PoolInfo
) -> None:
    assert pool_from_stats_payload(capture("5.2.2", pool)) == expected


def test_autoscale_capture_reports_the_minimum_as_max_concurrency() -> None:
    # The reason the ceiling comes from autoscaler.max: the capture's pool dict says 3.
    reply = capture("5.6.3", "autoscale")
    assert section(reply, "pool")["max-concurrency"] == 3
    assert section(reply, "autoscaler")["max"] == 10


def pool_payload(**pool: object) -> dict[str, object]:
    return {"pid": 1, "pool": pool}


@pytest.mark.parametrize(
    "payload",
    [
        {"pid": 1, "pool": "N/A"},
        {"pid": 1},
        {"pid": 1, "pool": None},
        {"pid": 1, "pool": ["prefork"]},
        {"autoscaler": {"max": 10}},
        None,
        "ok",
        [],
        42,
    ],
)
def test_pool_from_stats_with_no_pool_dict_returns_nulls(payload: object) -> None:
    assert pool_from_stats_payload(payload) == NO_POOL_INFO
    assert PoolInfo(None, None) == NO_POOL_INFO


@pytest.mark.parametrize("bad", ["8", 0, -1, 2.5, True, None, [8], {"n": 8}])
def test_pool_from_stats_with_unusable_max_concurrency_returns_null_slots(
    bad: object,
) -> None:
    payload = pool_payload(implementation="celery.concurrency.prefork:TaskPool")
    section(payload, "pool")["max-concurrency"] = bad
    assert pool_from_stats_payload(payload) == PoolInfo("prefork", None)


def test_pool_from_stats_without_max_concurrency_key_returns_null_slots() -> None:
    payload = pool_payload(implementation="celery.concurrency.solo:TaskPool")
    assert pool_from_stats_payload(payload) == PoolInfo("solo", None)


@pytest.mark.parametrize(
    ("implementation", "name"),
    [
        ("celery.concurrency.prefork:TaskPool", "prefork"),
        ("celery.concurrency.thread:TaskPool", "threads"),
        ("myapp.pools.fancy:Pool", "fancy"),
        ("myapp.pools:Pool", "pools"),
        ("custompool", "custompool"),
        ("", None),
        (":TaskPool", None),
        ("a.b.:Pool", None),
        (None, None),
        (7, None),
        (["x"], None),
    ],
)
def test_pool_from_stats_names_the_last_module_segment_of_implementation(
    implementation: object, name: str | None
) -> None:
    payload = pool_payload(**{"implementation": implementation, "max-concurrency": 2})
    assert pool_from_stats_payload(payload) == PoolInfo(name, 2)


@pytest.mark.parametrize("ceiling", ["10", 0, -3, None, True, 1.5])
def test_pool_from_stats_with_unusable_autoscaler_max_returns_null_slots(
    ceiling: object,
) -> None:
    # max-concurrency is the autoscaler's minimum, so it must not stand in for the ceiling.
    payload = capture("5.6.3", "autoscale")
    section(payload, "autoscaler")["max"] = ceiling
    assert pool_from_stats_payload(payload) == PoolInfo("prefork", None)


@pytest.mark.parametrize("autoscaler", [{}, {"min": 3}, "N/A", None, [10]])
def test_pool_from_stats_with_autoscaler_lacking_max_uses_max_concurrency(
    autoscaler: object,
) -> None:
    payload = capture("5.6.3", "autoscale")
    payload["autoscaler"] = autoscaler
    assert pool_from_stats_payload(payload) == PoolInfo("prefork", 3)


# --- the registry keeps them per pid --------------------------------------------------


def apply(
    registry: WorkerRegistry,
    pid: int,
    pool: PoolInfo | None,
    *,
    node: str = NODE,
    at: datetime = T,
) -> None:
    registry.apply_stats_reply(
        registry.event_generations(),
        {node: (pid, ["app.t"])},
        pools=None if pool is None else {node: pool},
        now=at,
    )


def row(registry: WorkerRegistry, pid: int) -> tuple[str | None, int | None]:
    (w,) = [w for w in registry.get_all_workers() if w.pid == pid]
    return w.pool, w.concurrency


def test_stats_reply_stores_pool_and_slots_for_the_pid() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    assert row(registry, 10) == ("prefork", 8)


def test_pool_and_slots_stay_for_later_reads_without_a_refresh() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    registry.mark_node_alive(NODE, 10, now=T + timedelta(seconds=5))
    assert row(registry, 10) == ("prefork", 8)
    assert registry.get_workers_by_hostname(NODE)[0].concurrency == 8


def test_worker_known_only_from_an_event_has_null_pool_and_slots() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE, 10, ["app.t"], event_timestamp=T)
    assert row(registry, 10) == (None, None)


def test_new_pid_on_the_same_nodename_gets_its_own_values() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    apply(registry, 11, PoolInfo("threads", 2), at=T + timedelta(seconds=30))
    assert row(registry, 11) == ("threads", 2)
    assert row(registry, 10) == ("prefork", 8)


def test_new_pid_named_without_pool_info_does_not_inherit_the_old_values() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    apply(registry, 11, NO_POOL_INFO, at=T + timedelta(seconds=30))
    assert row(registry, 11) == (None, None)


def test_reply_for_the_same_pid_with_a_different_pool_replaces_the_old_values() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    apply(registry, 10, PoolInfo("solo", 1), at=T + timedelta(seconds=30))
    assert row(registry, 10) == ("solo", 1)


def test_reply_for_the_same_pid_without_pool_info_clears_the_old_values() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    apply(registry, 10, NO_POOL_INFO, at=T + timedelta(seconds=30))
    assert row(registry, 10) == (None, None)


def test_reply_naming_a_nodename_missing_from_pools_clears_its_values() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    registry.apply_stats_reply(
        registry.event_generations(),
        {NODE: (10, None)},
        pools={},
        now=T + timedelta(seconds=30),
    )
    assert row(registry, 10) == (None, None)


def test_reply_without_a_pools_argument_keeps_the_stored_values() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    apply(registry, 10, None, at=T + timedelta(seconds=30))
    assert row(registry, 10) == ("prefork", 8)


def test_reply_without_a_pid_leaves_the_stored_values() -> None:
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    registry.apply_stats_reply(
        registry.event_generations(),
        {NODE: (None, None)},
        pools={NODE: PoolInfo("solo", 1)},
        now=T + timedelta(seconds=30),
    )
    assert row(registry, 10) == ("prefork", 8)


def test_reply_skipped_as_stale_does_not_apply_its_pool_info() -> None:
    registry = WorkerRegistry()
    seen = registry.event_generations()
    registry.register_worker(NODE, 10, ["app.t"], event_timestamp=T)  # newer than seen
    registry.apply_stats_reply(
        seen, {NODE: (10, ["app.t"])}, pools={NODE: PoolInfo("prefork", 8)}, now=T
    )
    assert row(registry, 10) == (None, None)


def test_each_nodename_gets_its_own_pool_info() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply(
        registry.event_generations(),
        {"celery@a": (1, ["app.t"]), "celery@b": (2, ["app.t"])},
        pools={
            "celery@a": PoolInfo("prefork", 4),
            "celery@b": PoolInfo("solo", 1),
        },
        now=T,
    )
    assert row(registry, 1) == ("prefork", 4)
    assert row(registry, 2) == ("solo", 1)


# --- the API -------------------------------------------------------------------------


class _Inspect:
    def __init__(self, stats: dict[str, object] | None) -> None:
        self._stats = stats

    def stats(self, timeout: float | None = None) -> dict[str, object] | None:
        del timeout
        return self._stats

    def registered(self, timeout: float | None = None) -> dict[str, object] | None:
        del timeout
        return {n: ["app.t"] for n in self._stats or {}}

    def ping(self, timeout: float | None = None) -> dict[str, object] | None:
        del timeout
        return {n: {"ok": "pong"} for n in self._stats or {}}


def workers_json(
    monkeypatch: pytest.MonkeyPatch, stats: dict[str, object]
) -> dict[str, dict[str, object]]:
    monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: _Inspect(stats))
    app = FastAPI()
    registry = WorkerRegistry()
    app.include_router(
        create_api_router(
            GraphStore(),
            worker_registry=registry,
            broker_url="redis://localhost:6379/0",
        )
    )
    client = TestClient(app)
    body = client.get("/api/workers").json()
    # A later read that doesn't ask Celery again returns the same values.
    again = client.get("/api/workers?refresh=false").json()
    assert again == body
    return {w["hostname"]: w for w in body["workers"]}


def test_workers_api_returns_pool_and_concurrency_from_captured_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats = {
        "w_prefork@capture": capture("5.6.3", "prefork"),
        "w_autoscale@capture": capture("5.6.3", "autoscale"),
        "w_threads@capture": capture("5.6.3", "threads"),
    }
    by_host = workers_json(monkeypatch, stats)
    assert {h: (w["pool"], w["concurrency"]) for h, w in by_host.items()} == {
        "w_prefork@capture": ("prefork", 4),
        "w_autoscale@capture": ("prefork", 10),
        "w_threads@capture": ("threads", 3),
    }


def test_workers_api_returns_nulls_when_the_reply_has_no_pool_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats = {"celery@odd": {"pid": 5, "pool": "N/A"}, "celery@bare": {"pid": 6}}
    by_host = workers_json(monkeypatch, stats)
    assert {h: (w["pool"], w["concurrency"]) for h, w in by_host.items()} == {
        "celery@odd": (None, None),
        "celery@bare": (None, None),
    }


def test_workers_api_with_a_pool_dict_lacking_every_field_returns_nulls_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_host = workers_json(monkeypatch, {"celery@a": {"pid": 5, "pool": {"x": 1}}})
    assert (by_host["celery@a"]["pool"], by_host["celery@a"]["concurrency"]) == (
        None,
        None,
    )


def test_worker_ready_for_a_pid_with_stored_values_keeps_them_until_the_next_reply() -> (
    None
):
    # A documented limitation: a process that reuses the pid keeps the old values
    # until an inspect reply names it.
    registry = WorkerRegistry()
    apply(registry, 10, PoolInfo("prefork", 8))
    registry.register_worker(
        NODE, 10, ["app.t"], event_timestamp=T + timedelta(minutes=1)
    )
    assert row(registry, 10) == ("prefork", 8)
