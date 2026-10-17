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
        f"{'.'.join(str(part) for part in e['loc']) or 'value'}: {e['msg']}"
        for e in error.errors()
    )


def _toml_loads(file: Path) -> Callable[[str], dict[str, Any]]:
    """Return ``tomllib.loads``, or explain why TOML files need Python 3.11+.

    Imported by name rather than ``import tomllib``: mypy checks against
    Python 3.10, where the module doesn't exist.
    """
    try:
        loads: Callable[[str], dict[str, Any]] = importlib.import_module(
            "tomllib"
        ).loads
    except ImportError:
        raise ConfigurationError(
            f"Cannot load task groups file {file}: TOML needs Python 3.11 or "
            "newer (tomllib). On Python 3.10, pass the groups in code: "
            "init_app(groups=[GroupRule(...)])."
        ) from None
    return loads


def load_groups_file(path: str | os.PathLike[str]) -> GroupConfig:
    """Read code-defined task groups from a TOML file.

    Args:
        path: TOML file with one ``[[groups]]`` table per group.

    Returns:
        The groups, in file order.

    Raises:
        ConfigurationError: If the file can't be read, isn't valid TOML, has
            invalid or duplicate groups, or Python is older than 3.11.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"Cannot read task groups file {file}: {exc}") from exc
    loads = _toml_loads(file)
    try:
        document = loads(text)
    except ValueError as exc:  # tomllib.TOMLDecodeError subclasses ValueError
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
            dicts with the same fields), a path to a TOML file, or None for
            automatic groups only.

    Returns:
        The validated configuration.

    Raises:
        ConfigurationError: If a rule is invalid, two rules share a name, or
            the file can't be loaded (see :func:`load_groups_file`).
    """
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
