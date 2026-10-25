"""Tests for scripts/check_docstrings.py."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_docstrings.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docstrings", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_docstrings"] = module
    spec.loader.exec_module(module)
    return module


check_docstrings = _load()


def _write(root: Path, source: str) -> Path:
    path = root / "mod.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_main_with_every_public_definition_documented_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path,
        'class A:\n    """A."""\n\n    def __init__(self) -> None:\n        """Init."""\n\n'
        "    def _private(self) -> None:\n        pass\n",
    )

    assert check_docstrings.main([str(tmp_path)]) == 0
    assert "All public APIs have docstrings" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("source", "name", "line"),
    [
        ("def f() -> None:\n    pass\n", "f", 1),
        (
            'class A:\n    """A."""\n\n    def __init__(self) -> None:\n        pass\n',
            "__init__",
            4,
        ),
        ("class B:\n    pass\n", "B", 1),
        ("async def g() -> None:\n    pass\n", "g", 1),
    ],
)
def test_main_with_missing_docstring_reports_it_and_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: str,
    name: str,
    line: int,
) -> None:
    root, elsewhere = tmp_path / "src", tmp_path / "cwd"
    root.mkdir()
    elsewhere.mkdir()
    path = _write(root, source)
    # Outside the working directory, the path is printed as given.
    monkeypatch.chdir(elsewhere)

    assert check_docstrings.main([str(root)]) == 1
    assert capsys.readouterr().out.strip() == f"{path}:{line} - {name}"


def test_main_under_working_directory_reports_relative_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    _write(package, "def f() -> None:\n    pass\n")
    monkeypatch.chdir(tmp_path)

    assert check_docstrings.main([str(package)]) == 1
    assert capsys.readouterr().out.strip() == "pkg/mod.py:1 - f"


def test_main_ignores_private_definitions(tmp_path: Path) -> None:
    _write(tmp_path, "def _helper() -> None:\n    pass\n")

    assert check_docstrings.main([str(tmp_path)]) == 0
