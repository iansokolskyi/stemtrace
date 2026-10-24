"""Read a chord body's first task ID from its serialized signature."""

from collections.abc import Mapping
from typing import Any

# Celery flattens nested chains, so this only guards against hand-built input.
_MAX_DEPTH = 8
_CANVAS_GROUPS = ("celery.chord", "celery.group")


def body_first_task_id(body: Any, _depth: int = 0) -> str | None:
    """Return the task ID the body of a chord starts with.

    Reads a task signature's ``options.task_id``, or for a ``celery.chain`` the
    first step's. Returns None for a group or chord body (or first step), and
    for anything that is not a well-formed signature. Never raises.

    Args:
        body: A signature dict, or a Celery ``Signature`` (a dict subclass).
    """
    if not isinstance(body, Mapping) or _depth > _MAX_DEPTH:
        return None
    task = body.get("task")
    if task in _CANVAS_GROUPS:
        return None
    if task == "celery.chain":
        kwargs = body.get("kwargs")
        steps = kwargs.get("tasks") if isinstance(kwargs, Mapping) else None
        if not isinstance(steps, (list, tuple)) or not steps:
            return None
        return body_first_task_id(steps[0], _depth + 1)
    options = body.get("options")
    task_id = options.get("task_id") if isinstance(options, Mapping) else None
    return task_id if isinstance(task_id, str) else None
