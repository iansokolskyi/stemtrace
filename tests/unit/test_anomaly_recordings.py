"""The classifier on streams recorded from real Celery workers (M3b, M4, M5).

Recorded by ``tests/integration/test_anomaly_real_worker.py`` (Celery 5.6.3,
Redis). ``ANOMALY_RECORD=1`` re-records them.
"""

from datetime import timedelta

from stemtrace.core.anomalies import AnomalyEvidence, AnomalyKind
from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent, WorkerEventType
from tests.unit.anomaly_pipeline import classify, load_recording


def _split(
    entries: list[TaskEvent | WorkerEvent],
) -> tuple[list[TaskEvent], list[WorkerEvent]]:
    return (
        [e for e in entries if isinstance(e, TaskEvent)],
        [e for e in entries if isinstance(e, WorkerEvent)],
    )


def _verdicts(
    entries: list[TaskEvent | WorkerEvent], extra_seconds: float
) -> dict[str, tuple[AnomalyKind, AnomalyEvidence | None]]:
    tasks, workers = _split(entries)
    now = max(e.timestamp for e in entries) + timedelta(seconds=extra_seconds)
    report = classify(tasks, workers, now=now)
    return {t: (a.kind, a.evidence) for t, a in report.anomalies.items()}


def test_m3b_cold_stop_stamps_the_cancel_retry_before_worker_terminating() -> None:
    entries = load_recording("m3b_cold_stop")
    tasks, workers = _split(entries)
    (retry,) = [e for e in tasks if e.state == TaskState.RETRY]
    (terminating,) = [
        w for w in workers if w.event_type == WorkerEventType.WORKER_TERMINATING
    ]

    # Recorded: the RETRY is published from the signal handler at once, and
    # WORKER_TERMINATING about a second later, when the pool terminates.
    assert retry.timestamp < terminating.timestamp
    assert retry.exception == "cancelled by Celery"
    assert _verdicts(entries, 1) == {
        retry.task_id: (AnomalyKind.INTERRUPTED, AnomalyEvidence.CANCELLED_RETRY)
    }


def test_m4_restart_is_interrupted_until_the_redelivered_started() -> None:
    entries = load_recording("m4_acks_late_sigkill")
    second_ready = [
        i
        for i, e in enumerate(entries)
        if isinstance(e, WorkerEvent) and e.event_type == WorkerEventType.WORKER_READY
    ][1]
    (task_id,) = {e.task_id for e in entries if isinstance(e, TaskEvent)}

    assert _verdicts(entries[: second_ready + 1], 0.5) == {
        task_id: (AnomalyKind.INTERRUPTED, AnomalyEvidence.WORKER_RESTARTED)
    }
    assert _verdicts(entries, 0.5) == {}


def test_m5_sigkill_is_stuck_after_the_threshold_then_interrupted_at_restart() -> None:
    entries = load_recording("m5_sigkill_restart")
    before, restarted = entries[:-1], entries
    (task_id,) = {e.task_id for e in entries if isinstance(e, TaskEvent)}

    assert _verdicts(before, 299 - 3) == {}
    assert _verdicts(before, 301) == {task_id: (AnomalyKind.STUCK, None)}
    assert _verdicts(restarted, 301) == {
        task_id: (AnomalyKind.INTERRUPTED, AnomalyEvidence.WORKER_RESTARTED)
    }
