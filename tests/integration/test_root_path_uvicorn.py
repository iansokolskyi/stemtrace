"""Form login on a real uvicorn server started with ``root_path``.

A reverse proxy that strips its prefix (``/proxy/stemtrace/x`` ->
``/stemtrace/x``) and uvicorn ``--root-path /proxy`` hand the app the same scope
as these direct requests: uvicorn re-prepends the root path to
``scope["path"]`` and ``raw_path``. Everything the browser receives (redirects,
the form action, cookie paths) must carry the prefix.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

import stemtrace
from stemtrace.server import __main__ as cli
from stemtrace.server.ui import static

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

_ROOT = "/proxy"
_START_TIMEOUT_S = 5.0


@pytest.fixture
def fake_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A built UI with an index shell and one hashed asset."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body>app shell</body></html>")
    (dist / "assets" / "index-abc123.js").write_text("console.log('ui')")
    monkeypatch.setattr(static, "_FRONTEND_DIR", dist)
    return dist


@pytest.fixture
def serve() -> Iterator[Callable[[FastAPI], str]]:
    """Start apps on uvicorn with root_path=/proxy; yield a base-URL factory."""
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    def _serve(app: FastAPI) -> str:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            root_path=_ROOT,
            log_level="warning",
            ws="none",  # the websockets backend warns on import; HTTP is enough
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append((server, thread))
        deadline = time.monotonic() + _START_TIMEOUT_S
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                pytest.fail("uvicorn did not start")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    yield _serve
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=5)


def _embedded_app() -> FastAPI:
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        login_username="admin",
        login_password="secret",  # NOSONAR - test credential only
        login_secret="test-secret",
    )
    return app


def _cli_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    captured: list[FastAPI] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **_kw: captured.append(app))
    monkeypatch.delenv("STEMTRACE_TRANSPORT_URL", raising=False)
    result = CliRunner().invoke(
        cli.app,
        [
            "server",
            "--broker-url",
            "memory://",
            "--login-username",
            "admin",
            "--login-password",
            "secret",  # NOSONAR - test credential only
            "--login-secret",
            "shared",
            "--root-path",
            _ROOT,
        ],
    )
    assert result.exit_code == 0, result.output
    return captured[0]


def test_embedded_login_flow_behind_root_path(
    fake_dist: Path, serve: Callable[[FastAPI], str]
) -> None:
    base = serve(_embedded_app())

    with httpx.Client(base_url=base, timeout=5) as client:
        api = client.get("/stemtrace/api/tasks")
        page = client.get("/stemtrace/graphs")
        form = client.get("/stemtrace/login")
        login = client.post(
            "/stemtrace/login",
            data={"username": "admin", "password": "secret"},  # NOSONAR - test only
        )
        cookie = login.cookies.get("stemtrace_session")
        # Sent by hand: the jar scopes it to /proxy/stemtrace (the browser URL),
        # and these requests skip the proxy.
        authed = client.get(
            "/stemtrace/api/tasks",
            headers={"cookie": f"stemtrace_session={cookie}"},
        )

    assert api.status_code == 401
    assert page.status_code == 303
    assert page.headers["location"] == (
        "/proxy/stemtrace/login?next=%2Fproxy%2Fstemtrace%2Fgraphs"
    )
    assert 'action="/proxy/stemtrace/login"' in form.text
    assert login.status_code == 303
    assert login.headers["location"] == "/proxy/stemtrace/"
    assert "Path=/proxy/stemtrace" in login.headers["set-cookie"]
    assert authed.status_code == 200


def test_cli_login_flow_behind_root_path(
    fake_dist: Path,
    serve: Callable[[FastAPI], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # uvicorn.run is stubbed to capture the app; serve() uses uvicorn.Server.
    base = serve(_cli_app(monkeypatch))

    with httpx.Client(base_url=base, timeout=5) as client:
        login_page = client.get("/login")
        asset = client.get("/assets/index-abc123.js")
        page = client.get("/workers")
        login = client.post(
            "/login",
            data={"username": "admin", "password": "secret"},  # NOSONAR - test only
        )

    assert login_page.status_code == 200
    assert 'action="/proxy/login"' in login_page.text
    assert asset.status_code == 200
    assert page.status_code == 303
    assert page.headers["location"] == "/proxy/login?next=%2Fproxy%2Fworkers"
    assert login.headers["location"] == "/proxy/"
    assert "Path=/proxy;" in login.headers["set-cookie"]
