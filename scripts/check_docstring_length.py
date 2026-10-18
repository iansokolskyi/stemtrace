"""Fail when a docstring's prose is longer than code-quality rule 3.5 allows.

Prose is every non-blank docstring line outside the Google sections (Args, Returns,
Raises, Yields, Example(s), Attributes). Functions and classes may have 5 lines of
prose (the summary plus 4); modules may have 8. Files in the allowlist are skipped,
and an allowlisted file that already passes is an error, so the list only shrinks.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_DEFINITION_PROSE_LINES = 5
MAX_MODULE_PROSE_LINES = 8
REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = REPO_ROOT / "scripts" / "docstring_length_allowlist.txt"

_SECTION_HEADER = re.compile(
    r"^(Args|Arguments|Returns|Return|Yields|Yield|Raises|Example|Examples|"
    r"Attributes):\s*$"
)
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class Violation:
    """A docstring whose prose exceeds its limit."""

    path: Path
    line: int
    name: str
    prose_lines: int
    limit: int

    def __str__(self) -> str:
        """Format as ``path:line name: N prose lines (max M)``."""
        return (
            f"{_display(self.path)}:{self.line} {self.name}: "
            f"{self.prose_lines} prose lines (max {self.limit})"
        )


def _display(path: Path) -> str:
    """Show ``path`` relative to the working directory when it is under it."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def count_prose_lines(docstring: str) -> int:
    """Count the non-blank docstring lines that sit outside the Google sections.

    A section starts at an unindented ``Header:`` line and covers the indented
    lines below it; the next unindented non-header line is prose again.
    """
    count = 0
    in_section = False
    for line in docstring.splitlines():
        if not line.strip():
            continue
        if _SECTION_HEADER.match(line):
            in_section = True
            continue
        if in_section and line[:1].isspace():
            continue
        in_section = False
        count += 1
    return count


def check_source(source: str, path: Path) -> list[Violation]:
    """Return the over-long docstrings in one Python source file, by line."""
    tree = ast.parse(source, filename=str(path))
    nodes: list[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, _DEFINITIONS))]
    violations: list[Violation] = []
    for node in nodes:
        docstring = ast.get_docstring(node)
        if docstring is None:
            continue
        is_module = isinstance(node, ast.Module)
        limit = MAX_MODULE_PROSE_LINES if is_module else MAX_DEFINITION_PROSE_LINES
        prose = count_prose_lines(docstring)
        if prose > limit:
            violations.append(
                Violation(
                    path=path,
                    line=1 if is_module else node.lineno,
                    name="<module>" if is_module else node.name,
                    prose_lines=prose,
                    limit=limit,
                )
            )
    return sorted(violations, key=lambda v: v.line)


def _inside(path: Path, root: Path) -> Path:
    """Resolve ``path`` and return it, or raise ValueError if it leaves ``root``."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"{path} is outside {root}")
    return resolved


def load_allowlist(path: Path, root: Path) -> set[Path]:
    """Read allowlisted files, one path relative to ``root`` per line.

    ``#`` starts a comment. A missing allowlist file means an empty list.

    Raises:
        ValueError: An entry points outside ``root``.
    """
    if not path.exists():
        return set()
    entries: set[Path] = set()
    for raw in path.read_text().splitlines():
        entry = raw.split("#", 1)[0].strip()
        if entry:
            entries.add(_inside(root / entry, root))
    return entries


def _repo_path(value: str) -> Path:
    """Parse a CLI path, rejecting anything outside the repository."""
    try:
        return _inside(Path(value), REPO_ROOT)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def run(roots: list[Path], allowlist: set[Path]) -> list[str]:
    """Check every ``.py`` file under ``roots`` and return the error lines."""
    files = {
        p.resolve()
        for root in roots
        for p in ([root] if root.is_file() else root.rglob("*.py"))
    }
    errors: list[str] = []
    failing: set[Path] = set()
    for path in sorted(files):
        violations = check_source(path.read_text(), path)
        if not violations:
            continue
        failing.add(path)
        if path not in allowlist:
            errors.extend(str(v) for v in violations)
    stale = {
        entry
        for entry in allowlist
        if not entry.exists() or (entry in files and entry not in failing)
    }
    errors.extend(
        f"{_display(entry)}: allowlisted but missing or within the limits; "
        "remove it from the allowlist"
        for entry in sorted(stale)
    )
    return errors


def main(argv: list[str] | None = None) -> int:
    """Run the check and print each error; return the process exit code."""
    parser = argparse.ArgumentParser(
        description="Fail on docstrings longer than code-quality rule 3.5 allows."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=_repo_path,
        default=[REPO_ROOT / "src" / "stemtrace"],
        help="files or directories inside the repository (default: src/stemtrace)",
    )
    args = parser.parse_args(argv)
    # Always the committed allowlist next to this script, never one from argv.
    errors = run(args.paths, load_allowlist(ALLOWLIST, REPO_ROOT))
    for error in errors:
        print(error)
    if errors:
        print(
            f"{len(errors)} docstring length error(s); see code-quality rule 3.5",
            file=sys.stderr,
        )
        return 1
    print("✓ All docstrings within length limits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
