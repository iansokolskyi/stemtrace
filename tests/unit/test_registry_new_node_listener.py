"""``WorkerRegistry`` tells a listener when it learns a nodename it didn't have (D4c)."""

import threading
from datetime import datetime, timedelta, timezone

from stemtrace.server.store import WorkerRegistry

NODE_A, NODE_B = "celery@a", "celery@b"
T = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
BOUND_S = 5.0


def counting(registry: WorkerRegistry) -> list[int]:
    calls: list[int] = []
    registry.add_new_node_listener(lambda: calls.append(1))
    return calls


def test_register_worker_for_an_unseen_nodename_notifies_once() -> None:
    registry = WorkerRegistry()
    calls = counting(registry)

    registry.register_worker(NODE_A, 1, ["app.x"])

    assert calls == [1]


def test_register_worker_for_a_known_nodename_does_not_notify() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, ["app.x"])
    calls = counting(registry)

    registry.register_worker(NODE_A, 2, ["app.x"])
    registry.register_worker(NODE_A, 2, ["app.x"])

    assert calls == []


def test_register_worker_with_an_invalid_pid_does_not_notify() -> None:
    registry = WorkerRegistry()
    calls = counting(registry)

    registry.register_worker(NODE_A, 0, ["app.x"])

    assert calls == []


def test_mark_seen_for_an_unseen_nodename_notifies() -> None:
    registry = WorkerRegistry()
    calls = counting(registry)

    registry.mark_seen(NODE_A, T, now=T)

    assert calls == [1]


def test_mark_seen_for_a_known_nodename_does_not_notify() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, [])
    calls = counting(registry)

    registry.mark_seen(NODE_A, T, now=T)

    assert calls == []


def test_mark_seen_after_the_cleanup_removed_the_nodename_notifies_again() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, [], event_timestamp=T)
    registry.mark_shutdown(NODE_A, 1)
    registry.remove_stale_workers(now=T + timedelta(hours=1))
    calls = counting(registry)

    registry.mark_seen(NODE_A, T, now=T)

    assert calls == [1]


def test_stats_reply_naming_an_unseen_nodename_notifies_once_per_new_node() -> None:
    registry = WorkerRegistry()
    registry.register_worker(NODE_A, 1, [])
    calls = counting(registry)

    registry.apply_stats_reply({}, {NODE_A: (1, ["app.x"]), NODE_B: (2, ["app.x"])})
    registry.apply_stats_reply({}, {NODE_A: (1, ["app.x"]), NODE_B: (2, ["app.x"])})

    assert calls == [1]


def test_ping_and_pidless_stats_reply_for_an_unseen_nodename_do_not_notify() -> None:
    registry = WorkerRegistry()
    calls = counting(registry)

    registry.apply_ping_reply([NODE_A])
    registry.apply_stats_reply({}, {NODE_B: (None, None)})

    assert calls == []


def test_mark_shutdown_for_an_unseen_nodename_does_not_notify() -> None:
    registry = WorkerRegistry()
    calls = counting(registry)

    registry.mark_shutdown(NODE_A, 1)

    assert calls == []


def test_a_listener_that_raises_does_not_break_registration() -> None:
    registry = WorkerRegistry()

    def boom() -> None:
        raise RuntimeError("listener failed")

    registry.add_new_node_listener(boom)

    registry.register_worker(NODE_A, 1, ["app.x"])

    assert registry.get_registered_tasks(NODE_A, 1) == ["app.x"]


def test_listener_is_called_without_the_registry_lock_held() -> None:
    registry = WorkerRegistry()
    other_thread_finished: list[bool] = []

    def read_from_another_thread() -> None:
        worker = threading.Thread(target=registry.get_all_workers)
        worker.start()
        worker.join(timeout=BOUND_S)
        other_thread_finished.append(not worker.is_alive())

    registry.add_new_node_listener(read_from_another_thread)

    registry.register_worker(NODE_A, 1, [])

    assert other_thread_finished == [True]


def test_the_same_listener_added_twice_is_called_once() -> None:
    registry = WorkerRegistry()
    calls: list[int] = []

    def listener() -> None:
        calls.append(1)

    registry.add_new_node_listener(listener)
    registry.add_new_node_listener(listener)

    registry.register_worker(NODE_A, 1, [])

    assert calls == [1]
