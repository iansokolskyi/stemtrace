"""The registry authority timestamp and the registry facts the classifier reads (D4).

Celery 5.6.3 ``control.inspect`` returns None when no worker answers within
the timeout, and ``registered()`` is a separate broadcast from ``stats()``, so a
worker can answer one and miss the other.
"""

from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core.anomalies import RegistryFacts
from stemtrace.server.api import routes
from stemtrace.server.store import WorkerRegistry

NODE_A, NODE_B = "celery@a", "celery@b"
T = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def facts(registry: WorkerRegistry, *, broker: bool = True) -> RegistryFacts:
    return registry.registry_facts(broker_configured=broker)


class _Inspect:
    """A Celery ``Inspect`` stub: each call returns, or raises, what it was given."""

    def __init__(
        self,
        *,
        stats: object = None,
        registered: object = None,
        ping: object = None,
    ) -> None:
        self._replies = {"stats": stats, "registered": registered, "ping": ping}

    def _answer(self, call: str) -> object:
        reply = self._replies[call]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def stats(self) -> object:
        return self._answer("stats")

    def registered(self) -> object:
        return self._answer("registered")

    def ping(self) -> object:
        return self._answer("ping")


def refresh(registry: WorkerRegistry, inspector: _Inspect) -> None:
    routes._refresh_worker_registry_from_inspect(registry, inspector)  # type: ignore[arg-type]


# --- apply_stats_reply stamps the authority only for a complete reply ---------


def test_apply_stats_reply_with_every_task_list_sets_the_authority_time() -> None:
    registry = WorkerRegistry()

    registry.apply_stats_reply({}, {NODE_A: (1, ["app.x"]), NODE_B: (2, [])}, now=T)

    assert facts(registry).authority_at == T


def test_authority_is_unset_before_any_reply() -> None:
    assert facts(WorkerRegistry()).authority_at is None


@pytest.mark.parametrize(
    "replies",
    [
        pytest.param({}, id="empty-reply"),
        pytest.param({NODE_A: (1, None)}, id="registered-call-failed"),
        pytest.param({NODE_A: (1, ["app.x"]), NODE_B: (2, None)}, id="node-missing"),
        pytest.param({NODE_A: (None, None)}, id="no-pid"),
    ],
)
def test_apply_stats_reply_without_a_complete_task_list_keeps_the_authority_time(
    replies: dict[str, tuple[int | None, list[str] | None]],
) -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {NODE_A: (1, ["app.x"])}, now=T)

    registry.apply_stats_reply({}, replies, now=T + timedelta(minutes=5))

    assert facts(registry).authority_at == T


def test_apply_stats_reply_with_a_missing_node_keeps_its_previous_task_list() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_B, 2, ["app.kept"])

    registry.apply_stats_reply({}, {NODE_A: (1, ["app.x"]), NODE_B: (2, None)}, now=T)

    assert facts(registry).online_names == {"app.x", "app.kept"}
    assert facts(registry).authority_at is None


def test_apply_ping_reply_does_not_set_the_authority_time() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, ["app.x"])

    registry.apply_ping_reply([NODE_A])

    assert facts(registry).authority_at is None


def test_remove_stale_workers_keeps_the_authority_time() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {NODE_A: (1, ["app.x"])}, now=T)

    registry.remove_stale_workers(now=datetime.now(timezone.utc) + timedelta(hours=2))

    assert registry.get_all_workers() == []
    assert facts(registry).authority_at == T


# --- The real refresh: which inspect replies stamp the authority --------------


def _stamped(registry: WorkerRegistry) -> bool:
    return facts(registry).authority_at is not None


def test_refresh_with_stats_and_registered_replies_sets_the_authority_time() -> None:
    registry = WorkerRegistry()
    before = datetime.now(timezone.utc)

    refresh(
        registry,
        _Inspect(stats={NODE_A: {"pid": 1}}, registered={NODE_A: ["app.x"]}),
    )

    stamp = facts(registry).authority_at
    assert stamp is not None
    assert before <= stamp <= datetime.now(timezone.utc)
    assert facts(registry).online_names == {"app.x"}


@pytest.mark.parametrize(
    "inspector",
    [
        pytest.param(
            _Inspect(stats={NODE_A: {"pid": 1}}, registered=RuntimeError("down")),
            id="registered-raises",
        ),
        pytest.param(_Inspect(stats={NODE_A: {"pid": 1}}), id="registered-none"),
        pytest.param(
            _Inspect(stats={NODE_A: {"pid": 1}}, registered={}), id="registered-empty"
        ),
        pytest.param(
            _Inspect(
                stats={NODE_A: {"pid": 1}, NODE_B: {"pid": 2}},
                registered={NODE_A: ["app.x"]},
            ),
            id="registered-misses-a-stats-node",
        ),
        pytest.param(
            _Inspect(stats={NODE_A: {"no": "pid"}}, registered={NODE_A: ["app.x"]}),
            id="stats-without-pid",
        ),
        pytest.param(
            _Inspect(stats={}, registered={NODE_A: ["app.x"]}, ping={NODE_A: {}}),
            id="ping-only",
        ),
        pytest.param(
            _Inspect(stats=RuntimeError("down"), registered={NODE_A: ["x"]}),
            id="stats-raises",
        ),
    ],
)
def test_refresh_without_a_complete_reply_leaves_the_authority_time_unset(
    inspector: _Inspect,
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, ["app.old"])

    refresh(registry, inspector)

    assert not _stamped(registry)


async def test_ping_only_refresh_moves_the_refresher_clock_but_not_the_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, ["app.x"])
    inspector = _Inspect(stats={}, registered=None, ping={NODE_A: {"ok": "pong"}})
    monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: inspector)
    refresher = routes._InspectRefresher(registry, "redis://broker")

    await refresher.maybe_refresh()

    # fact 8: the refresher's clock advances on every refresh that returns,
    # which is why the authority can't reuse it.
    assert refresher._last_refresh > 0.0
    assert not _stamped(registry)


# --- registry_facts -------------------------------------------------------------


def test_registry_facts_collect_only_online_workers_names() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, ["app.a", "app.shared"])
    registry.register_worker(NODE_B, 2, ["app.b", "app.shared"])
    registry.mark_shutdown(NODE_B, 2)

    got = facts(registry)

    assert got.online_names == frozenset({"app.a", "app.shared"})
    assert got.any_online is True


def test_registry_facts_with_every_worker_offline_have_no_names_and_no_online() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, ["app.a"])
    registry.mark_shutdown(NODE_A, 1)

    got = facts(registry)

    assert (got.online_names, got.any_online) == (frozenset(), False)


def test_registry_facts_count_an_online_worker_with_no_tasks_as_online() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, [])

    got = facts(registry)

    assert (got.online_names, got.any_online) == (frozenset(), True)


def test_registry_facts_pass_the_broker_flag_through() -> None:
    registry = WorkerRegistry()

    assert facts(registry, broker=True).broker_configured is True
    assert facts(registry, broker=False).broker_configured is False


def test_registry_facts_are_a_copy_later_registrations_do_not_change() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, ["app.a"])
    got = facts(registry)

    registry.register_worker(NODE_B, 2, ["app.b"])

    assert got.online_names == frozenset({"app.a"})
