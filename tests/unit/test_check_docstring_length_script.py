"""Tests for the scripts/check_docstring_length.py lint check."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_docstring_length.py"
_spec = importlib.util.spec_from_file_location("check_docstring_length", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
# Registered before exec: dataclasses resolves the module through sys.modules.
sys.modules["check_docstring_length"] = check
_spec.loader.exec_module(check)


def _function(prose_lines: int, sections: str = "") -> str:
    body = "\n".join(f"    Prose line {i}." for i in range(2, prose_lines + 1))
    gap = "\n\n" if body else ""
    return f'def f():\n    """Summary.{gap}{body}\n{sections}    """\n'


def _module(prose_lines: int) -> str:
    body = "\n".join(f"Prose line {i}." for i in range(2, prose_lines + 1))
    return f'"""Summary.\n\n{body}\n"""\n'


def _names(source: str) -> list[tuple[str, int]]:
    return [(v.name, v.prose_lines) for v in check.check_source(source, Path("x.py"))]


def test_check_source_with_five_prose_lines_passes() -> None:
    assert _names(_function(5)) == []


def test_check_source_with_six_prose_lines_reports_function() -> None:
    assert _names(_function(6)) == [("f", 6)]


def test_check_source_with_eight_module_lines_passes() -> None:
    assert _names(_module(8)) == []


def test_check_source_with_nine_module_lines_reports_module() -> None:
    assert _names(_module(9)) == [("<module>", 9)]


def test_check_source_reports_class_and_nested_function_by_line() -> None:
    six = "\n".join(f"        Line {i}." for i in range(6))
    source = (
        "class C:\n"
        f'    """Summary.\n\n{six.replace("        ", "    ")}\n    """\n\n'
        "    def method(self):\n"
        "        def inner():\n"
        f'            """Summary.\n\n{six.replace("        ", "            ")}\n'
        '            """\n'
    )
    violations = check.check_source(source, Path("x.py"))
    assert [(v.name, v.line) for v in violations] == [("C", 1), ("inner", 13)]


def test_count_prose_lines_skips_google_sections_with_blank_lines() -> None:
    docstring = (
        "Summary.\n\n"
        "Args:\n    a: First.\n\n    b: Second,\n        continued.\n\n"
        "Returns:\n    Something.\n\n"
        "Raises:\n    ValueError: Bad.\n\n"
        "Yields:\n    Items.\n\n"
        "Attributes:\n    x: An attribute.\n\n"
        "Example:\n    >>> f()\n\n    >>> g()\n\n"
        "Examples:\n    Plain::\n\n        code()\n"
    )
    assert check.count_prose_lines(docstring) == 1


def test_count_prose_lines_counts_prose_after_a_section() -> None:
    docstring = "Summary.\n\nArgs:\n    a: First.\n\nMore prose.\nAnd more.\n"
    assert check.count_prose_lines(docstring) == 3


def test_count_prose_lines_counts_unknown_headers_as_prose() -> None:
    docstring = "Summary.\n\nNote:\n    Hidden detail.\nUsage:\n    call()\n"
    assert check.count_prose_lines(docstring) == 5


def test_load_allowlist_strips_comments_and_resolves_against_root(
    tmp_path: Path,
) -> None:
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text(
        "# header comment\n\nsrc/a.py  # remove when PR #1 merges\n  src/b.py\n"
    )
    assert check.load_allowlist(allowlist, tmp_path) == {
        (tmp_path / "src" / "a.py").resolve(),
        (tmp_path / "src" / "b.py").resolve(),
    }


def test_load_allowlist_when_missing_returns_empty(tmp_path: Path) -> None:
    assert check.load_allowlist(tmp_path / "missing.txt", tmp_path) == set()


def test_run_with_long_docstring_reports_it(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(_function(6))
    (tmp_path / "good.py").write_text(_function(5))
    errors = check.run([tmp_path], set())
    assert len(errors) == 1
    assert errors[0].endswith("bad.py:1 f: 6 prose lines (max 5)")


def test_run_with_allowlisted_failing_file_passes(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(_function(6))
    assert check.run([tmp_path], {bad.resolve()}) == []


def test_run_with_allowlisted_passing_file_reports_stale_entry(
    tmp_path: Path,
) -> None:
    good = tmp_path / "good.py"
    good.write_text(_function(5))
    errors = check.run([tmp_path], {good.resolve()})
    assert len(errors) == 1
    assert errors[0].endswith(
        "good.py: allowlisted but missing or within the limits; "
        "remove it from the allowlist"
    )


def test_run_with_allowlisted_missing_file_reports_stale_entry(
    tmp_path: Path,
) -> None:
    (tmp_path / "good.py").write_text(_function(5))
    errors = check.run([tmp_path], {(tmp_path / "gone.py").resolve()})
    assert len(errors) == 1
    assert "gone.py: allowlisted but missing" in errors[0]


def test_main_on_failing_file_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(_module(9))
    empty = tmp_path / "allow.txt"
    empty.write_text("")
    assert check.main([str(bad), "--allowlist", str(empty)]) == 1
    out, err = capsys.readouterr()
    assert out.strip().endswith("bad.py:1 <module>: 9 prose lines (max 8)")
    assert err.strip() == ("1 docstring length error(s); see code-quality rule 3.5")


def test_main_on_repo_with_its_allowlist_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check.main([]) == 0
    assert capsys.readouterr().out.strip() == "✓ All docstrings within length limits"
