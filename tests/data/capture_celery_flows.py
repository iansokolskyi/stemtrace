"""Capture the stemtrace events real Celery canvas workflows emit.

Runs an in-process Celery worker (memory broker, ``cache+memory`` result
backend) with stemtrace's signal handlers publishing to ``MemoryTransport``,
runs each workflow below and writes the events, in arrival order, to
``celery_flows.json`` next to this file. The graph tests replay them.

Regenerate with: ``uv run python tests/data/capture_celery_flows.py``
"""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from celery import Celery, chain, chord, group
from celery.canvas import Signature
from celery.contrib.testing.worker import start_worker

from stemtrace.core.events import TaskEvent
from stemtrace.library.signals import connect_signals, disconnect_signals
from stemtrace.transports.memory import MemoryTransport

OUTPUT = Path(__file__).with_name("celery_flows.json")

app = Celery("flows", broker="memory://", backend="cache+memory://")
app.conf.update(task_always_eager=False, worker_hijack_root_logger=False)

_retried: set[str] = set()


@app.task(name="flows.add")
def add(x: int, y: int) -> int:
    """Add two numbers."""
    return x + y


@app.task(name="flows.tsum")
def tsum(values: list[int]) -> int:
    """Sum a list (a chord callback)."""
    return sum(values)


@app.task(name="flows.spawn_group")
def spawn_group(n: int) -> int:
    """Start a group from inside a task."""
    group(add.s(i, i) for i in range(n)).apply_async()
    return n


@app.task(bind=True, name="flows.flaky_add", max_retries=2)
def flaky_add(self: Any, x: int, y: int) -> int:
    """Add, failing (and retrying) once on its first run."""
    if self.request.id not in _retried:
        _retried.add(self.request.id)
        raise self.retry(countdown=0)
    return x + y


def _flows() -> dict[str, Callable[[], Signature]]:
    return {
        "chain": lambda: chain(add.s(1, 1), add.s(2), add.s(3)),
        "group": lambda: group(add.s(i, i) for i in range(3)),
        "chord": lambda: chord((add.s(i, i) for i in range(3)), tsum.s()),
        "chain_a_group_tsum_add": lambda: chain(
            add.s(1, 1), group(add.s(1), add.s(2)), tsum.s(), add.s(10)
        ),
        "chain_a_group_tsum_add_add": lambda: chain(
            add.s(1, 1), group(add.s(1), add.s(2)), tsum.s(), add.s(10), add.s(20)
        ),
        "chain_group_nested": lambda: chain(add.s(1, 1), group(add.s(1), add.s(2))),
        "group_spawned_in_task": lambda: spawn_group.s(3),
        "group_of_chains": lambda: group(
            chain(add.s(1, 1), add.s(1)), chain(add.s(2, 2), add.s(2))
        ),
        "chain_group_tsum_add": lambda: chain(
            group(add.s(1, 1), add.s(2, 2)), tsum.s(), add.s(10)
        ),
        "chord_retried_header": lambda: chord(
            [flaky_add.s(1, 1), add.s(2, 2)], tsum.s()
        ),
        "chord_in_chord_body": lambda: chord(
            [add.s(1, 1), add.s(2, 2)],
            chord([tsum.s(), tsum.s()], tsum.s()),
        ),
        "chord_with_chain_body": lambda: chord(
            [add.s(1, 1), add.s(2, 2)], chain(tsum.s(), add.s(10))
        ),
    }


def _event_dict(event: Any) -> dict[str, Any]:
    assert isinstance(event, TaskEvent)
    return event.model_dump(
        mode="json",
        include={
            "task_id",
            "name",
            "state",
            "timestamp",
            "parent_id",
            "root_id",
            "group_id",
            "chord_id",
            "chord_callback_id",
            "retries",
        },
    )


def main() -> None:
    """Run every workflow and write the captured events."""
    captures: dict[str, list[dict[str, Any]]] = {}
    connect_signals(MemoryTransport())
    try:
        with start_worker(app, pool="solo", perform_ping_check=False):
            for name, build in _flows().items():
                MemoryTransport.clear()
                result = build().apply_async()
                result.get(timeout=30, disable_sync_subtasks=False)
                if name == "group_spawned_in_task":
                    # The spawned group runs after its parent returns.
                    time.sleep(1)
                captures[name] = [
                    _event_dict(e)
                    for e in MemoryTransport.events
                    if isinstance(e, TaskEvent)
                ]
    finally:
        disconnect_signals()
    OUTPUT.write_text(json.dumps(captures, indent=1) + "\n")
    for name, events in captures.items():
        print(f"{name}: {len(events)} events")


if __name__ == "__main__":
    main()
