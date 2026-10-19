"""Tests for the scripts/assemble_changelog.py fragment assembler."""

from __future__ import annotations

import collections
import importlib.util
import random
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "assemble_changelog.py"
_spec = importlib.util.spec_from_file_location("assemble_changelog", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
assemble = importlib.util.module_from_spec(_spec)
# Registered before exec: dataclasses resolves the module through sys.modules.
sys.modules["assemble_changelog"] = assemble
_spec.loader.exec_module(assemble)

_section_spec = importlib.util.spec_from_file_location(
    "changelog_section", ROOT / "scripts" / "changelog_section.py"
)
assert _section_spec is not None
assert _section_spec.loader is not None
changelog_section = importlib.util.module_from_spec(_section_spec)
_section_spec.loader.exec_module(changelog_section)

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


FENCED_BODIES = [
    "- Run:\n  ```sh\n  # install it\n  pip install stemtrace\n  ```\n",
    "- Run:\n  ~~~sh\n  # install it\n  ~~~\n",
    "- A.\n  - Nested:\n    ```\n    ## Heading-like\n    ---\n    ```\n",
    "- A.\n  ```\n  # shorter fence inside\n  ``\n  # still inside\n  ```\n",
    "- A.\n  ````\n  ```\n  # inside a four-backtick fence\n  ````\n",
    "- A.\n  ```\n  # unclosed fence runs to the end\n",
    "- ```\n  # fence opened on the bullet line\n  ```\n",
    "- A.\n  > ```\n  > # quoted fence\n  > ```\n",
]


@pytest.mark.parametrize(
    "body",
    [
        "- A.\n## [Unreleased]\n",
        "- A.\n## [9.9.9] - 2026-01-01\n",
        "- A.\n   ## [Unreleased]\n",
        "- A.\n   ### Sneaky\n",
        "- A.\n#### Deeper\n",
        # CommonMark measures the indent from the list item's content column.
        "- A.\n    ## [Unreleased]\n",
        "- A.\n     ### Five\n",
        "- ## [Unreleased]\n",
        "- A.\n  - ## Nested\n",
        "- A.\n\t## Tab\n",
        "- A.\n  - B.\n      ## Nested six\n",
        "- A.\n  - B.\n       ## Nested seven\n",
        "- A.\n  1. ## Ordered\n",
        "- 1. ## Ordered direct\n",
        "- A.\n  > ## Quote\n",
        "- A.\n  >## Quote without space\n",
        "- A.\n  ---\n",
        "- A.\n  ===\n",
        "- A.\n  - B.\n    ----\n",
        "- A.\n  ```\n  # In a fence\n  ```\n  ## After the fence\n",
        # A fence ends with its container: these headings are outside it.
        "- Adds:\n  ```sh\n  pip install x\n- Fixes.\n  ## Installation\n",
        "- A.\n  ~~~\n- # H\n",
        "- A.\n  > ```\n  > code\n  ## After the quote\n",
        "- A.\n  - B.\n    ```\n    code\n  ## After the nested item\n",
        "- A.\n  ~~~\n  ```\n  ~~~\n  ## Heading\n",
    ],
)
def test_check_fragment_with_heading_line_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    root = _project(tmp_path, "", {"1.added.md": body})

    assert assemble.main(["--root", str(root), "--check"]) == 1

    assert "1.added.md: entry must not contain headings" in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        "- Fixes `#152` and #153.\n",
        "- A.\n  #152 is the pull request.\n",
        "- A.\n  A line of text\n  with an = sign and a - dash.\n",
        "- A.\nlazy continuation\n",
        "- A.\n      # indented code, not a heading\n",
        "- A.\n\n  Second paragraph.\n\n  - Nested.\n",
        "- Fixes [#12](https://example.com/12) and <https://example.com> too.\n",
        "- A.\n  [x] is not a definition.\n",
        "- A reference to a note[^1] without a definition.\n",
        "- Writes `[^1]: note text` in code.\n",
        "- A.\n  ```\n  [^1]: note text in a fence\n  ```\n",
        "- A.\n\n  [^ 1]: a space in the label is not a footnote.\n",
        # A definition can't interrupt a paragraph, so this is plain text.
        "- A.\n  [x]: https://paragraph.example\n",
        "- Writes `<b>` and a < b and 1 > 0 in code and text.\n",
        "- A.\n  * Nested items may use other markers.\n  + Like this.\n",
        *FENCED_BODIES,
    ],
)
def test_check_fragment_with_markup_that_is_not_rejected_is_valid(
    tmp_path: Path, body: str
) -> None:
    root = _project(tmp_path, "", {"1.added.md": body})

    assert assemble.main(["--root", str(root), "--check"]) == 0


@pytest.mark.parametrize(
    ("body", "offender"),
    [
        ("Not a bullet.\n", "Not a bullet."),
        ("- A.\n\nSeparate paragraph.\n", "Separate paragraph."),
        ("- A.\n* Star.\n", "* Star."),
        ("+ Plus.\n", "+ Plus."),
        ("1. Ordered.\n", "1. Ordered."),
        ("> Quote.\n", "> Quote."),
        ("- A.\n\n---\n", "---"),
        ("- A.\n\n```\ncode\n```\n", "```"),
    ],
)
def test_check_fragment_with_top_level_block_that_is_not_a_dash_list_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str, offender: str
) -> None:
    root = _project(tmp_path, "", {"1.added.md": body})

    assert assemble.main(["--root", str(root), "--check"]) == 1

    err = capsys.readouterr().err
    assert f"1.added.md: entry must be only '- ' bullets, not {offender!r}" in err


@pytest.mark.parametrize(
    ("body", "offender"),
    [
        ("[x]: https://example.com\n", "[x]: https://example.com"),
        # The first definition wins, so this one would hijack the [0.4.1] link.
        ("- A.\n\n[0.4.1]: https://evil.example\n", "[0.4.1]: https://evil.example"),
        (
            "- A.\n\n[unreleased]: https://example.com/compare/v0.4.1...HEAD\n",
            "[unreleased]: https://example.com/compare/v0.4.1...HEAD",
        ),
        ("- A.\n\n[^x]: footnote-style\n", "[^x]: footnote-style"),
        # A multi-word note isn't a CommonMark definition, but GitHub renders a
        # footnote, and two fragments that reuse [^1] would overwrite each other.
        ("- A[^1]\n\n  [^1]: This is the note.\n", "  [^1]: This is the note."),
        ("- A[^1]\n[^1]: note text\n", "[^1]: note text"),
        ("- A[^1]\n  [^1]: note text\n", "  [^1]: note text"),
        ("- A.\n\n[^note-1]: Requires Redis 7.\n", "[^note-1]: Requires Redis 7."),
        ("- A.\n\n  [x]: https://nested.example\n", "  [x]: https://nested.example"),
        ("- [x]: https://example.com\n", "- [x]: https://example.com"),
        ("- A.\n\n[x]: <https://e> 'Title'\n", "[x]: <https://e> 'Title'"),
    ],
)
def test_check_fragment_with_link_reference_definition_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str, offender: str
) -> None:
    root = _project(tmp_path, "", {"1.added.md": body})

    assert assemble.main(["--root", str(root), "--check"]) == 1

    err = capsys.readouterr().err
    assert (
        "1.added.md: entry must not contain link reference definitions, "
        f"found {offender!r}" in err
    )


@pytest.mark.parametrize(
    ("body", "offender"),
    [
        ("- C.\n\n  <h2>Hi</h2>\n", "  <h2>Hi</h2>"),
        (
            "- C.\n\n  </li></ul><h2>Top</h2><ul><li>\n",
            "  </li></ul><h2>Top</h2><ul><li>",
        ),
        ("- Uses <b>bold</b> text.\n", "- Uses <b>bold</b> text."),
        ("- A.\n\n<div>Raw HTML.</div>\n", "<div>Raw HTML.</div>"),
        ("- <!-- hidden -->\n", "- <!-- hidden -->"),
        ("- A.\n  - B <br> C.\n", "  - B <br> C."),
    ],
)
def test_check_fragment_with_raw_html_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str, offender: str
) -> None:
    root = _project(tmp_path, "", {"1.added.md": body})

    assert assemble.main(["--root", str(root), "--check"]) == 1

    err = capsys.readouterr().err
    assert f"1.added.md: entry must not contain raw HTML, found {offender!r}" in err


@pytest.mark.parametrize(
    ("body", "offender"),
    [("-", "-"), ("- A.\n-\n- B.\n", "-"), ("- -\n", "- -"), ("- A.\n\n-\n", "-")],
)
def test_check_fragment_with_empty_bullet_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str, offender: str
) -> None:
    root = _project(tmp_path, "", {"1.added.md": body})

    assert assemble.main(["--root", str(root), "--check"]) == 1

    err = capsys.readouterr().err
    assert f"1.added.md: entry must not contain an empty bullet: {offender!r}" in err


def test_check_fragment_with_utf8_bom_is_valid(tmp_path: Path) -> None:
    root = _project(tmp_path, "", {})
    (root / "changelog.d" / "1.added.md").write_bytes(b"\xef\xbb\xbf- A.\n")

    assert assemble.main(["--root", str(root)]) == 0

    assert _unreleased(root) == "## [Unreleased]\n\n### Added\n- A.\n\n"


def test_check_ignores_dotfiles_in_fragment_directory(tmp_path: Path) -> None:
    root = _project(tmp_path, "", {"1.added.md": "- A.\n"})
    (root / "changelog.d" / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")

    assert assemble.main(["--root", str(root), "--check"]) == 0


def test_check_prose_after_unreleased_subheading_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path,
        "\n### Changed\n- Existing.\n\n**Upgrading:** any order.\n",
        {"1.added.md": "- A.\n"},
    )
    before = _changelog(root)

    assert assemble.main(["--root", str(root), "--check"]) == 1

    err = capsys.readouterr().err
    assert "'- ' bullets" in err
    assert "**Upgrading:** any order." in err
    assert _changelog(root) == before


@pytest.mark.parametrize(
    ("section", "problem"),
    [
        (
            "- Existing.\n\n[0.1.0]: https://evil.example\n",
            "must not contain link reference definitions, "
            "found '[0.1.0]: https://evil.example'",
        ),
        (
            "- Existing[^1]\n\n  [^1]: A note.\n",
            "must not contain link reference definitions, found '  [^1]: A note.'",
        ),
        (
            "- Existing.\n\n<h2>Top</h2>\n",
            "must not contain raw HTML, found '<h2>Top</h2>'",
        ),
        (
            "- Existing <b>bold</b>.\n",
            "must not contain raw HTML, found '- Existing <b>bold</b>.'",
        ),
        ("-\n", "must not contain an empty bullet: '-'"),
        ("- Existing.\n  ## Nested\n", "must not contain headings: '  ## Nested'"),
    ],
)
def test_check_unreleased_section_with_unsupported_markup_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], section: str, problem: str
) -> None:
    root = _project(tmp_path, f"\n### Changed\n{section}", {"1.added.md": "- A.\n"})

    assert assemble.main(["--root", str(root), "--check"]) == 1

    err = capsys.readouterr().err
    assert f"text under '### Changed' in [Unreleased] {problem}" in err


def _bump_config_for_changelog(source: Path) -> tuple[str, str]:
    """Return the repo's bump config cut down to its CHANGELOG.md entries."""
    text = source.read_text(encoding="utf-8")
    marker = "[[tool.bumpversion.files]]"
    header, *entries = text.split(marker)
    kept = [e for e in entries if 'filename = "CHANGELOG.md"' in e]
    match = re.search(r'^current_version = "([^"]+)"', header, re.MULTILINE)
    assert match is not None
    return header + "".join(marker + e for e in kept), match.group(1)


def test_assemble_then_bump_with_quoted_heading_and_link_edits_changelog_once(
    tmp_path: Path,
) -> None:
    # bump-my-version 1.6.0 (files.py ConfiguredFile.make_file_change) substitutes
    # every match of the CHANGELOG search pattern; this runs the real config end to end.
    config, current = _bump_config_for_changelog(ROOT / ".bumpversion.toml")
    major, minor, patch = (int(part) for part in current.split("."))
    new = f"{major}.{minor}.{patch + 1}"
    (tmp_path / ".bumpversion.toml").write_text(config, encoding="utf-8")
    link = "https://github.com/iansokolskyi/stemtrace/compare"
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{current}] - 2026-01-01\n\n"
        f"### Fixed\n- Old.\n\n[unreleased]: {link}/v{current}...HEAD\n",
        encoding="utf-8",
    )
    fragment_dir = tmp_path / "changelog.d"
    fragment_dir.mkdir()
    quoted = f"[unreleased]: {link}/v{current}...HEAD"
    bullet = (
        "- A maintainer's release merges entries into `## [Unreleased]`.\n"
        f"  The footer reads `{quoted}`; this line quotes it too:\n  {quoted}\n"
    )
    (fragment_dir / "1.changed.md").write_text(bullet, encoding="utf-8")
    assert assemble.main(["--root", str(tmp_path)]) == 0

    subprocess.run(
        [sys.executable, "-m", "bumpversion", "bump", "patch", "--no-commit"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=120,
    )

    text = _changelog(tmp_path)
    assert len(re.findall(rf"^## \[{new}\]", text, re.MULTILINE)) == 1
    assert len(re.findall(r"^## \[Unreleased\]", text, re.MULTILINE)) == 1
    assert (
        changelog_section.changelog_section(text, new)
        == f"### Changed\n{bullet.strip()}"
    )
    assert text.endswith(
        f"[unreleased]: {link}/v{new}...HEAD\n[{new}]: {link}/v{current}...v{new}\n"
    )


class _RenderedTags(HTMLParser):
    """Record every tag of rendered HTML, and which ones sit at the top level."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.tags: list[str] = []
        self.top_level: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        self.tags.append(tag)
        if self.depth == 0:
            self.top_level.append(tag)
        self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        self.depth -= 1


def _oracle_reasons(body: str) -> set[str]:
    """Say why body should be rejected, without the validator's token walk.

    It parses with the same ``MarkdownIt("commonmark")`` as the validator, so a
    parser bug shows in both: what differs is the check, not the parser.
    Headings and top-level blocks come from rendered HTML, raw HTML from comparing
    renders with HTML on and off, and empty bullets from the output. The list marker
    and link definitions are not in the HTML, so those two read the parser output.
    """
    md = MarkdownIt("commonmark")
    html = md.render(body)
    tags = _RenderedTags()
    tags.feed(html)
    env: dict[str, object] = {}
    tokens = md.parse(body, env)
    checks = {
        "heading": any(re.fullmatch(r"h[1-6]", tag) for tag in tags.tags),
        "block": any(tag != "ul" for tag in tags.top_level),
        "html": html != MarkdownIt("commonmark", {"html": False}).render(body),
        "empty": "<li></li>" in html,
        "marker": any(
            t.type == "bullet_list_open" and t.level == 0 and t.markup != "-"
            for t in tokens
        ),
        "definition": bool(env.get("references"))
        or any(
            re.search(r"^\[\^[^\]\s]+\]:", t.content, re.MULTILINE)
            for t in tokens
            if t.type == "inline"
        ),
    }
    return {reason for reason, found in checks.items() if found}


def _fuzz_bodies(count: int, seed: int) -> list[str]:
    """Return seeded random bodies that mix indents, containers, fences and dedents."""
    rng = random.Random(seed)
    indents = ["", "", " ", "  ", "   ", "    ", "\t", "      "]
    prefixes = ["", "", "", "- ", "1. ", "> ", ">", "- - ", "> - ", "* ", "+ "]
    texts = ["# H", "## H", "###### H", "####### H", "#x", "---", "===", "--", "text"]
    texts += ["```", "```sh", "~~~", "````", "``", "`# x`", "- B", "- # H", "", ""]
    texts += ["more text", "word # not", "    code", "- B\n  - C", "-", "- "]
    texts += ["<h2>Hi</h2>", "<b>x</b> y", "a < b", "<div>", "[x]: https://e"]
    texts += ["[^x]: note", "[x]", "* B", "+ B", "[x]: https://e 'T'"]
    texts += ["[^1]: Requires Redis 7.", "A[^1]", "[^ 1]: no"]
    openers = [
        "- A.",
        "- ```",
        "- > Q",
        "- 1. N",
        "- - N",
        "- A.\n  ```",
        "- A.\n  ~~~",
    ]
    bodies = []
    for _ in range(count):
        lines = [rng.choice(openers)]
        for _ in range(rng.randint(1, 6)):
            lines.append(rng.choice(indents) + rng.choice(prefixes) + rng.choice(texts))
        bodies.append("\n".join(lines).strip())
    return bodies


def test_invalid_body_agrees_with_rendered_html_oracle_on_random_bodies() -> None:
    bodies = _fuzz_bodies(count=6000, seed=20261018)
    reasons = {body: _oracle_reasons(body) for body in bodies}

    disagreements = [
        body
        for body in bodies
        if (assemble._invalid_body(body) is not None) != bool(reasons[body])
    ]

    assert disagreements == []
    counts = collections.Counter(r for found in reasons.values() for r in found)
    assert set(counts) == {"heading", "block", "html", "empty", "marker", "definition"}
    assert min(counts.values()) >= 100
    assert sum(1 for found in reasons.values() if not found) >= 500
    assert sum(1 for b, found in reasons.items() if found and "```" in b) >= 100


def test_released_changelog_sections_and_current_fragments_pass_the_rules() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.split(r"^## \[", text, flags=re.MULTILINE)[1:]

    for section in sections:
        body = assemble._LINK_DEFINITIONS.sub("", section.partition("\n")[2])
        assemble._parse_sections(body)

    assert len(sections) >= 15
    assert assemble.main(["--root", str(ROOT), "--check"]) == 0
