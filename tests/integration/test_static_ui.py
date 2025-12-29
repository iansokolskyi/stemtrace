"""Integration tests for static UI serving."""

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.server.ui.static import get_static_router, is_ui_available


class TestStaticRouter:
    """Tests for static file router."""

    def test_get_static_router_returns_none_when_missing(self, tmp_path: Path) -> None:
        """Returns None if dist/ doesn't exist."""
        with patch(
            "stemtrace.server.ui.static._FRONTEND_DIR", tmp_path / "nonexistent"
        ):
            router = get_static_router()
            assert router is None

    def test_get_static_router_creates_router(self, tmp_path: Path) -> None:
        """Creates router when dist/ exists."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        assets_dir = dist_dir / "assets"
        assets_dir.mkdir()
        (dist_dir / "index.html").write_text("<html><body>Test</body></html>")

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None

    def test_serve_index(self, tmp_path: Path) -> None:
        """Serves index.html at root."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        assets_dir = dist_dir / "assets"
        assets_dir.mkdir()
        (dist_dir / "index.html").write_text("<html><body>Hello</body></html>")

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            response = client.get("/")
            assert response.status_code == 200
            assert "Hello" in response.text

    def test_serve_spa_fallback(self, tmp_path: Path) -> None:
        """SPA routes fall back to index.html."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        assets_dir = dist_dir / "assets"
        assets_dir.mkdir()
        (dist_dir / "index.html").write_text("<html><body>SPA</body></html>")

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            # Any path should return index.html for SPA
            response = client.get("/tasks/123")
            assert response.status_code == 200
            assert "SPA" in response.text

    def test_serve_static_file(self, tmp_path: Path) -> None:
        """Serves actual static files when they exist."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        assets_dir = dist_dir / "assets"
        assets_dir.mkdir()
        (dist_dir / "index.html").write_text("<html></html>")
        (dist_dir / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            response = client.get("/favicon.ico")
            assert response.status_code == 200


class TestIsUiAvailable:
    """Tests for is_ui_available() helper."""

    def test_returns_false_when_missing(self, tmp_path: Path) -> None:
        """Returns False if index.html doesn't exist."""
        with patch(
            "stemtrace.server.ui.static._FRONTEND_DIR", tmp_path / "nonexistent"
        ):
            assert is_ui_available() is False

    def test_returns_true_when_exists(self, tmp_path: Path) -> None:
        """Returns True if index.html exists."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "index.html").write_text("<html></html>")

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            assert is_ui_available() is True
