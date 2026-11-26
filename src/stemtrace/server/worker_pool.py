"""Read a worker's pool type and slot count from one ``inspect.stats()`` payload."""

from __future__ import annotations

from typing import NamedTuple


class PoolInfo(NamedTuple):
    """What ``stats()`` says about a worker's pool; ``None`` for what it didn't say."""

    pool: str | None = None
    concurrency: int | None = None


NO_POOL_INFO = PoolInfo()


def _positive_int(value: object) -> int | None:
    # bool is an int subclass; True would read as one slot.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _pool_name(implementation: object) -> str | None:
    """``celery.concurrency.prefork:TaskPool`` -> ``prefork``; ``thread`` -> ``threads``."""
    if not isinstance(implementation, str):
        return None
    module = implementation.partition(":")[0]
    name = module.rpartition(".")[2]
    if not name or name == "pool":  # "pool" alone says nothing about the pool type
        return None
    return "threads" if name == "thread" else name


def pool_from_stats_payload(payload: object) -> PoolInfo:
    """Extract the pool type and slot count from one worker's ``stats()`` payload.

    Never raises: a payload that isn't the shape Celery documents gives ``None``
    for the missing part. With ``--autoscale`` the slot count is the autoscaler
    ceiling, because ``max-concurrency`` is then its minimum.
    """
    if not isinstance(payload, dict):
        return NO_POOL_INFO
    pool = payload.get("pool")
    if not isinstance(pool, dict):  # Celery sends the string 'N/A' with no pool
        return NO_POOL_INFO
    # Celery 5.6.3 autoscale.py Autoscaler.info() puts the ceiling at 'max'.
    autoscaler = payload.get("autoscaler")
    if isinstance(autoscaler, dict) and "max" in autoscaler:
        concurrency = _positive_int(autoscaler["max"])
    else:
        concurrency = _positive_int(pool.get("max-concurrency"))
    return PoolInfo(_pool_name(pool.get("implementation")), concurrency)
