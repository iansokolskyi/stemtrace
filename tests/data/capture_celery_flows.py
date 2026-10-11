"""Capture the stemtrace events real Celery canvas workflows emit.

Runs an in-process Celery worker (memory broker, ``cache+memory`` result
backend) with stemtrace's signal handlers publishing to ``MemoryTransport``,
runs each workflow below and writes the events, in arrival order, to
``celery_flows.json`` next to this file. The graph tests replay them.

Regenerate with: ``uv run python tests/data/capture_celery_flows.py``. The
``@celery5.2`` captures come from running this script with Celery 5.2.0 (the
lowest supported version) installed: ``--only NAME... --suffix @celery5.2``. Then
``uv run --with sqlalchemy python tests/data/capture_celery_flows.py --unlock``
adds the chord flows again (keys ending ``@chord_unlock``) with a
``db+sqlite`` backend, which has no native chord support: Celery then runs
the callback from its ``celery.chord_unlock`` polling task. ``--only NAME...``
captures just those flows and merges them into the existing file;
``--suffix S`` stores them as ``NAME + S`` (e.g. to keep a capture made with
an older stemtrace alongside a new one). ``--worker-process`` makes the worker
forget which tasks were already announced before each task, as a worker in
its own process does (the client and the in-process worker otherwise share
that memory, which hides a worker's republished PENDING, e.g. from
``task.replace()``).
"""

import json
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from celery import Celery, chain, chord, group
from celery.canvas import Signature
from celery.contrib.testing.worker import start_worker
from celery.signals import task_prerun

import stemtrace.library.signals as stemtrace_signals
from stemtrace.core.events import TaskEvent
from stemtrace.library.signals import connect_signals, disconnect_signals
from stemtrace.transports.memory import MemoryTransport

OUTPUT = Path(__file__).with_name("celery_flows.json")

UNLOCK = "--unlock" in sys.argv
_BACKEND = (
    f"db+sqlite:///{tempfile.mkdtemp()}/results.sqlite" if UNLOCK else "cache+memory://"
)
app = Celery("flows", broker="memory://", backend=_BACKEND)
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


@app.task(name="flows.add_with_subtask")
def add_with_subtask(x: int, y: int) -> int:
    """Add, also sending an unrelated subtask (fire-and-forget)."""
    add.delay(x, 100)
    return x + y


@app.task(bind=True, name="flows.replace_with_chain")
def replace_with_chain(self: Any) -> None:
    """Replace this task with a chain (its last task reuses this task's ID)."""
    raise self.replace(chain(add.s(1, 1), add.s(2)))


@app.task(bind=True, name="flows.replace_with_group")
def replace_with_group(self: Any) -> None:
    """Replace this task with a group."""
    raise self.replace(group(add.s(1, 1), add.s(2, 2)))


@app.task(bind=True, name="flows.replace_with_task")
def replace_with_task(self: Any) -> None:
    """Replace this task with a single task (which takes over its ID)."""
    raise self.replace(add.s(1, 1))


@app.task(bind=True, name="flows.paginate")
def paginate(self: Any, page: int) -> int:
    """Fetch page 0 by replacing itself with the next two pages (same task)."""
    if page == 0:
        raise self.replace(chain(paginate.si(1), paginate.si(2)))
    return page


@app.task(name="flows.fail")
def fail() -> int:
    """Raise, so a chord using it as a header never runs its body."""
    raise ValueError("header failed")


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
        "chord_header_subtask": lambda: chord(
            [add_with_subtask.s(1, 1), add.s(2, 2)], tsum.s()
        ),
        "chord_header_fanout_group": lambda: chord(
            [spawn_group.s(2), add.s(1, 1)], tsum.s()
        ),
        "chord_failed_header": lambda: chord([fail.s(), add.s(1, 1)], tsum.s()),
        "chord_failed_header_subtask": lambda: chord(
            [add_with_subtask.s(1, 1), fail.s()], tsum.s()
        ),
        "chain_chord_header_subtask_add_add": lambda: chain(
            add.s(1, 1),
            group(add_with_subtask.s(1), add.s(2)),
            tsum.s(),
            add.s(10),
            add.s(20),
        ),
        "chain_group_of_chains": lambda: chain(
            add.s(1, 1),
            group(chain(add.s(1), add.s(1)), chain(add.s(2), add.s(2))),
        ),
        "chain_single_header_chord": lambda: chain(
            add.s(1, 1), chord([add.s(1)], tsum.s())
        ),
        "chain_chord": lambda: chain(
            add.s(1, 1), chord([add.s(1), add.s(2)], tsum.s())
        ),
        "group_chain_and_task": lambda: group(
            chain(add.s(1, 1), add.s(1)), add.s(2, 2)
        ),
        "chord_group_body": lambda: chord(
            [add.s(1, 1), add.s(2, 2)], group(tsum.s(), tsum.s())
        ),
        "chain_group_group": lambda: chain(
            group(add.s(1, 1), add.s(2, 2)), group(tsum.s(), tsum.s())
        ),
        "replace_chain": lambda: replace_with_chain.s(),
        "replace_task": lambda: replace_with_task.s(),
        "replace_same_name": lambda: paginate.s(0),
        "chain_replace_same_name": lambda: chain(add.s(1, 1), paginate.si(0)),
        "chord_header_replace_same_name": lambda: chord(
            [paginate.si(0), add.si(1, 1)], tsum.s()
        ),
        "replace_group": lambda: replace_with_group.s(),
        "chain_replace_group_tsum": lambda: chain(replace_with_group.s(), tsum.s()),
        "group_of_chains_link_spawns": lambda: group(
            chain(add_with_subtask.s(1, 1), add.s(1)), chain(add.s(2, 2), add.s(2))
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


_UNLOCK_FLOWS = ("chord", "chain_a_group_tsum_add", "chord_with_chain_body")


def _settle() -> None:
    """Wait until no new events arrive for a while (subtasks run after)."""
    seen = -1
    while seen != len(MemoryTransport.events):
        seen = len(MemoryTransport.events)
        time.sleep(0.5)


def _forget_announced(**_: Any) -> None:
    """Act as a worker in its own process: it never saw the client's sends."""
    stemtrace_signals._pending_emitted.clear()


def main() -> None:
    """Run the workflows and write (or, with --unlock, add) their events."""
    flows = _flows()
    suffix = sys.argv[sys.argv.index("--suffix") + 1] if "--suffix" in sys.argv else ""
    only = None
    if "--only" in sys.argv:
        only = [
            a
            for a in sys.argv[sys.argv.index("--only") + 1 :]
            if not a.startswith("--")
        ]
        if "--suffix" in sys.argv:
            only = [a for a in only if a != suffix]
    if UNLOCK:
        flows = {name: flows[name] for name in _UNLOCK_FLOWS}
    elif only:
        flows = {name: flows[name] for name in only}
    captures: dict[str, list[dict[str, Any]]] = {}
    connect_signals(MemoryTransport())
    if "--worker-process" in sys.argv:
        task_prerun.connect(_forget_announced, weak=False)
    try:
        with start_worker(app, pool="solo", perform_ping_check=False):
            for name, build in flows.items():
                MemoryTransport.clear()
                result = build().apply_async()
                result.get(timeout=60, propagate=False, disable_sync_subtasks=False)
                _settle()
                key = f"{name}@chord_unlock" if UNLOCK else f"{name}{suffix}"
                captures[key] = [
                    _event_dict(e)
                    for e in MemoryTransport.events
                    if isinstance(e, TaskEvent)
                ]
    finally:
        disconnect_signals()
    if UNLOCK or only:
        captures = {**json.loads(OUTPUT.read_text()), **captures}
    OUTPUT.write_text(json.dumps(captures, indent=1) + "\n")
    for name, events in captures.items():
        print(f"{name}: {len(events)} events")


if __name__ == "__main__":
    main()
