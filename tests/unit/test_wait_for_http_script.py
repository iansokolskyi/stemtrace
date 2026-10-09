"""Tests for the scripts/wait_for_http.py E2E helper."""

from __future__ import annotations

import http.client
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import urllib.request

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


class _FakeResponse:
    status = 200

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_main_when_connection_resets_retries_until_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[Exception] = [
        ConnectionResetError(104, "Connection reset by peer"),
        http.client.RemoteDisconnected("Remote end closed connection"),
    ]
    calls: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        calls.append(request.full_url)
        if errors:
            raise errors.pop(0)
        return _FakeResponse()

    monkeypatch.setattr(wait_for_http.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(wait_for_http.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "sys.argv",
        ["wait_for_http.py", "http://localhost:8000/health", "--timeout", "30"],
    )

    wait_for_http.main()

    assert calls == ["http://localhost:8000/health"] * 3
