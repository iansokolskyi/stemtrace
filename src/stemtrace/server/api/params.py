"""Query parameters shared by list and summary endpoints: states and time windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from stemtrace.core.events import TaskState
from stemtrace.server.stats import parse_duration
from stemtrace.server.store import ensure_tz_aware

_STATES_BY_NAME = {state.value.upper(): state for state in TaskState}


def utcnow() -> datetime:
    """The current time, timezone-aware. One call per request; tests replace it."""
    return datetime.now(timezone.utc)


def parse_states(text: str | None) -> frozenset[TaskState] | None:
    """Parse ``state=``: one state or a comma-separated list, matched in any case.

    Returns:
        The states (a task matches any of them), or None without a filter.

    Raises:
        HTTPException: 422 naming the valid states if any entry is unknown.
    """
    if text is None:
        return None
    names = [part.strip().upper() for part in text.split(",") if part.strip()]
    if not names:
        return None
    unknown = [name for name in names if name not in _STATES_BY_NAME]
    if unknown:
        valid = ", ".join(_STATES_BY_NAME)
        raise HTTPException(
            status_code=422,
            detail=f"Unknown state {', '.join(unknown)}; valid states: {valid}",
        )
    return frozenset(_STATES_BY_NAME[name] for name in names)


def parse_within(text: str) -> timedelta:
    """Parse ``within=`` (``30s``, ``15m``, ``6h``, ``7d``).

    Raises:
        HTTPException: 422 if it isn't a positive duration with one of those units.
    """
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"within: {exc}",
        ) from exc


def start_from(
    within: str | None, from_date: datetime | None, now: datetime
) -> datetime | None:
    """The start of a time filter: ``now - within``, or ``from_date``.

    A ``from_date`` without a timezone (``2026-10-18``) is UTC, as on the
    task list.

    Raises:
        HTTPException: 422 if both are given, or ``within`` is malformed.
    """
    if within is None:
        return ensure_tz_aware(from_date) if from_date is not None else None
    if from_date is not None:
        raise HTTPException(
            status_code=422,
            detail="Pass either within or from_date, not both",
        )
    return now - parse_within(within)
