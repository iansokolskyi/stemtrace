"""Tests for the scripts/assemble_changelog.py fragment assembler."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "assemble_changelog.py"
_spec = importlib.util.spec_from_file_location("assemble_changelog", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
assemble = importlib.util.module_from_spec(_spec)
# Registered before exec: dataclasses resolves the module through sys.modules.
sys.modules["assemble_changelog"] = assemble
_spec.loader.exec_module(assemble)

HEADER = "# Changelog\n\nIntro.\n\n"
RELEASED = "## [0.1.0] - 2026-01-01\n\n### Fixed\n- Old bug.\n"
LINKS = "\n[unreleased]: https://example.com/compare/v0.1.0...HEAD\n"


def _project(tmp_path: Path, unreleased: str, fragments: dict[str, str]) -> Path:
    """Write a CHANGELOG.md and changelog.d/ under tmp_path; return the root."""
    text = f"{HEADER}## [Unreleased]\n{unreleased}\n{RELEASED}{LINKS}"
    (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    fragment_dir = tmp_path / "changelog.d"
    fragment_dir.mkdir()
    (fragment_dir / "README.md").write_text("How to add an entry.\n", encoding="utf-8")
    for name, body in fragments.items():
        (fragment_dir / name).write_text(body, encoding="utf-8")
    return tmp_path


def _changelog(root: Path) -> str:
    return (root / "CHANGELOG.md").read_text(encoding="utf-8")


def _unreleased(root: Path) -> str:
    text = _changelog(root)
    start = text.index("## [Unreleased]")
    return text[start : text.index("## [0.1.0]")]


def _fragment_names(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "changelog.d").iterdir())


def test_assemble_into_existing_heading_appends_after_existing_entries(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        "\n### Added\n- Existing.\n",
        {"12.added.md": "- From a fragment.\n"},
    )

    assert assemble.main(["--root", str(root)]) == 0

    assert _unreleased(root) == (
        "## [Unreleased]\n\n### Added\n- Existing.\n- From a fragment.\n\n"
    )


def test_assemble_without_heading_creates_it(tmp_path: Path) -> None:
    root = _project(tmp_path, "", {"7.fixed.md": "- A fix.\n"})

    assert assemble.main(["--root", str(root)]) == 0

    assert _unreleased(root) == "## [Unreleased]\n\n### Fixed\n- A fix.\n\n"


def test_assemble_orders_headings_by_keep_a_changelog_order(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        "\n### Fixed\n- Existing fix.\n",
        {
            "1.security.md": "- S.\n",
            "2.removed.md": "- R.\n",
            "3.added.md": "- A.\n",
            "4.deprecated.md": "- D.\n",
            "5.changed.md": "- C.\n",
            "6.fixed.md": "- F.\n",
        },
    )

    assert assemble.main(["--root", str(root)]) == 0

    assert _unreleased(root) == (
        "## [Unreleased]\n\n"
        "### Added\n- A.\n\n"
        "### Changed\n- C.\n\n"
        "### Deprecated\n- D.\n\n"
        "### Removed\n- R.\n\n"
        "### Fixed\n- Existing fix.\n- F.\n\n"
        "### Security\n- S.\n\n"
    )


def test_assemble_sorts_fragments_by_filename_within_a_type(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        "",
        {
            "b-slug.added.md": "- B.\n",
            "a-slug.added.md": "- A.\n",
            "c-slug.added.md": "- C1.\n- C2.\n",
        },
    )

    assert assemble.main(["--root", str(root)]) == 0

    assert _unreleased(root) == (
        "## [Unreleased]\n\n### Added\n- A.\n- B.\n- C1.\n- C2.\n\n"
    )


def test_assemble_keeps_highlights_block_above_headings(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        "\n**Highlights:**\n- Big thing.\n\n**Upgrading:** any order.\n\n"
        "### Changed\n- Existing.\n",
        {"9.added.md": "- New.\n"},
    )

    assert assemble.main(["--root", str(root)]) == 0

    assert _unreleased(root) == (
        "## [Unreleased]\n\n"
        "**Highlights:**\n- Big thing.\n\n**Upgrading:** any order.\n\n"
        "### Added\n- New.\n\n"
        "### Changed\n- Existing.\n\n"
    )


def test_assemble_preserves_text_outside_unreleased(tmp_path: Path) -> None:
    root = _project(tmp_path, "", {"1.added.md": "- A.\n"})

    assert assemble.main(["--root", str(root)]) == 0

    text = _changelog(root)
    assert text.startswith(HEADER)
    assert text.endswith(f"{RELEASED}{LINKS}")


def test_assemble_when_unreleased_is_last_keeps_link_references(
    tmp_path: Path,
) -> None:
    text = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- Old.\n\n"
        "[unreleased]: https://example.com\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "1.added.md").write_text("- New.\n", encoding="utf-8")

    assert assemble.main(["--root", str(tmp_path)]) == 0

    assert _changelog(tmp_path) == (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- Old.\n- New.\n\n"
        "[unreleased]: https://example.com\n"
    )


def test_assemble_keeps_unknown_headings_after_the_standard_ones(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        "\n### Notes\n- Custom.\n\n### Added\n- A1.\n",
        {"1.fixed.md": "- F.\n"},
    )

    assert assemble.main(["--root", str(root)]) == 0

    assert _unreleased(root) == (
        "## [Unreleased]\n\n### Added\n- A1.\n\n### Fixed\n- F.\n\n"
        "### Notes\n- Custom.\n\n"
    )


def test_assemble_keeps_multiline_bullets_intact(tmp_path: Path) -> None:
    body = "- First line\n  continues here.\n\n  Second paragraph.\n- Second bullet.\n"
    root = _project(tmp_path, "", {"1.added.md": body})

    assert assemble.main(["--root", str(root)]) == 0

    assert _unreleased(root) == f"## [Unreleased]\n\n### Added\n{body}\n"


def test_assemble_deletes_fragments_but_keeps_readme(tmp_path: Path) -> None:
    root = _project(tmp_path, "", {"1.added.md": "- A.\n", "2.fixed.md": "- F.\n"})

    assert assemble.main(["--root", str(root)]) == 0

    assert _fragment_names(root) == ["README.md"]


def test_assemble_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    root = _project(tmp_path, "", {"1.added.md": "- A.\n"})
    assert assemble.main(["--root", str(root)]) == 0
    once = _changelog(root)

    assert assemble.main(["--root", str(root)]) == 0

    assert _changelog(root) == once


def test_assemble_without_fragments_leaves_changelog_untouched(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, "\n### Added\n- Existing.\n", {})
    before = _changelog(root)

    assert assemble.main(["--root", str(root)]) == 0

    assert _changelog(root) == before


def test_assemble_without_changelog_dir_succeeds(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")

    assert assemble.main(["--root", str(tmp_path), "--check"]) == 0


@pytest.mark.parametrize(
    "name",
    ["1.feature.md", "1.Added.md", "1.md", "1.added.txt", "added.md", "1.added.md.bak"],
)
def test_assemble_rejects_fragment_with_invalid_name(
    tmp_path: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, "", {name: "- A.\n", "2.added.md": "- Fine.\n"})
    before = _changelog(root)

    assert assemble.main(["--root", str(root)]) == 1

    assert name in capsys.readouterr().err
    assert _changelog(root) == before
    assert {name, "2.added.md"} <= set(_fragment_names(root))


@pytest.mark.parametrize(
    "body",
    [
        "",
        "\n\n",
        "Not a bullet.\n",
        "### Added\n- Heading inside.\n",
        "- Ok.\n## [1.0.0]\n",
    ],
)
def test_assemble_rejects_fragment_with_invalid_content(
    tmp_path: Path, body: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, "", {"1.added.md": body})

    assert assemble.main(["--root", str(root)]) == 1

    assert "1.added.md" in capsys.readouterr().err
    assert "1.added.md" in _fragment_names(root)


def test_assemble_reports_every_invalid_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, "", {"1.nope.md": "- A.\n", "2.added.md": "text\n"})

    assert assemble.main(["--root", str(root), "--check"]) == 1

    err = capsys.readouterr().err
    assert "1.nope.md" in err
    assert "2.added.md" in err


def test_assemble_without_unreleased_heading_fails_and_keeps_fragments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "1.added.md").write_text("- A.\n", encoding="utf-8")

    assert assemble.main(["--root", str(tmp_path)]) == 1

    assert "## [Unreleased]" in capsys.readouterr().err
    assert (tmp_path / "changelog.d" / "1.added.md").exists()


def test_check_valid_fragments_writes_and_deletes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, "", {"1.added.md": "- A.\n"})
    before = _changelog(root)

    assert assemble.main(["--root", str(root), "--check"]) == 0

    assert _changelog(root) == before
    assert _fragment_names(root) == ["1.added.md", "README.md"]


def test_dry_run_prints_result_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, "", {"1.added.md": "- A.\n"})
    before = _changelog(root)

    assert assemble.main(["--root", str(root), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "## [Unreleased]\n\n### Added\n- A.\n\n## [0.1.0]" in out
    assert _changelog(root) == before
    assert _fragment_names(root) == ["1.added.md", "README.md"]


def test_check_and_dry_run_together_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        assemble.main(["--root", str(tmp_path), "--check", "--dry-run"])

    assert exc.value.code == 2
