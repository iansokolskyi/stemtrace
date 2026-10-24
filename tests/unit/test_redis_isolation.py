"""Per-xdist-worker Redis database selection (tests/redis_isolation.py)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.redis_isolation import (
    check_worker_count,
    count_tx_workers,
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


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_controller(
    args: list[str],
    redis_url: str,
    target: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run pytest as an xdist controller with tests/conftest.py loaded as a plugin."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-W",
            "ignore::DeprecationWarning",
            *args,
            str(target),
        ],
        cwd=REPO_ROOT,
        # This test may itself run on an xdist worker; the child is a controller.
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")},
            "REDIS_URL": redis_url,
            "PYTHONPATH": str(REPO_ROOT),
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    ("tx", "expected"),
    [
        (["popen"], 1),
        (["3*popen"], 3),
        (["2*popen", "popen//python=python3", "4*popen"], 7),
        (["ssh=host"], 1),
    ],
)
def test_count_tx_workers_expands_multipliers(tx: list[str], expected: int) -> None:
    assert count_tx_workers(tx) == expected


def test_pytest_with_tx_multiplier_over_the_dbs_exits_with_usage_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    pass\n")
    result = _run_controller(
        ["--tx", "3*popen", "--dist", "load"], f"{BASE}/1", tmp_path
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "at most 2 xdist workers but 3 were requested" in result.stderr


def test_pytest_with_restarts_over_the_dbs_exits_with_usage_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    pass\n")
    result = _run_controller(
        ["-n", "2", "--max-worker-restart", "1"], f"{BASE}/1", tmp_path
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "at most 2 xdist workers but 3 were requested" in result.stderr


def test_pytest_with_crashing_test_fails_instead_of_respawning_past_the_dbs(
    tmp_path: Path,
) -> None:
    # xdist 3.8.0 names a crashed gw1's replacement gw2, which has no db below /1.
    (tmp_path / "test_crash.py").write_text(
        "import os\n\ndef test_crash():\n    os._exit(1)\n"
    )
    result = _run_controller(["-n", "2"], f"{BASE}/1", tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "worker restarting disabled" in output, output
    assert "no Redis db below" not in output, output


_DIE = {
    "sigkill": "os.kill(os.getpid(), signal.SIGKILL)",
    "exit": "os._exit(1)",
}
_CRASH_CONFTEST = """\
import os
import signal

import pytest

if os.environ.get("PYTEST_XDIST_WORKER") == "gw1":
    {startup}


@pytest.fixture(scope="session", autouse=True)
def _die_at_teardown():
    yield
    if os.environ.get("PYTEST_XDIST_WORKER") == "gw1":
        {teardown}
"""
_MANY_TESTS = (
    "import pytest\n\n@pytest.mark.parametrize('i', range(40))\n"
    "def test_ok(i):\n    pass\n"
)


@pytest.mark.parametrize("how", _DIE)
@pytest.mark.parametrize("when", ["startup", "teardown"])
def test_pytest_when_a_worker_dies_outside_a_test_exits_non_zero(
    tmp_path: Path, when: str, how: str
) -> None:
    """A worker lost without a test item is not replaced, so it must fail the run."""
    die = _DIE[how]
    (tmp_path / "conftest.py").write_text(
        _CRASH_CONFTEST.format(
            startup=die if when == "startup" else "pass",
            teardown=die if when == "teardown" else "pass",
        )
    )
    (tmp_path / "test_many.py").write_text(_MANY_TESTS)
    result = _run_controller(["-n", "2", "--dist", "worksteal"], f"{BASE}/15", tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "worker gw1 crashed and worker restarting disabled" in output, output


def test_pytest_controller_inheriting_worker_env_when_worker_dies_exits_cleanly(
    tmp_path: Path,
) -> None:
    """A nested controller that inherited PYTEST_XDIST_WORKER has no restart limit set."""
    (tmp_path / "conftest.py").write_text(
        _CRASH_CONFTEST.format(startup=_DIE["sigkill"], teardown="pass")
    )
    (tmp_path / "test_many.py").write_text(_MANY_TESTS)
    result = _run_controller(
        ["-n", "2", "--dist", "worksteal"],
        f"{BASE}/15",
        tmp_path,
        extra_env={"PYTEST_XDIST_WORKER": "gw3"},
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Traceback" not in output, output
    assert "TypeError" not in output, output


def test_pytest_with_healthy_workers_exits_zero(tmp_path: Path) -> None:
    (tmp_path / "test_many.py").write_text(_MANY_TESTS)
    result = _run_controller(["-n", "2", "--dist", "worksteal"], f"{BASE}/15", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "40 passed" in result.stdout


def test_pytest_with_invalid_max_worker_restart_exits_with_usage_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    pass\n")
    result = _run_controller(
        ["-n", "2", "--max-worker-restart", "x"], f"{BASE}/15", tmp_path
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "--max-worker-restart" in result.stderr
