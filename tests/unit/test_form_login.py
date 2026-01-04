"""Unit tests for built-in form login protection."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import stemtrace


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

            # Assets are 401 when unauthenticated
            resp = client.get("/stemtrace/assets/index.js", follow_redirects=False)
            assert resp.status_code == 401

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
            assert ext.ws_manager.connection_count == 0

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
            assert resp.headers["location"].startswith(
                "http://testserver/stemtrace/login?"
            )

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
            assert logout.headers["location"].startswith(
                "http://testserver/stemtrace/login"
            )

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
