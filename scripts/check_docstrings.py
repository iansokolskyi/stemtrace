"""Fail when a public function, method or class under src/stemtrace has no docstring.

Public means the name doesn't start with ``_``; dunder methods such as ``__init__`` count as
public, so they need a docstring too.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_public(name: str) -> bool:
    return not name.startswith("_") or name.startswith("__")


def missing_docstrings(root: Path) -> list[str]:
    """Return ``path:line - name`` for every public definition under ``root`` without a docstring."""
    missing: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        missing.extend(
            f"{path}:{node.lineno} - {node.name}"
            for node in ast.walk(tree)
            if isinstance(node, _DEFINITIONS)
            and _is_public(node.name)
            and not ast.get_docstring(node)
        )
    return missing


def main(argv: list[str] | None = None) -> int:
    """Print each missing docstring and return 1 if any, else 0."""
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else REPO_ROOT / "src" / "stemtrace"
    missing = missing_docstrings(root)
    for line in missing:
        print(line)
    if missing:
        return 1
    print("✓ All public APIs have docstrings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
