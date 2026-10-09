"""Loopback HTTP tests for scripts/wait_for_http.py redirect handling."""

from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wait_for_http.py"
_spec = importlib.util.spec_from_file_location("wait_for_http_redirects", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
wait_for_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wait_for_http)


class _Handler(BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            # Same-host target that would be healthy if the redirect were followed
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: object) -> None:
        return None


@pytest.fixture
def loopback_server() -> Iterator[str]:
    _Handler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _main(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["wait_for_http.py", url, "--timeout", "0.5", "--interval", "0.1"],
    )
    wait_for_http.main()


def test_main_with_healthy_endpoint_returns(
    monkeypatch: pytest.MonkeyPatch, loopback_server: str
) -> None:
    _main(monkeypatch, f"{loopback_server}/ok")

    assert _Handler.hits == ["/ok"]


def test_main_with_redirect_does_not_follow_and_times_out(
    monkeypatch: pytest.MonkeyPatch, loopback_server: str
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _main(monkeypatch, f"{loopback_server}/redirect")

    assert str(exc_info.value.code).endswith(": HTTP 302")
    assert set(_Handler.hits) == {"/redirect"}
