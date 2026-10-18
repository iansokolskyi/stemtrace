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

The graph tests compare names and topology, not task IDs or arrival order,
so a regenerated capture must still pass them; keep the ``@`` variants made
with older stemtrace or Celery versions (their keys say which).
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


@app.task(bind=True, name="flows.outer")
def outer(self: Any) -> None:
    """Replace this task with a chain whose first task replaces itself too."""
    raise self.replace(chain(inner.si(), add.si(5, 5)))


@app.task(bind=True, name="flows.inner")
def inner(self: Any) -> None:
    """Replace this task with a two-task chain."""
    raise self.replace(chain(add.si(1, 1), add.si(2, 2)))


@app.task(bind=True, name="flows.outer_g")
def outer_g(self: Any) -> None:
    """Replace this task with a chain whose first task replaces itself by a group."""
    raise self.replace(chain(replace_with_group.si(), tsum.s()))


@app.task(bind=True, name="flows.outer_last")
def outer_last(self: Any) -> None:
    """Replace this task with a chain whose last task replaces itself too."""
    raise self.replace(chain(add.si(1, 1), replace_with_chain.si()))


@app.task(name="flows.fetch")
def fetch(page: int) -> int:
    """Fetch one page."""
    return page


@app.task(bind=True, name="flows.prec")
def prec(self: Any, page: int, pages: int) -> int:
    """Paginate recursively: replace with chain(fetch(page), prec(page + 1))."""
    if page < pages:
        raise self.replace(chain(fetch.si(page), prec.si(page + 1, pages)))
    return page


@app.task(bind=True, name="flows.mid_replacer")
def mid_replacer(self: Any) -> None:
    """Replace this task with a three-task chain of different task names."""
    raise self.replace(chain(add.si(1, 1), tsum.si([1, 2]), fetch.si(3)))


@app.task(name="flows.send_with_parent")
def send_with_parent(parent_id: str) -> None:
    """Send a task naming another, earlier task as its parent."""
    add.apply_async((2, 2), parent_id=parent_id)


def _after_earlier_task(build: Callable[[str], Signature]) -> Signature:
    """Run a task first, then build the flow from its ID."""
    earlier = add.delay(1, 1)
    earlier.get(timeout=60)
    return build(earlier.id)


@app.task(bind=True, name="flows.paginate_to")
def paginate_to(self: Any, page: int, pages: int) -> int:
    """Fetch page 0 by replacing itself with a chain of pages 1..pages."""
    if page == 0:
        raise self.replace(chain(paginate_to.si(i, pages) for i in range(1, pages + 1)))
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


_retried_later: set[str] = set()


@app.task(bind=True, name="flows.retry_later")
def retry_later(self: Any, x: int, y: int) -> int:
    """Add, retrying once with a one-second countdown on its first run."""
    if self.request.id not in _retried_later:
        _retried_later.add(self.request.id)
        raise self.retry(countdown=1)
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
        "chain_replace3": lambda: chain(add.s(1, 1), paginate_to.si(0, 3)),
        "chain_replace4": lambda: chain(add.s(1, 1), paginate_to.si(0, 4)),
        "chain_replace_group": lambda: chain(add.s(1, 1), replace_with_group.si()),
        # Celery defaults root_id to the task's own ID outside a worker.
        "client_explicit_parent": lambda: _after_earlier_task(
            lambda parent: add.s(2, 2).set(parent_id=parent)
        ),
        "worker_explicit_parent": lambda: _after_earlier_task(
            lambda parent: send_with_parent.s(parent)
        ),
        "chord_header_replace_same_name": lambda: chord(
            [paginate.si(0), add.si(1, 1)], tsum.s()
        ),
        "replace_group": lambda: replace_with_group.s(),
        "chain_replace_group_tsum": lambda: chain(replace_with_group.s(), tsum.s()),
        "group_of_chains_link_spawns": lambda: group(
            chain(add_with_subtask.s(1, 1), add.s(1)), chain(add.s(2, 2), add.s(2))
        ),
        # task.replace() inside a replacement, in each position, and
        # replacements of 3+ tasks in each position (--worker-process).
        "nested_replace": lambda: chain(add.s(1, 1), outer.si()),
        "nested_replace_root": lambda: outer.s(),
        "nested_replace_header": lambda: chord([outer.si(), add.si(1, 1)], tsum.s()),
        "nested_replace_group": lambda: chain(add.s(1, 1), outer_g.si()),
        "nested_replace_group_root": lambda: outer_g.s(),
        "nested_replace_last": lambda: chain(add.s(1, 1), outer_last.si()),
        "recursive_replace": lambda: chain(add.s(1, 1), prec.si(0, 3)),
        "recursive_replace_root": lambda: prec.s(0, 3),
        "two_replacers": lambda: chain(
            add.s(1, 1), replace_with_chain.si(), replace_with_chain.si()
        ),
        "two_replacers_root": lambda: chain(
            replace_with_chain.si(), replace_with_chain.si()
        ),
        "mid_chain_replace3": lambda: chain(
            add.s(1, 1), paginate_to.si(0, 3), add.si(7, 7)
        ),
        "mid_chain_replace_names": lambda: chain(
            add.s(1, 1), mid_replacer.si(), add.si(7, 7)
        ),
        "chord_header_replace3": lambda: chord(
            [paginate_to.si(0, 3), add.si(1, 1)], tsum.s()
        ),
        "chord_body_replace3": lambda: chord(
            [add.si(1, 1), add.si(2, 2)], paginate_to.si(0, 3)
        ),
        "group_chain_replace3": lambda: group(
            chain(add.si(1, 1), paginate_to.si(0, 3)), add.si(2, 2)
        ),
        "chain_replace12": lambda: chain(add.s(1, 1), paginate_to.si(0, 12)),
        # Sent with a countdown: the events carry its ETA (eta is captured from here on).
        "countdown": lambda: add.s(1, 1).set(countdown=2),
        "countdown_retry_countdown": lambda: retry_later.s(1, 1).set(countdown=1),
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
            "eta",
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
