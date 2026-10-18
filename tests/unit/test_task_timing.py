"""queue_wait_ms, runtime_ms and total_runtime_ms on TaskNode."""

import itertools
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import NodeType, TaskGraph, TaskNode

T0 = datetime(2026, 10, 17, tzinfo=timezone.utc)
FLOWS = Path(__file__).parents[1] / "data" / "celery_flows.json"


def _event(state: TaskState, at: float, retries: int = 0, **fields: Any) -> TaskEvent:
    return TaskEvent(
        task_id="task-1",
        name=fields.pop("name", "app.encode"),
        state=state,
        timestamp=T0 + timedelta(seconds=at),
        retries=retries,
        **fields,
    )


def _node(*events: TaskEvent) -> TaskNode:
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
    return graph.nodes["task-1"]


def _timing(node: TaskNode) -> tuple[int | None, int | None, int | None]:
    return node.queue_wait_ms, node.runtime_ms, node.total_runtime_ms


P, R, S, RT = TaskState.PENDING, TaskState.RECEIVED, TaskState.STARTED, TaskState.RETRY
OK, FAIL, REVOKED = TaskState.SUCCESS, TaskState.FAILURE, TaskState.REVOKED


class TestTiming:
    """Each field from hand-built histories."""

    @pytest.mark.parametrize(
        ("events", "expected"),
        [
            ([(P, 0), (S, 2), (OK, 5)], (2000, 3000, 3000)),
            ([(R, 1), (S, 2), (OK, 3)], (1000, 1000, 1000)),
            # The earliest of PENDING and RECEIVED starts the wait.
            ([(P, 1.5), (R, 1), (S, 2), (OK, 3)], (1000, 1000, 1000)),
            ([(P, 1), (R, 1.5), (S, 2), (OK, 3)], (1000, 1000, 1000)),
            # No producer-side PENDING and RECEIVED off: no wait.
            ([(S, 2), (OK, 3)], (None, 1000, 1000)),
            # PENDING is stamped after publish: it can follow STARTED.
            ([(S, 2), (P, 2.5), (OK, 3)], (0, 1000, 1000)),
            # Running: a wait, no runtime yet.
            ([(P, 0), (S, 2)], (2000, None, None)),
            # Revoked before it started.
            ([(P, 0), (REVOKED, 4)], (None, None, None)),
            ([(P, 0), (S, 1), (REVOKED, 4)], (1000, 3000, 3000)),
            ([(P, 0), (S, 1), (TaskState.REJECTED, 1.5)], (1000, 500, 500)),
            ([(P, 0)], (None, None, None)),
        ],
        ids=[
            "pending",
            "received",
            "received-earlier",
            "pending-earlier",
            "no-pre-start",
            "pending-after-started",
            "running",
            "revoked-before-start",
            "revoked-while-running",
            "rejected",
            "only-pending",
        ],
    )
    def test_single_attempt(
        self, events: list[tuple[TaskState, float]], expected: tuple[int | None, ...]
    ) -> None:
        assert _timing(_node(*(_event(state, at) for state, at in events))) == expected

    def test_retries_wait_is_first_attempt_runtime_is_last_total_sums(self) -> None:
        node = _node(
            _event(P, 0),
            _event(S, 1),
            _event(RT, 2, exception_type="KeyError"),
            _event(S, 12, retries=1),
            _event(OK, 15, retries=1),
        )

        assert _timing(node) == (1000, 3000, 4000)

    def test_failure_from_main_process_pairs_with_last_started(self) -> None:
        # Celery stamps retries=0 on a hard time limit's FAILURE.
        node = _node(
            _event(S, 1),
            _event(RT, 2),
            _event(S, 12, retries=1),
            _event(FAIL, 14),
        )

        assert (node.runtime_ms, node.total_runtime_ms) == (2000, 3000)

    def test_missed_started_of_last_attempt_gives_no_runtime(self) -> None:
        node = _node(_event(S, 1), _event(RT, 2), _event(OK, 15, retries=1))

        assert (node.runtime_ms, node.total_runtime_ms) == (None, None)

    def test_last_run_paired_with_an_earlier_attempt_gives_no_runtime(self) -> None:
        # Attempt 1's RETRY and attempt 2's STARTED were both missed.
        node = _node(_event(S, 1), _event(OK, 15, retries=1))

        assert (node.runtime_ms, node.total_runtime_ms) == (None, None)

    def test_missed_last_started_with_main_process_failure_gives_no_runtime(
        self,
    ) -> None:
        # Attempt 1's STARTED was missed; the FAILURE's retries=0 can't tell.
        node = _node(_event(S, 1), _event(RT, 2), _event(FAIL, 14))

        assert (node.runtime_ms, node.total_runtime_ms) == (None, None)

    def test_failure_of_an_earlier_retry_never_ends_a_later_run(self) -> None:
        # Only a retries=0 FAILURE comes from the worker's main process; a
        # FAILURE(1) stamped after STARTED(2) (skewed clocks) is attempt 1's.
        node = _node(
            _event(S, 1, retries=1),
            _event(FAIL, 30, retries=1),
            _event(S, 12, retries=2),
        )

        assert (node.runtime_ms, node.total_runtime_ms) == (None, None)

    def test_waiting_to_retry_has_no_runtime(self) -> None:
        node = _node(_event(P, 0), _event(S, 1), _event(RT, 2))

        assert _timing(node) == (1000, None, None)

    def test_missed_first_started_gives_no_wait(self) -> None:
        node = _node(_event(P, 0), _event(S, 12, retries=1), _event(OK, 15, retries=1))

        assert _timing(node) == (None, 3000, 3000)

    def test_redelivery_in_same_attempt_counts_both_runs(self) -> None:
        # A lost connection cancels the run (RETRY, same retries count) and the
        # message is delivered again.
        node = _node(
            _event(P, 0),
            _event(S, 1),
            _event(RT, 2),
            _event(S, 5),
            _event(OK, 7),
        )

        assert _timing(node) == (1000, 2000, 3000)

    def test_sub_second_spans_are_exact_milliseconds(self) -> None:
        # int(1.001 * 1000) is 1000 in floating point.
        node = _node(_event(P, 0), _event(S, 1.003), _event(OK, 2.004))

        assert _timing(node) == (1003, 1001, 1001)

    def test_run_without_a_reported_end_is_left_out(self) -> None:
        # A pool process lost mid-run reports nothing; acks_late redelivers.
        node = _node(_event(P, 0), _event(S, 1), _event(S, 5), _event(OK, 7))

        assert _timing(node) == (1000, 2000, 2000)

    @pytest.mark.parametrize(
        ("behind", "expected"),
        [
            (0, (10000, 2000, 3000)),
            (0.5, (10000, 2000, 3000)),
            (2, (10000, None, None)),
            (5, (7000, None, None)),
        ],
    )
    def test_redelivery_to_a_worker_whose_clock_is_behind(
        self, behind: float, expected: tuple[int | None, ...]
    ) -> None:
        # Worker A runs attempt 0 and loses the broker (cancel RETRY); worker B,
        # its clock `behind` seconds slow, runs it again. Once B's STARTED is
        # stamped before A's RETRY, B's run sorts first and the final event
        # ends no run: both fields are empty, and the wait can shrink.
        node = _node(
            _event(P, 0),
            _event(S, 10),
            _event(RT, 11),
            _event(S, 12 - behind),
            _event(OK, 14 - behind),
        )

        assert _timing(node) == expected

    @pytest.mark.parametrize(
        ("behind", "expected"),
        [
            (5, (10000, 2000, 2000)),
            (6, (9000, 1000, 1000)),
            (8, (7000, 0, 0)),
            (20, (0, 0, 0)),
        ],
    )
    def test_lost_run_redelivered_to_a_worker_whose_clock_is_behind(
        self, behind: float, expected: tuple[int | None, ...]
    ) -> None:
        # Documented limitation: worker A's pool process is lost after STARTED
        # (acks_late, no end event); B, `behind` seconds slow, runs it in 2 s.
        # Once B's STARTED is stamped before A's, A's STARTED starts the last
        # run, read against B's SUCCESS: too short, 0 at worst.
        node = _node(
            _event(P, 0),
            _event(S, 10),
            _event(S, 15 - behind),
            _event(OK, 17 - behind),
        )

        assert _timing(node) == expected

    def test_clock_stepped_back_mid_run_counts_as_zero(self) -> None:
        # One worker's clock stepped back (NTP) between STARTED and SUCCESS.
        node = _node(_event(S, 10), _event(RT, 11), _event(S, 12), _event(OK, 9))

        assert (node.runtime_ms, node.total_runtime_ms) == (0, 1000)

    def test_group_node_has_no_timing(self) -> None:
        graph = TaskGraph()
        for task_id in ("a", "b"):
            graph.add_event(
                TaskEvent(
                    task_id=task_id,
                    name="app.encode",
                    state=OK,
                    timestamp=T0,
                    group_id="g",
                )
            )
        [group] = [n for n in graph.nodes.values() if n.node_type == NodeType.GROUP]

        assert _timing(group) == (None, None, None)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _own_until(events: list[TaskEvent]) -> dict[str, int]:
    """Replaced task ID -> arrival index of its replacement's PENDING (its second)."""
    seen: dict[str, int] = {}
    cut: dict[str, int] = {}
    for i, e in enumerate(events):
        if e.state == P and e.retries == 0:
            seen[e.task_id] = seen.get(e.task_id, 0) + 1
            if seen[e.task_id] == 2:
                cut.setdefault(e.task_id, i)
    return cut


def _wait(events: list[TaskEvent], eta: datetime | None) -> int | None:
    """Wait from the first sent event (or ``eta`` if later) to the first STARTED."""
    sent = [e.timestamp for e in events if e.state in (P, R)]
    started = [e.timestamp for e in events if e.state == S]
    if not sent or not started:
        return None
    wait_from = min(sent) if eta is None else max(min(sent), eta)
    return max(0, (min(started) - wait_from) // timedelta(milliseconds=1))


REPLACE_DELAYED = [
    "replace_delayed_root",
    "replace_delayed_mid_chain",
    "replace_delayed_header",
    "replace_delayed_nested",
    "replace_delayed_same_name",
]
SCENARIOS = [
    "as-captured",
    "sender-behind",
    "sender-ahead",
    "replacement-behind",
    "no-own-pending",
    "replacement-received",
    "both-received",
]
# Protocol 1 messages carry no parent ID: these clock and event cases take the
# replacement's ETA (docs/features.md, Limitations).
PROTOCOL_1_LEAKS = {"sender-ahead", "replacement-behind", "no-own-pending"}


def _mutate(
    events: list[TaskEvent], own_until: dict[str, int], scenario: str
) -> list[TaskEvent]:
    """Drop or shift the replaced tasks' events as ``scenario`` says."""
    out: list[TaskEvent] = []
    minute = timedelta(seconds=60)
    for i, e in enumerate(events):
        if e.task_id not in own_until:
            out.append(e)
            continue
        own = i < own_until[e.task_id]
        if scenario == "no-own-pending" and own and e.state == P:
            continue
        if scenario == "sender-ahead" and own and e.state == P:
            e = e.model_copy(update={"timestamp": e.timestamp + minute})
        if scenario == "sender-behind" and own and e.state == P:
            e = e.model_copy(update={"timestamp": e.timestamp - minute})
        if scenario == "replacement-behind" and not own:
            e = e.model_copy(update={"timestamp": e.timestamp - minute})
        out.append(e)
        # RECEIVED (publish_received=True) on the replacement's worker only, or on both;
        # stamped by the worker just before the run starts.
        received = (scenario == "replacement-received" and not own) or (
            scenario == "both-received"
        )
        if received and e.state == S and e.retries == 0:
            stamp = e.timestamp - timedelta(seconds=0.005)
            out.insert(-1, e.model_copy(update={"state": R, "timestamp": stamp}))
    return out


class TestEta:
    """A first attempt sent with a countdown or ETA: the wait starts at the ETA."""

    @pytest.mark.parametrize(
        ("events", "expected_wait"),
        [
            # Sent at 0 with countdown=3, started half a second after the ETA.
            ([(P, 0, 3), (S, 3.5, 3), (OK, 4, 3)], 500),
            # RECEIVED arrives before the ETA (the worker holds the message): still 500.
            ([(P, 0, 3), (R, 0.1, 3), (S, 3.5, 3), (OK, 4, 3)], 500),
            # Only the worker's events carry it (the sender doesn't run stemtrace).
            ([(R, 0.1, 3), (S, 3.5, 3), (OK, 4, 3)], 500),
            # Only STARTED carries it.
            ([(P, 0, None), (S, 3.5, 3), (OK, 4, 3)], 500),
            # An ETA already past when the task was sent changes nothing.
            ([(P, 2, 1), (S, 3.5, 1), (OK, 4, 1)], 1500),
            # No ETA anywhere: unchanged.
            ([(P, 0, None), (S, 3.5, None), (OK, 4, None)], 3500),
        ],
        ids=[
            "countdown",
            "received-before-eta",
            "worker-only",
            "started-only",
            "eta-in-the-past",
            "no-eta",
        ],
    )
    def test_wait_starts_at_eta(
        self,
        events: list[tuple[TaskState, float, float | None]],
        expected_wait: int,
    ) -> None:
        node = _node(
            *(
                _event(state, at, eta=None if eta is None else _at(eta))
                for state, at, eta in events
            )
        )

        assert node.queue_wait_ms == expected_wait
        assert node.runtime_ms == 500

    def test_node_eta_is_the_first_attempts(self) -> None:
        node = _node(
            _event(P, 0, eta=_at(3)),
            _event(S, 3.5, eta=_at(3)),
            _event(RT, 4, eta=_at(3)),
            _event(S, 20, retries=1, eta=_at(19)),
            _event(OK, 21, retries=1, eta=_at(19)),
        )

        assert node.eta == _at(3)
        assert node.queue_wait_ms == 500

    def test_retry_eta_never_shortens_the_first_wait(self) -> None:
        # The first attempt had no ETA; only the retry did.
        node = _node(
            _event(P, 0),
            _event(S, 2),
            _event(RT, 3),
            _event(S, 20, retries=1, eta=_at(19)),
            _event(OK, 21, retries=1, eta=_at(19)),
        )

        assert node.eta is None
        assert node.queue_wait_ms == 2000

    def test_no_eta(self) -> None:
        assert _node(_event(P, 0), _event(S, 2)).eta is None

    def test_older_senders_unparented_pending_counts(self) -> None:
        # A sender before stemtrace 0.4.0 sent PENDING without the parent ID.
        node = _node(
            _event(P, 0),
            _event(S, 5, parent_id="parent-1", eta=_at(3)),
        )

        assert (node.eta, node.queue_wait_ms) == (_at(3), 2000)

    def test_root_task_keeps_the_senders_eta_when_received_comes_first(self) -> None:
        # Root task (no parent IDs under any protocol): sent with countdown=60 by code
        # on this release, received by an older worker (no eta on its events) 5 ms
        # before the sender stamped PENDING.
        node = _node(
            _event(R, -0.005),
            _event(P, 0, eta=_at(60)),
            _event(S, 60.2),
            _event(OK, 61),
        )

        assert (node.eta, node.queue_wait_ms) == (_at(60), 200)

    def test_older_senders_pending_still_counts_once_the_task_is_replaced(self) -> None:
        # Mixed fleet: a pre-0.4.0 sender's PENDING has no parent; the task waits 10 s
        # in the broker, then replaces itself with a delayed task.
        replacement = {"name": "app.add", "parent_id": "task-1", "eta": _at(41)}
        node = _node(
            _event(P, 0),
            _event(S, 10, parent_id="parent-1"),
            _event(P, 11, **replacement),
            _event(S, 41.1, **replacement),
            _event(OK, 41.2, **replacement),
        )

        assert (node.eta, node.queue_wait_ms) == (None, 10_000)

    def test_without_parent_ids_a_replacements_received_does_not_count(self) -> None:
        # Protocol 1: the task's worker has RECEIVED off, the replacement's worker has
        # it on; the replacement was sent with countdown=58 while the task ran.
        node = _node(
            _event(P, 0),
            _event(S, 1),
            _event(P, 2, name="app.add", eta=_at(60)),
            _event(R, 2.01, name="app.add", eta=_at(60)),
            _event(S, 60.1, name="app.add", eta=_at(60)),
            _event(OK, 60.2, name="app.add", eta=_at(60)),
        )

        assert (node.eta, node.queue_wait_ms) == (None, 1000)

    @pytest.mark.parametrize(
        ("own_parent", "replacement_name"),
        [
            (None, "app.encode"),  # root task replacing itself
            (None, "app.add"),  # root task replaced by another task
            ("parent-1", "app.encode"),  # child task replacing itself
        ],
        ids=["root-same-name", "root-other-name", "child-same-name"],
    )
    def test_pre_040_replacement_pending_without_own_sent_events(
        self, own_parent: str | None, replacement_name: str
    ) -> None:
        # Documented limit: a worker before stemtrace 0.4.0 sends PENDING without a
        # parent. With no PENDING or RECEIVED of the task's own (e.g. a sender without
        # init_worker and RECEIVED off), the replacement's PENDING stands in for the
        # task's: the wait is read across the two runs and counts as 0.
        replacement = {"name": replacement_name, "parent_id": "task-1"}
        node = _node(
            _event(S, 10, parent_id=own_parent),
            _event(P, 11, name=replacement_name),
            _event(S, 41.1, **replacement),
            _event(OK, 41.2, **replacement),
        )

        assert (node.eta, node.queue_wait_ms) == (None, 0)

    def test_redelivered_run_after_a_worker_without_etas(self) -> None:
        # Documented limit: without parent IDs only the first run's events are read.
        # The first run's worker (an earlier release) recorded no ETA or RECEIVED; the
        # run redelivered to a worker on this release did, so the countdown counts.
        node = _node(
            _event(P, 0),
            _event(S, 10),
            _event(R, 15, eta=_at(9)),
            _event(S, 15.1, eta=_at(9)),
            _event(OK, 16, eta=_at(9)),
        )

        assert (node.eta, node.queue_wait_ms) == (None, 10_000)

    def test_eta_from_received_before_the_task_starts(self) -> None:
        # An older sender (PENDING without eta), a worker that records it on RECEIVED.
        node = _node(_event(P, 0), _event(R, 0.1, eta=_at(3)))

        assert node.eta == _at(3)

    def test_delayed_replacement_is_not_the_first_attempts_eta(self) -> None:
        # self.replace(add.si(1, 1).set(countdown=2)) after waiting 2.5 s in the queue:
        # replace() reuses the ID, sends with retries=0 and the task as the parent
        # (real worker, celery 5.6.3).
        replacement = {"name": "app.add", "parent_id": "task-1", "eta": _at(4.793)}
        node = _node(
            _event(P, 0),
            _event(S, 2.493),
            _event(P, 2.794, **replacement),
            _event(S, 4.795, **replacement),
            _event(OK, 4.84, **replacement),
        )

        assert node.eta is None
        assert node.queue_wait_ms == 2493

    def test_delayed_task_replaced_by_a_delayed_task_keeps_its_own_eta(self) -> None:
        replacement = {"name": "app.add", "parent_id": "task-1", "eta": _at(3.5)}
        node = _node(
            _event(P, 0, eta=_at(1)),
            _event(S, 1.2, eta=_at(1)),
            _event(P, 1.5, **replacement),
            _event(S, 3.6, **replacement),
            _event(OK, 3.7, **replacement),
        )

        assert node.eta == _at(1)
        assert node.queue_wait_ms == 200


SKEWS = (-60, 0, 60)
PRE_START = ("pending", "received", "both", "none")


def _history(
    skews: tuple[int, ...], ending: str, pre_start: str, sender_skew: int
) -> tuple[list[TaskEvent], tuple[int | None, int, int]]:
    """A task run ``len(skews)`` times, attempt ``n`` on worker ``n`` (clock off by ``skews[n]``).

    Returns the events and the expected (queue wait, runtime, total runtime):
    each attempt's start and end come from one worker's clock, so runtimes don't
    depend on the skews.
    """
    sent, received = 0.0, 1.0
    run = [3.0 + n for n in range(len(skews))]  # attempt n runs 3+n seconds
    events: list[TaskEvent] = []
    if pre_start in ("pending", "both"):
        events.append(_event(P, sent + sender_skew))
    if pre_start in ("received", "both"):
        events.append(_event(R, received + skews[0]))
    start = 2.0
    last = len(skews) - 1
    for n, skew in enumerate(skews):
        events.append(_event(S, start + skew, retries=n))
        end = start + run[n]
        if n < last:
            events.append(_event(RT, end + skew, retries=n))
        elif ending == "main-process-failure":
            events.append(_event(FAIL, end + skew))
        else:
            events.append(_event(TaskState[ending], end + skew, retries=n))
        start = end + 10
    starts = []
    if pre_start in ("pending", "both"):
        starts.append(sent + sender_skew)
    if pre_start in ("received", "both"):
        starts.append(received + skews[0])
    wait = max(0, round((2.0 + skews[0] - min(starts)) * 1000)) if starts else None
    return events, (wait, round(run[last] * 1000), round(sum(run) * 1000))


def _sweep() -> list[tuple[tuple[int, ...], str]]:
    return [
        (skews, ending)
        for attempts in (1, 2, 3)
        for skews in itertools.product(SKEWS, repeat=attempts)
        for ending in ("SUCCESS", "FAILURE", "main-process-failure")
    ]


class TestTimingSweep:
    """Worker clocks, sender clock, pre-start events and arrival order don't matter."""

    @pytest.mark.parametrize(("skews", "ending"), _sweep())
    def test_timing(self, skews: tuple[int, ...], ending: str) -> None:
        rng = random.Random(f"{skews}{ending}")
        for pre_start in PRE_START:
            # The sender's clock only matters when there is a PENDING.
            sender_skews = SKEWS if pre_start in ("pending", "both") else (0,)
            for sender_skew in sender_skews:
                events, expected = _history(skews, ending, pre_start, sender_skew)
                orders = [events, events[::-1], rng.sample(events, len(events))]
                for order in orders:
                    assert _timing(_node(*order)) == expected, (pre_start, sender_skew)


@pytest.mark.parametrize("last_end", [OK, FAIL])
@pytest.mark.parametrize("skews", list(itertools.product(SKEWS, repeat=2)))
def test_retry_without_raising_never_pairs_attempts(
    skews: tuple[int, int], last_end: TaskState
) -> None:
    # self.retry(throw=False) republishes and returns: attempt 0 ends in its
    # own SUCCESS (celery 5.6.3 app/task.py Task.retry, `return ret`; no
    # task_retry). With worker 1's clock behind, that SUCCESS is the final
    # event; it must not end attempt 1's run.
    first, second = skews
    events = [
        _event(P, 0),
        _event(S, 1 + first),
        _event(OK, 2 + first),
        _event(S, 12 + second, retries=1),
        _event(last_end, 15 + second, retries=1),
    ]
    attempt0_is_final = 2 + first >= 15 + second
    # Attempt 0's run ended in a terminal event that isn't the final one, so
    # it isn't counted in the total.
    expected = (None, None) if attempt0_is_final else (3000, 3000)

    for order in (events, events[::-1], random.Random(str(skews)).sample(events, 5)):
        node = _node(*order)
        assert (node.runtime_ms, node.total_runtime_ms) == expected


def _capture(flow: str) -> list[TaskEvent]:
    return [TaskEvent.model_validate(e) for e in json.loads(FLOWS.read_text())[flow]]


class TestRealCaptures:
    """Timing from events real Celery workflows emitted (tests/data/celery_flows.json)."""

    def test_retried_task(self) -> None:
        events = _capture("chord_retried_header")
        graph = TaskGraph()
        for event in events:
            graph.add_event(event)
        [flaky] = [n for n in graph.nodes.values() if n.name == "flows.flaky_add"]
        mine = {(e.state, e.retries): e.timestamp for e in flaky.events}

        def span(a: tuple[TaskState, int], b: tuple[TaskState, int]) -> timedelta:
            return mine[b] - mine[a]

        def ms(delta: timedelta) -> int:
            return delta // timedelta(milliseconds=1)

        assert _timing(flaky) == (
            ms(span((P, 0), (S, 0))),
            ms(span((S, 1), (OK, 1))),
            ms(span((S, 0), (RT, 0)) + span((S, 1), (OK, 1))),
        )

    def test_countdown_is_not_counted_as_waiting(self) -> None:
        # add.s(1, 1).set(countdown=2) on a worker with nothing else to do.
        graph = TaskGraph()
        for event in _capture("countdown"):
            graph.add_event(event)
        [node] = graph.nodes.values()
        etas = {e.eta for e in node.events}
        started = next(e.timestamp for e in node.events if e.state == S)
        pending = next(e.timestamp for e in node.events if e.state == P)

        assert len(etas) == 1
        assert node.eta is not None
        assert etas == {node.eta}
        assert node.eta - pending > timedelta(seconds=1.9)
        assert node.queue_wait_ms == max(
            0, (started - node.eta) // timedelta(milliseconds=1)
        )
        assert node.queue_wait_ms is not None
        assert node.queue_wait_ms < 100

    def test_retry_countdown_has_its_own_eta(self) -> None:
        # Sent with countdown=1, then self.retry(countdown=1) once.
        graph = TaskGraph()
        for event in _capture("countdown_retry_countdown"):
            graph.add_event(event)
        [node] = graph.nodes.values()
        first = {e.eta for e in node.events if e.retries == 0}
        retry = {e.eta for e in node.events if e.retries == 1}
        started = next(
            e.timestamp for e in node.events if e.state == S and e.retries == 0
        )

        assert first == {node.eta}
        assert len(retry) == 1
        [retry_eta] = retry
        assert node.eta is not None
        assert retry_eta is not None
        assert retry_eta - node.eta > timedelta(seconds=0.9)
        assert node.queue_wait_ms == max(
            0, (started - node.eta) // timedelta(milliseconds=1)
        )

    @pytest.mark.parametrize(
        "flow",
        [
            "replace_delayed_root",
            "replace_delayed_mid_chain",
            "replace_delayed_header",
            "replace_delayed_nested",
            "replace_delayed_same_name",
        ],
    )
    def test_delayed_replacement_never_becomes_the_tasks_eta(self, flow: str) -> None:
        # No task in these flows was sent with a delay; only replace() replacements were.
        graph = TaskGraph()
        for event in _capture(flow):
            graph.add_event(event)
        replaced = [
            n for n in graph.nodes.values() if any(e.eta is not None for e in n.events)
        ]

        assert replaced
        for node in graph.nodes.values():
            assert node.eta is None, node.name
            sent = [
                e.timestamp for e in node.events if e.retries == 0 and e.state in (P, R)
            ]
            started = [
                e.timestamp for e in node.events if e.state == S and e.retries == 0
            ]
            if sent and started:
                wait = (min(started) - min(sent)) // timedelta(milliseconds=1)
                assert node.queue_wait_ms == max(0, wait), node.name

    @pytest.mark.parametrize(
        "flow",
        [
            "replace_delayed_root",
            "replace_delayed_mid_chain",
            "replace_delayed_header",
            "replace_delayed_nested",
            "replace_delayed_same_name",
        ],
    )
    @pytest.mark.parametrize("scenario", SCENARIOS)
    @pytest.mark.parametrize("protocol", [1, 2])
    def test_replacement_eta_never_leaks_whatever_the_clocks(
        self, flow: str, scenario: str, protocol: int
    ) -> None:
        # Oracle from arrival order, not from TaskNode: a replaced task's own events
        # are those that arrived before its second attempt-0 PENDING (the replacement's).
        events = _capture(flow)
        own_until = _own_until(events)
        assert own_until
        mutated = _mutate(events, own_until, scenario)
        if protocol == 1:  # celery 5.6.3 as_task_v1 sends no parent_id or root_id
            mutated = [
                e.model_copy(update={"parent_id": None, "root_id": None})
                for e in mutated
            ]
        graph = TaskGraph()
        for event in mutated:
            graph.add_event(event)

        for task_id, cut in own_until.items():
            node = graph.nodes[task_id]
            own = [e for e in events[:cut] if e.task_id == task_id]
            wait = (
                next(e.timestamp for e in own if e.state == S)
                - next(e.timestamp for e in own if e.state == P)
            ) // timedelta(milliseconds=1)
            expected = {
                "as-captured": wait,
                "sender-behind": wait + 60_000,
                "sender-ahead": max(0, wait - 60_000),
                "replacement-behind": wait,
                "no-own-pending": None,
                "replacement-received": wait,
                # The task's own RECEIVED (5 ms before its STARTED) can be earlier
                # than its PENDING; the wait counts from the earlier one.
                "both-received": max(wait, 5),
            }
            if protocol == 1 and scenario in PROTOCOL_1_LEAKS:
                # Documented platform limit (docs/features.md): without parent IDs the
                # values can come from the replacement instead of the task.
                replacement_eta = next(
                    (e.eta for e in events[cut:] if e.task_id == task_id and e.eta),
                    None,
                )
                assert node.eta in (None, replacement_eta), (task_id, scenario)
                # The task's own wait or the replacement's; with the task's own PENDING
                # missed, a span from its STARTED to the replacement's PENDING (negative,
                # so 0).
                theirs = [e for e in mutated[cut:] if e.task_id == task_id]
                allowed = {expected[scenario], _wait(theirs, replacement_eta)}
                if scenario == "no-own-pending":
                    allowed.add(0)
                assert node.queue_wait_ms in allowed, (task_id, scenario)
                continue

            assert node.eta is None, (task_id, scenario)
            assert node.queue_wait_ms == expected[scenario], (task_id, scenario)

    @pytest.mark.parametrize("flow", REPLACE_DELAYED)
    def test_replacement_eta_never_leaks_while_events_arrive(self, flow: str) -> None:
        # Any arrival order, checked after every event (not just at the end).
        events = _capture(flow)
        for seed in range(20):
            graph = TaskGraph()
            for event in random.Random(seed).sample(events, len(events)):
                graph.add_event(event)
                assert all(n.eta is None for n in graph.nodes.values()), seed

    def test_delayed_task_replaced_with_a_delay_keeps_its_first_eta(self) -> None:
        graph = TaskGraph()
        for event in _capture("delayed_replace_delayed"):
            graph.add_event(event)
        [node] = graph.nodes.values()
        first = min((e for e in node.events if e.state == P), key=lambda e: e.timestamp)
        etas = {e.eta for e in node.events}

        assert len(etas) == 2
        assert node.eta == first.eta
        assert node.queue_wait_ms is not None
        assert node.queue_wait_ms < 100

    @pytest.mark.parametrize("flow", sorted(json.loads(FLOWS.read_text())))
    def test_every_captured_task_has_consistent_timing(self, flow: str) -> None:
        graph = TaskGraph()
        for event in _capture(flow):
            graph.add_event(event)

        for node in graph.nodes.values():
            if node.node_type != NodeType.TASK:
                assert _timing(node) == (None, None, None)
                continue
            wait, runtime, total = _timing(node)
            has_started = any(e.state == S for e in node.events)
            assert (runtime is None) == (total is None)
            if node.state in (OK, FAIL) and has_started:
                assert runtime is not None
                assert total is not None
                assert total >= runtime >= 0
            if wait is not None:
                assert wait >= 0
