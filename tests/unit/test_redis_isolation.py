"""Per-xdist-worker Redis database selection (tests/redis_isolation.py)."""

import os
import subprocess
import sys

import pytest

from tests.redis_isolation import (
    check_worker_count,
    max_workers,
    redis_url_for_worker,
)

BASE = "redis://localhost:6379"


@pytest.mark.parametrize(
    ("url", "worker", "expected"),
    [
        (f"{BASE}/15", "gw0", f"{BASE}/15"),
        (f"{BASE}/15", "gw3", f"{BASE}/12"),
        (f"{BASE}/15", "gw15", f"{BASE}/0"),
        (f"{BASE}/15?socket_timeout=2", "gw1", f"{BASE}/14?socket_timeout=2"),
        (f"{BASE}/15", "master", f"{BASE}/15"),
        (f"{BASE}/15", None, f"{BASE}/15"),
        (f"{BASE}/15", "gwx", f"{BASE}/15"),
        (BASE, "gw0", f"{BASE}/0"),
        (f"{BASE}?db=5", "gw2", f"{BASE}/3"),
        ("unix:///tmp/redis.sock?db=5", "gw2", "unix:///tmp/redis.sock?db=3"),
        ("unix:///tmp/redis.sock", "gw0", "unix:///tmp/redis.sock?db=0"),
    ],
)
def test_redis_url_for_worker_maps_each_worker_to_its_own_db(
    url: str, worker: str | None, expected: str
) -> None:
    assert redis_url_for_worker(url, worker) == expected


@pytest.mark.parametrize("url", [f"{BASE}/0", BASE, "unix:///s.sock"])
def test_redis_url_for_worker_with_no_db_left_raises_value_error(url: str) -> None:
    with pytest.raises(ValueError, match="no Redis db below 0 for worker gw1"):
        redis_url_for_worker(url, "gw1")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://h/15", 16),
        ("redis://h/0", 1),
        ("redis://h", 1),
        ("unix:///s.sock?db=7", 8),
    ],
)
def test_max_workers_counts_dbs_up_to_the_configured_one(
    url: str, expected: int
) -> None:
    assert max_workers(url) == expected


def test_check_worker_count_at_capacity_passes() -> None:
    check_worker_count("redis://h/15", 16)


def test_check_worker_count_over_capacity_names_counts() -> None:
    with pytest.raises(ValueError, match=r"at most 1 xdist workers but 2 were"):
        check_worker_count("redis://h/0", 2)


@pytest.mark.parametrize("url", [f"{BASE}/0", f"{BASE}/1", BASE])
def test_pytest_with_too_few_dbs_exits_with_usage_error(url: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "3",
            "--dist",
            "loadfile",
            "-p",
            "no:cacheprovider",
            # The lowest-versions CI job passes this too (old floors warn on import).
            "-W",
            "ignore::DeprecationWarning",
            "tests/unit/test_redis_isolation.py",
        ],
        # This test may itself run on an xdist worker; the child is a controller.
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")},
            "REDIS_URL": url,
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "serves at most" in result.stderr
