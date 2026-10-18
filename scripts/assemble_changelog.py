"""Merge changelog fragments into CHANGELOG.md under ``## [Unreleased]``.

Usage: python scripts/assemble_changelog.py [--check | --dry-run]

Each fragment is ``changelog.d/<slug>.<type>.md`` holding ``- `` bullets, where type is
a Keep a Changelog category. Text above the first ``###`` heading in Unreleased (such
as a Highlights block) stays on top. Without a flag, the script writes CHANGELOG.md
and deletes the fragments; ``--check`` only validates them; ``--dry-run`` prints.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Keep a Changelog order; also the order of the headings in the assembled section.
TYPES = ("added", "changed", "deprecated", "removed", "fixed", "security")
README_NAME = "README.md"
DESCRIPTION = "Merge changelog fragments into CHANGELOG.md under [Unreleased]."

_UNRELEASED = re.compile(r"^## \[Unreleased\][^\n]*\n", re.MULTILINE)
_NEXT_VERSION = re.compile(r"^## \[", re.MULTILINE)
_SUBHEADING = re.compile(r"^### +(.+?)[ \t]*$", re.MULTILINE)
_LINK_DEFINITIONS = re.compile(
    r"(?:^\[[^\]\n]+\]:[ \t]+\S+[ \t]*(?:\n|\Z))+\Z", re.MULTILINE
)


class ChangelogError(Exception):
    """CHANGELOG.md can't take the fragments."""


@dataclass(frozen=True)
class Fragment:
    """A valid changelog fragment."""

    path: Path
    type: str
    body: str


def _invalid_name(name: str) -> str | None:
    """Return why ``name`` isn't ``<slug>.<type>.md``, or None when it is."""
    stem = name.removesuffix(".md")
    slug, _, kind = stem.rpartition(".")
    if stem != name and slug and kind in TYPES:
        return None
    return f"name must be <slug>.<type>.md, type one of: {', '.join(TYPES)}"


def _invalid_body(body: str) -> str | None:
    """Return why ``body`` can't be inserted as bullets, or None when it can."""
    if not body:
        return "file is empty"
    if not body.startswith("- "):
        return "entry must start with a '- ' bullet"
    if re.search(r"^#", body, re.MULTILINE):
        return "entry must not contain headings (the file name sets the heading)"
    return None


def _read_fragment(path: Path) -> Fragment | str:
    """Return the fragment at ``path``, or the reason it is invalid."""
    problem = _invalid_name(path.name)
    if problem:
        return problem
    try:
        body = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return f"can't read file: {exc}"
    problem = _invalid_body(body)
    if problem:
        return problem
    return Fragment(path, path.name.removesuffix(".md").rpartition(".")[2], body)


def read_fragments(directory: Path) -> tuple[list[Fragment], list[str]]:
    """Read every fragment in ``directory``, sorted by file name.

    Args:
        directory: The ``changelog.d`` directory; a missing one has no fragments.

    Returns:
        The valid fragments and one message per invalid file. ``README.md`` is skipped.
    """
    fragments: list[Fragment] = []
    errors: list[str] = []
    paths = sorted(directory.iterdir()) if directory.is_dir() else []
    for path in paths:
        if path.name == README_NAME:
            continue
        result = _read_fragment(path)
        if isinstance(result, Fragment):
            fragments.append(result)
        else:
            errors.append(f"{path.name}: {result}")
    return fragments, errors


def _split_unreleased(text: str) -> tuple[str, str, str]:
    """Split ``text`` into the Unreleased heading, its body and the rest."""
    heading = _UNRELEASED.search(text)
    if heading is None:
        raise ChangelogError("CHANGELOG.md has no '## [Unreleased]' heading")
    rest = text[heading.end() :]
    boundary = _NEXT_VERSION.search(rest) or _LINK_DEFINITIONS.search(rest)
    cut = boundary.start() if boundary else len(rest)
    return text[: heading.end()], rest[:cut], rest[cut:]


def _parse_sections(body: str) -> tuple[str, dict[str, str]]:
    """Split an Unreleased body into text above the headings and ``### `` sections."""
    headings = list(_SUBHEADING.finditer(body))
    preamble = body[: headings[0].start()] if headings else body
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        content = body[heading.end() : end].strip()
        previous = sections.get(heading.group(1))
        sections[heading.group(1)] = f"{previous}\n{content}" if previous else content
    return preamble.strip(), sections


def merge_fragments(text: str, fragments: list[Fragment]) -> str:
    """Return ``text`` with ``fragments`` merged into its Unreleased section.

    Args:
        text: Full CHANGELOG.md content (Keep a Changelog format).
        fragments: Valid fragments; entries are appended in the order given.

    Returns:
        The new CHANGELOG.md content; ``text`` unchanged when there are no fragments.

    Raises:
        ChangelogError: If ``text`` has no ``## [Unreleased]`` heading.
    """
    if not fragments:
        return text
    head, body, tail = _split_unreleased(text)
    preamble, sections = _parse_sections(body)
    for fragment in fragments:
        name = fragment.type.capitalize()
        previous = sections.get(name)
        sections[name] = f"{previous}\n{fragment.body}" if previous else fragment.body
    known = [kind.capitalize() for kind in TYPES]
    ordered = sorted(
        sections, key=lambda n: known.index(n) if n in known else len(known)
    )
    blocks = [preamble] if preamble else []
    blocks += [f"### {name}\n{sections[name]}".rstrip("\n") for name in ordered]
    return f"{head}\n" + "\n\n".join(blocks) + "\n" + (f"\n{tail}" if tail else "")


def main(argv: list[str] | None = None) -> int:
    """Validate, merge and delete the fragments; return the exit status."""
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate fragments only")
    mode.add_argument(
        "--dry-run", action="store_true", help="print the new CHANGELOG.md"
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    args = parser.parse_args(argv)

    changelog = args.root / "CHANGELOG.md"
    fragments, errors = read_fragments(args.root / "changelog.d")
    try:
        merged = merge_fragments(changelog.read_text(encoding="utf-8"), fragments)
    except (ChangelogError, OSError) as exc:
        errors.append(str(exc))
    for error in errors:
        print(f"changelog.d: {error}", file=sys.stderr)
    if errors:
        return 1
    if args.check:
        print(f"{len(fragments)} changelog fragment(s) valid")
    elif args.dry_run:
        sys.stdout.write(merged)
    elif fragments:
        changelog.write_text(merged, encoding="utf-8")
        for fragment in fragments:
            fragment.path.unlink()
        print(f"Assembled {len(fragments)} fragment(s) into CHANGELOG.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
