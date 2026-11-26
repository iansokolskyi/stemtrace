"""``RunRecord.queue_wait_ms`` and the runtime and wait samples (overview p95, matrix R1-R10).

The record's wait comes from ``TaskNode.queue_wait_ms`` (the rule in
``attribution.queue_wait_ms``) and is set only once the task is terminal, the
gate ``runtime_ms`` uses. ``test_store_runs`` checks it against the node on
every recorded flow.
"""

from datetime import datetime, timedelta, timezone

import pytest

from stemtrace.core import attribution
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.stats import RunRecord, queue_wait_sample, runtime_sample
from stemtrace.server.store import GraphStore

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _event(
    state: TaskState,
    seconds: float,
    *,
    retries: int = 0,
    name: str = "app.work",
    task_id: str = "t",
) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name=name,
        state=state,
        retries=retries,
        timestamp=T0 + timedelta(seconds=seconds),
    )


def _record(*events: TaskEvent) -> RunRecord:
    store = GraphStore()
    for event in events:
        store.add_event(event)
    (record,) = store.run_records()
    return record


P, R, S, OK = (
    TaskState.PENDING,
    TaskState.RECEIVED,
    TaskState.STARTED,
    TaskState.SUCCESS,
)


def test_r1_wait_is_started_minus_pending_and_runtime_is_started_to_success() -> None:
    record = _record(_event(P, 0), _event(S, 2), _event(OK, 5))

    assert record.queue_wait_ms == 2000
    assert record.runtime_ms == 3000
    assert queue_wait_sample(record) == 2000
    assert runtime_sample(record) == 3000


def test_r2_running_task_has_no_wait_yet() -> None:
    record = _record(_event(P, 0), _event(S, 2))

    assert record.queue_wait_ms is None
    assert queue_wait_sample(record) is None
    assert runtime_sample(record) is None


def test_r3_without_a_sent_event_there_is_no_wait_but_a_runtime() -> None:
    record = _record(_event(S, 0), _event(OK, 3))

    assert record.queue_wait_ms is None
    assert record.runtime_ms == 3000


def test_r4_received_counts_as_the_sent_event() -> None:
    record = _record(_event(R, 0), _event(S, 1), _event(OK, 3))

    assert record.queue_wait_ms == 1000
    assert record.runtime_ms == 2000


def test_r6_retry_gives_the_first_attempts_wait_and_the_last_runs_runtime() -> None:
    record = _record(
        _event(P, 0),
        _event(S, 1),
        _event(TaskState.RETRY, 3),
        _event(S, 10, retries=1),
        _event(OK, 12, retries=1),
    )

    assert record.queue_wait_ms == 1000
    assert record.runtime_ms == 2000


def test_r7_missing_started_gives_neither() -> None:
    record = _record(_event(P, 0), _event(OK, 5))

    assert record.queue_wait_ms is None
    assert record.runtime_ms is None


def test_r8_late_pending_after_success_recomputes_the_wait() -> None:
    store = GraphStore()
    for event in (_event(P, 3), _event(S, 4), _event(OK, 6)):
        store.add_event(event)
    assert store.run_records()[0].queue_wait_ms == 1000

    store.add_event(_event(P, 0))  # sent earlier than first seen, arrives last

    (record,) = store.run_records()
    assert record.queue_wait_ms == 4000
    assert record.runtime_ms == 2000


def test_r8_wait_appears_when_the_sent_event_arrives_after_the_end() -> None:
    store = GraphStore()
    for event in (_event(S, 4), _event(OK, 6)):
        store.add_event(event)
    assert store.run_records()[0].queue_wait_ms is None

    store.add_event(_event(P, 0))

    assert store.run_records()[0].queue_wait_ms == 4000


def test_r9_revoked_before_start_gives_no_sample() -> None:
    record = _record(_event(P, 0), _event(TaskState.REVOKED, 3))

    assert record.queue_wait_ms is None
    assert queue_wait_sample(record) is None
    assert runtime_sample(record) is None


def test_r9_revoked_after_start_waited_but_gives_no_runtime_sample() -> None:
    record = _record(_event(P, 0), _event(S, 2), _event(TaskState.REVOKED, 3))

    assert queue_wait_sample(record) == 2000
    assert runtime_sample(record) is None


def test_r10_ignored_gives_no_sample() -> None:
    record = _record(_event(P, 0), _event(S, 2), _event(TaskState.IGNORED, 3))

    assert queue_wait_sample(record) is None
    assert runtime_sample(record) is None


def test_failure_gives_both_samples() -> None:
    record = _record(_event(P, 0), _event(S, 2), _event(TaskState.FAILURE, 4))

    assert queue_wait_sample(record) == 2000
    assert runtime_sample(record) == 2000


def test_non_terminal_events_never_compute_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading it scans every event under the store lock: a task retrying 1000 times would be quadratic."""
    calls: list[int] = []
    real = attribution.queue_wait_ms

    def counting(events: list[TaskEvent], parent: str | None) -> int | None:
        calls.append(len(events))
        return real(events, parent)

    monkeypatch.setattr(attribution, "queue_wait_ms", counting)
    store = GraphStore()
    store.add_event(_event(P, 0))
    for i in range(200):
        state = S if i % 2 == 0 else TaskState.RETRY
        store.add_event(_event(state, 1 + i, retries=i // 2))
    assert calls == []

    store.add_event(_event(OK, 400, retries=100))

    assert len(calls) == 1
    assert store.run_records()[0].queue_wait_ms == 1000
