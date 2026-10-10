"""Unit tests for form login in the standalone ``stemtrace server`` CLI app."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from stemtrace.server import __main__ as cli
from stemtrace.server.fastapi import form_auth
from stemtrace.server.ui import static

_REAL_STATIC_ROUTER = static.get_static_router_with_base

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.conftest import RawGet

_COOKIE_NAME = "stemtrace_session"
_FORM_AUTH_LOGGER = "stemtrace.server.fastapi.form_auth"


def _fake_static_router(
    prefix: str | None,
    *,
    show_logout: bool = False,
    logout_path: str | None = None,
) -> APIRouter:
    """Stand-in for the bundled UI: an index page and one hashed asset."""
    router = APIRouter()

    @router.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse("<html>ui</html>")

    @router.get("/assets/index-abc123.js")
    async def bundle() -> PlainTextResponse:
        return PlainTextResponse("console.log('ui')")

    return router


@pytest.fixture
def build_cli_app(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FastAPI]:
    """Return a factory that runs the CLI and captures the app it would serve."""
    monkeypatch.delenv("STEMTRACE_LOGIN_SECRET", raising=False)
    monkeypatch.delenv("STEMTRACE_TRANSPORT_URL", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    monkeypatch.setattr(form_auth, "_last_mismatch_warning_at", None)
    monkeypatch.setattr(static, "get_static_router_with_base", _fake_static_router)

    def _build(*extra_args: str) -> FastAPI:
        captured: list[FastAPI] = []

        def _fake_run(app: FastAPI, **kwargs: object) -> None:
            captured.append(app)

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
                *extra_args,
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        return captured[0]

    return _build


def _login(client: TestClient) -> str:
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "secret", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cookie = client.cookies.get(_COOKIE_NAME)
    assert isinstance(cookie, str)
    return cookie


class TestCliFormLogin:
    def test_cli_protects_api_and_ui_but_serves_assets(
        self, build_cli_app: Callable[..., FastAPI]
    ) -> None:
        app = build_cli_app()

        with TestClient(app) as client:
            asset = client.get("/assets/index-abc123.js")
            api = client.get("/stemtrace/api/health")
            page = client.get("/", follow_redirects=False)
            login_page = client.get("/login")

            _login(client)
            api_after_login = client.get("/stemtrace/api/health")
            page_after_login = client.get("/")

        assert asset.status_code == 200
        assert api.status_code == 401
        assert api.json() == {"detail": "Authentication required"}
        assert page.status_code == 303
        assert page.headers["location"] == "/login?next=%2F"
        assert login_page.status_code == 200
        assert api_after_login.status_code == 200
        assert page_after_login.text == "<html>ui</html>"

    def test_cli_without_secret_reports_mismatch_across_processes(
        self, build_cli_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)
        app_a = build_cli_app()
        app_b = build_cli_app()

        with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
            client_b.cookies.set(_COOKIE_NAME, _login(client_a))
            api = client_b.get("/stemtrace/api/health")
            page = client_b.get("/", follow_redirects=False)
            asset = client_b.get("/assets/index-abc123.js")

        assert api.status_code == 401
        assert api.json()["reason"] == "session_secret_mismatch"
        assert page.headers["location"] == "/login?next=%2F&reason=session_mismatch"
        assert asset.status_code == 200
        warnings = [r.getMessage() for r in caplog.records]
        assert sum("only work within this single process" in m for m in warnings) == 2
        assert sum("different secret" in m for m in warnings) == 1

    def test_cli_shared_secret_accepts_cookie_across_processes(
        self, build_cli_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)
        app_a = build_cli_app("--login-secret", "shared")
        app_b = build_cli_app("--login-secret", "shared")

        with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
            client_b.cookies.set(_COOKIE_NAME, _login(client_a))
            api = client_b.get("/stemtrace/api/health")

        assert api.status_code == 200
        assert caplog.records == []

    def test_cli_live_endpoint_is_public_and_reflects_consumer(
        self, build_cli_app: Callable[..., FastAPI]
    ) -> None:
        app = build_cli_app("--login-secret", "shared")

        # The CLI always embeds a consumer; with the memory:// transport its
        # thread either still runs or has finished draining the (empty)
        # stream normally. Neither is degraded, the same rule as /health.
        with TestClient(app) as client:
            live = client.get("/stemtrace/api/health/live")
            health = client.get("/stemtrace/api/health")

        assert live.status_code == 200
        assert live.json() == {"status": "ok"}
        assert health.status_code == 401


_CLI_TRAVERSAL_PATHS = [
    "/assets/../stemtrace/api/tasks",
    "/assets/../index.html",
    "/assets/./index-abc123.js",
    "/assets/%2e%2e/stemtrace/api/tasks",
    "/assets/%2E%2E%2Fstemtrace%2Fapi%2Ftasks",
    "/assets/..%2f..%2f..%2fetc%2fhostname",
    "/assets/..%5c..%5cetc%5chostname",
    "/assets/%252e%252e/stemtrace/api/tasks",
]


class TestCliDotSegments:
    @pytest.mark.parametrize("raw_path", _CLI_TRAVERSAL_PATHS)
    async def test_cli_dot_segment_paths_rejected_before_any_handler(
        self,
        build_cli_app: Callable[..., FastAPI],
        raw_get: RawGet,
        raw_path: str,
    ) -> None:
        app = build_cli_app()
        reached: list[str] = []

        async def spy(rest: str) -> PlainTextResponse:
            reached.append(rest)
            return PlainTextResponse("leaked")

        # Catch-all under assets: would match "assets/../anything" verbatim.
        app.add_api_route("/assets/{rest:path}", spy)

        status = await raw_get(app, raw_path)

        assert status == 404
        assert reached == []

    async def test_cli_plain_asset_path_still_served(
        self, build_cli_app: Callable[..., FastAPI], raw_get: RawGet
    ) -> None:
        app = build_cli_app()

        assert await raw_get(app, "/assets/index-abc123.js") == 200


_CLI_NON_FILE_ASSET_PATHS = [
    "/assets/",
    "/assets/index.html",
    "/assets//etc/passwd",
    "/assets/missing.js",
    "/assets/sub/",
]


@pytest.fixture
def real_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_cli_app: Callable[..., FastAPI],
) -> Callable[[], FastAPI]:
    """Build the CLI app with the real static router over a fake built UI."""
    dist = tmp_path / "dist"
    (dist / "assets" / "sub").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body>app shell</body></html>")
    (dist / "assets" / "index-abc123.js").write_text("console.log('ui')")
    monkeypatch.setattr(static, "_FRONTEND_DIR", dist)
    monkeypatch.setattr(static, "get_static_router_with_base", _REAL_STATIC_ROUTER)
    return lambda: build_cli_app("--login-secret", "shared")


class TestCliPublicAssets:
    @pytest.mark.parametrize("raw_path", _CLI_NON_FILE_ASSET_PATHS)
    async def test_cli_non_file_asset_path_without_session_returns_404(
        self, real_ui: Callable[[], FastAPI], raw_get: RawGet, raw_path: str
    ) -> None:
        assert await raw_get(real_ui(), raw_path) == 404

    def test_cli_real_asset_without_session_returns_200_and_shell_is_protected(
        self, real_ui: Callable[[], FastAPI]
    ) -> None:
        with TestClient(real_ui()) as client:
            asset = client.get("/assets/index-abc123.js")
            shell = client.get("/workers", follow_redirects=False)

        assert asset.status_code == 200
        assert asset.text == "console.log('ui')"
        assert shell.status_code == 303
