"""Which event fields name a chord, its header group and its callback.

Reads ``chord_truth_flows.json`` and counts, per backend and Celery version,
how often the events stemtrace captured carry each piece of evidence, checked
against the truth Celery recorded at send time (``checks`` in the file compares
the truth with the same run's events).

Run: ``python tests/data/chord_truth_evidence.py``.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DATA = Path(__file__).with_name("chord_truth_flows.json")
Events = list[dict[str, Any]]


def body_callback_id(body: Any) -> str | None:
    """The ID of the first task a serialized body signature starts with."""
    if not isinstance(body, dict):
        return None
    if body.get("task") == "celery.chain":
        tasks = body.get("kwargs", {}).get("tasks") or []
        return body_callback_id(tasks[0]) if tasks else None
    if body.get("task") in ("celery.chord", "celery.group"):
        return None
    options = body.get("options") or {}
    return options.get("task_id") if isinstance(options, dict) else None


def count_event_fields(events: Events, count: Counter[str]) -> None:
    """Per event state: how many events carry ``chord_id``/``chord_callback_id``."""
    for event in events:
        state = event["state"]
        count[f"events_{state}"] += 1
        count[f"events_{state}_with_chord_callback_id"] += "chord_callback_id" in event
        count[f"events_{state}_with_chord_id"] += "chord_id" in event
        count[f"events_{state}_with_chord_body_first_id"] += (
            "chord_body_first_id" in event
        )
        count[f"events_{state}_with_group_id"] += "group_id" in event


def count_chord(
    chord: dict[str, Any], check: dict[str, Any], events: Events, count: Counter[str]
) -> None:
    """What the events say about one chord."""
    by_id: dict[str, Events] = defaultdict(list)
    for event in events:
        by_id[event["task_id"]].append(event)
    members = {m["id"] for m in chord["header_members"] if m["kind"] != "chord"}
    own = [e for m in members for e in by_id[m]]
    count["chords"] += 1
    count["header_seen"] += check["header_seen"]
    count["callback_seen"] += check["callback_seen"]
    count["header_group_on_member_pending"] += any(
        e["state"] == "PENDING" and e.get("group_id") == chord["group_id"] for e in own
    )
    count["member_events_name_callback"] += any("chord_callback_id" in e for e in own)
    count["named_callback_is_stamped_id"] += check["stamp_matches"] is True
    count["named_callback_not_stamped_id"] += check["stamp_matches"] is False
    count["chord_id_is_own_group"] += check["chord_id_matches"] is True
    count["chord_id_not_own_group"] += check["chord_id_matches"] is False
    if chord["callback_kind"] == "task":
        count["task_callbacks"] += 1
        count[f"callback_parent_{check['callback_parent_is']}"] += 1
        count["callback_group_id_is_header_group"] += any(
            e.get("group_id") == chord["group_id"] for e in by_id[chord["callback_id"]]
        )


def count_unlock(chord: dict[str, Any], events: Events, count: Counter[str]) -> None:
    """Whether ``celery.chord_unlock``'s args give this chord's group and callback."""
    for event in events:
        args = event.get("args")
        if event["name"] != "celery.chord_unlock" or not isinstance(args, list):
            continue
        if not args or args[0] != chord["group_id"]:
            continue
        state = event["state"].lower()
        count[f"unlock_{state}_args_have_group_id"] += 1
        if chord["callback_kind"] == "task":
            found = body_callback_id(args[1]) if len(args) > 1 else None
            count[f"unlock_{state}_args_have_callback_id"] += (
                found == chord["callback_id"]
            )


def main() -> None:
    """Print the evidence table."""
    stats: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for key, capture in json.loads(DATA.read_text()).items():
        _, backend, version = key.split("@")
        count = stats[(backend.split("+")[0], version.removeprefix("celery"))]
        events = capture["events"]
        count_event_fields(events, count)
        chords = zip(capture["truth"]["chords"], capture["checks"], strict=True)
        for chord, check in chords:
            count_chord(chord, check, events, count)
            count_unlock(chord, events, count)
        unlock_events = [
            e
            for e in events
            if e["name"] == "celery.chord_unlock"
            and e["state"] in ("PENDING", "STARTED")
        ]
        count["unlock_events_with_args"] += sum("args" in e for e in unlock_events)
        count["unlock_events_args_truncated_or_missing"] += sum(
            not isinstance(e.get("args"), list) or e["args"][:1] == [None]
            for e in unlock_events
        )
    for (backend, version), count in sorted(stats.items()):
        print(f"== {backend} celery {version}: {count['chords']} chords")
        for name, value in sorted(count.items()):
            if name != "chords":
                print(f"   {name}: {value}")


if __name__ == "__main__":
    main()
