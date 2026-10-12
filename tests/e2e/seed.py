"""Seed the E2E stack with the tasks the Playwright specs look for, and wait for them.

Run from the repository root against a running stack, for both the CI workflow and
the Makefile targets, so the list of seeded tasks and what the specs expect to find
have one owner::

    uv run python -m tests.e2e.seed

Environment:
    CELERY_BROKER_URL / CELERY_RESULT_BACKEND: where to submit (see tests/e2e/tasks.py).
    CELERY_FLOW_SERVER_URL: stemtrace server to poll (default http://localhost:8000).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

WAIT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wait_for_http.py"
WAIT_TIMEOUT_S = 90
WAIT_INTERVAL_S = 2

# NAME=STATE[:COUNT] for scripts/wait_for_http.py --expect-task: what submit_tasks()
# produces once every task has reached its terminal state. Counts:
#   add x2, process_item x5 (3 from create_workflow's group + 2 from batch_processor),
#   one batch_processor, the workflow's aggregate callback and one failing task.
EXPECTED_TASKS = (
    "e2e.add=SUCCESS:2",
    "e2e.process_item=SUCCESS:5",
    "e2e.batch_processor=SUCCESS",
    "e2e.aggregate=SUCCESS",
    "e2e.always_fails=FAILURE",
)


def submit_tasks() -> None:
    """Submit the tasks that give every graph and task spec something to show."""
    # Imported here: importing tests.e2e.tasks initialises stemtrace on the Celery app.
    from tests.e2e.tasks import add, always_fails, batch_processor, create_workflow

    add.delay(1, 2)
    add.delay(3, 4)
    # chord: a group of 3 process_item tasks followed by the aggregate callback
    create_workflow([1, 2, 3]).apply_async()
    # a parent task that spawns a group of 2 process_item tasks
    batch_processor.delay([1, 2])
    always_fails.delay("test failure")
    print("Tasks submitted!", flush=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def wait_command(server_url: str, since: datetime) -> list[str]:
    """Build the command that waits until the API lists every expected task.

    Only tasks updated at or after ``since`` count (the API's ``from_date`` filters
    on last update): a reused stack keeps finished tasks from earlier seeds, which
    would otherwise satisfy the counts before this run's tasks finish.
    """
    query = urlencode({"limit": 500, "from_date": since.isoformat()})
    command = [
        sys.executable,
        str(WAIT_SCRIPT),
        f"{server_url.rstrip('/')}/stemtrace/api/tasks?{query}",
        "--timeout",
        str(WAIT_TIMEOUT_S),
        "--interval",
        str(WAIT_INTERVAL_S),
    ]
    for expected in EXPECTED_TASKS:
        command += ["--expect-task", expected]
    return command


def main() -> int:
    """Submit the seed tasks, then block until the API reports them finished."""
    since = _utcnow()
    submit_tasks()
    server_url = os.environ.get("CELERY_FLOW_SERVER_URL", "http://localhost:8000")
    return subprocess.run(wait_command(server_url, since), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
