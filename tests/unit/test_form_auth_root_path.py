"""Form login behind a path-prefixing reverse proxy (ASGI ``root_path``).

uvicorn ``--root-path /proxy`` puts the prefix in ``scope["root_path"]`` and
prepends it to ``scope["path"]``; Starlette strips it again before routing.
``TestClient`` sends the path as given, so these tests request
``/proxy/...`` with ``root_path="/proxy"`` to build the scope uvicorn builds.
"""

from __future__ import annotations

import logging
import re
import types
import urllib.parse
from typing import TYPE_CHECKING, Any

import pytest
import starlette._utils
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import stemtrace
from stemtrace.server import __main__ as cli
from stemtrace.server.fastapi import paths
from stemtrace.server.fastapi.login_routes import _safe_next
from stemtrace.server.fastapi.paths import strip_root_path
from stemtrace.server.ui import static

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping
    from pathlib import Path

_COOKIE_NAME = "stemtrace_session"
_ROOT = "/proxy"
_SHELL = (
    '<html><head></head><body>app shell<script src="/assets/index-abc123.js">'
    "</script></body></html>"
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def fake_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A built UI with an index shell and one hashed asset."""
    monkeypatch.setattr(static, "_unsafe_root_path_warned", False)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_SHELL)
    (dist / "assets" / "index-abc123.js").write_text("console.log('ui')")
    monkeypatch.setattr(static, "_FRONTEND_DIR", dist)
    return dist


def _embedded_app() -> FastAPI:
    app = FastAPI()

    @app.get("/other")
    async def other() -> dict[str, str]:
        return {"app": "host"}

    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        login_username="admin",
        login_password="secret",  # NOSONAR - test credential only
        login_secret="test-secret",
    )
    return app


def _login(client: TestClient, login_path: str, next_path: str | None = None) -> Any:
    data = {"username": "admin", "password": "secret"}  # NOSONAR - test only
    if next_path is not None:
        data["next"] = next_path
    resp = client.post(login_path, data=data, follow_redirects=False)
    assert resp.status_code == 303
    return resp


def _cookie_path(set_cookie: str) -> str:
    attrs = [part.strip() for part in set_cookie.split(";")]
    return next(a.split("=", 1)[1] for a in attrs if a.lower().startswith("path="))


class TestEmbeddedBehindRootPath:
    def test_api_without_session_returns_401(self, fake_dist: Path) -> None:
        with TestClient(_embedded_app(), root_path=_ROOT) as client:
            resp = client.get(f"{_ROOT}/stemtrace/api/tasks")

        assert resp.status_code == 401
        assert resp.json() == {"detail": "Authentication required"}

    def test_ui_without_session_redirects_to_prefixed_login(
        self, fake_dist: Path
    ) -> None:
        with TestClient(_embedded_app(), root_path=_ROOT) as client:
            resp = client.get(f"{_ROOT}/stemtrace/graphs?x=1", follow_redirects=False)

        assert resp.status_code == 303
        next_target = urllib.parse.quote(f"{_ROOT}/stemtrace/graphs?x=1", safe="")
        assert resp.headers["location"] == (
            f"{_ROOT}/stemtrace/login?next={next_target}"
        )

    def test_public_paths_served_without_session(self, fake_dist: Path) -> None:
        with TestClient(_embedded_app(), root_path=_ROOT) as client:
            login_page = client.get(f"{_ROOT}/stemtrace/login")
            asset = client.get(f"{_ROOT}/stemtrace/assets/index-abc123.js")
            live = client.get(f"{_ROOT}/stemtrace/api/health/live")
            host_route = client.get(f"{_ROOT}/other")

        assert login_page.status_code == 200
        assert f'action="{_ROOT}/stemtrace/login"' in login_page.text
        assert asset.status_code == 200
        assert asset.text == "console.log('ui')"
        assert live.status_code == 200
        assert host_route.json() == {"app": "host"}

    def test_login_sets_prefixed_cookie_and_default_redirect(
        self, fake_dist: Path
    ) -> None:
        with TestClient(_embedded_app(), root_path=_ROOT) as client:
            resp = _login(client, f"{_ROOT}/stemtrace/login")
            api = client.get(f"{_ROOT}/stemtrace/api/tasks")
            shell = client.get(f"{_ROOT}/stemtrace/workers")

        assert resp.headers["location"] == f"{_ROOT}/stemtrace/"
        assert _cookie_path(resp.headers["set-cookie"]) == f"{_ROOT}/stemtrace"
        assert api.status_code == 200
        assert shell.status_code == 200
        assert "app shell" in shell.text

    def test_login_honors_next_under_root_path(self, fake_dist: Path) -> None:
        with TestClient(_embedded_app(), root_path=_ROOT) as client:
            resp = _login(
                client, f"{_ROOT}/stemtrace/login", f"{_ROOT}/stemtrace/graphs"
            )

        assert resp.headers["location"] == f"{_ROOT}/stemtrace/graphs"

    def test_logout_clears_prefixed_cookie(self, fake_dist: Path) -> None:
        with TestClient(_embedded_app(), root_path=_ROOT) as client:
            _login(client, f"{_ROOT}/stemtrace/login")
            resp = client.post(f"{_ROOT}/stemtrace/logout", follow_redirects=False)

        assert resp.headers["location"] == f"{_ROOT}/stemtrace/login"
        assert _cookie_path(resp.headers["set-cookie"]) == f"{_ROOT}/stemtrace"

    def test_bare_prefix_redirect_keeps_root_path(self, fake_dist: Path) -> None:
        with TestClient(_embedded_app(), root_path=_ROOT) as client:
            _login(client, f"{_ROOT}/stemtrace/login")
            resp = client.get(f"{_ROOT}/stemtrace", follow_redirects=False)

        assert resp.status_code == 307
        assert resp.headers["location"] == f"{_ROOT}/stemtrace/"

    def test_ui_shell_uses_prefixed_base(self, fake_dist: Path) -> None:
        with TestClient(_embedded_app(), root_path=_ROOT) as client:
            _login(client, f"{_ROOT}/stemtrace/login")
            shell = client.get(f"{_ROOT}/stemtrace/workers")

        assert f'window.__STEMTRACE_BASE__="{_ROOT}/stemtrace"' in shell.text
        assert f'__STEMTRACE_LOGOUT_PATH__="{_ROOT}/stemtrace/logout"' in shell.text
        assert f'src="{_ROOT}/stemtrace/assets/index-abc123.js"' in shell.text

    def test_unsafe_root_path_logs_one_warning(
        self, fake_dist: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="stemtrace.server.ui.static")
        with TestClient(_embedded_app(), root_path="/~ops") as client:
            _login(client, "/~ops/stemtrace/login")
            first = client.get("/~ops/stemtrace/workers")
            second = client.get("/~ops/stemtrace/")

        assert first.status_code == second.status_code == 200
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1
        assert "'/~ops'" in messages[0]

    async def test_root_path_not_in_scope_path_still_protected(
        self, fake_dist: Path
    ) -> None:
        # Some servers set root_path without prepending it to path; Starlette
        # routes the path unchanged then, so the middleware must too.
        status = await _send(_embedded_app(), "/stemtrace/api/tasks", root_path=_ROOT)

        assert status == 401

    @pytest.mark.parametrize(
        "path", ["/apiXv1/stemtrace/api/tasks", "/api.v1/stemtrace/api/tasks"]
    )
    async def test_metacharacter_root_path_never_serves_api_without_session(
        self, fake_dist: Path, path: str
    ) -> None:
        # Whatever the installed Starlette routes, the middleware must see the
        # same path: protected (401) or unroutable (404), never 200.
        status = await _send(_embedded_app(), path, root_path="/api.v1")

        assert status in {401, 404}

    @pytest.mark.parametrize("path", ["/stemtrace/", "/stemtrace/tasks/1"])
    async def test_shell_prefix_when_server_does_not_prepend_root_path(
        self, fake_dist: Path, path: str
    ) -> None:
        # uvicorn < 0.26 sets root_path but leaves it out of scope["path"].
        app = _embedded_app()
        with TestClient(app) as client:
            cookie = _login(client, "/stemtrace/login").cookies.get(_COOKIE_NAME)
        status, body = await _send_full(
            app,
            path,
            root_path=_ROOT,
            headers=[(b"cookie", f"{_COOKIE_NAME}={cookie}".encode())],
        )

        assert status == 200
        assert f'window.__STEMTRACE_BASE__="{_ROOT}/stemtrace"' in body
        assert f'src="{_ROOT}/stemtrace/assets/index-abc123.js"' in body

    async def test_dot_segments_under_root_path_rejected(self, fake_dist: Path) -> None:
        status = await _send(
            _embedded_app(),
            f"{_ROOT}/stemtrace/assets/../api/tasks",
            root_path=_ROOT,
        )

        assert status == 404


async def _send(app: Any, raw_path: str, *, root_path: str) -> int:
    """GET a raw path through an ASGI app with a given root_path."""
    status, _ = await _send_full(app, raw_path, root_path=root_path)
    return status


async def _send_full(
    app: Any,
    raw_path: str,
    *,
    root_path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, str]:
    """GET a raw path with a given root_path; return status and body."""
    messages: list[MutableMapping[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": urllib.parse.unquote(raw_path),
        "raw_path": raw_path.encode("latin-1"),
        "root_path": root_path,
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *(headers or [])],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return int(start["status"]), body.decode()


@pytest.fixture
def cli_app(
    monkeypatch: pytest.MonkeyPatch, fake_dist: Path
) -> Callable[..., tuple[FastAPI, dict[str, object]]]:
    """Run ``stemtrace server`` and capture the app and uvicorn options."""
    monkeypatch.delenv("STEMTRACE_ROOT_PATH", raising=False)
    monkeypatch.delenv("STEMTRACE_TRANSPORT_URL", raising=False)

    def _build(*extra: str) -> tuple[FastAPI, dict[str, object]]:
        captured: list[tuple[FastAPI, dict[str, object]]] = []

        def _fake_run(app: FastAPI, **kwargs: object) -> None:
            captured.append((app, kwargs))

        monkeypatch.setattr(uvicorn, "run", _fake_run)
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
                *extra,
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        return captured[0]

    return _build


class TestCliBehindRootPath:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("/proxy", "/proxy"), ("proxy/", "/proxy"), ("/a/b/", "/a/b"), ("", "")],
    )
    def test_root_path_option_reaches_uvicorn_normalized(
        self,
        cli_app: Callable[..., tuple[FastAPI, dict[str, object]]],
        value: str,
        expected: str,
    ) -> None:
        _, kwargs = cli_app("--root-path", value)

        assert kwargs["root_path"] == expected

    @pytest.mark.parametrize("value", ["/~ops", "/a b", "/" + "x" * 65, "/a/../b"])
    def test_root_path_rejected_when_ui_cannot_use_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_dist: Path,
        value: str,
    ) -> None:
        ran: list[object] = []
        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: ran.append(a))
        result = CliRunner().invoke(
            cli.app, ["server", "--broker-url", "memory://", "--root-path", value]
        )

        assert result.exit_code == 2
        # Typer colors its errors when CI forces a terminal (GITHUB_ACTIONS),
        # which splits the option name with ANSI codes.
        assert "--root-path" in _ANSI.sub("", result.output)
        assert ran == []

    def test_root_path_env_var_reaches_uvicorn(
        self,
        cli_app: Callable[..., tuple[FastAPI, dict[str, object]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("STEMTRACE_ROOT_PATH", "/proxy")

        _, kwargs = cli_app()

        assert kwargs["root_path"] == "/proxy"

    def test_root_path_without_flag_defaults_to_empty(
        self, cli_app: Callable[..., tuple[FastAPI, dict[str, object]]]
    ) -> None:
        _, kwargs = cli_app()

        assert kwargs["root_path"] == ""

    def test_login_page_and_public_paths_open_under_root_path(
        self, cli_app: Callable[..., tuple[FastAPI, dict[str, object]]]
    ) -> None:
        app, _ = cli_app("--root-path", _ROOT)

        with TestClient(app, root_path=_ROOT) as client:
            login_page = client.get(f"{_ROOT}/login", follow_redirects=False)
            asset = client.get(f"{_ROOT}/assets/index-abc123.js")
            live = client.get(f"{_ROOT}/stemtrace/api/health/live")
            api = client.get(f"{_ROOT}/stemtrace/api/health")
            page = client.get(f"{_ROOT}/workers", follow_redirects=False)

        assert login_page.status_code == 200
        assert f'action="{_ROOT}/login"' in login_page.text
        assert asset.status_code == 200
        assert live.status_code == 200
        assert api.status_code == 401
        assert page.status_code == 303
        assert page.headers["location"] == f"{_ROOT}/login?next=%2Fproxy%2Fworkers"

    def test_login_redirects_and_cookie_under_root_path(
        self, cli_app: Callable[..., tuple[FastAPI, dict[str, object]]]
    ) -> None:
        app, _ = cli_app("--root-path", _ROOT)

        with TestClient(app, root_path=_ROOT) as client:
            resp = _login(client, f"{_ROOT}/login")
            api = client.get(f"{_ROOT}/stemtrace/api/health")
            shell = client.get(f"{_ROOT}/workers")
            logout = client.post(f"{_ROOT}/logout", follow_redirects=False)

        assert resp.headers["location"] == f"{_ROOT}/"
        assert _cookie_path(resp.headers["set-cookie"]) == _ROOT
        assert api.status_code == 200
        assert 'window.__STEMTRACE_BASE__="/proxy/stemtrace"' in shell.text
        assert 'window.__STEMTRACE_LOGOUT_PATH__="/proxy/logout"' in shell.text
        assert 'src="/proxy/assets/index-abc123.js"' in shell.text
        assert logout.headers["location"] == f"{_ROOT}/login"
        assert _cookie_path(logout.headers["set-cookie"]) == _ROOT

    def test_cli_without_root_path_unchanged(
        self, cli_app: Callable[..., tuple[FastAPI, dict[str, object]]]
    ) -> None:
        app, _ = cli_app()

        with TestClient(app) as client:
            resp = _login(client, "/login")
            shell = client.get("/workers")

        assert resp.headers["location"] == "/"
        assert _cookie_path(resp.headers["set-cookie"]) == "/"
        assert 'window.__STEMTRACE_BASE__="/stemtrace"' in shell.text
        assert 'window.__STEMTRACE_LOGOUT_PATH__="/logout"' in shell.text
        assert 'src="/assets/index-abc123.js"' in shell.text


class TestSafeNext:
    @pytest.mark.parametrize(
        "target",
        [
            "//evil.example",
            "/\\evil.example",
            "/\\/evil.example",
            "https://evil.example",
            "evil.example",
            "/\tx",
            "",
            None,
        ],
    )
    def test_safe_next_rejects_off_site_targets(self, target: str | None) -> None:
        assert _safe_next(target, default="/stemtrace/") == "/stemtrace/"

    @pytest.mark.parametrize(
        "target", ["/stemtrace/graphs", "/proxy/stemtrace/tasks/1?x=1"]
    )
    def test_safe_next_keeps_local_paths(self, target: str) -> None:
        assert _safe_next(target, default="/stemtrace/") == target


class TestStripRootPath:
    @pytest.mark.parametrize(
        ("path", "root_path", "expected"),
        [
            ("/proxy/stemtrace/api", "/proxy", "/stemtrace/api"),
            ("/proxy", "/proxy", ""),
            ("/proxyx/stemtrace", "/proxy", "/proxyx/stemtrace"),
            ("/stemtrace/api", "/proxy", "/stemtrace/api"),
            ("/stemtrace/api", "", "/stemtrace/api"),
            # Regex metacharacters: starlette 0.38.x strips with an unescaped
            # regex, where "." matches any character.
            ("/apiXv1/stemtrace/api", "/api.v1", None),
            ("/api.v1/stemtrace/api", "/api.v1", "/stemtrace/api"),
        ],
    )
    def test_strip_root_path_matches_starlette_routing(
        self, path: str, root_path: str, expected: str | None
    ) -> None:
        scope = {"type": "http", "path": path, "root_path": root_path}
        starlette_route_path = getattr(starlette._utils, "get_route_path", None)
        if starlette_route_path is None:  # pragma: no cover - future Starlette
            pytest.skip("this Starlette has no get_route_path to compare with")

        if expected is not None:
            assert strip_root_path(path, root_path) == expected
        assert strip_root_path(path, root_path) == starlette_route_path(scope)

    def test_uses_starlette_route_path_when_present(self) -> None:
        assert paths._get_route_path is getattr(
            starlette._utils, "get_route_path", paths._literal_route_path
        )

    @pytest.mark.parametrize(
        ("path", "root_path", "expected"),
        [
            ("/proxy/stemtrace/api", "/proxy", "/stemtrace/api"),
            ("/proxy", "/proxy", ""),
            ("/proxyx/stemtrace", "/proxy", "/proxyx/stemtrace"),
            ("/apiXv1/stemtrace/api", "/api.v1", "/apiXv1/stemtrace/api"),
            ("/stemtrace/api", "", "/stemtrace/api"),
        ],
    )
    def test_fallback_without_starlette_symbol_strips_literal_prefix(
        self, path: str, root_path: str, expected: str
    ) -> None:
        bare = types.ModuleType("starlette._utils_without_get_route_path")

        route_path = paths._resolve_route_path(bare)

        assert route_path is paths._literal_route_path
        assert route_path({"path": path, "root_path": root_path}) == expected
