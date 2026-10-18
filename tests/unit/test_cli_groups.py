"""Unit tests for ``stemtrace server --groups`` / ``STEMTRACE_GROUPS``."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
import typer
import uvicorn
from fastapi import APIRouter
from rich.text import Text
from typer.testing import CliRunner

from stemtrace.core.groups import GroupConfig
from stemtrace.server import __main__ as cli
from stemtrace.server.fastapi import extension as extension_module
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.groups import GROUPS_ENV_VAR
from stemtrace.server.ui import static

if TYPE_CHECKING:
    from pathlib import Path


_TOML = '[[groups]]\nname = "money"\nmatch = ["billing.*"]\ntags = ["pci"]\n'
_OTHER_TOML = '[[groups]]\nname = "other"\nmatch = ["other.*"]\n'
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _fake_static_router(
    prefix: str | None,
    *,
    show_logout: bool = False,
    logout_path: str | None = None,
) -> APIRouter:
    """Stand-in for the bundled UI so tests don't depend on a frontend build."""
    del prefix, show_logout, logout_path
    return APIRouter()


@pytest.fixture
def captured_groups(monkeypatch: pytest.MonkeyPatch) -> list[GroupConfig]:
    """Record the group config the CLI hands to the extension."""
    captured: list[GroupConfig] = []

    class _RecordingExtension(StemtraceExtension):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]  # forwarding the CLI's kwargs as-is
            captured.append(self.groups)

    monkeypatch.setattr(extension_module, "StemtraceExtension", _RecordingExtension)
    monkeypatch.setattr(static, "get_static_router_with_base", _fake_static_router)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)
    monkeypatch.delenv(GROUPS_ENV_VAR, raising=False)
    monkeypatch.delenv("STEMTRACE_TRANSPORT_URL", raising=False)
    return captured


def _run(*args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli.app, ["server", "--broker-url", "memory://", *args], env=env
    )
    # Typer colors its errors when CI forces a terminal (GITHUB_ACTIONS),
    # which splits the option name with ANSI codes.
    return result.exit_code, _ANSI.sub("", result.output)


def _file(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_server_with_groups_flag_loads_file(
    captured_groups: list[GroupConfig], tmp_path: Path
) -> None:
    code, output = _run("--groups", _file(tmp_path, "g.toml", _TOML))

    assert code == 0, output
    assert [r.name for r in captured_groups[0].rules] == ["money"]
    assert "Task groups: 1 code-defined" in output


def test_server_with_groups_env_var_loads_file(
    captured_groups: list[GroupConfig], tmp_path: Path
) -> None:
    code, output = _run(env={GROUPS_ENV_VAR: _file(tmp_path, "g.toml", _TOML)})

    assert code == 0, output
    assert [r.name for r in captured_groups[0].rules] == ["money"]


def test_server_with_flag_and_env_var_prefers_flag(
    captured_groups: list[GroupConfig], tmp_path: Path
) -> None:
    code, output = _run(
        "--groups",
        _file(tmp_path, "flag.toml", _TOML),
        env={GROUPS_ENV_VAR: _file(tmp_path, "env.toml", _OTHER_TOML)},
    )

    assert code == 0, output
    assert [r.name for r in captured_groups[0].rules] == ["money"]


def test_server_with_empty_groups_flag_falls_back_to_env_var(
    captured_groups: list[GroupConfig], tmp_path: Path
) -> None:
    code, output = _run(
        "--groups", "", env={GROUPS_ENV_VAR: _file(tmp_path, "env.toml", _OTHER_TOML)}
    )

    assert code == 0, output
    assert [r.name for r in captured_groups[0].rules] == ["other"]


@pytest.mark.parametrize("env", [None, {GROUPS_ENV_VAR: ""}, {GROUPS_ENV_VAR: "  "}])
def test_server_without_groups_is_automatic_only(
    captured_groups: list[GroupConfig], env: dict[str, str] | None
) -> None:
    code, output = _run(env=env)

    assert code == 0, output
    assert captured_groups == [GroupConfig()]
    assert "Task groups" not in output


def test_server_with_missing_groups_file_exits_with_usage_error(
    captured_groups: list[GroupConfig], tmp_path: Path
) -> None:
    # Wide enough that the usage error box doesn't wrap the message.
    code, output = _run(
        "--groups", str(tmp_path / "missing.toml"), env={"COLUMNS": "1000"}
    )

    assert code == 2
    assert "'--groups'" in output
    assert "Cannot read task groups file" in output
    assert "missing.toml" in output
    assert captured_groups == []


def test_groups_help_keeps_toml_table_syntax_under_rich_markup() -> None:
    """Rich markup must not swallow ``[[groups]]`` as a style tag.

    Typer's default "rich" markup mode renders option help with
    ``rich.text.Text.from_markup`` (typer 0.13.0 and 0.27.3,
    ``rich_utils._make_rich_text``). Rendered here directly rather than via
    ``--help``, which crashes on the typer 0.13 floor with click >= 8.2.
    """
    server = typer.main.get_command(cli.app).commands["server"]  # type: ignore[attr-defined]  # a click Group
    groups_help = next(p.help for p in server.params if p.name == "groups")

    assert "([[groups]] tables with" in Text.from_markup(groups_help).plain
