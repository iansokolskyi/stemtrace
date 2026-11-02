"""Task names in a ``registered()`` inspect reply carry Celery's info suffix.

With no arguments, Celery's ``registered`` control appends ``exchange``,
``routing_key`` and ``rate_limit`` when a task sets them (``name [rate_limit=10/m]``).
The replies here come from Celery's own handler, not hand-written strings.
"""

from typing import Any

import pytest
from celery import Celery
from celery.worker.control import Panel

from stemtrace.server.api import routes
from stemtrace.server.store import WorkerRegistry

_NAME = "myapp.tasks.limited"


class _Inspect:
    """Inspector that answers stats() for one node and registered() with ``reply``."""

    def __init__(self, reply: list[str]) -> None:
        self._reply = reply

    def stats(self) -> dict[str, object]:
        return {"celery@worker-1": {"pid": 1111}}

    def registered(self) -> dict[str, object]:
        return {"celery@worker-1": self._reply}

    def ping(self) -> None:
        return None


class _State:
    """The worker state Celery's control handlers read (only ``app``)."""

    def __init__(self, app: Celery) -> None:
        self.app = app


def _celery_registered_reply(name: str, **task_attrs: Any) -> list[str]:
    """What a worker running one task with ``task_attrs`` answers to registered()."""
    app = Celery("info-suffix", set_as_current=False)

    def body() -> None: ...

    # shared=False: a shared task joins every app created later (the next case's).
    task = app.task(name=name, shared=False)(body)
    # The handler reads these with getattr; exchange/routing_key aren't task options.
    for attr, value in task_attrs.items():
        setattr(task, attr, value)
    reply: list[str] = Panel.data["registered"](_State(app), taskinfoitems=())
    # Shared tasks other test modules define join every app; keep only this one.
    return [entry for entry in reply if entry.startswith(name)]


@pytest.mark.parametrize(
    ("name", "task_attrs"),
    [
        (_NAME, {}),
        (_NAME, {"rate_limit": "10/m"}),
        (_NAME, {"exchange": "tasks"}),
        (_NAME, {"routing_key": "tasks.limited"}),
        (_NAME, {"rate_limit": "10/m", "exchange": "ex", "routing_key": "rk"}),
        ("myapp.tasks.weird [v2]", {}),
        ("myapp.tasks.weird [v2]", {"rate_limit": "1/s"}),
    ],
    ids=[
        "plain",
        "rate_limit",
        "exchange",
        "routing_key",
        "all-three",
        "brackets-in-name",
        "brackets-in-name-and-suffix",
    ],
)
def test_refresh_with_info_suffix_stores_bare_task_name(
    name: str, task_attrs: dict[str, Any]
) -> None:
    """The registry keeps the task's own name, whatever info Celery appends."""
    reply = _celery_registered_reply(name, **task_attrs)
    worker_registry = WorkerRegistry()

    routes._refresh_worker_registry_from_inspect(worker_registry, _Inspect(reply))  # type: ignore[arg-type]  # structural fake

    (worker,) = worker_registry.get_all_workers()
    assert worker.registered_tasks == [name]
