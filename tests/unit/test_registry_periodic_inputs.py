"""What the periodic refresh reads from the worker registry (D7b readings 1, 4, 6)."""

from datetime import datetime, timedelta, timezone

from stemtrace.server.store import WorkerRegistry

T = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
NODE, OTHER = "celery@a", "celery@b"


def test_any_online_is_false_for_an_empty_registry() -> None:
    assert WorkerRegistry().any_online(now=T) is False


def test_any_online_is_true_for_a_registered_worker_and_false_after_its_stop() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE, 1, ["app.a"], event_timestamp=T)

    assert registry.any_online(now=T) is True

    registry.mark_shutdown(NODE, 1)
    assert registry.any_online(now=T) is False


def test_any_online_counts_a_node_known_only_from_task_events() -> None:
    registry = WorkerRegistry()
    registry.mark_seen(NODE, T, now=T)

    assert registry.any_online(now=T) is True
    assert registry.registry_facts(broker_configured=True, now=T).any_online is True


def test_any_online_is_false_for_a_node_silent_with_evidence() -> None:
    registry = WorkerRegistry()
    registry.apply_stats_reply({}, {NODE: (1, ["app.a"])}, now=T)
    later = T + timedelta(minutes=10)
    registry.apply_stats_reply({}, {OTHER: (2, ["app.a"])}, now=later)

    # NODE answered before, then a completed broadcast named only OTHER.
    assert registry.any_online(now=later) is True
    registry.mark_shutdown(OTHER, 2)
    assert registry.any_online(now=later) is False


def test_any_online_matches_the_fact_the_anomaly_gate_reads() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE, 1, ["app.a"], event_timestamp=T)
    registry.mark_seen(OTHER, T, now=T)
    for now in (T, T + timedelta(minutes=2), T + timedelta(minutes=40)):
        facts = registry.registry_facts(broker_configured=True, now=now)
        assert registry.any_online(now=now) is facts.any_online


def test_authority_stamps_count_only_replies_that_listed_every_node() -> None:
    registry = WorkerRegistry()

    registry.apply_stats_reply({}, {NODE: (1, None)}, now=T)
    assert registry.authority_stamps == 0
    assert registry.authority_age_s(now=T) is None

    registry.apply_stats_reply({}, {NODE: (1, ["app.a"])}, now=T)
    assert registry.authority_stamps == 1
    assert registry.authority_age_s(now=T + timedelta(seconds=90)) == 90.0


def test_a_liveness_listener_hears_a_worker_ready_for_a_known_nodename() -> None:
    registry = WorkerRegistry()
    heard: list[str] = []
    registry.register_worker(NODE, 1, ["app.a"], event_timestamp=T)
    registry.add_liveness_listener(heard.append)

    registry.register_worker(NODE, 2, ["app.a"], event_timestamp=T)

    assert heard == [NODE]


def test_a_liveness_listener_hears_a_node_turning_live_again_but_not_a_steady_one() -> (
    None
):
    registry = WorkerRegistry()
    heard: list[str] = []
    registry.add_liveness_listener(heard.append)
    registry.mark_seen(NODE, T, now=T)
    assert heard == [NODE]  # first sight of an unlisted node

    registry.mark_seen(NODE, T + timedelta(seconds=5), now=T + timedelta(seconds=5))
    assert heard == [NODE]  # already live: nothing changed

    registry.register_worker(OTHER, 1, ["app.a"], event_timestamp=T)
    registry.mark_shutdown(OTHER, 1)
    registry.remove_stale_workers(now=T + timedelta(minutes=10))
    heard.clear()
    registry.mark_seen(OTHER, T + timedelta(minutes=11), now=T + timedelta(minutes=11))
    assert heard == [OTHER]  # a task event after its stop: it runs again
