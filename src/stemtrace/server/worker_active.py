"""Busy slots per worker, counted from run records (B4b)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from stemtrace.core.events import TaskState
from stemtrace.server.store import ensure_tz_aware

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from stemtrace.server.stats import RunRecord


def count_active_by_node(
    records: Iterable[RunRecord], since: Mapping[str, datetime | None]
) -> dict[str, int]:
    """Count the STARTED runs on each live nodename, in one pass over ``records``.

    Args:
        records: Run records; one pass, whatever the number of nodenames.
        since: Live nodename to its pid's ``worker_ready`` stamp; None means no
            lower bound. Runs started before the stamp are the old process's.

    Returns:
        Nodename to count, with 0 for a nodename that has none.
    """
    counts = dict.fromkeys(since, 0)
    for record in records:
        host = record.state_host
        if record.state != TaskState.STARTED or host is None or host not in since:
            continue
        bound = since[host]
        started = record.state_since
        if bound is not None and (
            started is None or ensure_tz_aware(started) < ensure_tz_aware(bound)
        ):
            continue
        counts[host] += 1
    return counts
