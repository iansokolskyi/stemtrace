"""Golden snapshot of the per-node values derived from a task's events.

Replays every recorded flow (``tests/data/celery_flows.json`` and
``chord_truth_flows.json``) in several variants and digests, per flow and
variant, every value a consumer reads from a ``TaskNode``: state, event order,
times, describing fields, ETA, queue wait and runtimes. The digests are
committed in ``tests/data/attribution_golden.json``; a refactor of how events
are attributed to runs must leave every one of them unchanged.

Variants: the captured order; seeded shuffles; each single event dropped (a
seeded sample of 40 in flows over 100 events); a
seeded clock offset per task; seeded worker nodenames (the captures have none,
so the per-worker run pairing would otherwise go unexercised); synthetic\ninputs no flow has (a late retry PENDING, a STARTED without a worker, a late\nIGNORED). The expected
values are main's own output, not ground truth: this guards equivalence only.

Regenerate (only when a behavior change is intended): ``uv run python -m
tests.attribution_golden``.
"""

import hashlib
import json
import random
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.graph import TaskGraph, TaskNode

DATA = Path(__file__).parent / "data"
GOLDEN = DATA / "attribution_golden.json"
SHUFFLES = 3
_DROP_ALL_UP_TO = 100
_DROP_SAMPLE = 40
_SEED = 20261025
_HOSTS = ("celery@a", "celery@b")
_WORKER_STATES = frozenset(TaskState) - {TaskState.PENDING}


def load_flows() -> dict[str, list[dict[str, Any]]]:
    """Every recorded flow's events (as captured), keyed by a unique flow name."""
    flows: dict[str, list[dict[str, Any]]] = {}
    for key, events in json.loads((DATA / "celery_flows.json").read_text()).items():
        flows[f"flows:{key}"] = events
    truth = json.loads((DATA / "chord_truth_flows.json").read_text())
    for key, flow in truth.items():
        flows[f"truth:{key}"] = flow["events"]
    return flows


def _offsets(events: list[TaskEvent], rng: random.Random) -> list[TaskEvent]:
    shift = {
        task_id: timedelta(milliseconds=rng.randint(-2000, 2000))
        for task_id in sorted({e.task_id for e in events})
    }
    return [
        e.model_copy(update={"timestamp": e.timestamp + shift[e.task_id]})
        for e in events
    ]


def _hosts(events: list[TaskEvent], rng: random.Random) -> list[TaskEvent]:
    # One worker per (task, attempt), sometimes the other one; a FAILURE
    # sometimes bare, as a main-process failure from workers up to 0.4.1.
    host = {
        key: rng.choice(_HOSTS)
        for key in sorted({(e.task_id, e.retries) for e in events})
    }
    stamped = []
    for e in events:
        if e.state not in _WORKER_STATES:
            stamped.append(e)
            continue
        name = host[(e.task_id, e.retries)]
        if e.state == TaskState.FAILURE and rng.random() < 0.3:
            name = None
        elif rng.random() < 0.1:
            name = _HOSTS[1 - _HOSTS.index(name)]
        stamped.append(e.model_copy(update={"hostname": name}))
    return stamped


def _synthetic(events: list[TaskEvent], rng: random.Random) -> list[TaskEvent]:
    # Inputs no recorded flow has, so the rules for them are pinned too: a
    # retry's PENDING stamped after its STARTED (an older sender), a STARTED
    # without a worker before a stamped end (a mid-upgrade fleet) and an
    # IGNORED after another terminal (a replaced task's).
    out: list[TaskEvent] = []
    for e in events:
        if (
            e.state == TaskState.STARTED
            and e.hostname is not None
            and rng.random() < 0.3
        ):
            e = e.model_copy(update={"hostname": None})
        out.append(e)
        later = e.timestamp + timedelta(milliseconds=5)
        if e.state == TaskState.STARTED and e.retries > 0:
            out.append(
                e.model_copy(update={"state": TaskState.PENDING, "timestamp": later})
            )
        if e.state in (TaskState.SUCCESS, TaskState.FAILURE) and rng.random() < 0.3:
            out.append(
                e.model_copy(update={"state": TaskState.IGNORED, "timestamp": later})
            )
    rng.shuffle(out)
    return out


def variants(raw: list[dict[str, Any]]) -> Iterator[tuple[str, list[TaskEvent]]]:
    """(variant name, events in arrival order) for one flow."""
    events = [TaskEvent.model_validate(e) for e in raw]
    rng = random.Random(_SEED)
    yield "captured", events
    for i in range(SHUFFLES):
        order = events[:]
        rng.shuffle(order)
        yield f"shuffle{i}", order
    # Each single event dropped; for big flows, a seeded sample (cost is quadratic).
    dropped = range(len(events))
    if len(events) > _DROP_ALL_UP_TO:
        dropped = sorted(rng.sample(dropped, _DROP_SAMPLE))
    for i in dropped:
        yield f"drop{i}", events[:i] + events[i + 1 :]
    yield "offsets", _offsets(events, rng)
    hosted = _hosts(events, rng)
    yield "hosts", hosted
    order = hosted[:]
    rng.shuffle(order)
    yield "hosts_shuffle", order
    yield "hosts_offsets", _offsets(hosted, rng)
    yield "synthetic", _synthetic(hosted, rng)


def _iso(value: Any) -> Any:
    return value.isoformat() if value is not None else None


def node_values(node: TaskNode) -> list[Any]:
    """Every value derived from the node's events, in a JSON-stable form."""
    return [
        node.task_id,
        node.name,
        node.node_type.value,
        node.state.value,
        node.parent_id,
        node.reported_parent_id,
        [
            [e.state.value, e.timestamp.isoformat(), e.retries, e.hostname]
            for e in node.events
        ],
        _iso(node.first_seen),
        _iso(node.last_updated),
        node.hostname,
        node.queue,
        node.routing_key,
        node.exception_type,
        _iso(node.eta) if node.events else None,
        node.queue_wait_ms if node.events else None,
        list(node.run_times()) if node.events else None,
    ]


def digest(events: list[TaskEvent]) -> str:
    """Digest of every node's values after replaying ``events`` in order."""
    graph = TaskGraph()
    for event in events:
        graph.add_event(event)
    values = [node_values(graph.nodes[k]) for k in sorted(graph.nodes)]
    payload = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def flow_digests(raw: list[dict[str, Any]]) -> dict[str, str]:
    """Variant name -> digest for one flow; the drop-one variants share one digest."""
    digests: dict[str, str] = {}
    dropped = hashlib.sha256()
    for name, events in variants(raw):
        if name.startswith("drop"):
            dropped.update(digest(events).encode())
        else:
            digests[name] = digest(events)
    digests["drop_each"] = dropped.hexdigest()[:16]
    return digests


def main() -> None:
    """Write the golden file from the current code."""
    golden = {key: flow_digests(raw) for key, raw in sorted(load_flows().items())}
    GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
