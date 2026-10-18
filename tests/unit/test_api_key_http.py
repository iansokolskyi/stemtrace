"""API key auth across modes, surfaces and entry points (HTTP and WebSocket).

Matrix: auth mode {key only, form + key, form only, auth_dependency + key} x
surface {JSON API, liveness probe, UI page, WebSocket} x entry point
{init_app, StemtraceExtension, create_router, CLI}.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import TYPE_CHECKING

import pytest
import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

import stemtrace
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server import __main__ as cli
from stemtrace.server.fastapi import form_auth
from stemtrace.server.fastapi.api_key import API_KEY_ENV_VAR
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.fastapi.login_throttle import LOGIN_IP_MAX_FAILURES
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore
from stemtrace.server.ui import static

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

KEY = "0123456789abcdef0123456789abcdef"
BEARER = {"Authorization": f"Bearer {KEY}"}
WRONG = {"Authorization": "Bearer not-the-key-at-all-0000"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        API_KEY_ENV_VAR,
        "STEMTRACE_LOGIN_USERNAME",
        "STEMTRACE_LOGIN_PASSWORD",
        "STEMTRACE_LOGIN_SECRET",
        "STEMTRACE_TRANSPORT_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(form_auth, "_last_mismatch_warning_at", None)


def _app(**kwargs: object) -> FastAPI:
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        **kwargs,  # type: ignore[arg-type]  # test passes init_app options through
    )
    return app


def _form_kwargs() -> dict[str, object]:
    return {
        "login_username": "admin",
        "login_password": "secret",  # NOSONAR - test credential only
        "login_secret": "test-secret-test-secret",
    }


@pytest.fixture
def key_only() -> Iterator[TestClient]:
    with TestClient(_app(api_key=KEY)) as client:
        yield client


@pytest.fixture
def form_and_key() -> Iterator[TestClient]:
    with TestClient(_app(api_key=KEY, **_form_kwargs())) as client:
        yield client


@pytest.fixture
def form_only() -> Iterator[TestClient]:
    with TestClient(_app(**_form_kwargs())) as client:
        yield client


def _login(client: TestClient) -> None:
    resp = client.post(
        "/stemtrace/login",
        data={"username": "admin", "password": "secret", "next": "/stemtrace/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _ws_subprotocols(key: str) -> list[str]:
    encoded = base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
    return ["stemtrace", f"stemtrace.bearer.{encoded}"]


def _assert_ws_denied(client: TestClient, **kwargs: object) -> None:
    """The handshake is refused before accept (close code 1008)."""
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/stemtrace/ws", **kwargs),  # type: ignore[arg-type]  # headers/subprotocols
    ):
        # Fail at once instead of waiting for a message that never comes.
        pytest.fail("the WebSocket handshake was accepted")
    assert exc_info.value.code == 1008


# --- key only ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/stemtrace/api/tasks", "/stemtrace/api/graphs", "/stemtrace/api/health"]
)
def test_key_only_api_with_valid_key_returns_json(
    key_only: TestClient, path: str
) -> None:
    resp = key_only.get(path, headers=BEARER)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "Bearer"])
def test_key_only_scheme_is_case_insensitive(key_only: TestClient, scheme: str) -> None:
    resp = key_only.get(
        "/stemtrace/api/tasks", headers={"Authorization": f"{scheme} {KEY}"}
    )

    assert resp.status_code == 200


@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        ({}, "Authentication required"),
        ({"Authorization": "Basic YWRtaW46c2VjcmV0"}, "Authentication required"),
        (WRONG, "Invalid API key"),
        ({"Authorization": "Bearer"}, "Invalid API key"),
    ],
)
def test_key_only_api_without_valid_key_is_401_bearer(
    key_only: TestClient, headers: dict[str, str], detail: str
) -> None:
    resp = key_only.get("/stemtrace/api/tasks", headers=headers)

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {"detail": detail}


def test_key_only_liveness_probe_needs_no_key(key_only: TestClient) -> None:
    resp = key_only.get("/stemtrace/api/health/live")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_key_only_full_health_needs_the_key(key_only: TestClient) -> None:
    assert key_only.get("/stemtrace/api/health").status_code == 401


def test_key_only_locks_out_after_failures_before_comparing(
    key_only: TestClient,
) -> None:
    codes = [
        key_only.get("/stemtrace/api/tasks", headers=WRONG).status_code
        for _ in range(LOGIN_IP_MAX_FAILURES)
    ]

    assert codes == [401] * (LOGIN_IP_MAX_FAILURES - 1) + [429]
    locked = key_only.get("/stemtrace/api/tasks", headers=BEARER)
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "30"
    assert locked.json() == {"detail": "Too many failed API key attempts"}
    # Missing credentials are not attempts: they stay 401, not 429.
    assert key_only.get("/stemtrace/api/tasks").status_code == 401


def test_key_only_websocket(key_only: TestClient) -> None:
    _assert_ws_denied(key_only)
    _assert_ws_denied(key_only, headers=WRONG)
    _assert_ws_denied(key_only, subprotocols=_ws_subprotocols("wrong-key-wrong-key"))
    _assert_ws_denied(key_only, subprotocols=["stemtrace", "stemtrace.bearer.%%"])

    with key_only.websocket_connect("/stemtrace/ws", headers=BEARER) as ws:
        assert ws.accepted_subprotocol is None
    with key_only.websocket_connect(
        "/stemtrace/ws", subprotocols=_ws_subprotocols(KEY)
    ) as ws:
        # The key's own subprotocol is never echoed back.
        assert ws.accepted_subprotocol == "stemtrace"
    # The key's subprotocol alone: refused (a browser would drop the accept).
    _assert_ws_denied(key_only, subprotocols=_ws_subprotocols(KEY)[1:])


def test_key_never_logged(
    key_only: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    key_only.get("/stemtrace/api/tasks", headers=BEARER)
    key_only.get("/stemtrace/api/tasks", headers={"Authorization": f"Bearer {KEY}x"})
    with key_only.websocket_connect("/stemtrace/ws", headers=BEARER):
        pass

    assert all(KEY not in r.getMessage() for r in caplog.records)


# --- form login + key -------------------------------------------------------


def test_form_and_key_valid_key_without_cookie_gets_json_not_redirect(
    form_and_key: TestClient,
) -> None:
    resp = form_and_key.get(
        "/stemtrace/api/tasks", headers=BEARER, follow_redirects=False
    )

    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


def test_form_and_key_cookie_without_key_passes(form_and_key: TestClient) -> None:
    _login(form_and_key)

    assert form_and_key.get("/stemtrace/api/tasks").status_code == 200


def test_form_and_key_neither_is_401_bearer(form_and_key: TestClient) -> None:
    resp = form_and_key.get("/stemtrace/api/tasks", follow_redirects=False)

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_form_and_key_wrong_key_with_valid_cookie_is_401(
    form_and_key: TestClient,
) -> None:
    _login(form_and_key)

    resp = form_and_key.get("/stemtrace/api/tasks", headers=WRONG)

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid API key"}
    assert resp.headers["www-authenticate"] == "Bearer"


def test_form_and_key_bearer_is_ignored_on_ui_pages(form_and_key: TestClient) -> None:
    resp = form_and_key.get("/stemtrace/", headers=BEARER, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/stemtrace/login?")


def test_form_and_key_liveness_probe_is_public(form_and_key: TestClient) -> None:
    assert form_and_key.get("/stemtrace/api/health/live").status_code == 200


def test_form_and_key_login_failures_count_toward_key_lockout(
    form_and_key: TestClient,
) -> None:
    for _ in range(LOGIN_IP_MAX_FAILURES):
        form_and_key.post(
            "/stemtrace/login",
            data={"username": "admin", "password": "nope", "next": "/stemtrace/"},
            follow_redirects=False,
        )

    resp = form_and_key.get("/stemtrace/api/tasks", headers=BEARER)

    assert resp.status_code == 429


def test_form_and_key_websocket(form_and_key: TestClient) -> None:
    _assert_ws_denied(form_and_key)
    with form_and_key.websocket_connect("/stemtrace/ws", headers=BEARER):
        pass
    with form_and_key.websocket_connect(
        "/stemtrace/ws", subprotocols=_ws_subprotocols(KEY)
    ) as ws:
        assert ws.accepted_subprotocol == "stemtrace"

    _login(form_and_key)
    with form_and_key.websocket_connect("/stemtrace/ws"):
        pass
    # A wrong key is refused even with a valid session cookie.
    _assert_ws_denied(form_and_key, headers=WRONG)


# --- form login only: Bearer proves nothing --------------------------------


def test_form_only_bearer_header_does_not_bypass_login(form_only: TestClient) -> None:
    resp = form_only.get("/stemtrace/api/tasks", headers=BEARER)

    assert resp.status_code == 401
    assert "www-authenticate" not in resp.headers
    _assert_ws_denied(form_only, headers=BEARER)


# --- auth_dependency + key: both needed ------------------------------------


def test_auth_dependency_and_key_both_required() -> None:
    app = _app(
        api_key=KEY, auth_dependency=stemtrace.require_api_key("other-secret-value")
    )
    both = {**BEARER, "X-API-Key": "other-secret-value"}

    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=both).status_code == 200
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 401
        only_dep = client.get(
            "/stemtrace/api/tasks", headers={"X-API-Key": "other-secret-value"}
        )
        assert only_dep.status_code == 401
        assert only_dep.headers["www-authenticate"] == "Bearer"


# --- entry points -----------------------------------------------------------


def test_init_app_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, KEY)

    with TestClient(_app()) as client:
        assert client.get("/stemtrace/api/tasks").status_code == 401
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200


def test_init_app_without_key_stays_open() -> None:
    with TestClient(_app()) as client:
        assert client.get("/stemtrace/api/tasks").status_code == 200


def test_init_app_short_key_raises_without_echoing_it() -> None:
    with pytest.raises(ConfigurationError, match="at least 16") as info:
        _app(api_key="tooshort")

    assert "tooshort" not in str(info.value)


def test_extension_api_key_guards_api_and_owns_throttle() -> None:
    extension = StemtraceExtension(
        "memory://", embedded_consumer=False, serve_ui=False, api_key=KEY
    )
    app = FastAPI()
    extension.init_app(app)

    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks").status_code == 401
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
    assert extension.auth_throttle is not None


def test_extension_without_key_has_no_throttle() -> None:
    extension = StemtraceExtension("memory://", embedded_consumer=False)

    assert extension.auth_throttle is None


def test_create_router_api_key_guards_api() -> None:
    app = FastAPI()
    app.include_router(create_router(store=GraphStore(), api_key=KEY))

    with TestClient(app) as client:
        assert client.get("/api/tasks").status_code == 401
        assert client.get("/api/tasks", headers=BEARER).status_code == 200
        assert client.get("/api/health/live").status_code == 200


def test_create_router_short_key_raises() -> None:
    with pytest.raises(ConfigurationError, match="at least 16"):
        create_router(api_key="short")


# --- CLI --------------------------------------------------------------------


def _fake_static_router(
    prefix: str | None,
    *,
    show_logout: bool = False,
    logout_path: str | None = None,
) -> APIRouter:
    del prefix, show_logout, logout_path
    router = APIRouter()

    @router.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse("<html>ui</html>")

    return router


@pytest.fixture
def run_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., tuple[int, str, FastAPI | None]]:
    monkeypatch.setattr(static, "get_static_router_with_base", _fake_static_router)

    def _run(
        *args: str, env: dict[str, str] | None = None
    ) -> tuple[int, str, FastAPI | None]:
        captured: list[FastAPI] = []
        monkeypatch.setattr(uvicorn, "run", lambda app, **_kwargs: captured.append(app))
        result = CliRunner().invoke(
            cli.app,
            ["server", "--broker-url", "memory://", *args],
            env={"COLUMNS": "1000", **(env or {})},
        )
        output = _ANSI.sub("", result.output)
        return result.exit_code, output, captured[0] if captured else None

    return _run


@pytest.mark.parametrize("source", ["flag", "env"])
def test_cli_api_key_guards_api_and_is_not_echoed(
    run_cli: Callable[..., tuple[int, str, FastAPI | None]], source: str
) -> None:
    if source == "flag":
        code, output, app = run_cli("--api-key", KEY)
    else:
        code, output, app = run_cli(env={API_KEY_ENV_VAR: KEY})

    assert code == 0, output
    assert "API key: set" in output
    assert KEY not in output
    assert app is not None
    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks").status_code == 401
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        assert client.get("/stemtrace/api/health/live").status_code == 200
        assert client.get("/").status_code == 200  # UI shell stays public


def test_cli_api_key_with_login_accepts_either(
    run_cli: Callable[..., tuple[int, str, FastAPI | None]],
) -> None:
    code, output, app = run_cli(
        "--api-key", KEY, "--login-username", "admin", "--login-password", "secret"
    )

    assert code == 0, output
    assert app is not None
    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        unauthenticated = client.get("/stemtrace/api/tasks")
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["www-authenticate"] == "Bearer"
        assert (
            client.get("/", headers=BEARER, follow_redirects=False).status_code == 303
        )
        with client.websocket_connect("/stemtrace/ws", headers=BEARER):
            pass


def test_cli_without_api_key_does_not_mention_it(
    run_cli: Callable[..., tuple[int, str, FastAPI | None]],
) -> None:
    code, output, app = run_cli("--api-key", "  ")

    assert code == 0, output
    assert "API key" not in output
    assert app is not None
    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks").status_code == 200


def test_cli_short_api_key_is_a_usage_error(
    run_cli: Callable[..., tuple[int, str, FastAPI | None]],
) -> None:
    code, output, app = run_cli("--api-key", "short-key")

    assert code == 2
    assert "'--api-key'" in output
    assert "at least 16 characters" in output
    assert "short-key" not in output.replace("'--api-key'", "")
    assert app is None


def test_form_and_key_wrong_keys_count_toward_login_lockout(
    form_and_key: TestClient,
) -> None:
    for _ in range(LOGIN_IP_MAX_FAILURES):
        form_and_key.get("/stemtrace/api/tasks", headers=WRONG)

    resp = form_and_key.post(
        "/stemtrace/login",
        data={"username": "admin", "password": "secret", "next": "/stemtrace/"},
        follow_redirects=False,
    )

    assert resp.status_code == 429


def test_key_in_query_string_is_never_accepted(key_only: TestClient) -> None:
    for name in ("api_key", "key", "token", "access_token"):
        assert key_only.get(f"/stemtrace/api/tasks?{name}={KEY}").status_code == 401
        _assert_ws_denied_url(key_only, f"/stemtrace/ws?{name}={KEY}")


def _assert_ws_denied_url(client: TestClient, url: str) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(url):
        pytest.fail("the WebSocket handshake was accepted")
    assert exc_info.value.code == 1008


@pytest.mark.parametrize(
    "path",
    [
        "/stemtrace/api/tasks/",
        "/stemtrace/api//tasks",
        "/stemtrace/api/HEALTH/live",
        "/stemtrace/api/health/live/",
        "/stemtrace/apix",
    ],
)
@pytest.mark.parametrize("headers", [WRONG, BEARER])
def test_form_and_key_bearer_on_non_api_route_is_404_not_ui(
    path: str, headers: dict[str, str]
) -> None:
    """A deferred Bearer request that no API route checked never gets the UI shell."""
    app = FastAPI()

    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        api_key=KEY,
        **_form_kwargs(),
    )

    @app.get("/stemtrace/{rest:path}")
    async def catch_all(rest: str) -> HTMLResponse:  # stands in for the SPA
        return HTMLResponse("<html>ui shell</html>")

    with TestClient(app) as client:
        resp = client.get(path, headers=headers, follow_redirects=False)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_cli_typer_hides_locals_in_tracebacks() -> None:
    assert cli.app.pretty_exceptions_show_locals is False


@pytest.mark.parametrize("headers", [BEARER, WRONG])
def test_form_and_key_liveness_with_any_bearer_is_200(
    form_and_key: TestClient, headers: dict[str, str]
) -> None:
    """An agent's helper sends its key everywhere; the probe must still answer."""
    resp = form_and_key.get("/stemtrace/api/health/live", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_auth_dependency_and_key_websocket_and_liveness() -> None:
    """The dependency covers the JSON API only: WS needs the key, liveness the dependency."""
    app = _app(
        api_key=KEY, auth_dependency=stemtrace.require_api_key("other-secret-value")
    )
    dep = {"X-API-Key": "other-secret-value"}

    with TestClient(app) as client:
        with client.websocket_connect("/stemtrace/ws", headers=BEARER):
            pass
        _assert_ws_denied(client, headers=dep)
        assert client.get("/stemtrace/api/health/live").status_code == 401
        assert client.get("/stemtrace/api/health/live", headers=dep).status_code == 200
        # A browser with only the host's auth can't use the dashboard's data.
        assert client.get("/stemtrace/api/tasks", headers=dep).status_code == 401


def test_cli_login_failures_count_toward_key_lockout(
    run_cli: Callable[..., tuple[int, str, FastAPI | None]],
) -> None:
    code, output, app = run_cli(
        "--api-key", KEY, "--login-username", "admin", "--login-password", "secret"
    )

    assert code == 0, output
    assert app is not None
    with TestClient(app) as client:
        for _ in range(LOGIN_IP_MAX_FAILURES):
            client.post(
                "/login",
                data={"username": "admin", "password": "nope", "next": "/"},
                follow_redirects=False,
            )
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 429
