"""Load code-defined task groups from Python objects or a TOML file.

TOML format (``STEMTRACE_GROUPS=groups.toml`` or ``stemtrace server --groups``)::

    [[groups]]
    name = "billing"
    match = ["billing.*", "payments.*"]
    tags = ["money"]
    color = "#16a34a"

Rules apply in file order; the first match wins.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, ValidationError

from stemtrace.core.exceptions import ConfigurationError
from stemtrace.core.groups import GroupConfig, GroupRule

GROUPS_ENV_VAR = "STEMTRACE_GROUPS"

GroupsInput: TypeAlias = (
    GroupConfig
    | Sequence[GroupRule | Mapping[str, Any]]
    | str
    | os.PathLike[str]
    | None
)
"""A GroupConfig, rules as GroupRule objects or dicts, a TOML file path, or None."""


class _GroupsFile(BaseModel):
    """Top level of a groups TOML file: only ``[[groups]]`` tables."""

    model_config = ConfigDict(extra="forbid")

    groups: tuple[GroupRule, ...]


def _describe(error: ValidationError) -> str:
    """One line per problem, e.g. ``groups.0.color: String should match pattern``."""
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
        if e["loc"]
        else e["msg"]
        for e in error.errors()
    )


def _toml_loads() -> Callable[[str], dict[str, Any]]:
    """Return the TOML parser: stdlib ``tomllib``, or ``tomli`` before Python 3.11.

    ``tomli`` is the package ``tomllib`` was taken from, with the same
    ``loads`` and a ``TOMLDecodeError`` that subclasses ValueError; stemtrace
    depends on it only on Python 3.10. Imported by name: mypy checks against
    3.10, where ``tomllib`` doesn't exist, while ``tomli`` isn't installed on
    the newer Pythons the type check runs on.
    """
    module = "tomllib" if sys.version_info >= (3, 11) else "tomli"
    loads: Callable[[str], dict[str, Any]] = importlib.import_module(module).loads
    return loads


def load_groups_file(path: str | os.PathLike[str]) -> GroupConfig:
    """Read code-defined task groups from a TOML file.

    Args:
        path: TOML file with one ``[[groups]]`` table per group.

    Returns:
        The groups, in file order.

    Raises:
        ConfigurationError: If the file can't be read, isn't valid TOML, or
            has invalid or duplicate groups.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"Cannot read task groups file {file}: {exc}") from exc
    try:
        document = _toml_loads()(text)
    except ValueError as exc:  # TOMLDecodeError subclasses ValueError (both)
        raise ConfigurationError(
            f"Task groups file {file} is not valid TOML: {exc}"
        ) from exc
    try:
        parsed = _GroupsFile.model_validate(document)
        return GroupConfig(rules=parsed.groups)
    except ValidationError as exc:
        raise ConfigurationError(
            f"Invalid task groups in {file}: {_describe(exc)}"
        ) from exc


def build_group_config(groups: GroupsInput) -> GroupConfig:
    """Turn the ``groups=`` argument into a validated :class:`GroupConfig`.

    Args:
        groups: A GroupConfig (returned as is), rules (GroupRule objects or
            dicts with the same fields), a path to a TOML file, or None (or a
            blank string) for automatic groups only.

    Returns:
        The validated configuration.

    Raises:
        ConfigurationError: If a rule is invalid, two rules share a name, or
            the file can't be loaded (see :func:`load_groups_file`).
    """
    if isinstance(groups, str):
        groups = groups.strip() or None  # blank means none, not the cwd
    if groups is None:
        return GroupConfig()
    if isinstance(groups, GroupConfig):
        return groups
    if isinstance(groups, (str, os.PathLike)):
        return load_groups_file(groups)
    try:
        return GroupConfig.model_validate({"rules": groups})
    except ValidationError as exc:
        raise ConfigurationError(f"Invalid task groups: {_describe(exc)}") from exc


def configured_groups(groups: GroupsInput) -> GroupConfig:
    """Build the groups from ``groups``, or from ``STEMTRACE_GROUPS`` if it's unset.

    ``groups`` counts as unset when it is None or a blank string, so an empty
    ``--groups ""`` or ``groups=""`` falls back to the env var, as the node
    alias option does. A blank env var means no file.

    Args:
        groups: See :func:`build_group_config`.

    Returns:
        The validated configuration.

    Raises:
        ConfigurationError: See :func:`build_group_config`.
    """
    if isinstance(groups, str):
        groups = groups.strip() or None
    if groups is None:
        groups = (os.getenv(GROUPS_ENV_VAR) or "").strip() or None
    return build_group_config(groups)
