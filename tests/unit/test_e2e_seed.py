"""Tests for the tests/e2e/seed.py script shared by CI and the Makefile E2E targets."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tests.e2e import seed

if TYPE_CHECKING:
    import pytest

WAIT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wait_for_http.py"
_spec = importlib.util.spec_from_file_location("wait_for_http", WAIT_SCRIPT)
assert _spec is not None
assert _spec.loader is not None
wait_for_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wait_for_http)


def test_wait_command_lists_every_expectation_for_the_tasks_endpoint() -> None:
    command = seed.wait_command("http://localhost:8000")

    assert command == [
        sys.executable,
        str(WAIT_SCRIPT),
        "http://localhost:8000/stemtrace/api/tasks?limit=500",
        "--timeout",
        "90",
        "--interval",
        "2",
        "--expect-task",
        "e2e.add=SUCCESS:2",
        "--expect-task",
        "e2e.process_item=SUCCESS:5",
        "--expect-task",
        "e2e.batch_processor=SUCCESS",
        "--expect-task",
        "e2e.aggregate=SUCCESS",
        "--expect-task",
        "e2e.always_fails=FAILURE",
    ]


def test_wait_command_with_trailing_slash_in_server_url_builds_one_slash() -> None:
    command = seed.wait_command("http://localhost:8000/")

    assert command[2] == "http://localhost:8000/stemtrace/api/tasks?limit=500"


def test_expectations_are_accepted_by_wait_for_http() -> None:
    parsed = [
        wait_for_http._parse_task_expectation(spec) for spec in seed.EXPECTED_TASKS
    ]

    assert [(p.name, p.state, p.min_count) for p in parsed] == [
        ("e2e.add", "SUCCESS", 2),
        ("e2e.process_item", "SUCCESS", 5),
        ("e2e.batch_processor", "SUCCESS", 1),
        ("e2e.aggregate", "SUCCESS", 1),
        ("e2e.always_fails", "FAILURE", 1),
    ]


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, command: list[str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0)


def test_main_submits_tasks_then_waits_for_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    recorder = _Recorder()

    def fake_submit() -> None:
        events.append("submit")

    def fake_run(command: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        events.append("wait")
        return recorder(command, check)

    monkeypatch.setattr(seed, "submit_tasks", fake_submit)
    monkeypatch.setattr(seed.subprocess, "run", fake_run)
    monkeypatch.setenv("CELERY_FLOW_SERVER_URL", "http://localhost:18000")

    code = seed.main()

    assert code == 0
    assert events == ["submit", "wait"]
    assert recorder.calls == [seed.wait_command("http://localhost:18000")]


def test_main_when_wait_fails_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seed, "submit_tasks", lambda: None)
    monkeypatch.setattr(
        seed.subprocess,
        "run",
        lambda command, check: subprocess.CompletedProcess(command, 1),
    )

    assert seed.main() == 1
