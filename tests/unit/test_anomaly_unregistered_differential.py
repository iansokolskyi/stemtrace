"""Differential fuzz: the unregistered classifier against an independent reference.

Every capture, synthetic flow and recording, every prefix, a seeded draw of
registry facts (names registered, any worker online, authority age, broker
flag) and "now" on both sides of the grace and the authority window. The
reference reads raw ``TaskEvent`` lists only (its own state rule, no
``RunRecord``, no store state) and writes the gate and the grouping out again.
"""

import random
import re
from dataclasses import replace
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import pytest

from stemtrace.core.anomalies import (
    AnomalyKind,
    AnomalySettings,
    RegistryFacts,
    classify_anomalies,
)
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.anomaly_facts import classifier_facts
from tests.unit.anomaly_fuzz import SETTINGS as STUCK_SETTINGS
from tests.unit.anomaly_fuzz import Cell, build, load_flows, prefixes
from tests.unit.anomaly_reference import _state_event

GRACE = timedelta(seconds=2)
WINDOW = timedelta(seconds=30)
SETTINGS: AnomalySettings = replace(
    STUCK_SETTINGS, pending_grace=GRACE, authority_window=WINDOW
)
SEEDS = (1, 2)
REGISTRIES_PER_PREFIX = 2
Expected = set[tuple[str, str | None, datetime, int, str | None]]


def reference_hint(name: str, live: frozenset[str]) -> str | None:
    """The D5 rule written out from the spec, with plain loops over every live name."""
    if "." not in name:
        return None
    module, last = name.rsplit(".", 1)
    stem = re.sub(r"_v\d+$", "", last)
    best, winners = -1.0, []
    for other in sorted(live):
        if "." not in other or other == name or other.startswith("celery."):
            continue
        other_module, other_last = other.rsplit(".", 1)
        other_stem = re.sub(r"_v\d+$", "", other_last)
        if other_module != module or not stem or not other_stem:
            continue
        score = SequenceMatcher(None, stem, other_stem).ratio()
        if score > best:
            best, winners = score, [other]
        elif score == best:
            winners.append(other)
    return winners[0] if best >= 0.75 and len(winners) == 1 else None


def reference(
    task_events: list[TaskEvent],
    names: dict[str, str],
    registry: RegistryFacts,
    now: datetime,
) -> tuple[Expected, set[str]]:
    """Items as (kind, name, since, count) and the flagged task IDs, from raw events."""
    confirmed = registry.authority_at
    trusted = registry.any_live and (
        (confirmed is not None and (now - confirmed) <= WINDOW)
        or (not registry.broker_configured and len(registry.live_names) > 0)
    )
    by_task: dict[str, list[TaskEvent]] = {}
    for event in task_events:
        by_task.setdefault(event.task_id, []).append(event)
    waiting: list[tuple[str, str, datetime]] = []
    for task_id, events in by_task.items():
        state = _state_event(events)
        name = names[task_id]
        if (
            state.state == TaskState.PENDING
            and not name.startswith("celery.")
            and (now - state.timestamp) > GRACE
        ):
            waiting.append((task_id, name, state.timestamp))
    if not waiting:
        return set(), set()
    if not trusted:
        return set(), set()
    unknown = [w for w in waiting if w[1] not in registry.live_names]
    items: Expected = set()
    for name in {w[1] for w in unknown}:
        group = [w for w in unknown if w[1] == name]
        items.add(
            (
                "unregistered",
                name,
                min(w[2] for w in group),
                len(group),
                reference_hint(name, registry.live_names),
            )
        )
    return items, {w[0] for w in unknown}


def draw_registry(rng: random.Random, names: list[str], now: datetime) -> RegistryFacts:
    """Registry facts: some of ``names`` (and twins of others) online, authority just inside, just outside or never."""
    registered = frozenset(n for n in names if rng.random() < 0.5)
    # Renamed and near-miss twins of some names, so the D5 hint is reachable.
    twins = {
        rng.choice([re.sub(r"_v\d+$", "", n) + "_v2", n + "s", n + "_v2"])
        for n in names
        if rng.random() < 0.3
    }
    registered |= twins
    authority = rng.choice(
        [
            now - WINDOW,
            now - WINDOW - timedelta(seconds=0.001),
            now - WINDOW + timedelta(seconds=0.001),
            None,
        ]
    )
    return RegistryFacts(
        live_names=registered,
        any_live=bool(registered) or rng.random() < 0.7,
        authority_at=authority,
        broker_configured=rng.random() < 0.6,
    )


def nows(task_events: list[TaskEvent]) -> list[datetime]:
    """Both sides of the grace for every PENDING stamp, and long after."""
    found = {
        e.timestamp + GRACE + timedelta(seconds=d)
        for e in task_events
        if e.state == TaskState.PENDING
        for d in (-0.001, 0.0, 0.001)
    }
    found.add(max(e.timestamp for e in task_events) + timedelta(hours=2))
    return sorted(found)


def check(cell: Cell, rng: random.Random) -> list[str]:
    """Classifier vs reference for one cell, at every "now", with drawn registries."""
    built = build(cell)
    records = built.store.run_records()
    names = {r.task_id: r.name for r in records}
    task_events = [e for e in built.task_events if e.task_id in names]
    problems: list[str] = []
    for now in nows(task_events):
        registry = draw_registry(rng, sorted(set(names.values())), now)
        facts = classifier_facts(records, built.timeline.snapshot(), now)
        report = classify_anomalies(
            **facts._asdict(), now=now, settings=SETTINGS, registry=registry
        )
        kinds = (AnomalyKind.UNREGISTERED, AnomalyKind.NO_WORKERS_ONLINE)
        got: Expected = {
            (a.kind.value, a.name, a.since, a.task_count, a.similar_name)
            for a in report.items
            if a.kind in kinds
        }
        got_ids = {t for t, a in report.anomalies.items() if a.kind in kinds}
        want, want_ids = reference(task_events, names, registry, now)
        if (got, got_ids) != (want, want_ids):
            problems.append(
                f"{cell.name}[:{len(cell.events)}] now={now.isoformat()} "
                f"registry={registry}: fast={sorted(map(str, got))} "
                f"ref={sorted(map(str, want))}"
            )
        if report.counts[AnomalyKind.UNREGISTERED] != sum(
            1 for i in want if i[0] == "unregistered"
        ):
            problems.append(f"{cell.name} now={now}: counts differ")
    return problems


@pytest.mark.parametrize("seed", SEEDS)
def test_unregistered_matches_reference_on_every_prefix_of_every_flow(
    seed: int,
) -> None:
    rng = random.Random(seed)
    problems: list[str] = []
    cells = 0
    for name, events in prefixes(load_flows()):
        cell = Cell(name, events, "one", 0.0, (), "none", False, 0)
        for _ in range(REGISTRIES_PER_PREFIX):
            cells += 1
            problems += check(cell, rng)

    assert problems[:5] == []
    assert cells > 500


def test_the_sample_reaches_every_outcome_of_the_rule() -> None:
    rng = random.Random(9)
    seen: set[str] = set()
    for name, events in prefixes(load_flows()):
        built = build(Cell(name, events, "one", 0.0, (), "none", False, 0))
        records = built.store.run_records()
        names = {r.task_id: r.name for r in records}
        task_events = [e for e in built.task_events if e.task_id in names]
        for now in nows(task_events):
            registry = draw_registry(rng, sorted(set(names.values())), now)
            items, _ = reference(task_events, names, registry, now)
            seen.update(kind for kind, *_ in items)
            seen.update("hinted" if hinted else "unhinted" for *_, hinted in items)
            if not items and any(r.state == TaskState.PENDING for r in records):
                seen.add("pending-but-quiet")

    assert seen == {"unregistered", "pending-but-quiet", "hinted", "unhinted"}
