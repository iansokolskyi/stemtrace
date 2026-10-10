"""Tests for the scripts/changelog_section.py release-notes helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "changelog_section.py"
_spec = importlib.util.spec_from_file_location("changelog_section", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
changelog_section = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog_section)

CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-01-02

### Added
- New thing.

## [0.1.0] - 2026-01-01

### Fixed
- Old bug.

[0.2.0]: https://example.com/compare/v0.1.0...v0.2.0
"""


def test_section_for_version_returns_body_without_heading() -> None:
    assert changelog_section.changelog_section(CHANGELOG, "0.2.0") == (
        "### Added\n- New thing."
    )


def test_section_accepts_leading_v() -> None:
    assert changelog_section.changelog_section(CHANGELOG, "v0.2.0") == (
        "### Added\n- New thing."
    )


def test_last_section_drops_link_references() -> None:
    assert changelog_section.changelog_section(CHANGELOG, "0.1.0") == (
        "### Fixed\n- Old bug."
    )


def test_empty_or_missing_section_returns_empty() -> None:
    assert changelog_section.changelog_section(CHANGELOG, "Unreleased") == ""
    assert changelog_section.changelog_section(CHANGELOG, "9.9.9") == ""


def test_version_dots_are_not_regex_wildcards() -> None:
    assert changelog_section.changelog_section(CHANGELOG, "0x2x0") == ""


def test_main_prints_section_and_fails_when_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")

    assert changelog_section.main(["x", "0.2.0", str(path)]) == 0
    assert capsys.readouterr().out == "### Added\n- New thing.\n"

    assert changelog_section.main(["x", "Unreleased", str(path)]) == 1
    assert "No CHANGELOG.md entries for [Unreleased]" in capsys.readouterr().err


def test_every_released_version_in_repo_changelog_has_notes() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = [
        line.split("]")[0].removeprefix("## [")
        for line in text.splitlines()
        if line.startswith("## [") and not line.startswith("## [Unreleased]")
    ]
    assert versions
    for version in versions:
        assert changelog_section.changelog_section(text, version), version
