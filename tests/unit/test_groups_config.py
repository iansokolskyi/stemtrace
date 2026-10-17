"""Tests for loading task groups from code and TOML files (server/groups.py)."""

import re
import sys
from pathlib import Path

import pytest

from stemtrace.core.exceptions import ConfigurationError
from stemtrace.core.groups import GroupConfig, GroupRule, TaskAttributes
from stemtrace.server.groups import (
    GROUPS_ENV_VAR,
    build_group_config,
    configured_groups,
    load_groups_file,
)

needs_tomllib = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib is Python 3.11+"
)

_VALID_TOML = """
[[groups]]
name = "everything"
match = ["*"]

[[groups]]
name = "billing"
match = ["billing.*", "payments.*"]
tags = ["money", "pci"]
color = "#16a34a"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "groups.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_groups_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GROUPS_ENV_VAR, raising=False)


# --- TOML files ------------------------------------------------------------


@needs_tomllib
def test_load_groups_file_keeps_file_order(tmp_path: Path) -> None:
    config = load_groups_file(_write(tmp_path, _VALID_TOML))

    assert config.rules == (
        GroupRule(name="everything", match=["*"]),
        GroupRule(
            name="billing",
            match=["billing.*", "payments.*"],
            tags=["money", "pci"],
            color="#16a34a",
        ),
    )
    # file order decides: the catch-all comes first, so it wins
    assert config.resolve(TaskAttributes(name="billing.charge")).name == "everything"


@needs_tomllib
def test_load_groups_file_accepts_str_path(tmp_path: Path) -> None:
    config = load_groups_file(str(_write(tmp_path, _VALID_TOML)))

    assert [r.name for r in config.rules] == ["everything", "billing"]


@needs_tomllib
def test_load_groups_file_with_unicode_names(tmp_path: Path) -> None:
    path = _write(tmp_path, '[[groups]]\nname = "платежі"\nmatch = ["платежі.*"]\n')

    config = load_groups_file(path)

    assert config.resolve(TaskAttributes(name="платежі.charge")).name == "платежі"


def test_load_groups_file_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"

    with pytest.raises(ConfigurationError, match="Cannot read task groups file"):
        load_groups_file(missing)


def test_load_groups_file_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Cannot read task groups file"):
        load_groups_file(tmp_path)


def test_load_groups_file_not_utf8_raises(tmp_path: Path) -> None:
    path = tmp_path / "groups.toml"
    path.write_bytes(b'[[groups]]\nname = "\xff"\n')

    with pytest.raises(ConfigurationError, match="Cannot read task groups file"):
        load_groups_file(path)


@needs_tomllib
def test_load_groups_file_invalid_toml_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "[[groups]\nname = ")

    with pytest.raises(ConfigurationError, match="is not valid TOML"):
        load_groups_file(path)


@needs_tomllib
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "groups: Field required"),
        ('[[group]]\nname = "a"\nmatch = ["a.*"]\n', "group: Extra inputs"),
        ('[[groups]]\nname = "a"\n', "groups.0.match: Field required"),
        ('[[groups]]\nname = "a"\nmatch = "a.*"\n', "groups.0.match"),
        ('[[groups]]\nname = "a"\nmatches = ["a.*"]\n', "groups.0.matches: Extra"),
        ('[[groups]]\nname = "a"\nmatch = ["a.*"]\ncolor = "red"\n', "groups.0.color"),
        (
            '[[groups]]\nname = "a"\nmatch = ["a.*"]\n'
            '[[groups]]\nname = "a"\nmatch = ["b.*"]\n',
            "duplicate group name 'a'",
        ),
    ],
)
def test_load_groups_file_invalid_groups_raises_with_location(
    tmp_path: Path, text: str, message: str
) -> None:
    path = _write(tmp_path, text)

    with pytest.raises(ConfigurationError, match="Invalid task groups in") as info:
        load_groups_file(path)

    assert message in str(info.value)
    assert str(path) in str(info.value)


def test_load_groups_file_without_tomllib_explains_python_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python 3.10 has no tomllib; a None entry makes the import fail the same way."""
    monkeypatch.setitem(sys.modules, "tomllib", None)

    path = _write(tmp_path, _VALID_TOML)

    with pytest.raises(ConfigurationError, match=r"TOML needs Python 3\.11 or newer"):
        load_groups_file(path)

    with pytest.raises(ConfigurationError, match=re.escape(str(path))):
        load_groups_file(path)


@pytest.mark.skipif(sys.version_info >= (3, 11), reason="Python 3.10 only")
def test_load_groups_file_on_python_310_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"TOML needs Python 3\.11 or newer"):
        load_groups_file(_write(tmp_path, _VALID_TOML))


# --- groups= argument ------------------------------------------------------


def test_build_group_config_none_is_automatic_only() -> None:
    assert build_group_config(None) == GroupConfig()


def test_build_group_config_returns_group_config_as_is() -> None:
    config = GroupConfig(rules=[GroupRule(name="a", match=["a.*"])])

    assert build_group_config(config) is config


def test_build_group_config_from_rules_and_dicts_keeps_order() -> None:
    config = build_group_config(
        [
            GroupRule(name="billing", match=["billing.*"]),
            {"name": "search", "match": ["search.*"], "tags": ["s"]},
        ]
    )

    assert [r.name for r in config.rules] == ["billing", "search"]
    assert config.rules[1].tags == ("s",)


@pytest.mark.parametrize("blank", ["", "   "])
def test_build_group_config_blank_string_is_automatic_only(blank: str) -> None:
    assert build_group_config(blank) == GroupConfig()


def test_build_group_config_from_empty_list_is_automatic_only() -> None:
    assert build_group_config([]) == GroupConfig()


@pytest.mark.parametrize(
    ("groups", "message"),
    [
        ([{"name": "a"}], "rules.0.match: Field required"),
        ([{"name": "a", "match": ["a.*"], "tag": ["x"]}], "rules.0.tag: Extra"),
        (
            [{"name": "a", "match": ["a.*"]}, {"name": "a", "match": ["b.*"]}],
            "duplicate group name 'a'",
        ),
    ],
)
def test_build_group_config_invalid_rules_raises(
    groups: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(ConfigurationError, match="Invalid task groups") as info:
        build_group_config(groups)

    assert message in str(info.value)


@needs_tomllib
def test_build_group_config_from_path_loads_file(tmp_path: Path) -> None:
    config = build_group_config(_write(tmp_path, _VALID_TOML))

    assert [r.name for r in config.rules] == ["everything", "billing"]


# --- configured_groups: argument, then STEMTRACE_GROUPS --------------------


@needs_tomllib
def test_configured_groups_without_argument_reads_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GROUPS_ENV_VAR, f"  {_write(tmp_path, _VALID_TOML)}  ")

    config = configured_groups(None)

    assert [r.name for r in config.rules] == ["everything", "billing"]


@needs_tomllib
@pytest.mark.parametrize("blank", ["", "   "])
def test_configured_groups_blank_argument_falls_back_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv(GROUPS_ENV_VAR, str(_write(tmp_path, _VALID_TOML)))

    config = configured_groups(blank)

    assert [r.name for r in config.rules] == ["everything", "billing"]


def test_configured_groups_argument_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(GROUPS_ENV_VAR, str(tmp_path / "missing.toml"))

    config = configured_groups([{"name": "code", "match": ["*"]}])

    assert [r.name for r in config.rules] == ["code"]


@pytest.mark.parametrize("value", [None, "", "  "])
def test_configured_groups_blank_env_is_automatic_only(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv(GROUPS_ENV_VAR, value)

    assert configured_groups(None) == GroupConfig()


def test_configured_groups_env_file_missing_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(GROUPS_ENV_VAR, str(tmp_path / "missing.toml"))

    with pytest.raises(ConfigurationError, match=r"missing\.toml"):
        configured_groups(None)
