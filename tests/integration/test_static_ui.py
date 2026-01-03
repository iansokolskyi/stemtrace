"""Integration tests for static UI serving."""

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.server.ui.static import (
    get_static_router,
    get_static_router_with_base,
    is_ui_available,
)


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

    def test_serve_index_returns_503_when_index_missing(self, tmp_path: Path) -> None:
        """If dist exists but index.html is missing, / should return 503."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "assets").mkdir()

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            response = client.get("/")
            assert response.status_code == 503

    def test_serve_spa_returns_404_when_index_missing(self, tmp_path: Path) -> None:
        """If index.html is missing, SPA fallback should return 404 for unknown paths."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "assets").mkdir()

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            response = client.get("/some/spa/route")
            assert response.status_code == 404

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


class TestDeploymentModes:
    """Tests for standalone server vs embedded FastAPI deployment modes.

    Ensures both modes work correctly:
    - Embedded: UI and API at same prefix (e.g., /stemtrace)
    - Standalone: UI at root (/), API at different prefix (/stemtrace)
    """

    def _create_dist(self, tmp_path: Path) -> Path:
        """Create mock dist directory with index.html containing asset refs."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        assets_dir = dist_dir / "assets"
        assets_dir.mkdir()
        # HTML with asset references and head tag for injection
        (dist_dir / "index.html").write_text(
            "<html><head></head><body>"
            '<script src="/assets/index.js"></script>'
            '<link href="/assets/style.css">'
            "</body></html>"
        )
        # Create actual asset files
        (assets_dir / "index.js").write_text("console.log('app');")
        (assets_dir / "style.css").write_text("body { color: red; }")
        return dist_dir

    def test_embedded_mode_assets_rewritten(self, tmp_path: Path) -> None:
        """Embedded mode: assets rewritten to match mount prefix."""
        dist_dir = self._create_dist(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            # Use default get_static_router (derives base from URL)
            router = get_static_router()
            assert router is not None

            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            response = client.get("/stemtrace/")
            assert response.status_code == 200

            # Assets should be rewritten to /stemtrace/assets/
            assert "/stemtrace/assets/index.js" in response.text
            assert "/stemtrace/assets/style.css" in response.text

            # API base should match mount prefix
            assert 'window.__STEMTRACE_BASE__="/stemtrace"' in response.text

    def test_embedded_mode_assets_accessible(self, tmp_path: Path) -> None:
        """Embedded mode: asset files accessible at rewritten paths."""
        dist_dir = self._create_dist(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None

            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            # Assets should be accessible at /stemtrace/assets/
            js_response = client.get("/stemtrace/assets/index.js")
            assert js_response.status_code == 200
            assert "console.log" in js_response.text

            css_response = client.get("/stemtrace/assets/style.css")
            assert css_response.status_code == 200
            assert "color: red" in css_response.text

    def test_standalone_mode_assets_not_rewritten(self, tmp_path: Path) -> None:
        """Standalone mode: assets stay at /assets/, API base set explicitly."""
        dist_dir = self._create_dist(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            # Use explicit API base (like stemtrace server CLI does)
            router = get_static_router_with_base("/stemtrace")
            assert router is not None

            app = FastAPI()
            # UI mounted at root
            app.include_router(router)
            client = TestClient(app)

            response = client.get("/")
            assert response.status_code == 200

            # Assets should NOT be rewritten (stay at /assets/)
            assert '"/assets/index.js"' in response.text
            assert '"/assets/style.css"' in response.text
            # Should NOT have /stemtrace prefix on assets
            assert "/stemtrace/assets" not in response.text

            # API base should be set to /stemtrace for WebSocket/API calls
            assert 'window.__STEMTRACE_BASE__="/stemtrace"' in response.text

    def test_standalone_mode_assets_accessible(self, tmp_path: Path) -> None:
        """Standalone mode: assets accessible at /assets/ (root mount)."""
        dist_dir = self._create_dist(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router_with_base("/stemtrace")
            assert router is not None

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            # Assets should be at /assets/ (not /stemtrace/assets/)
            js_response = client.get("/assets/index.js")
            assert js_response.status_code == 200
            assert "console.log" in js_response.text

            css_response = client.get("/assets/style.css")
            assert css_response.status_code == 200
            assert "color: red" in css_response.text

    def test_standalone_mode_spa_routing(self, tmp_path: Path) -> None:
        """Standalone mode: SPA routes work and preserve API base."""
        dist_dir = self._create_dist(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router_with_base("/stemtrace")
            assert router is not None

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            # SPA route should return index.html
            response = client.get("/tasks/abc-123")
            assert response.status_code == 200

            # Assets should still be at /assets/ (not rewritten)
            assert '"/assets/index.js"' in response.text
            # API base should still be /stemtrace
            assert 'window.__STEMTRACE_BASE__="/stemtrace"' in response.text

    def test_full_standalone_server_simulation(self, tmp_path: Path) -> None:
        """Full simulation of standalone CLI server setup."""
        dist_dir = self._create_dist(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            from stemtrace.server.fastapi.extension import StemtraceExtension

            # Simulate what the CLI does:
            # 1. Create extension without UI
            extension = StemtraceExtension(
                broker_url="redis://localhost:6379",
                serve_ui=False,
            )

            app = FastAPI()

            # 2. Mount API at /stemtrace
            app.include_router(extension.router, prefix="/stemtrace")

            # 3. Mount UI at root with explicit API base
            ui_router = get_static_router_with_base("/stemtrace")
            assert ui_router is not None
            app.include_router(ui_router)

            client = TestClient(app)

            # UI at root should work
            ui_response = client.get("/")
            assert ui_response.status_code == 200
            assert 'window.__STEMTRACE_BASE__="/stemtrace"' in ui_response.text
            # Assets should be at /assets/
            assert '"/assets/index.js"' in ui_response.text

            # Assets should be accessible at /assets/
            assert client.get("/assets/index.js").status_code == 200

            # API at /stemtrace should work
            api_response = client.get("/stemtrace/api/health")
            assert api_response.status_code == 200

            # WebSocket path should exist at /stemtrace/ws
            # (TestClient doesn't do WS, but route should be there)

    def test_full_embedded_simulation(self, tmp_path: Path) -> None:
        """Full simulation of embedded FastAPI setup."""
        dist_dir = self._create_dist(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            from stemtrace.server.fastapi.extension import StemtraceExtension

            # Simulate embedded usage:
            # Extension with UI enabled (default), mounted at prefix
            extension = StemtraceExtension(
                broker_url="redis://localhost:6379",
                serve_ui=True,  # UI served from extension router
            )

            app = FastAPI()
            app.include_router(extension.router, prefix="/stemtrace")

            client = TestClient(app)

            # UI at /stemtrace/ should work
            ui_response = client.get("/stemtrace/")
            assert ui_response.status_code == 200
            assert 'window.__STEMTRACE_BASE__="/stemtrace"' in ui_response.text
            # Assets should be rewritten to /stemtrace/assets/
            assert "/stemtrace/assets/index.js" in ui_response.text

            # Assets at /stemtrace/assets/ should work
            assert client.get("/stemtrace/assets/index.js").status_code == 200

            # API at /stemtrace/api/ should work
            api_response = client.get("/stemtrace/api/health")
            assert api_response.status_code == 200
