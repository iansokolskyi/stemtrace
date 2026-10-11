"""Unit tests for built-in form login protection."""

from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import stemtrace
from stemtrace.server.fastapi import form_auth
from stemtrace.server.fastapi.form_auth import sign_session
from stemtrace.server.ui import static

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import RawGet

_COOKIE_NAME = "stemtrace_session"
_FORM_AUTH_LOGGER = "stemtrace.server.fastapi.form_auth"
_MISMATCH_EXPLANATION = "signed by a different server process"


def _make_login_app(*, login_secret: str | None = None) -> FastAPI:
    """Build a separately-initialized app, as each worker process would."""
    app = FastAPI()

    # Stands in for the hashed UI bundle served under <prefix>/assets/.
    @app.get("/stemtrace/assets/index-abc123.js")
    async def bundle() -> PlainTextResponse:
        return PlainTextResponse("console.log('ui')")

    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        login_username="admin",
        login_password="secret",  # NOSONAR - test credential only
        login_secret=login_secret,
    )
    return app


def _login_and_get_cookie(client: TestClient) -> str:
    """Log in through ``client`` and return the issued session cookie value."""
    resp = client.post(
        "/stemtrace/login",
        data={"username": "admin", "password": "secret", "next": "/stemtrace/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cookie = client.cookies.get(_COOKIE_NAME)
    assert isinstance(cookie, str)
    return cookie


def _form_auth_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _FORM_AUTH_LOGGER and r.levelno == logging.WARNING
    ]


class TestFormLoginAcrossWorkers:
    """Sessions across separately-initialized apps (workers/replicas).

    Each app is initialized separately, simulating independent worker processes
    (e.g. ``uvicorn --workers 2``) or replicas behind a load balancer.
    """

    @pytest.fixture(autouse=True)
    def _isolated_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep ambient env vars and the warning rate limit out of these tests."""
        monkeypatch.delenv("STEMTRACE_LOGIN_SECRET", raising=False)
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
        monkeypatch.setattr(form_auth, "_last_mismatch_warning_at", None)

    def test_cookie_from_other_app_without_secret_returns_mismatch_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        worker_a = _make_login_app()
        worker_b = _make_login_app()
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        with TestClient(worker_a) as client_a, TestClient(worker_b) as client_b:
            cookie = _login_and_get_cookie(client_a)
            assert client_a.get("/stemtrace/api/health").status_code == 200

            client_b.cookies.set(_COOKIE_NAME, cookie)
            api = client_b.get("/stemtrace/api/health")
            api_again = client_b.get("/stemtrace/api/health")
            page = client_b.get("/stemtrace/workers", follow_redirects=False)

        assert api.status_code == 401
        assert api.json() == {
            "detail": "Authentication required",
            "reason": "session_secret_mismatch",
        }
        assert api_again.json()["reason"] == "session_secret_mismatch"
        assert page.status_code == 303
        assert page.headers["location"] == (
            "/stemtrace/login?next=%2Fstemtrace%2Fworkers&reason=session_mismatch"
        )

        # Three rejected requests, but the warning is rate-limited to one.
        mismatch_warnings = [
            m for m in _form_auth_warnings(caplog) if "different secret" in m
        ]
        assert len(mismatch_warnings) == 1
        assert "STEMTRACE_LOGIN_SECRET" in mismatch_warnings[0]
        assert cookie not in mismatch_warnings[0]

    def test_assets_served_without_session_while_api_and_ui_stay_protected(
        self,
    ) -> None:
        worker_a = _make_login_app()
        worker_b = _make_login_app()

        with TestClient(worker_a) as client_a, TestClient(worker_b) as client_b:
            # No cookie at all.
            asset = client_b.get("/stemtrace/assets/index-abc123.js")
            api = client_b.get("/stemtrace/api/health")
            page = client_b.get("/stemtrace/", follow_redirects=False)
            # Cookie from the other worker.
            client_b.cookies.set(_COOKIE_NAME, _login_and_get_cookie(client_a))
            asset_other_worker = client_b.get("/stemtrace/assets/index-abc123.js")

        assert asset.status_code == 200
        assert asset.text == "console.log('ui')"
        assert asset_other_worker.status_code == 200
        assert api.status_code == 401
        assert page.status_code == 303
        assert page.headers["location"].startswith("/stemtrace/login?next=")

    def test_forged_cookie_response_does_not_depend_on_username(self) -> None:
        app = _make_login_app()
        # Issued "now" (iat after the app started).
        now = time.time()
        forged = {
            username: sign_session(
                {"u": username, "exp": int(now) + 86400, "iat": now}, "forger"
            )
            for username in ("admin", "not-admin")
        }

        responses = {}
        with TestClient(app) as client:
            for username, cookie in forged.items():
                client.cookies.set(_COOKIE_NAME, cookie)
                api = client.get("/stemtrace/api/health")
                page = client.get("/stemtrace/", follow_redirects=False)
                responses[username] = (
                    api.status_code,
                    api.json(),
                    page.headers["location"],
                )

        assert responses["admin"] == responses["not-admin"]
        # Both are reported as a mismatch (no secret configured), not just 401.
        assert responses["admin"][1]["reason"] == "session_secret_mismatch"

    def test_cookie_from_before_restart_gives_plain_401_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Single process restarted: its old cookie was issued before this
        # process started, so it is just expired, not a sibling's session.
        issued_before_start = time.time() - 3600
        old_cookie = sign_session(
            {
                "u": "admin",
                "exp": int(issued_before_start) + 86400,
                "iat": issued_before_start,
            },
            "pre-restart-secret",
        )
        app = _make_login_app()
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        with TestClient(app) as client:
            client.cookies.set(_COOKIE_NAME, old_cookie)
            api = client.get("/stemtrace/api/health")
            page = client.get("/stemtrace/", follow_redirects=False)

        assert api.status_code == 401
        assert api.json() == {"detail": "Authentication required"}
        assert page.headers["location"] == "/stemtrace/login?next=%2Fstemtrace%2F"
        assert [m for m in _form_auth_warnings(caplog) if "different secret" in m] == []

    def test_configured_secret_mismatch_gives_plain_401_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # E.g. a rotated secret or a forged cookie: no mismatch reason/warning.
        old = _make_login_app(login_secret="old-secret")
        new = _make_login_app(login_secret="new-secret")
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        with TestClient(old) as client_old, TestClient(new) as client_new:
            cookie = _login_and_get_cookie(client_old)
            client_new.cookies.set(_COOKIE_NAME, cookie)
            api = client_new.get("/stemtrace/api/health")
            page = client_new.get("/stemtrace/", follow_redirects=False)

        assert api.status_code == 401
        assert api.json() == {"detail": "Authentication required"}
        assert page.headers["location"] == "/stemtrace/login?next=%2Fstemtrace%2F"
        assert _form_auth_warnings(caplog) == []

    def test_websocket_from_other_app_denied_and_logs_mismatch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        worker_a = _make_login_app()
        worker_b = _make_login_app()
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        with TestClient(worker_a) as client_a, TestClient(worker_b) as client_b:
            client_b.cookies.set(_COOKIE_NAME, _login_and_get_cookie(client_a))
            for _ in range(2):
                with (
                    pytest.raises(WebSocketDisconnect) as exc_info,
                    client_b.websocket_connect("/stemtrace/ws") as ws,
                ):
                    ws.receive_text()
                assert exc_info.value.code == 1008
                assert exc_info.value.reason == ""

        mismatch = [m for m in _form_auth_warnings(caplog) if "different secret" in m]
        assert len(mismatch) == 1  # two denied handshakes, rate-limited warning

    def test_websocket_with_configured_secret_mismatch_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        old = _make_login_app(login_secret="old-secret")
        new = _make_login_app(login_secret="new-secret")
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        with TestClient(old) as client_old, TestClient(new) as client_new:
            client_new.cookies.set(_COOKIE_NAME, _login_and_get_cookie(client_old))
            with (
                pytest.raises(WebSocketDisconnect) as exc_info,
                client_new.websocket_connect("/stemtrace/ws") as ws,
            ):
                ws.receive_text()

        assert exc_info.value.code == 1008
        assert _form_auth_warnings(caplog) == []

    def test_garbage_cookie_returns_401_without_mismatch_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = _make_login_app(login_secret="explicit-secret")
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        with TestClient(app) as client:
            client.cookies.set(_COOKIE_NAME, "not-a-session")
            api = client.get("/stemtrace/api/health")
            page = client.get("/stemtrace/", follow_redirects=False)

        assert api.status_code == 401
        assert api.json() == {"detail": "Authentication required"}
        assert "reason=" not in page.headers["location"]
        assert _form_auth_warnings(caplog) == []

    def test_deeply_nested_json_cookie_returns_401_not_500(self) -> None:
        # The payload is parsed before the signature check, so an unsigned
        # cookie must not be able to crash the JSON decoder (RecursionError).
        nested = base64.urlsafe_b64encode(b"[" * 100_000).decode("ascii").rstrip("=")
        app = _make_login_app(login_secret=None)

        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set(_COOKIE_NAME, f"{nested}.c2ln")
            api = client.get("/stemtrace/api/health")
            page = client.get("/stemtrace/", follow_redirects=False)

        assert api.status_code == 401
        assert api.json() == {"detail": "Authentication required"}
        assert page.status_code == 303
        assert page.headers["location"].startswith("/stemtrace/login?next=")

    def test_shared_explicit_secret_accepts_cookie_from_other_app(self) -> None:
        worker_a = _make_login_app(login_secret="shared-secret")
        worker_b = _make_login_app(login_secret="shared-secret")

        with TestClient(worker_a) as client_a, TestClient(worker_b) as client_b:
            cookie = _login_and_get_cookie(client_a)
            client_b.cookies.set(_COOKIE_NAME, cookie)

            assert client_b.get("/stemtrace/api/health").status_code == 200

    def test_shared_env_secret_accepts_cookie_from_other_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEMTRACE_LOGIN_SECRET", "env-secret")
        worker_a = _make_login_app()
        worker_b = _make_login_app()

        with TestClient(worker_a) as client_a, TestClient(worker_b) as client_b:
            cookie = _login_and_get_cookie(client_a)
            client_b.cookies.set(_COOKIE_NAME, cookie)

            assert client_b.get("/stemtrace/api/health").status_code == 200

    def test_explicit_secret_takes_precedence_over_env_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEMTRACE_LOGIN_SECRET", "env-secret")
        explicit_app = _make_login_app(login_secret="explicit-secret")
        env_app = _make_login_app()

        with (
            TestClient(explicit_app) as client_explicit,
            TestClient(env_app) as client_env,
        ):
            cookie = _login_and_get_cookie(client_explicit)
            client_env.cookies.set(_COOKIE_NAME, cookie)

            assert client_env.get("/stemtrace/api/health").status_code == 401

    def test_init_app_without_secret_logs_single_process_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        _make_login_app()

        warnings = _form_auth_warnings(caplog)
        assert len(warnings) == 1
        assert "only work within this single process" in warnings[0]
        assert "STEMTRACE_LOGIN_SECRET" in warnings[0]

    def test_init_app_with_secret_logs_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        _make_login_app(login_secret="explicit-secret")

        assert _form_auth_warnings(caplog) == []

    def test_login_page_with_mismatch_reason_shows_explanation(self) -> None:
        app = _make_login_app(login_secret="explicit-secret")

        with TestClient(app) as client:
            explained = client.get("/stemtrace/login?reason=session_mismatch")
            plain = client.get("/stemtrace/login")
            unknown = client.get("/stemtrace/login?reason=<script>")

        assert explained.status_code == 200
        assert _MISMATCH_EXPLANATION in explained.text
        assert "STEMTRACE_LOGIN_SECRET" in explained.text
        assert _MISMATCH_EXPLANATION not in plain.text
        assert _MISMATCH_EXPLANATION not in unknown.text
        assert "<script>" not in unknown.text


class TestFormLoginProtection:
    def test_protects_ui_api_assets_and_websocket(self) -> None:
        """Unauthenticated requests are redirected/401, and login enables access."""
        app = FastAPI()
        ext = stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )

        with TestClient(app) as client:
            # UI routes: redirect to login
            resp = client.get("/stemtrace/", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/stemtrace/login?next=")

            # Login page is accessible
            resp = client.get("/stemtrace/login")
            assert resp.status_code == 200
            assert "Sign in" in resp.text

            # API is 401 (no redirect)
            resp = client.get("/stemtrace/api/health", follow_redirects=False)
            assert resp.status_code == 401

            # Assets (public UI bundle) are not gated: 404 here as no UI is served
            resp = client.get("/stemtrace/assets/index.js", follow_redirects=False)
            assert resp.status_code == 404

            # WebSocket is closed when unauthenticated
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect("/stemtrace/ws") as ws,
            ):
                ws.receive_text()

            # Login should set cookie and redirect to next
            resp = client.post(
                "/stemtrace/login",
                data={
                    "username": "admin",
                    "password": "secret",
                    "next": "/stemtrace/",
                },  # NOSONAR - test credential only
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/stemtrace/"

            # API now accessible
            resp = client.get("/stemtrace/api/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

            # WebSocket now connects and is tracked by ws_manager
            with client.websocket_connect("/stemtrace/ws"):
                assert ext.ws_manager.connection_count == 1
            # The server handles the disconnect asynchronously; wait for it.
            deadline = time.monotonic() + 2.0
            while ext.ws_manager.connection_count and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ext.ws_manager.connection_count == 0

    def test_unauthenticated_websocket_denied_at_handshake(self) -> None:
        """An unauthenticated WebSocket must be denied DURING the handshake,
        without calling ``accept()`` first.

        Accepting the handshake and then closing it half-opens the connection;
        when an unauthenticated client (e.g. a port scanner) aborts during that
        accept->close dance, uvicorn's legacy ``websockets`` implementation runs
        ``close()`` on a protocol whose ``transfer_data_task`` was never set,
        raising ``AttributeError: 'WebSocketProtocol' object has no attribute
        'transfer_data_task'``. Denying before accept avoids opening the
        protocol object at all. Regression test for that crash.
        """
        app = FastAPI()
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )

        with TestClient(app) as client:
            accepted = False
            with (  # noqa: PT012
                pytest.raises(WebSocketDisconnect) as exc_info,
                client.websocket_connect("/stemtrace/ws") as ws,
            ):
                # Only reached if the server accepted the handshake first.
                accepted = True
                ws.receive_text()

            assert accepted is False, (
                "unauthenticated WebSocket must be denied before accept(); "
                "accepting then closing crashes uvicorn's legacy websockets impl"
            )
            assert exc_info.value.code == 1008

    def test_login_urls_relative_and_cookie_secure_behind_tls_proxy(self) -> None:
        """Behind a TLS-terminating proxy, login URLs must stay relative and the
        session cookie must still be Secure.

        When the proxy forwards ``X-Forwarded-Proto: https`` but the ASGI scheme
        is left as ``http`` (e.g. uvicorn without ``--forwarded-allow-ips``),
        building absolute URLs from the request scheme downgrades the login form
        action / redirects to ``http://``. The browser then posts over http, the
        proxy answers 301->https, the POST body is dropped, and login silently
        fails. Relative URLs keep the page's https scheme; the cookie's Secure
        flag falls back to the forwarded-proto header.
        """
        app = FastAPI()
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )
        # ALB-style header; the ASGI scheme stays http (proxy headers not applied).
        proxied = {"x-forwarded-proto": "https"}

        with TestClient(app) as client:
            # The login form action is a relative path, never an absolute URL
            # that a proxy could downgrade to http.
            page = client.get("/stemtrace/login", headers=proxied)
            assert page.status_code == 200
            assert 'action="/stemtrace/login"' in page.text
            assert 'action="http' not in page.text

            # Successful login: relative redirect + Secure cookie (via forwarded proto).
            resp = client.post(
                "/stemtrace/login",
                data={
                    "username": "admin",
                    "password": "secret",
                    "next": "/stemtrace/",
                },  # NOSONAR - test credential only
                headers=proxied,
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/stemtrace/"
            assert "Secure" in resp.headers["set-cookie"]

            # Failed login: relative redirect, not an absolute http:// URL.
            bad = client.post(
                "/stemtrace/login",
                data={
                    "username": "admin",
                    "password": "nope",
                    "next": "/stemtrace/",
                },  # NOSONAR - test credential only
                headers=proxied,
                follow_redirects=False,
            )
            assert bad.headers["location"].startswith("/stemtrace/login?")
            assert not bad.headers["location"].startswith("http")

    def test_invalid_credentials_redirects_to_login_with_error(self) -> None:
        """Invalid credentials redirect back to the login page with an error."""
        app = FastAPI()
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )

        with TestClient(app) as client:
            resp = client.post(
                "/stemtrace/login",
                data={
                    "username": "admin",
                    "password": "wrong",
                    "next": "/stemtrace/",
                },  # NOSONAR - test credential only
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/stemtrace/login?")

            page = client.get(resp.headers["location"])
            assert page.status_code == 200
            assert "Invalid username or password" in page.text

    def test_logout_clears_cookie_and_restricts_access_again(self) -> None:
        """Logout clears the session cookie and access becomes restricted again."""
        app = FastAPI()
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )

        with TestClient(app) as client:
            resp = client.post(
                "/stemtrace/login",
                data={
                    "username": "admin",
                    "password": "secret",
                    "next": "/stemtrace/",
                },  # NOSONAR - test credential only
                follow_redirects=False,
            )
            assert resp.status_code == 303

            ok = client.get("/stemtrace/api/health")
            assert ok.status_code == 200

            logout = client.post("/stemtrace/logout", follow_redirects=False)
            assert logout.status_code == 303
            assert logout.headers["location"].startswith("/stemtrace/login")
            assert not logout.headers["location"].startswith("http")

            forbidden = client.get("/stemtrace/api/health", follow_redirects=False)
            assert forbidden.status_code == 401

    def test_redirect_preserves_query_string_in_next(self) -> None:
        """Unauthenticated redirect encodes the full path + query into next."""
        app = FastAPI()

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"ok": "true"}

        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )

        with TestClient(app) as client:
            passthrough = client.get("/healthz")
            assert passthrough.status_code == 200

            resp = client.get("/stemtrace/?foo=bar", follow_redirects=False)
            assert resp.status_code == 303
            assert "%3Ffoo%3Dbar" in resp.headers["location"]


class TestLivenessEndpoint:
    """``<prefix>/api/health/live`` is open under form login and exposes no data."""

    @staticmethod
    def _app(*, embedded_consumer: bool) -> FastAPI:
        app = FastAPI()
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=embedded_consumer,
            serve_ui=False,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )
        return app

    def test_live_without_cookie_returns_ok_and_only_status(self) -> None:
        # No embedded consumer: nothing to be unhealthy about.
        with TestClient(self._app(embedded_consumer=False)) as client:
            live = client.get("/stemtrace/api/health/live")
            health = client.get("/stemtrace/api/health")
            tasks = client.get("/stemtrace/api/tasks")

        assert live.status_code == 200
        assert live.json() == {"status": "ok"}
        assert health.status_code == 401
        assert tasks.status_code == 401

    def test_live_with_stopped_consumer_returns_200_degraded(self) -> None:
        # No lifespan (no ``with``): the enabled consumer was never started.
        # Liveness is about the process, so it stays 200; the body informs.
        client = TestClient(self._app(embedded_consumer=True))

        live = client.get("/stemtrace/api/health/live")

        assert live.status_code == 200
        assert live.json() == {"status": "degraded"}

    def test_live_subpaths_are_not_public(self) -> None:
        with TestClient(self._app(embedded_consumer=False)) as client:
            resp = client.get("/stemtrace/api/health/live/extra")

        assert resp.status_code == 401


_TRAVERSAL_PATHS = [
    "/stemtrace/assets/../api/tasks",
    "/stemtrace/assets/../index.html",
    "/stemtrace/assets/./index-abc123.js",
    "/stemtrace/assets/%2e%2e/api/tasks",
    "/stemtrace/assets/%2E%2E%2Fapi%2Ftasks",
    "/stemtrace/assets/..%2f..%2f..%2fetc%2fhostname",
    "/stemtrace/assets/..%5c..%5cetc%5chostname",
    "/stemtrace/assets/%252e%252e/api/tasks",
    "/stemtrace/../stemtrace/api/tasks",
    "/stemtrace/assets/%25252525252e%25252525252e/api/tasks",
]


class TestFormLoginDotSegments:
    """``assets/../`` must never sneak past the public-assets exemption."""

    @pytest.mark.parametrize("raw_path", _TRAVERSAL_PATHS)
    async def test_dot_segment_paths_rejected_before_any_handler(
        self, raw_get: RawGet, raw_path: str
    ) -> None:
        app = _make_login_app(login_secret="test-secret")
        reached: list[str] = []

        # Catch-all under assets: would match "assets/../anything" verbatim.
        @app.get("/stemtrace/assets/{rest:path}")
        async def spy(rest: str) -> PlainTextResponse:
            reached.append(rest)
            return PlainTextResponse("leaked")

        status = await raw_get(app, raw_path)

        assert status == 404
        assert reached == []

    async def test_plain_asset_path_still_served(self, raw_get: RawGet) -> None:
        app = _make_login_app(login_secret="test-secret")

        assert await raw_get(app, "/stemtrace/assets/index-abc123.js") == 200


_SHELL = "<html><head></head><body>app shell</body></html>"
_NON_FILE_ASSET_PATHS = [
    "/stemtrace/assets/",
    "/stemtrace/assets/index.html",
    "/stemtrace/assets//etc/passwd",
    "/stemtrace/assets/missing.js",
    "/stemtrace/assets/sub/",
]


@pytest.fixture
def fake_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A built UI with an index shell and one hashed asset, used by serve_spa."""
    dist = tmp_path / "dist"
    (dist / "assets" / "sub").mkdir(parents=True)
    (dist / "index.html").write_text(_SHELL)
    (dist / "assets" / "index-abc123.js").write_text("console.log('ui')")
    monkeypatch.setattr(static, "_FRONTEND_DIR", dist)
    return dist


# Decoded, these contain '?' or '#', which a parsed URL would split off,
# making them look like an open path (login, logout, liveness) or an asset.
_ENCODED_DELIMITER_PATHS = [
    "/stemtrace/api/health/live%3Fx",
    "/stemtrace/api/health/live%23x",
    "/stemtrace/login%23/graphs",
    "/stemtrace/login%3F/graphs",
    "/stemtrace/logout%3Fa",
    "/stemtrace/assets/index-abc123.js%3F",
    "/stemtrace/assets/%3F/graphs",
]


class TestPublicAssetsWithServedUi:
    """The public assets/ exemption must only ever serve real bundle files."""

    @staticmethod
    def _ui_app() -> FastAPI:
        app = FastAPI()
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=True,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )
        return app

    @pytest.mark.parametrize("raw_path", _NON_FILE_ASSET_PATHS)
    async def test_non_file_asset_path_without_session_returns_404(
        self, fake_dist: Path, raw_get: RawGet, raw_path: str
    ) -> None:
        assert await raw_get(self._ui_app(), raw_path) == 404

    @pytest.mark.parametrize("raw_path", _ENCODED_DELIMITER_PATHS)
    async def test_encoded_delimiter_path_without_session_returns_404(
        self, fake_dist: Path, raw_get: RawGet, raw_path: str
    ) -> None:
        assert await raw_get(self._ui_app(), raw_path) == 404

    def test_real_asset_without_session_returns_200_and_shell_is_protected(
        self, fake_dist: Path
    ) -> None:
        with TestClient(self._ui_app()) as client:
            asset = client.get("/stemtrace/assets/index-abc123.js")
            bare = client.get("/stemtrace/assets", follow_redirects=False)
            shell = client.get("/stemtrace/workers", follow_redirects=False)

        assert asset.status_code == 200
        assert asset.text == "console.log('ui')"
        assert bare.status_code == 303  # not under assets/: login required
        assert shell.status_code == 303

    def test_bare_assets_path_with_session_returns_404_not_shell(
        self, fake_dist: Path
    ) -> None:
        # Bare "assets" misses the /assets mount and reaches the SPA catch-all.
        with TestClient(self._ui_app()) as client:
            _login_and_get_cookie(client)
            bare = client.get("/stemtrace/assets", follow_redirects=False)
            shell = client.get("/stemtrace/workers", follow_redirects=False)

        assert bare.status_code == 404
        assert "app shell" not in bare.text
        assert shell.status_code == 200
        assert "app shell" in shell.text

    @pytest.mark.parametrize(
        "path",
        [
            "/stemtrace/assets/missing.js",
            "/stemtrace/tasks/assets/index-abc123.js",
            "/stemtrace/graph/x/assets/index-abc123.css",
        ],
    )
    def test_misplaced_asset_path_with_session_returns_404_not_shell(
        self, fake_dist: Path, path: str
    ) -> None:
        with TestClient(self._ui_app()) as client:
            _login_and_get_cookie(client)
            resp = client.get(path, follow_redirects=False)

        assert resp.status_code == 404
        assert "app shell" not in resp.text

    @pytest.mark.parametrize(
        "path",
        [
            "/stemtrace/tasks/assets/index-abc123.js",
            "/stemtrace/graph/x/assets/index-abc123.css",
        ],
    )
    def test_misplaced_asset_path_without_session_returns_404(
        self, fake_dist: Path, path: str
    ) -> None:
        with TestClient(self._ui_app()) as client:
            resp = client.get(path, follow_redirects=False)

        assert resp.status_code == 404
        assert "location" not in resp.headers


class TestLoginPageFavicon:
    """The login page carries the brand favicon without an extra request."""

    def test_login_page_embeds_inline_favicon_without_request(self) -> None:
        """Login page favicon is an inline data URI, so it never hits the auth wall."""
        app = FastAPI()
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            login_username="admin",
            login_password="secret",  # NOSONAR - test credential only
            login_secret="test-secret",
        )

        with TestClient(app) as client:
            page = client.get("/stemtrace/login")
            assert page.status_code == 200
            favicon = '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,'
            assert favicon in page.text
            assert "/vite.svg" not in page.text


class TestAssetsSegmentInApiPaths:
    """A task id of "assets" is an API path, not a misplaced asset path."""

    def test_api_path_with_assets_segment_without_session_returns_401(
        self, fake_dist: Path
    ) -> None:
        with TestClient(TestPublicAssetsWithServedUi._ui_app()) as client:
            resp = client.get("/stemtrace/api/tasks/assets/children")

        assert resp.status_code == 401

    def test_api_path_with_assets_segment_with_session_reaches_api(
        self, fake_dist: Path
    ) -> None:
        with TestClient(TestPublicAssetsWithServedUi._ui_app()) as client:
            _login_and_get_cookie(client)
            resp = client.get("/stemtrace/api/tasks/assets/children")

        assert resp.status_code == 404
        assert resp.json() == {"detail": "Task assets not found"}
