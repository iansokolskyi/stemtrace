"""Print one version's section of CHANGELOG.md, for use as GitHub release notes.

Usage: python scripts/changelog_section.py <version|Unreleased> [changelog_path]

Exits with status 1 when the section is missing or empty, so a release can't be
published without notes.
"""

import re
import sys
from pathlib import Path


def changelog_section(text: str, version: str) -> str:
    """Return the body of the ``## [version]`` section, without its heading.

    Args:
        text: Full CHANGELOG.md content (Keep a Changelog format).
        version: Version such as ``0.3.8`` (a leading ``v`` is ignored) or
            ``Unreleased``.

    Returns:
        The section body with surrounding blank lines stripped; empty if the
        section is missing or holds only subheadings or HTML comments.
    """
    version = version.removeprefix("v")
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    match = heading.search(text)
    if match is None:
        return ""
    rest = text[match.end() :]
    next_heading = re.search(r"^## \[", rest, re.MULTILINE)
    body = rest[: next_heading.start()] if next_heading else rest
    # Link reference definitions at the end of the file aren't release notes.
    body = re.sub(r"^\[[^\]]+\]:\s+\S+\s*$", "", body, flags=re.MULTILINE)
    body = body.strip()
    # A section with only "### Added"-style headings or comments has no notes.
    content = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    content = re.sub(r"^#{3,}.*$", "", content, flags=re.MULTILINE)
    return body if content.strip() else ""


def main(argv: list[str]) -> int:
    """Print the requested section; return 1 if it's missing or empty."""
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    path = Path(argv[2]) if len(argv) == 3 else Path("CHANGELOG.md")
    section = changelog_section(path.read_text(encoding="utf-8"), argv[1])
    if not section:
        print(f"No CHANGELOG.md entries for [{argv[1]}]", file=sys.stderr)
        return 1
    print(section)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
