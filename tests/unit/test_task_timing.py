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
        name="app.encode",
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
        # Events without a hostname (workers before this release): worker A runs
        # attempt 0 and loses the broker (cancel RETRY); worker B, its clock
        # `behind` seconds slow, runs it again. Once B's STARTED is stamped
        # before A's RETRY, B's run sorts first and the final event ends no
        # run: both fields are empty, and the wait can shrink.
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
        # Events without a hostname (workers before this release): worker A's
        # pool process is lost after STARTED (acks_late, no end event); B,
        # `behind` seconds slow, runs it in 2 s. Once B's STARTED is stamped
        # before A's, A's STARTED starts the last run, read against B's
        # SUCCESS: too short, 0 at worst.
        node = _node(
            _event(P, 0),
            _event(S, 10),
            _event(S, 15 - behind),
            _event(OK, 17 - behind),
        )

        assert _timing(node) == expected

    @pytest.mark.parametrize("behind", [0, 0.5, 1, 2, 5, 60])
    def test_redelivery_to_another_worker_pairs_each_run_on_its_worker(
        self, behind: float
    ) -> None:
        # Each run is read on one worker's clock: A's cancelled run (1 s) and
        # B's run (2 s), whatever B's clock says.
        a, b = {"hostname": "celery@a"}, {"hostname": "celery@b"}
        node = _node(
            _event(P, 0),
            _event(S, 10, **a),
            _event(RT, 11, **a),
            _event(S, 12 - behind, **b),
            _event(OK, 14 - behind, **b),
        )
        for order in (
            node.events,
            node.events[::-1],
            node.events[1::2] + node.events[::2],
        ):
            node = _node(*order)
            assert (node.runtime_ms, node.total_runtime_ms) == (2000, 3000)

    @pytest.mark.parametrize("behind", [0, 5, 6, 8, 20, 60])
    def test_lost_run_is_left_out_whatever_the_clocks(self, behind: float) -> None:
        # A's pool process is lost after STARTED (no end); B runs it in 2 s.
        a, b = {"hostname": "celery@a"}, {"hostname": "celery@b"}
        node = _node(
            _event(P, 0),
            _event(S, 10, **a),
            _event(S, 15 - behind, **b),
            _event(OK, 17 - behind, **b),
        )
        for order in (
            node.events,
            node.events[::-1],
            node.events[1::2] + node.events[::2],
        ):
            node = _node(*order)
            assert (node.runtime_ms, node.total_runtime_ms) == (2000, 2000)

    def test_main_process_failure_with_its_worker_pairs_on_that_worker(
        self,
    ) -> None:
        # This release's workers give a hard time limit's FAILURE (sent from
        # the worker's main process) its attempt and worker: it ends B's run
        # even with A's clock ahead.
        a, b = {"hostname": "celery@a"}, {"hostname": "celery@b"}
        node = _node(
            _event(S, 70, **a),
            _event(RT, 71, **a),
            _event(S, 12, retries=1, **b),
            _event(FAIL, 15, retries=1, **b),
        )

        assert (node.runtime_ms, node.total_runtime_ms) == (3000, 4000)

    def test_first_attempt_failing_after_retry_without_raising_pairs_on_its_worker(
        self,
    ) -> None:
        # self.retry(throw=False), then attempt 0 raises late: its FAILURE
        # (retries=0, from the task's own process, so with its worker) is the
        # final event. It ends attempt 0's run on A, not attempt 1's on B.
        a, b = {"hostname": "celery@a"}, {"hostname": "celery@b"}
        node = _node(
            _event(S, 1, **a),
            _event(S, 3, retries=1, **b),
            _event(OK, 5, retries=1, **b),
            _event(FAIL, 20, **a),
        )

        assert (node.runtime_ms, node.total_runtime_ms) == (19000, 19000)

    def test_retries_zero_failure_with_a_worker_is_not_from_an_older_main_process(
        self,
    ) -> None:
        # Only an older worker's main-process FAILURE has retries=0 whatever
        # the attempt (and no worker); this release's FAILURE(0) on B with B's
        # STARTED(1) missing ends no run.
        b = {"hostname": "celery@b"}
        node = _node(_event(S, 1, retries=1, hostname="celery@a"), _event(FAIL, 4, **b))

        assert (node.runtime_ms, node.total_runtime_ms) == (None, None)

    def test_late_failure_of_attempt_zero_never_ends_attempt_one_on_one_worker(
        self,
    ) -> None:
        # Both attempts on worker A (throw=False); attempt 0's FAILURE comes
        # last and carries its worker, so it isn't an older main-process one.
        a = {"hostname": "celery@a"}
        node = _node(
            _event(S, 1, **a), _event(S, 3, retries=1, **a), _event(FAIL, 20, **a)
        )

        assert (node.runtime_ms, node.total_runtime_ms) == (None, None)

    def test_end_without_worker_closes_the_latest_run(self) -> None:
        # A FAILURE without a worker (from an older worker's main process)
        # closes the latest unfinished run: A's second run, not B's.
        a, b = {"hostname": "celery@a"}, {"hostname": "celery@b"}
        node = _node(
            _event(S, 1, **a),
            _event(S, 2, **b),
            _event(S, 3, **a),
            _event(FAIL, 5),
        )

        assert (node.runtime_ms, node.total_runtime_ms) == (2000, 2000)

    def test_end_from_this_release_pairs_with_an_older_workers_start(self) -> None:
        # Mid-upgrade: STARTED from an older worker (no hostname), SUCCESS
        # from this release's.
        node = _node(_event(S, 10), _event(OK, 12, hostname="celery@a"))

        assert (node.runtime_ms, node.total_runtime_ms) == (2000, 2000)

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


SKEWS = (-60, 0, 60)
PRE_START = ("pending", "received", "both", "none")


def _history(
    skews: tuple[int, ...],
    ending: str,
    pre_start: str,
    sender_skew: int,
    with_hostnames: bool = False,
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
        # This release's workers put their nodename on every worker event,
        # and the attempt and nodename on a main-process FAILURE.
        on = {"hostname": f"celery@w{n}"} if with_hostnames else {}
        events.append(_event(S, start + skew, retries=n, **on))
        end = start + run[n]
        if n < last:
            events.append(_event(RT, end + skew, retries=n, **on))
        elif ending == "main-process-failure":
            failed = {"retries": n, **on} if with_hostnames else {}
            events.append(_event(FAIL, end + skew, **failed))
        else:
            events.append(_event(TaskState[ending], end + skew, retries=n, **on))
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

    @pytest.mark.parametrize(
        "with_hostnames", [False, True], ids=["older-workers", "this-release"]
    )
    @pytest.mark.parametrize(("skews", "ending"), _sweep())
    def test_timing(
        self, skews: tuple[int, ...], ending: str, with_hostnames: bool
    ) -> None:
        rng = random.Random(f"{skews}{ending}")
        for pre_start in PRE_START:
            # The sender's clock only matters when there is a PENDING.
            sender_skews = SKEWS if pre_start in ("pending", "both") else (0,)
            for sender_skew in sender_skews:
                events, expected = _history(
                    skews, ending, pre_start, sender_skew, with_hostnames
                )
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
