"""Tests for auth helpers."""

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.server.fastapi.auth import no_auth, require_api_key, require_basic_auth


def _create_app_with_auth(auth_dependency: object) -> FastAPI:
    """Create a simple app with auth-protected endpoint."""
    app = FastAPI()

    @app.get("/protected", dependencies=[auth_dependency])
    async def protected() -> dict[str, str]:
        return {"status": "ok"}

    return app


class TestRequireBasicAuth:
    def test_valid_credentials(self) -> None:
        app = _create_app_with_auth(require_basic_auth("admin", "secret"))
        client = TestClient(app)

        credentials = base64.b64encode(b"admin:secret").decode()
        response = client.get(
            "/protected", headers={"Authorization": f"Basic {credentials}"}
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_invalid_username(self) -> None:
        app = _create_app_with_auth(require_basic_auth("admin", "secret"))
        client = TestClient(app)

        credentials = base64.b64encode(b"wrong:secret").decode()
        response = client.get(
            "/protected", headers={"Authorization": f"Basic {credentials}"}
        )

        assert response.status_code == 401
        assert "Invalid credentials" in response.json()["detail"]

    def test_invalid_password(self) -> None:
        app = _create_app_with_auth(require_basic_auth("admin", "secret"))
        client = TestClient(app)

        credentials = base64.b64encode(b"admin:wrong").decode()
        response = client.get(
            "/protected", headers={"Authorization": f"Basic {credentials}"}
        )

        assert response.status_code == 401
        assert "Invalid credentials" in response.json()["detail"]

    def test_missing_auth_header(self) -> None:
        app = _create_app_with_auth(require_basic_auth("admin", "secret"))
        client = TestClient(app)

        response = client.get("/protected")

        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]
        assert response.headers.get("www-authenticate") == "Basic"


class TestRequireApiKey:
    def test_valid_api_key(self) -> None:
        app = _create_app_with_auth(require_api_key("my-secret-key"))
        client = TestClient(app)

        response = client.get("/protected", headers={"X-API-Key": "my-secret-key"})

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_invalid_api_key(self) -> None:
        app = _create_app_with_auth(require_api_key("my-secret-key"))
        client = TestClient(app)

        response = client.get("/protected", headers={"X-API-Key": "wrong-key"})

        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]

    def test_missing_api_key(self) -> None:
        app = _create_app_with_auth(require_api_key("my-secret-key"))
        client = TestClient(app)

        response = client.get("/protected")

        assert response.status_code == 401
        assert "API key required" in response.json()["detail"]

    def test_custom_header_name(self) -> None:
        app = _create_app_with_auth(
            require_api_key("my-secret-key", header_name="X-Custom-Key")
        )
        client = TestClient(app)

        # Should fail with default header
        response = client.get("/protected", headers={"X-API-Key": "my-secret-key"})
        assert response.status_code == 401

        # Should work with custom header
        response = client.get("/protected", headers={"X-Custom-Key": "my-secret-key"})
        assert response.status_code == 200


class TestNoAuth:
    def test_allows_all_requests(self) -> None:
        app = _create_app_with_auth(no_auth())
        client = TestClient(app)

        response = client.get("/protected")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
