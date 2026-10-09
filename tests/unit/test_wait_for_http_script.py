"""Tests for the scripts/wait_for_http.py E2E helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wait_for_http.py"
_spec = importlib.util.spec_from_file_location("wait_for_http", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
wait_for_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wait_for_http)


def _run(monkeypatch: pytest.MonkeyPatch, url: str) -> str:
    monkeypatch.setattr("sys.argv", ["wait_for_http.py", url, "--timeout", "0"])
    with pytest.raises(SystemExit) as exc_info:
        wait_for_http.main()
    return str(exc_info.value.code)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/health",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5:8000/stemtrace/api/health",
        "http://localhost.example.com/health",
        "http://localhost@example.com/health",
        "file:///etc/passwd",
        "ftp://localhost/health",
        "localhost:8000/health",
    ],
)
def test_main_with_disallowed_url_exits_with_refusal(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    message = _run(monkeypatch, url)

    assert message.startswith(f"Refusing to poll {url!r}")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/stemtrace/api/health",
        "http://127.0.0.1:8000/stemtrace/api/health",
        "http://[::1]:8000/stemtrace/api/health",
        "https://localhost/health",
        "http://server:8000/stemtrace/api/health",
    ],
)
def test_main_with_allowed_url_reaches_polling_loop(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    message = _run(monkeypatch, url)

    assert message == "Service not healthy within 0.0s: None"
