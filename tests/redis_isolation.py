"""Give each pytest-xdist worker its own Redis database.

Integration modules read ``REDIS_URL`` at import and pass it to worker
subprocesses. All workers share one Redis server, so worker ``gwN`` uses the
database N below the configured one. The controller checks this fits before any
worker starts, because a worker that fails at import is respawned by xdist and
the run ends green with no tests. A replacement for a crashed worker gets the next
unused id (xdist 3.8.0 ``_clone_node``), so the check counts restarts too.
"""

import urllib.parse

from redis.connection import parse_url

DEFAULT_REDIS_URL = "redis://localhost:6379/15"


def _base_db(url: str) -> int:
    return int(parse_url(url).get("db", 0))


def max_workers(url: str) -> int:
    """Return how many xdist workers ``url``'s database number can serve."""
    return _base_db(url) + 1


def count_tx_workers(tx: list[str]) -> int:
    """Return how many workers the ``--tx`` specs start (``3*popen`` is three)."""
    total = 0
    for spec in tx:
        count, star, _ = spec.partition("*")
        total += int(count) if star and count.isdigit() else 1
    return total


def redis_url_for_worker(url: str, worker_id: str | None) -> str:
    """Return ``url`` pointing at the database only ``worker_id`` uses.

    Ids that are not ``gwN`` (no xdist, or the controller) get ``url`` unchanged.
    Raises ``ValueError`` when no database is left for the worker.
    """
    if not worker_id or not worker_id.startswith("gw") or not worker_id[2:].isdigit():
        return url
    db = _base_db(url) - int(worker_id[2:])
    if db < 0:
        raise ValueError(f"no Redis db below {_base_db(url)} for worker {worker_id}")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "unix":
        query = [
            (k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "db"
        ] + [("db", str(db))]
        # urlunsplit would drop the empty netloc and turn unix:/// into unix:/
        return f"unix://{parts.netloc}{parts.path}?{urllib.parse.urlencode(query)}"
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "db"]
    return urllib.parse.urlunsplit(
        parts._replace(path=f"/{db}", query=urllib.parse.urlencode(query))
    )


def check_worker_count(url: str, workers: int) -> None:
    """Raise ``ValueError`` with a usage message if ``workers`` exceed the dbs."""
    if workers > max_workers(url):
        raise ValueError(
            f"REDIS_URL {url!r} selects database {_base_db(url)}, which serves at most "
            f"{max_workers(url)} xdist workers but {workers} were requested. "
            "Use a higher database number (Redis has 16 by default), set REDIS_URL, "
            "or pass fewer workers (-n / --maxprocesses, --tx) or restarts "
            "(--max-worker-restart)."
        )
