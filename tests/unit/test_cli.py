"""Unit tests for ``stemtrace server`` CLI options passed to the extension."""

from __future__ import annotations

import pytest
import uvicorn
from fastapi import APIRouter
from typer.testing import CliRunner

from stemtrace.server import __main__ as cli
from stemtrace.server.fastapi import extension as extension_module
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.ui import static

_ENV_VAR = "STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS"


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
def captured_alias(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Record the ``node_alias_from_arguments`` the CLI hands to the extension."""
    captured: list[str | None] = []

    class _RecordingExtension(StemtraceExtension):
        def __init__(
            self,
            *args: object,
            node_alias_from_arguments: str | None = None,
            **kwargs: object,
        ) -> None:
            captured.append(node_alias_from_arguments)
            super().__init__(
                *args, node_alias_from_arguments=node_alias_from_arguments, **kwargs
            )

    monkeypatch.setattr(extension_module, "StemtraceExtension", _RecordingExtension)
    monkeypatch.setattr(static, "get_static_router_with_base", _fake_static_router)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)
    monkeypatch.delenv(_ENV_VAR, raising=False)
    monkeypatch.delenv("STEMTRACE_TRANSPORT_URL", raising=False)
    return captured


def _invoke(*args: str, env: dict[str, str] | None = None) -> str:
    """Run ``stemtrace server`` with a memory broker, assert success, return output."""
    result = CliRunner().invoke(
        cli.app, ["server", "--broker-url", "memory://", *args], env=env
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_server_with_node_alias_flag_passes_alias(
    captured_alias: list[str | None],
) -> None:
    """``--node-alias-from-arguments`` reaches the extension unchanged."""
    _invoke("--node-alias-from-arguments", "0")

    assert captured_alias == ["0"]


def test_server_with_node_alias_env_var_passes_alias(
    captured_alias: list[str | None],
) -> None:
    """The env var is used when the flag is omitted."""
    _invoke(env={_ENV_VAR: "operator_type"})

    assert captured_alias == ["operator_type"]


def test_server_with_flag_and_env_var_prefers_flag(
    captured_alias: list[str | None],
) -> None:
    """An explicit flag overrides the env var."""
    _invoke("--node-alias-from-arguments", "0", env={_ENV_VAR: "operator_type"})

    assert captured_alias == ["0"]


def test_server_without_node_alias_passes_none(
    captured_alias: list[str | None],
) -> None:
    """Without flag or env var the extension gets ``None`` (task names are shown)."""
    _invoke()

    assert captured_alias == [None]


def test_server_with_empty_node_alias_env_var_passes_none(
    captured_alias: list[str | None],
) -> None:
    """An empty env var counts as unset."""
    _invoke(env={_ENV_VAR: ""})

    assert captured_alias == [None]


def test_server_with_empty_node_alias_flag_passes_none(
    captured_alias: list[str | None],
) -> None:
    """An empty flag value counts as unset."""
    _invoke("--node-alias-from-arguments", "")

    assert captured_alias == [None]


def test_server_with_empty_node_alias_flag_falls_back_to_env_var(
    captured_alias: list[str | None],
) -> None:
    """A blank flag counts as unset, so the env var still applies."""
    _invoke("--node-alias-from-arguments", "", env={_ENV_VAR: "operator_type"})

    assert captured_alias == ["operator_type"]


def test_server_with_padded_node_alias_env_var_strips_whitespace(
    captured_alias: list[str | None],
) -> None:
    """Surrounding whitespace (common in env files) is stripped."""
    _invoke(env={_ENV_VAR: " operator_type "})

    assert captured_alias == ["operator_type"]


def test_server_with_padded_node_alias_flag_strips_whitespace(
    captured_alias: list[str | None],
) -> None:
    """Surrounding whitespace in the flag value is stripped."""
    _invoke("--node-alias-from-arguments", " 0 ")

    assert captured_alias == ["0"]


def test_server_with_blank_node_alias_env_var_passes_none(
    captured_alias: list[str | None],
) -> None:
    """A whitespace-only env var counts as unset."""
    _invoke(env={_ENV_VAR: "  "})

    assert captured_alias == [None]


def test_server_with_node_alias_echoes_it_at_startup(
    captured_alias: list[str | None],
) -> None:
    """The resolved alias is printed with the other startup settings."""
    output = _invoke(env={_ENV_VAR: " operator_type "})

    assert "Node alias: operator_type\n" in output
    assert captured_alias == ["operator_type"]


def test_server_without_node_alias_does_not_echo_it(
    captured_alias: list[str | None],
) -> None:
    """No alias line is printed when the option is unset."""
    output = _invoke(env={_ENV_VAR: "  "})

    assert "Node alias" not in output
    assert "Broker: memory://" in output
    assert captured_alias == [None]
