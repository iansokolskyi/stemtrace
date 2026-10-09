"""Tests for the build_ui.py hatch build hook helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "build_ui.py"
_spec = importlib.util.spec_from_file_location("build_ui", HOOK)
assert _spec is not None
assert _spec.loader is not None
build_ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_ui)


def test_npm_install_command_with_lockfile_uses_ci_without_scripts(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")

    assert build_ui.npm_install_command(tmp_path) == ["npm", "ci", "--ignore-scripts"]


def test_npm_install_command_without_lockfile_installs_without_scripts(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text("{}")

    assert build_ui.npm_install_command(tmp_path) == [
        "npm",
        "install",
        "--ignore-scripts",
    ]


def test_npm_install_command_for_repo_frontend_uses_lockfile() -> None:
    frontend_dir = HOOK.parent / "src" / "stemtrace" / "server" / "ui" / "frontend"

    assert build_ui.npm_install_command(frontend_dir) == [
        "npm",
        "ci",
        "--ignore-scripts",
    ]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
def test_ui_build_skipped_with_truthy_env_returns_true(value: str) -> None:
    assert build_ui.ui_build_skipped({"STEMTRACE_SKIP_UI_BUILD": value}) is True


@pytest.mark.parametrize(
    "environ", [{}, {"STEMTRACE_SKIP_UI_BUILD": ""}, {"STEMTRACE_SKIP_UI_BUILD": "0"}]
)
def test_ui_build_skipped_without_truthy_env_returns_false(
    environ: dict[str, str],
) -> None:
    assert build_ui.ui_build_skipped(environ) is False


def test_ui_build_skipped_defaults_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEMTRACE_SKIP_UI_BUILD", "1")

    assert build_ui.ui_build_skipped() is True
