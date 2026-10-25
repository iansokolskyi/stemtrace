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
    (dist / "index.html").write_text(
        '<html><head></head><body>app shell<script src="/assets/index-abc123.js">'
        "</script></body></html>"
    )
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
        session = {"cookie": f"stemtrace_session={cookie}"}
        authed = client.get("/stemtrace/api/tasks", headers=session)
        shell = client.get("/stemtrace/workers", headers=session)
        asset = client.get("/stemtrace/assets/index-abc123.js")

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
    # The SPA must load its bundle and call the API through the proxy prefix.
    assert shell.status_code == 200
    assert 'src="/proxy/stemtrace/assets/index-abc123.js"' in shell.text
    assert 'window.__STEMTRACE_BASE__="/proxy/stemtrace"' in shell.text
    assert 'window.__STEMTRACE_LOGOUT_PATH__="/proxy/stemtrace/logout"' in shell.text
    assert asset.status_code == 200
    assert asset.text == "console.log('ui')"


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
        cookie = login.cookies.get("stemtrace_session")
        session = {"cookie": f"stemtrace_session={cookie}"}
        outside_base = client.get("/workers", headers=session)
        shell = client.get("/stemtrace/workers", headers=session)

    # A page URL outside the router basepath moves into it, with the prefix.
    assert outside_base.status_code == 307
    assert outside_base.headers["location"] == "/proxy/stemtrace/workers"
    assert shell.status_code == 200
    assert 'src="/proxy/assets/index-abc123.js"' in shell.text
    assert 'window.__STEMTRACE_BASE__="/proxy/stemtrace"' in shell.text
    assert login_page.status_code == 200
    assert 'action="/proxy/login"' in login_page.text
    assert asset.status_code == 200
    assert page.status_code == 303
    assert page.headers["location"] == "/proxy/login?next=%2Fproxy%2Fworkers"
    assert login.headers["location"] == "/proxy/"
    assert "Path=/proxy;" in login.headers["set-cookie"]


def _standalone_ui_app() -> FastAPI:
    router = static.get_static_router_with_base("/stemtrace")
    assert router is not None
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/?state=FAILURE",
        "/tasks/a%3Fb",
        "/tasks/a%2Fb",
        "/tasks/a%23b",
        "/tasks/100%25",
    ],
)
def test_landing_behind_root_path_redirects_into_the_base_byte_for_byte(
    fake_dist: Path, serve: Callable[[FastAPI], str], path: str
) -> None:
    """A stripped request outside the base is redirected under it, encoding kept."""
    base = serve(_standalone_ui_app())

    response = httpx.get(base + path, follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"{_ROOT}/stemtrace{path}"


@pytest.mark.parametrize("path", [_ROOT, f"{_ROOT}/", f"{_ROOT}/tasks/x"])
def test_unstripped_prefix_is_served_not_redirected_in_a_loop(
    fake_dist: Path, serve: Callable[[FastAPI], str], path: str
) -> None:
    """A proxy that forwards the prefix too gets the shell instead of a growing redirect."""
    base = serve(_standalone_ui_app())

    response = httpx.get(base + path, follow_redirects=False)

    assert response.status_code == 200
