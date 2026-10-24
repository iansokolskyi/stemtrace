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
an older stemtrace alongside a new one: the ``@field`` captures are the chord
flows recorded again with ``chord_body_first_id`` on their header events). ``--worker-process`` makes the worker
forget which tasks were already announced before each task, as a worker in
its own process does (the client and the in-process worker otherwise share
that memory, which hides a worker's republished PENDING, e.g. from
``task.replace()``).

``--truth-flows`` and ``--generate N --seed S [--slice A:B]`` write a different
file, ``chord_truth_flows.json`` (flow key -> events, truth, checks), and leave
``celery_flows.json`` alone: they capture the chord workflows (the existing ones,
or seeded random canvases) together with the *truth* about every chord, taken
from Celery's own canvas at send time (see ``_ChordRecorder``), not from what
stemtrace's graph draws. Keys read ``NAME@native|unlock|redis[+wp]@celeryX.Y.Z``.
``--backend-url redis://...`` runs the native flows on that result backend;
``--only NAME...`` picks flows (the ``_extra_flows`` ones too); a flow that
doesn't finish, or that Celery refuses, gets a fresh worker for the next one.

The graph tests compare names and topology, not task IDs or arrival order,
so a regenerated capture must still pass them; keep the ``@`` variants made
with older stemtrace or Celery versions (their keys say which).
"""

import json
import random
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import celery
from celery import Celery, chain, chord, current_task, group
from celery.canvas import Signature, _chain, _chord
from celery.contrib.testing.worker import start_worker
from celery.exceptions import Ignore, Reject
from celery.exceptions import TimeoutError as CeleryTimeout
from celery.signals import task_prerun

import stemtrace.library.signals as stemtrace_signals
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.signals import connect_signals, disconnect_signals
from stemtrace.transports.memory import MemoryTransport

OUTPUT = Path(__file__).with_name("celery_flows.json")
TRUTH_OUTPUT = Path(
    sys.argv[sys.argv.index("--out") + 1]
    if "--out" in sys.argv
    else Path(__file__).with_name("chord_truth_flows.json")
)

UNLOCK = "--unlock" in sys.argv
_BACKEND_URL = (
    sys.argv[sys.argv.index("--backend-url") + 1]
    if "--backend-url" in sys.argv
    else None
)
_BACKEND = _BACKEND_URL or (
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


@app.task(name="flows.step")
def step(*_args: Any) -> int:
    """Do nothing (a node of a generated canvas: header, body or chain step)."""
    return 1


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


@app.task(name="flows.unstorable")
def unstorable() -> object:
    """Return a result json can't encode: storing it fails after the chain moved on."""
    return object()


@app.task(name="flows.ignore")
def ignore() -> int:
    """Raise Ignore: it never reports a chord part, so the chord never completes."""
    raise Ignore()


@app.task(bind=True, name="flows.ignore_done")
def ignore_done(self: Any) -> int:
    """Report its own result, then raise Ignore: the chord still completes."""
    self.backend.mark_as_done(self.request.id, 5, request=self.request)
    raise Ignore()


@app.task(name="flows.reject")
def reject() -> int:
    """Raise Reject without requeue: the message is dropped, the chord never completes."""
    raise Reject("bad input", requeue=False)


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


@app.task(bind=True, name="flows.replace_delayed")
def replace_delayed(self: Any) -> Any:
    """Replace this task with a task sent with a one-second countdown."""
    raise self.replace(add.si(1, 1).set(countdown=1))


@app.task(bind=True, name="flows.outer_delayed")
def outer_delayed(self: Any) -> Any:
    """Replace this task with a chain whose first task replaces itself with a delay."""
    raise self.replace(chain(replace_delayed.si(), add.si(2, 2)))


@app.task(bind=True, name="flows.page_delayed")
def page_delayed(self: Any, page: int) -> Any:
    """Fetch pages 0..2, each next page sent by replacing this task, one second later."""
    if page < 2:
        raise self.replace(page_delayed.si(page + 1).set(countdown=1))
    return page


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
        # Celery stamps the last tsum with the first group's ID.
        "chain_groups_tasks": lambda: chain(
            group(add.si(1, 1), add.si(2, 2), add.si(3, 3)),
            tsum.s(),
            group(add.si(4, 4), add.si(5, 5)),
            tsum.s(),
            group(add.si(6, 6), add.si(7, 7)),
            tsum.s(),
        ),
        # The last tsum and the add below it are chord 2's callback and the
        # step after it; Celery stamps both with the first group's ID.
        "chain_groups_tasks_add": lambda: chain(
            group(add.si(1, 1), add.si(2, 2)),
            tsum.s(),
            group(add.si(4, 4), add.si(5, 5)),
            tsum.s(),
            add.s(10),
        ),
        "chain_failed_head": lambda: chain(fail.si(), add.s(1), add.s(2)),
        "chain_head_result_not_stored": lambda: chain(
            unstorable.si(), add.si(1, 1), add.si(2, 2)
        ),
        "chord_chain_header_failed_head": lambda: chord(
            [chain(fail.si(), add.s(1)), chain(add.s(1, 1), add.s(1))], tsum.s()
        ),
        "chord_ignored_header": lambda: chord([ignore.s(), add.s(1, 1)], tsum.s()),
        "chord_ignored_header_marked_done": lambda: chord(
            [ignore_done.s(), add.s(1, 1)], tsum.s()
        ),
        "chord_rejected_header": lambda: chord([reject.s(), add.s(1, 1)], tsum.s()),
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
        # task.replace() with a countdown on the replacement, in each position
        # (--worker-process): the replacement's ETA is not the task's own.
        "replace_delayed_root": lambda: replace_delayed.s(),
        "replace_delayed_mid_chain": lambda: chain(
            add.s(1, 1), replace_delayed.si(), add.si(7, 7)
        ),
        "replace_delayed_header": lambda: chord(
            [replace_delayed.si(), add.si(1, 1)], tsum.s()
        ),
        "replace_delayed_nested": lambda: outer_delayed.s(),
        "replace_delayed_same_name": lambda: page_delayed.s(0),
        "delayed_replace_delayed": lambda: replace_delayed.s().set(countdown=1),
    }


def _chords_flow(count: int) -> Callable[[], Signature]:
    """``chain(group, tsum)`` repeated ``count`` times (each pair is one chord)."""

    def build() -> Signature:
        steps: list[Signature] = [group(add.s(1, 1), add.s(2, 2)), tsum.s()]
        for _ in range(count - 1):
            steps += [group(add.s(1), add.s(2)), tsum.s()]
        return chain(*steps)

    return build


def _nested_chord_flows() -> dict[str, Callable[[], Signature]]:
    """Chains of chords, and chords nested in a header or a body."""

    return {
        # A generator header (the flow "chord") never completes on Celery 5.2.0.
        "chord_list": lambda: chord([add.s(i, i) for i in range(3)], tsum.s()),
        **{f"chain_chords_{n}": _chords_flow(n) for n in (1, 2, 3, 4)},
        "chord_chord_header": lambda: chord(
            [chord([add.s(1, 1), add.s(2, 2)], tsum.s()), add.s(3, 3)], tsum.s()
        ),
        "chain_chord_chord_body_add": lambda: chain(
            chord([add.s(1, 1), add.s(2, 2)], chord([tsum.s(), tsum.s()], tsum.s())),
            add.s(10),
        ),
        "chord_chain_chord_body": lambda: chord(
            [add.s(1, 1), add.s(2, 2)],
            chain(chord([tsum.s(), tsum.s()], tsum.s()), add.s(10)),
        ),
        "chain_add_chord_chord_body": lambda: chain(
            add.s(1, 1),
            chord([add.s(1), add.s(2)], chord([tsum.s(), tsum.s()], tsum.s())),
        ),
        "chord_depth3_body": lambda: chord(
            [add.s(1, 1), add.s(2, 2)],
            chord([tsum.s(), tsum.s()], chord([tsum.s(), tsum.s()], tsum.s())),
        ),
        "chord_chain_tsum_chord_body": lambda: chord(
            [add.s(1, 1), add.s(2, 2)],
            chain(tsum.s(), chord([add.s(1), add.s(2)], tsum.s())),
        ),
    }


def _extra_flows() -> dict[str, Callable[[], Signature]]:
    """Chord flows whose body never runs, and sweeps over chains of chords.

    ``q6_*``: a header fails, is revoked (expired), ignored or rejected.
    ``q1_*``: a header that spawns an unrelated task and fails. ``q3_*``:
    headers whose ID list is long. ``q2_L{n}_G{g}_T{t}``: a task, then ``n``
    pairs of a group of ``g`` tasks and a task, then ``t`` more tasks.
    """
    past = lambda: datetime.now(timezone.utc) - timedelta(seconds=30)  # noqa: E731
    flows: dict[str, Callable[[], Signature]] = {
        "q6_fail_task_body": lambda: chord([fail.s(), add.s(1, 1)], tsum.s()),
        "q6_fail_chain_body": lambda: chord(
            [fail.s(), add.s(1, 1)], chain(tsum.s(), add.s(10))
        ),
        "q6_fail_chain_SG": lambda: chain(
            add.s(1, 1), group(fail.si(), add.s(2)), tsum.s(), add.s(3)
        ),
        "q6_fail_chord_body": lambda: chord(
            [fail.s(), add.s(1, 1)], chord([tsum.s(), tsum.s()], tsum.s())
        ),
        "q6_fail_group_body": lambda: chord(
            [fail.s(), add.s(1, 1)], group(tsum.s(), tsum.s())
        ),
        "q6_revoked_task_body": lambda: chord(
            [add.si(1, 1).set(expires=past()), add.si(2, 2)], tsum.s()
        ),
        "q6_revoked_chain_body": lambda: chord(
            [add.si(1, 1).set(expires=past()), add.si(2, 2)],
            chain(tsum.s(), add.s(10)),
        ),
        "q6_ignored_task_body": lambda: chord([ignore.s(), add.s(1, 1)], tsum.s()),
        "q6_rejected_task_body": lambda: chord([reject.s(), add.s(1, 1)], tsum.s()),
        "q1_fail_header_subtask": lambda: chord(
            [add_with_subtask.s(1, 1), fail.s()], tsum.s()
        ),
        "q1_fail_header_subtask_SG": lambda: chain(
            add.s(1, 1),
            group(add_with_subtask.s(1), fail.si()),
            tsum.s(),
            add.s(3),
        ),
        "q3_big_header": lambda: chord([add.si(i, i) for i in range(200)], tsum.s()),
        "q3_chain_headers": lambda: chord(
            [chain(add.si(i, i), add.s(1), add.s(2)) for i in range(40)], tsum.s()
        ),
    }

    def sweep(pairs: int, size: int, tail: int) -> Callable[[], Signature]:
        def build() -> Signature:
            steps: list[Signature] = []
            for _ in range(pairs):
                steps += [group(*[step.s() for _ in range(size)]), step.s()]
            steps += [step.s() for _ in range(tail)]
            return chain(step.s(), *steps)

        return build

    for pairs in range(1, 6):
        for size in range(1, 5):
            for tail in range(3):
                flows[f"q2_L{pairs}_G{size}_T{tail}"] = sweep(pairs, size, tail)
    return flows


# Flows that contain a chord and complete. A header that fails, is ignored or
# is rejected never completes its chord, so there is no body to place.
_TRUTH_FLOWS = (
    "chord_list",
    "chain_a_group_tsum_add",
    "chain_a_group_tsum_add_add",
    "chord_retried_header",
    "chord_in_chord_body",
    "chord_with_chain_body",
    "chord_header_subtask",
    "chord_header_fanout_group",
    "chain_groups_tasks",
    "chain_groups_tasks_add",
    "chain_group_tsum_add",
    "chain_chord_header_subtask_add_add",
    "chain_single_header_chord",
    "chain_chord",
    "chord_group_body",
    "chain_group_group",
    "chain_replace_group_tsum",
    "chord_header_replace_same_name",
    "nested_replace_header",
    "chord_header_replace3",
    "chord_body_replace3",
    "replace_delayed_header",
    "chain_chords_1",
    "chain_chords_2",
    "chain_chords_3",
    "chain_chords_4",
    "chord_chord_header",
    "chain_chord_chord_body_add",
    "chord_chain_chord_body",
    "chain_add_chord_chord_body",
    "chord_depth3_body",
    "chord_chain_tsum_chord_body",
)


def _kind(sig: Any) -> str:
    """``chord``, ``chain``, ``group`` or ``task`` (a plain signature)."""
    if isinstance(sig, _chord):
        return "chord"
    if isinstance(sig, _chain):
        return "chain"
    if isinstance(sig, group):
        return "group"
    return "task"


def _header_kind(kinds: list[str]) -> str:
    """The kind all header members share, else ``mixed:`` and the kinds present."""
    unique = sorted(set(kinds))
    return unique[0] if len(unique) == 1 else "mixed:" + "+".join(unique)


def _stamp(chord_option: Any) -> str | None:
    """The task ID of a ``chord`` option (a frozen body signature or its dict)."""
    if chord_option is None:
        return None
    options = getattr(chord_option, "options", None)
    if options is None and isinstance(chord_option, dict):
        options = chord_option.get("options")
    return options.get("task_id") if isinstance(options, dict) else None


def _body_truth(body: Any) -> dict[str, Any]:
    """Where a frozen chord body starts: ``callback_kind`` and ``callback_id``.

    ``task``: ``callback_id`` is the first task's ID. ``chord_group``: the body
    (or its first step) is a chord; ``callback_id`` is that chord's header group
    ID. ``group``: the body (or its first step) is a group; ``callback_id`` is
    the group's ID and ``callback_member_ids`` its tasks' IDs.
    """
    first = body
    if _kind(body) == "chain":
        # Celery 5.2.0 and 5.6.3 _chain.freeze: _frozen = (steps, results), reversed.
        first = body._frozen[0][-1]
    first_kind = _kind(first)
    return {
        "body_first_kind": first_kind,
        "callback_kind": {"task": "task", "chord": "chord_group", "group": "group"}[
            first_kind
        ],
        "callback_id": first.options["task_id"] if first_kind == "group" else first.id,
        "callback_member_ids": [t.id for t in first.tasks]
        if first_kind == "group"
        else None,
    }


def _sender() -> dict[str, Any] | None:
    """The worker task that is sending a chord now (None: the client sends it).

    ``group`` is the sender's ``request.group``: for the header task that
    completes a chord (native backends) it is that chord's header group ID;
    ``unlock_group`` is a ``celery.chord_unlock`` sender's first argument.
    """
    if not current_task:
        return None
    request = current_task.request
    args = request.args or ()
    return {
        "task_id": request.id,
        "name": current_task.name,
        "group": request.group,
        "unlock_group": args[0]
        if current_task.name == "celery.chord_unlock" and args
        else None,
    }


def _resolve_callbacks(chords: list[dict[str, Any]]) -> None:
    """Settle ``callback_id`` of chords whose body starts with a chord (or group).

    A body that starts with a chord is serialized into the header tasks'
    messages and rebuilt when the header completes. ``_chord.apply_async``
    then builds the inner header group with a new ID (the dict carries no
    group ``task_id``), so the ID frozen at the outer ``run``
    (``callback_id_frozen``) never appears again. The callback's ID is the
    header group ID of the chord the completing sender sends next: found by
    its ``request.group`` (native) or its ``celery.chord_unlock`` argument.
    Left None when that chord never ran.
    """
    for chord_truth in chords:
        chord_truth["callback_id_frozen"] = chord_truth["callback_id"]
        if chord_truth["callback_kind"] != "chord_group":
            continue
        gid = chord_truth["group_id"]
        chord_truth["callback_id"] = next(
            (
                other["group_id"]
                for other in chords
                if other["sent_by"] is not None
                and gid in (other["sent_by"]["group"], other["sent_by"]["unlock_group"])
            ),
            None,
        )


class _ChordRecorder:
    """Record each chord Celery runs: its header group ID and where its body starts.

    The truth comes from Celery's own canvas at send time, not from stemtrace's
    events or graph. ``_chord.run`` freezes the body (``body.freeze``) and the
    header (``header._freeze_group_tasks(group_id=..., chord=body)``) and then
    sends; the wrapper reads the frozen objects after it returns.

    Celery 5.6.3 ``canvas.py``: the header group's ID is ``header.options
    ['task_id']`` (``_chord.run``: ``group_id = header.options.get('task_id')
    or uuid()``). A header task's ``request.chord`` is the frozen body, so the
    ID Celery stamps as the callback is ``body.options['task_id']`` (kept here
    as ``stamped_callback_id``): a task body's own ID; for a chain body its
    *last* task's ID (``_chain.prepare_steps`` hands ``last_task_id`` to the
    last step, ``self.id = last_task_id``); for a chord body its header group
    ID (``_chord.freeze``: ``self.id = self.tasks.id``); for a group body its
    group ID, which 5.6.3 makes the enclosing header group's ID
    (``body.freeze(body_task_id, group_id=group_id)``, then
    ``_freeze_group_tasks``: ``gid = opts['task_id'] = group_id or uuid()``).
    Celery 5.2.0 ``_chord.run`` calls ``body.freeze(task_id, root_id=root_id)``
    with no ``group_id``, so a group body has an ID of its own there. The
    callback is the task that runs first after the header: the first step of
    the frozen body (``chain.prepare_steps`` upgrades ``group | task`` to a
    chord, so that step can be a chord, whose header group ID then names it).
    """

    def __init__(self) -> None:
        self.chords: list[dict[str, Any]] = []
        self._original_run: Callable[..., Any] | None = None

    def install(self) -> None:
        """Wrap ``_chord.run`` (once): the wrapper runs it, then records."""
        if self._original_run is not None:
            return
        original = _chord.run
        self._original_run = original
        recorder = self

        def run(chord_sig: Any, header: Any, body: Any, *args: Any, **kw: Any) -> Any:
            kinds = [_kind(task) for task in list(header.tasks)]
            result = original(chord_sig, header, body, *args, **kw)
            recorder.record(chord_sig, header, body, kinds, kw)
            return result

        _chord.run = run

    def uninstall(self) -> None:
        """Put the original ``_chord.run`` back."""
        if self._original_run is not None:
            _chord.run = self._original_run
            self._original_run = None

    def record(
        self,
        chord_sig: Any,
        header: Any,
        body: Any,
        kinds: list[str],
        kw: dict[str, Any],
    ) -> None:
        """Add one chord from the header and body ``run`` has just frozen."""
        group_id = header.options["task_id"]
        if any(known["group_id"] == group_id for known in self.chords):
            return
        enclosing = kw.get("chord") or chord_sig.options.get("chord")
        self.chords.append(
            {
                "seq": len(self.chords),
                "group_id": group_id,
                "header_kind": _header_kind(kinds),
                "header_members": [
                    {"kind": _kind(t), "id": t.id} for t in list(header.tasks)
                ],
                "body_kind": _kind(body),
                "stamped_callback_id": body.options.get("task_id"),
                "enclosing_chord_stamp": _stamp(enclosing),
                "sent_by": _sender(),
                **_body_truth(body),
            }
        )


# --- seeded random canvases -------------------------------------------------
# A spec is a tuple: ("t",), ("chain", [spec]), ("group", [spec]) or
# ("chord", [header member spec], body spec). Dimensions: header member kinds
# (task, chain, chord, group of tasks), body kinds (task, chain, group, chord;
# a chain body starts with a task, group or chord), chord nesting depth <= 3,
# 1-4 chords made by ``group | task`` pairs in a chain, an optional task
# before and after, at most _MAX_TASKS tasks.
_MAX_TASKS = 15
Spec = tuple[Any, ...]


def _leaves(spec: Spec) -> int:
    """How many tasks the spec holds."""
    match spec[0]:
        case "t":
            return 1
        case "chord":
            return sum(_leaves(m) for m in spec[1]) + _leaves(spec[2])
        case _:
            return sum(_leaves(m) for m in spec[1])


def _g_header_member(rng: random.Random, depth: int) -> Spec:
    kind = rng.choices(
        ["task", "chain", "chord", "group"], [5, 2, 2 if depth > 1 else 0, 1]
    )[0]
    if kind == "task":
        return ("t",)
    if kind == "chain":
        return ("chain", [("t",) for _ in range(rng.choice((2, 2, 3)))])
    if kind == "group":
        return ("group", [("t",), ("t",)])
    return _g_chord(rng, depth - 1)


def _g_body(rng: random.Random, depth: int) -> Spec:
    kind = rng.choices(
        ["task", "chain", "group", "chord"], [4, 3, 2, 2 if depth > 1 else 0]
    )[0]
    if kind == "task":
        return ("t",)
    if kind == "group":
        return ("group", [("t",), ("t",)])
    if kind == "chord":
        return _g_chord(rng, depth - 1)
    first = rng.choice(("task", "task", "group", "chord" if depth > 1 else "task"))
    head: Spec = ("t",)
    if first == "group":
        head = ("group", [("t",), ("t",)])
    elif first == "chord":
        head = _g_chord(rng, depth - 1)
    return ("chain", [head, ("t",)])


def _g_chord(rng: random.Random, depth: int) -> Spec:
    members = [_g_header_member(rng, depth) for _ in range(rng.choice((1, 2, 2, 3)))]
    return ("chord", members, _g_body(rng, depth))


def _g_canvas(rng: random.Random) -> Spec:
    family = rng.choice(("single", "single", "implicit", "implicit", "explicit"))
    pre: list[Spec] = [("t",)] if rng.random() < 0.4 else []
    post: list[Spec] = [("t",)] if rng.random() < 0.4 else []
    if family == "single":
        one = _g_chord(rng, 3)
        return ("chain", [*pre, one, *post]) if pre or post else one
    steps: list[Spec] = []
    if family == "implicit":
        for _ in range(rng.randint(1, 4)):
            members = [("t",) for _ in range(rng.choice((2, 2, 3)))]
            steps += [("group", members), ("t",)]
    else:
        steps = [_g_chord(rng, 2) for _ in range(rng.randint(1, 2))]
    return ("chain", [*pre, *steps, *post])


def generate_canvas(seed: int, index: int) -> Spec:
    """The ``index``-th canvas of ``seed`` (stable whatever other indexes run)."""
    rng = random.Random(f"{seed}:{index}")
    while True:
        spec = _g_canvas(rng)
        if _leaves(spec) <= _MAX_TASKS:
            return spec


def build_canvas(spec: Spec) -> Signature:
    """Turn a spec into Celery signatures."""
    match spec[0]:
        case "t":
            return step.s()
        case "chain":
            return chain(*[build_canvas(s) for s in spec[1]])
        case "group":
            return group(*[build_canvas(s) for s in spec[1]])
        case _:
            return chord([build_canvas(s) for s in spec[1]], build_canvas(spec[2]))


def describe_canvas(spec: Spec) -> str:
    """A readable form, e.g. ``chain(t, chord([t, t], group(t, t)))``."""
    match spec[0]:
        case "t":
            return "t"
        case "chord":
            members = ", ".join(describe_canvas(s) for s in spec[1])
            return f"chord([{members}], {describe_canvas(spec[2])})"
        case kind:
            return f"{kind}({', '.join(describe_canvas(s) for s in spec[1])})"


def _event_dict(event: Any) -> dict[str, Any]:
    """An event for ``celery_flows.json``; ``celery.chord_unlock`` also keeps its args."""
    assert isinstance(event, TaskEvent)
    fields = {
        "task_id",
        "name",
        "state",
        "timestamp",
        "parent_id",
        "root_id",
        "group_id",
        "chord_id",
        "chord_callback_id",
        "chord_body_first_id",
        "retries",
        "eta",
    }
    if event.name == "celery.chord_unlock":
        fields |= {"args", "kwargs"}
    return event.model_dump(mode="json", include=fields)


_UNLOCK_FLOWS = ("chord", "chain_a_group_tsum_add", "chord_with_chain_body")
# Chords Celery never completes: there is no result to wait for.
_NEVER_COMPLETE = ("chord_ignored_header", "chord_rejected_header")
_TERMINAL = frozenset(
    {TaskState.SUCCESS, TaskState.FAILURE, TaskState.IGNORED, TaskState.REJECTED}
)


def _settle() -> None:
    """Wait until no new events arrive for a while (subtasks run after)."""
    seen = -1
    while seen != len(MemoryTransport.events):
        seen = len(MemoryTransport.events)
        time.sleep(0.5)


def _wait_for_headers(count: int) -> None:
    """Wait until ``count`` headers have ended (a chord that never completes)."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ended = {
            e.task_id
            for e in MemoryTransport.events
            if isinstance(e, TaskEvent) and e.state in _TERMINAL
        }
        if len(ended) >= count:
            return
        time.sleep(0.1)
    raise TimeoutError(f"{count} headers didn't end within 60s")


def _forget_announced(**_: Any) -> None:
    """Act as a worker in its own process: it never saw the client's sends."""
    stemtrace_signals._pending_emitted.clear()


_FLOW_TIMEOUT_S = 45
_TRUTH_EVENT_FIELDS = {
    "task_id",
    "name",
    "state",
    "timestamp",
    "parent_id",
    "root_id",
    "group_id",
    "chord_id",
    "chord_callback_id",
    "chord_body_first_id",
    "retries",
    "eta",
}


def _truth_event_dict(event: Any) -> dict[str, Any]:
    """An event for ``chord_truth_flows.json``: set fields only, and the args of
    ``celery.chord_unlock`` (the group ID and body Celery sends it, when captured)."""
    assert isinstance(event, TaskEvent)
    fields = set(_TRUTH_EVENT_FIELDS)
    if event.name == "celery.chord_unlock":
        fields |= {"args", "kwargs"}
    data = event.model_dump(mode="json", include=fields)
    return {
        key: value
        for key, value in data.items()
        if value is not None and not (key == "retries" and value == 0)
    }


def _parent_role(
    parent: str | None, members: set[str], unlocks: set[str], sender: str | None
) -> str:
    """What the callback's reported parent is, relative to its chord."""
    if parent is None:
        return "none"
    if parent in members:
        return "header_member"
    if parent in unlocks:
        return "unlock"
    return "sender" if parent == sender else "other"


def _callback_seen(
    chord_truth: dict[str, Any], task_ids: set[str], group_ids: set[str]
) -> bool:
    """Whether the events show the callback ran (see ``_checks``)."""
    callback_id = chord_truth["callback_id"]
    if chord_truth["callback_kind"] == "task":
        return callback_id in task_ids
    if chord_truth["callback_kind"] == "group":
        return all(m in task_ids for m in chord_truth["callback_member_ids"])
    return callback_id is not None and callback_id in group_ids


def _checks(
    chords: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Compare each recorded chord with the events the same run published.

    ``header_seen``: an event carries the header group ID as ``group_id``.
    ``callback_seen``: the callback ran (a ``task`` callback has events, so do
    all of a ``group`` body's tasks, a ``chord_group`` callback's header group
    ID is some event's ``group_id``). The header members' events (a plain header
    task, or a chain's last task) name the chord Celery stamped:
    ``callbacks_named`` and ``chord_ids_named`` are what they carry;
    ``stamp_matches`` and ``chord_id_matches`` say whether that is
    ``stamped_callback_id`` and the chord's own header group ID (None when no
    member event names a chord). ``callback_parent_is``: the parent the events
    give a ``task`` callback: ``header_member``, this chord's ``unlock`` task,
    the ``sender`` of the chord (the task that was running when it was sent),
    ``none`` or ``other``. ``body_group_ids_seen``: the ``group_id`` values on a
    group body's task events.
    """
    task_ids = {e["task_id"] for e in events}
    group_ids = {e["group_id"] for e in events if "group_id" in e}
    checks = []
    for chord_truth in chords:
        gid = chord_truth["group_id"]
        # Header members as the graph sees them: the group ID and a chord
        # stamp or body field on one event. A header chain's own ID isn't one
        # (its last task is: ``header_members`` holds ``None`` for it).
        members = {
            e["task_id"]
            for e in events
            if e.get("group_id") == gid
            and ("chord_callback_id" in e or "chord_body_first_id" in e)
        }
        named = [
            e for e in events if e["task_id"] in members and "chord_callback_id" in e
        ]
        callbacks = sorted({e["chord_callback_id"] for e in named})
        chord_ids = sorted({e.get("chord_id") or "" for e in named})
        callback_id = chord_truth["callback_id"]
        unlocks = {
            e["task_id"]
            for e in events
            if e["name"] == "celery.chord_unlock"
            and isinstance(e.get("args"), list)
            and e["args"][:1] == [gid]
        }
        parent = next(
            (
                e["parent_id"]
                for e in events
                if e["task_id"] == callback_id and "parent_id" in e
            ),
            None,
        )
        sender = (chord_truth["sent_by"] or {}).get("task_id")
        is_task = chord_truth["callback_kind"] == "task"
        member_groups = (
            sorted(
                {
                    e["group_id"]
                    for e in events
                    if e["task_id"] in chord_truth["callback_member_ids"]
                    and "group_id" in e
                }
            )
            if chord_truth["callback_kind"] == "group"
            else None
        )
        checks.append(
            {
                "group_id": gid,
                "header_seen": gid in group_ids,
                "callback_seen": _callback_seen(chord_truth, task_ids, group_ids),
                "callbacks_named": callbacks,
                "chord_ids_named": chord_ids,
                "callback_parent": parent if is_task else None,
                "callback_parent_is": (
                    _parent_role(parent, members, unlocks, sender) if is_task else None
                ),
                "body_group_ids_seen": member_groups,
                "stamp_matches": callbacks == [chord_truth["stamped_callback_id"]]
                if named
                else None,
                "chord_id_matches": chord_ids == [gid] if named else None,
            }
        )
    return checks


def _compact_unlock_args(events: list[dict[str, Any]]) -> None:
    """Keep a ``celery.chord_unlock`` task's args and kwargs on its first
    PENDING and first STARTED event only (every poll attempt repeats them)."""
    seen: set[tuple[str, str]] = set()
    for event in events:
        if event["name"] != "celery.chord_unlock":
            continue
        marker = (event["task_id"], event["state"])
        if marker in seen or event["state"] not in ("PENDING", "STARTED"):
            event.pop("args", None)
            event.pop("kwargs", None)
        seen.add(marker)


def _forget_unseen_callbacks(
    chords: list[dict[str, Any]], checks: list[dict[str, Any]]
) -> None:
    """Set ``callback_id`` to None where the body never ran (``callback_seen``).

    ``callback_id_frozen`` keeps the ID Celery froze; a chord that never ran
    its body has no callback to draw, which is not the same as a missing one.
    """
    for chord_truth, check in zip(chords, checks, strict=True):
        if not check["callback_seen"]:
            chord_truth["callback_id"] = None


def _write_truth(captures: dict[str, dict[str, Any]]) -> None:
    lines = [
        f" {json.dumps(k)}: {json.dumps(v, separators=(',', ':'))}"
        for k, v in sorted(captures.items())
    ]
    TRUTH_OUTPUT.write_text("{\n" + ",\n".join(lines) + "\n}\n")


def recheck() -> None:
    """Recompute ``checks`` of every stored capture from its events and truth."""
    captures = json.loads(TRUTH_OUTPUT.read_text())
    for capture in captures.values():
        _compact_unlock_args(capture["events"])
        capture["checks"] = _checks(capture["truth"]["chords"], capture["events"])
    _write_truth(captures)


def _capture_truth(
    flows: dict[str, tuple[str, Callable[[], Signature]]],
) -> dict[str, dict[str, Any]]:
    """Run each flow and return its events, the truth about its chords and checks."""
    recorder = _ChordRecorder()
    recorder.install()
    captures: dict[str, dict[str, Any]] = {}
    version = f"celery{celery.__version__}"
    backend = "redis" if _BACKEND_URL else "unlock" if UNLOCK else "native"
    wp = "+wp" if "--worker-process" in sys.argv else ""
    # solo runs the header tasks one after another in the worker's only thread:
    # a chord whose counter completes early blocks that thread joining results
    # that only the same thread could produce (a hang on e.g. a chord nested in a
    # header). threads runs 4 at a time and avoids that.
    pool = _arg_value("--pool") or "solo"
    connect_signals(MemoryTransport())
    if wp:
        task_prerun.connect(_forget_announced, weak=False)

    def start() -> Any:
        return start_worker(
            app,
            pool=pool,
            concurrency=4 if pool == "threads" else 1,
            perform_ping_check=False,
        )

    workers = ExitStack()
    workers.enter_context(start())
    try:
        for name, (desc, build) in flows.items():
            MemoryTransport.clear()
            recorder.chords.clear()
            status = "ok"
            error = None
            try:
                build().apply_async().get(
                    timeout=_FLOW_TIMEOUT_S,
                    propagate=False,
                    disable_sync_subtasks=False,
                )
            except CeleryTimeout:
                status = "timeout"
            except Exception as exc:  # noqa: BLE001  # Celery refused the canvas
                status, error = "rejected", f"{type(exc).__name__}: {exc}"
            _settle()
            events = [
                _truth_event_dict(e)
                for e in MemoryTransport.events
                if isinstance(e, TaskEvent)
            ]
            if status != "ok":
                # A flow that never finished leaves tasks, a chord counter or a
                # retry timer behind: the next flow gets a worker of its own.
                workers.close()
                app.control.purge()
                workers = ExitStack()
                workers.enter_context(start())
            _compact_unlock_args(events)
            chords = list(recorder.chords)
            _resolve_callbacks(chords)
            checks = _checks(chords, events)
            _forget_unseen_callbacks(chords, checks)
            captures[f"{name}@{backend}{wp}@{version}"] = {
                "desc": desc,
                "status": status,
                "error": error,
                "pool": pool,
                "events": events,
                "truth": {"chords": chords},
                "checks": checks,
            }
            print(f"{name}: {status}, {len(chords)} chords, {len(events)} events")
    finally:
        workers.close()
        disconnect_signals()
        recorder.uninstall()
    return captures


def _arg_value(flag: str) -> str | None:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def _names_after(flag: str) -> list[str]:
    """The arguments that follow ``flag``, up to the next flag."""
    names: list[str] = []
    for arg in sys.argv[sys.argv.index(flag) + 1 :]:
        if arg.startswith("--"):
            break
        names.append(arg)
    return names


def _builder(spec: Spec) -> Callable[[], Signature]:
    return lambda: build_canvas(spec)


def truth_main() -> None:
    """Capture chord flows with truth: ``--truth-flows`` or ``--generate N --seed S``."""
    flows: dict[str, tuple[str, Callable[[], Signature]]] = {}
    if "--truth-flows" in sys.argv:
        known = {**_flows(), **_nested_chord_flows(), **_extra_flows()}
        names = _names_after("--only") if "--only" in sys.argv else list(_TRUTH_FLOWS)
        flows.update({n: (n, known[n]) for n in names})
    if "--generate" in sys.argv:
        count = int(_arg_value("--generate") or 0)
        seed = int(_arg_value("--seed") or 0)
        low, high = 0, count
        if "--slice" in sys.argv:
            low, high = (int(x) for x in (_arg_value("--slice") or "0:0").split(":"))
        for index in range(low, min(high, count)):
            spec = generate_canvas(seed, index)
            flows[f"gen{seed}_{index:03d}"] = (describe_canvas(spec), _builder(spec))
    captures = _capture_truth(flows)
    if TRUTH_OUTPUT.exists():
        captures = {**json.loads(TRUTH_OUTPUT.read_text()), **captures}
    _write_truth(captures)


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
                if name in _NEVER_COMPLETE:
                    _wait_for_headers(2)
                else:
                    result.get(timeout=60, propagate=False, disable_sync_subtasks=False)
                _settle()
                key = f"{name}@chord_unlock{suffix}" if UNLOCK else f"{name}{suffix}"
                captures[key] = [
                    _event_dict(e)
                    for e in MemoryTransport.events
                    if isinstance(e, TaskEvent)
                ]
                _compact_unlock_args(captures[key])
    finally:
        disconnect_signals()
    if UNLOCK or only:
        captures = {**json.loads(OUTPUT.read_text()), **captures}
    OUTPUT.write_text(json.dumps(captures, indent=1) + "\n")
    for name, events in captures.items():
        print(f"{name}: {len(events)} events")


if __name__ == "__main__":
    if "--recheck" in sys.argv:
        recheck()
    elif "--truth-flows" in sys.argv or "--generate" in sys.argv:
        truth_main()
    else:
        main()
