"""What each result backend does when a failure follows a stored SUCCESS.

A hard time limit in ``on_success`` makes the worker's main process call
``mark_as_failure`` for a task whose child already called ``mark_as_done``, and for
the chain steps after it. docs/graphs.md says what the backend keeps. No broker is
needed: a memory cache backend stands for the key-value backends (Redis, Memcached)
and ``db+sqlite`` for the database backends.

Celery 5.6.3 ``backends/base.py`` ``BaseKeyValueStoreBackend._store_result`` ignores an
update once SUCCESS is stored (5.2.7 too); ``backends/database/__init__.py``
``DatabaseBackend._store_result`` updates unconditionally.
"""

from pathlib import Path

import pytest
from billiard.exceptions import TimeLimitExceeded
from celery import Celery
from celery.app.task import Context
from celery.backends.base import Backend

KILLED = "killed-step"
FINISHED = "finished-later-step"
UNSENT = "unsent-later-step"


def _backend(tmp_path: Path, kind: str) -> Backend:
    url = {"key-value": "cache+memory://", "database": f"db+sqlite:///{tmp_path}/r.db"}
    backend: Backend = Celery("backend_test", backend=url[kind]).backend
    return backend


def _step(task_id: str) -> dict[str, object]:
    return {"task": "t", "options": {"task_id": task_id}}


def _states(backend: Backend) -> dict[str, str]:
    return {t: backend.get_state(t) for t in (KILLED, FINISHED, UNSENT)}


def _fail_after_success(backend: Backend) -> None:
    backend.mark_as_done(KILLED, 1)
    backend.mark_as_done(FINISHED, 1)
    backend.mark_as_failure(
        KILLED,
        TimeLimitExceeded(2),
        request=Context(chain=[_step(FINISHED), _step(UNSENT)]),
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("key-value", {KILLED: "SUCCESS", FINISHED: "SUCCESS", UNSENT: "FAILURE"}),
        ("database", {KILLED: "FAILURE", FINISHED: "FAILURE", UNSENT: "FAILURE"}),
    ],
)
def test_failure_after_success_in_backend_by_kind(
    tmp_path: Path, kind: str, expected: dict[str, str]
) -> None:
    backend = _backend(tmp_path, kind)

    _fail_after_success(backend)

    assert _states(backend) == expected
