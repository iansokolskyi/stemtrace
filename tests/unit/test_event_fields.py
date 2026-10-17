"""exception_type, hostname, queue and routing_key on TaskEvent and TaskNode."""

import itertools
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import TaskGraph

T0 = datetime(2026, 10, 17, tzinfo=timezone.utc)


def _event(state: TaskState, second: int, retries: int = 0, **fields: Any) -> TaskEvent:
    return TaskEvent(
        task_id="task-1",
        name="app.encode",
        state=state,
        timestamp=T0 + timedelta(seconds=second),
        retries=retries,
        **fields,
    )


class TestTaskEventCompatibility:
    """Events from older and newer stemtrace versions both parse."""

    def test_event_from_older_worker_parses_with_fields_none(self) -> None:
        # What a 0.4 worker publishes: none of the new keys.
        old = {
            "task_id": "task-1",
            "name": "app.encode",
            "state": "FAILURE",
            "timestamp": "2026-10-17T00:00:00Z",
            "exception": "ValueError: boom",
        }

        event = TaskEvent.model_validate(old)

        assert (
            event.exception_type,
            event.hostname,
            event.queue,
            event.routing_key,
        ) == (None, None, None, None)

    def test_event_with_unknown_fields_parses(self) -> None:
        # A server reading events from newer workers: pydantic's default
        # extra="ignore" drops keys it doesn't know (how a 0.4 server reads
        # this release's events, and how this one reads a later release's).
        payload = _event(TaskState.STARTED, 0, hostname="celery@w1").model_dump(
            mode="json"
        )
        payload["field_from_the_future"] = "x"

        event = TaskEvent.model_validate(payload)

        assert event.hostname == "celery@w1"
        assert "field_from_the_future" not in event.model_dump()

    def test_fields_round_trip_through_json(self) -> None:
        event = _event(
            TaskState.FAILURE,
            0,
            exception_type="myapp.errors.PartnerAPIError",
            hostname="celery@media-gpu-1",
            queue="media",
            routing_key="media",
        )

        parsed = TaskEvent.model_validate(json.loads(event.model_dump_json()))

        assert parsed == event


class TestTaskNodeFields:
    """A node reports the latest non-null value of each field, in lifecycle order."""

    def test_node_reports_fields_of_its_events(self) -> None:
        graph = TaskGraph()
        graph.add_event(
            _event(TaskState.PENDING, 0, queue="media", routing_key="media")
        )
        graph.add_event(
            _event(
                TaskState.STARTED,
                1,
                hostname="celery@w1",
                queue="media",
                routing_key="media",
            )
        )
        graph.add_event(
            _event(
                TaskState.FAILURE,
                2,
                hostname="celery@w1",
                exception_type="ValueError",
            )
        )

        node = graph.nodes["task-1"]
        assert (node.hostname, node.queue, node.routing_key, node.exception_type) == (
            "celery@w1",
            "media",
            "media",
            "ValueError",
        )

    def test_later_attempt_wins(self) -> None:
        # A retry that ran on another worker and from another queue.
        graph = TaskGraph()
        for event in (
            _event(TaskState.STARTED, 0, hostname="celery@w1", queue="media"),
            _event(TaskState.RETRY, 1, hostname="celery@w1", exception_type="KeyError"),
            _event(TaskState.STARTED, 2, retries=1, hostname="celery@w2", queue="slow"),
            _event(TaskState.FAILURE, 3, retries=1, exception_type="TimeoutError"),
        ):
            graph.add_event(event)

        node = graph.nodes["task-1"]
        assert (node.hostname, node.queue, node.exception_type) == (
            "celery@w2",
            "slow",
            "TimeoutError",
        )

    def test_null_on_last_event_keeps_earlier_value(self) -> None:
        # A FAILURE sent from the worker's main process carries no hostname.
        graph = TaskGraph()
        graph.add_event(_event(TaskState.STARTED, 0, hostname="celery@w1"))
        graph.add_event(_event(TaskState.FAILURE, 1, exception_type="WorkerLostError"))

        assert graph.nodes["task-1"].hostname == "celery@w1"

    def test_lifecycle_order_not_arrival_order(self) -> None:
        # STARTED of the retry arrives before the first attempt's STARTED.
        graph = TaskGraph()
        graph.add_event(_event(TaskState.STARTED, 5, retries=1, hostname="celery@w2"))
        graph.add_event(_event(TaskState.STARTED, 0, hostname="celery@w1"))

        assert graph.nodes["task-1"].hostname == "celery@w2"

    def test_recovered_retry_keeps_its_exception_type(self) -> None:
        graph = TaskGraph()
        graph.add_event(_event(TaskState.RETRY, 0, exception_type="KeyError"))
        graph.add_event(_event(TaskState.SUCCESS, 1, retries=1))

        node = graph.nodes["task-1"]
        assert (node.state, node.exception_type) == (TaskState.SUCCESS, "KeyError")

    @pytest.mark.parametrize("field", ["hostname", "queue", "routing_key"])
    def test_node_without_values_reports_none(self, field: str) -> None:
        graph = TaskGraph()
        graph.add_event(_event(TaskState.STARTED, 0))

        assert getattr(graph.nodes["task-1"], field) is None


WORKERS = ("celery@w0", "celery@w1", "celery@w2")
SKEWS = (-60, 0, 60)


def _attempts_history(
    skews: tuple[int, ...], *, ending: TaskState, from_main_process: bool
) -> list[TaskEvent]:
    """A task retried ``len(skews) - 1`` times, attempt ``n`` on worker ``n``.

    Worker ``n``'s clock is off by ``skews[n]`` seconds; attempts are 10 s
    apart. Each attempt reports its own worker, queue and exception class.
    The last attempt ends in ``ending``; with ``from_main_process`` its
    FAILURE comes from the worker's main process as Celery sends it there
    (``retries=0``, no worker fields).
    """
    events = [_event(TaskState.PENDING, 0, queue="q0", routing_key="q0")]
    last = len(skews) - 1
    for n, skew in enumerate(skews):
        at = 10 * (n + 1) + skew
        where = {"hostname": WORKERS[n], "queue": f"q{n}", "routing_key": f"q{n}"}
        events.append(_event(TaskState.STARTED, at, retries=n, **where))
        if n < last:
            events.append(
                _event(
                    TaskState.RETRY, at + 1, retries=n, exception_type=f"E{n}", **where
                )
            )
        elif ending == TaskState.SUCCESS:
            events.append(_event(TaskState.SUCCESS, at + 1, retries=n, **where))
        elif from_main_process:
            events.append(_event(TaskState.FAILURE, at + 1, exception_type=f"E{n}"))
        else:
            events.append(
                _event(
                    TaskState.FAILURE,
                    at + 1,
                    retries=n,
                    exception_type=f"E{n}",
                    **where,
                )
            )
    return events


def _skew_cases() -> list[tuple[tuple[int, ...], TaskState, bool]]:
    cases = []
    for attempts in (1, 2, 3):
        for skews in itertools.product(SKEWS, repeat=attempts):
            cases.append((skews, TaskState.SUCCESS, False))
            cases.append((skews, TaskState.FAILURE, False))
            cases.append((skews, TaskState.FAILURE, True))
    return cases


class TestTaskNodeFieldsUnderClockSkew:
    """The fields come from the attempt that decides the state, whatever the clocks say."""

    @pytest.mark.parametrize(("skews", "ending", "from_main_process"), _skew_cases())
    def test_fields_follow_the_final_attempt(
        self, skews: tuple[int, ...], ending: TaskState, from_main_process: bool
    ) -> None:
        events = _attempts_history(
            skews, ending=ending, from_main_process=from_main_process
        )
        last = len(skews) - 1
        if ending == TaskState.FAILURE:
            exception_type: str | None = f"E{last}"
        else:
            exception_type = f"E{last - 1}" if last else None
        expected = (ending, WORKERS[last], f"q{last}", f"q{last}", exception_type)
        orders = [events, events[::-1]]
        rng = random.Random(f"{skews}{ending}{from_main_process}")
        orders += [rng.sample(events, len(events)) for _ in range(3)]

        for order in orders:
            graph = TaskGraph()
            for event in order:
                graph.add_event(event)
            node = graph.nodes["task-1"]

            assert (
                node.state,
                node.hostname,
                node.queue,
                node.routing_key,
                node.exception_type,
            ) == expected
