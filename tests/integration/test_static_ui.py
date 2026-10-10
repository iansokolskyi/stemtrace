"""Integration tests for static UI serving."""

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message

from stemtrace.server.ui.static import (
    _contained_frontend_path,
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


class TestStemtraceBaseInjectionEscaping:
    """Regression tests for safe __STEMTRACE_BASE__ injection into index.html."""

    def test_injected_base_is_json_escaped_and_cannot_terminate_script(
        self, tmp_path: Path
    ) -> None:
        """Injected prefix must be a JS expression produced via JSON serialization."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "assets").mkdir()
        (dist_dir / "index.html").write_text(
            "<html><head></head><body>OK</body></html>"
        )

        # This would terminate the script tag if injected unescaped.
        dangerous_prefix = "/stemtrace</script><script>window.__PWNED__=true</script>"

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router_with_base(dangerous_prefix)
            assert router is not None

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            resp = client.get("/")
            assert resp.status_code == 200

            expected_js = json.dumps(dangerous_prefix).replace("</", "<\\/")
            assert f"window.__STEMTRACE_BASE__={expected_js};" in resp.text


class TestDerivedPrefixSanitization:
    """Regression tests for sanitizing prefixes derived from request paths."""

    @pytest.mark.parametrize(
        "suffix",
        [
            # Basic reflected XSS payloads
            "%3Cscript%3Ealert(1)%3C%2Fscript%3E",
            "%3Cimg%20src%3Dx%20onerror%3Dwindow.__PWNED__%3Dtrue%3E",
            "%3Csvg%20onload%3Dwindow.__PWNED__%3Dtrue%3E",
            # Try to break out of the injected <script> context
            "%3C%2Fscript%3E%3Cscript%3Ewindow.__PWNED__%3Dtrue%3C%2Fscript%3E",
            # Try to confuse routing/prefix extraction (encoded slash)
            "a%2Fb%3Cscript%3Ewindow.__PWNED__%3Dtrue%3C%2Fscript%3E",
        ],
    )
    def test_embedded_mode_blocks_xss_payloads_in_path(
        self, tmp_path: Path, suffix: str
    ) -> None:
        """Try hard to inject executable content via user-controlled SPA paths."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "assets").mkdir()
        (dist_dir / "index.html").write_text(
            "<html><head></head><body>OK</body></html>"
        )

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None

            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            resp = client.get(f"/stemtrace/{suffix}")
            assert resp.status_code == 200

            # Correct base must be preserved for embedded mode.
            assert 'window.__STEMTRACE_BASE__="/stemtrace"' in resp.text

            # If we ever reflect decoded user-controlled data into the HTML/JS,
            # these would show up and indicate an exploitable injection path.
            decoded = unquote(suffix)
            assert decoded not in resp.text
            assert "window.__PWNED__" not in resp.text
            assert "</script><script>" not in resp.text


_SECRET_MARKER = "STEMTRACE-SECRET-OUTSIDE-DIST"


async def _asgi_get(app: FastAPI, raw_path: str) -> tuple[int, str]:
    """Send a GET to the ASGI app using a hand-built HTTP scope.

    TestClient re-normalizes URLs (it collapses a leading ``//`` and decodes
    percent-escapes twice), so it cannot reproduce some request paths a real
    server hands to the app. This scope mirrors uvicorn's path decoding:
    ``path`` is ``raw_path`` percent-decoded exactly once.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
    }
    status = 0
    body = bytearray()

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await app(scope, receive, send)
    return status, body.decode("utf-8", errors="replace")


class TestStaticPathContainment:
    """Regression tests: SPA file serving must never escape the frontend dir."""

    def _create_layout(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create dist/ plus a secret file that lives outside of it."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "assets").mkdir()
        (dist_dir / "index.html").write_text(
            "<html><head></head><body>SPA-INDEX</body></html>"
        )
        (dist_dir / "favicon.svg").write_text("<svg>icon</svg>")
        secret = tmp_path / "secret.txt"
        secret.write_text(_SECRET_MARKER)
        return dist_dir, secret

    @staticmethod
    def _traversal_payloads(secret: Path) -> list[str]:
        """Build raw (already URL-encoded) path suffixes that try to reach secret."""
        abs_no_slash = str(secret).lstrip("/")
        deep_dot = "..%2F" * 20
        return [
            "..%2Fsecret.txt",
            "..%2f..%2fdist%2f..%2fsecret.txt",
            "%2e%2e%2Fsecret.txt",
            "%2E%2E/secret.txt",
            "%2e%2e%2fsecret.txt",
            "assets%2F..%2F..%2Fsecret.txt",
            "tasks%2F..%2F..%2Fsecret.txt",
            f"{deep_dot}{abs_no_slash}",
            f"{deep_dot}{abs_no_slash.replace('/', '%2F')}",
            # Absolute path param (literal "//" is covered by the raw ASGI tests,
            # since TestClient collapses it before it reaches the app).
            f"%2F{abs_no_slash.replace('/', '%2F')}",
        ]

    @staticmethod
    def _odd_encoding_payloads() -> list[str]:
        """Backslash, double-encoded and NUL-byte payloads.

        Note: TestClient decodes percent-escapes twice, so under TestClient the
        double-encoded entries arrive at the app as real dot-segments.
        """
        return [
            "..%5Csecret.txt",
            "..%5C..%5Csecret.txt",
            "%252e%252e%252fsecret.txt",
            "..%252Fsecret.txt",
            "..%2Fsecret.txt%00",
            "%00..%2Fsecret.txt",
            "favicon.svg%00",
        ]

    @pytest.mark.parametrize("mount", ["/stemtrace", ""])
    def test_serve_spa_with_traversal_payload_returns_404_without_content(
        self, tmp_path: Path, mount: str
    ) -> None:
        """Encoded dot-segments and absolute paths never return outside files."""
        dist_dir, secret = self._create_layout(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix=mount)
            client = TestClient(app)

            for payload in self._traversal_payloads(secret):
                resp = client.get(f"{mount}/{payload}")
                assert _SECRET_MARKER not in resp.text, payload
                assert resp.status_code == 404, payload

    @pytest.mark.parametrize("mount", ["/stemtrace", ""])
    def test_serve_spa_with_odd_encoding_payload_never_returns_outside_file(
        self, tmp_path: Path, mount: str
    ) -> None:
        """Backslash, double-encoded and NUL payloads get index.html or a 404.

        Depending on how many decoding passes they get, these arrive either as
        literal names (SPA fallback) or as dot-segments (404); both are fine as
        long as the outside file is never returned.
        """
        dist_dir, _secret = self._create_layout(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix=mount)
            client = TestClient(app)

            for payload in self._odd_encoding_payloads():
                resp = client.get(f"{mount}/{payload}")
                assert _SECRET_MARKER not in resp.text, payload
                assert resp.status_code in (200, 404), payload
                if resp.status_code == 200:
                    assert "SPA-INDEX" in resp.text, payload

    def test_serve_spa_standalone_with_traversal_payload_returns_404(
        self, tmp_path: Path
    ) -> None:
        """Standalone mode (explicit API base, root mount) is also confined."""
        dist_dir, secret = self._create_layout(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router_with_base("/stemtrace")
            assert router is not None
            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            for payload in self._traversal_payloads(secret):
                resp = client.get(f"/{payload}")
                assert _SECRET_MARKER not in resp.text, payload
                assert resp.status_code == 404, payload

    def test_serve_spa_with_symlinks_inside_dist_follows_them(
        self, tmp_path: Path
    ) -> None:
        """Symlinks shipped inside dist/ are trusted and served.

        They come from the installed package (e.g. symlink-mode installs), not
        from the request, so their targets may live anywhere.
        """
        dist_dir, _secret = self._create_layout(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "robots.txt").write_text("User-agent: *")
        (dist_dir / "robots.txt").symlink_to(elsewhere / "robots.txt")
        (dist_dir / "vendor").symlink_to(elsewhere, target_is_directory=True)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            for target in ("robots.txt", "vendor/robots.txt"):
                resp = client.get(f"/stemtrace/{target}")
                assert resp.status_code == 200, target
                assert resp.text == "User-agent: *", target

    def test_serve_spa_with_dot_segments_after_symlinked_dir_stays_lexical(
        self, tmp_path: Path
    ) -> None:
        """Dot-segments are collapsed lexically, never relative to a link target."""
        dist_dir, _secret = self._create_layout(tmp_path)
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        (tmp_path / "a" / "favicon.svg").write_text("WRONG-TARGET")
        (dist_dir / "vendor").symlink_to(deep, target_is_directory=True)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            inside = client.get("/stemtrace/vendor%2F..%2Ffavicon.svg")
            assert inside.status_code == 200
            assert inside.text == "<svg>icon</svg>"

            outside = client.get("/stemtrace/vendor%2F..%2F..%2Fsecret.txt")
            assert _SECRET_MARKER not in outside.text
            assert outside.status_code == 404

    def test_serve_spa_with_per_file_symlinked_files_serves_them(
        self, tmp_path: Path
    ) -> None:
        """A real dist/ whose files are symlinks into a cache still serves them.

        Uses paths outside ``assets/`` so the request is handled by the SPA
        route rather than the ``/assets`` StaticFiles mount, whichever FastAPI
        version is installed.
        """
        cache = tmp_path / "cache"
        (cache / "icons").mkdir(parents=True)
        (cache / "index.html").write_text(
            "<html><head></head><body>SPA-INDEX</body></html>"
        )
        (cache / "icons" / "app.svg").write_text("<svg>app</svg>")
        (cache / "favicon.svg").write_text("<svg>icon</svg>")

        dist_dir = tmp_path / "dist"
        (dist_dir / "assets").mkdir(parents=True)
        (dist_dir / "icons").mkdir()
        for rel in ("index.html", "favicon.svg", "icons/app.svg"):
            (dist_dir / rel).symlink_to(cache / rel)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            nested = client.get("/stemtrace/icons/app.svg")
            assert nested.status_code == 200
            assert nested.text == "<svg>app</svg>"

            icon = client.get("/stemtrace/favicon.svg")
            assert icon.status_code == 200
            assert icon.text == "<svg>icon</svg>"

            page = client.get("/stemtrace/tasks/abc-123")
            assert page.status_code == 200
            assert "SPA-INDEX" in page.text

    @pytest.mark.parametrize("mount", ["/stemtrace", ""])
    @pytest.mark.parametrize("via_symlinked_parent", [False, True])
    def test_assets_with_per_file_symlinks_are_served(
        self, tmp_path: Path, mount: str, via_symlinked_parent: bool
    ) -> None:
        """Bundle assets that are symlinks into a cache are served.

        Per-file symlinked installs (e.g. ``uv pip install --link-mode
        symlink``) produce exactly this layout. On FastAPI versions that keep
        the ``/assets`` StaticFiles mount, the mount itself must follow them.
        """
        cache = tmp_path / "cache"
        (cache / "assets").mkdir(parents=True)
        (cache / "assets" / "index-abc.js").write_text("console.log('ok')")
        (cache / "assets" / "index-abc.css").write_text("body{}")

        dist_dir = tmp_path / "dist"
        (dist_dir / "assets").mkdir(parents=True)
        (dist_dir / "index.html").write_text(
            "<html><head></head><body>SPA-INDEX</body></html>"
        )
        for rel in ("assets/index-abc.js", "assets/index-abc.css"):
            (dist_dir / rel).symlink_to(cache / rel)
        secret = tmp_path / "secret.txt"
        secret.write_text(_SECRET_MARKER)

        frontend_dir = dist_dir
        if via_symlinked_parent:
            # The install path itself goes through a symlink (e.g. /var on macOS).
            link = tmp_path / "linked-site-packages"
            link.symlink_to(tmp_path, target_is_directory=True)
            frontend_dir = link / "dist"

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", frontend_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix=mount)
            client = TestClient(app)

            js = client.get(f"{mount}/assets/index-abc.js")
            assert js.status_code == 200
            assert js.text == "console.log('ok')"

            css = client.get(f"{mount}/assets/index-abc.css")
            assert css.status_code == 200
            assert css.text == "body{}"

            escape = client.get(f"{mount}/assets/..%2F..%2Fsecret.txt")
            assert _SECRET_MARKER not in escape.text
            assert escape.status_code == 404

    def test_serve_spa_with_overlong_segment_falls_back_to_index(
        self, tmp_path: Path
    ) -> None:
        """A path segment longer than the OS name limit does not cause a 500."""
        dist_dir, _secret = self._create_layout(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            resp = client.get(f"/stemtrace/tasks/{'x' * 300}")
            assert resp.status_code == 200
            assert "SPA-INDEX" in resp.text

    def test_serve_spa_with_symlinked_dist_dir_serves_files(
        self, tmp_path: Path
    ) -> None:
        """A dist/ that is itself a symlink (e.g. editable installs) still works."""
        real_dist, _secret = self._create_layout(tmp_path)
        linked_dist = tmp_path / "linked-dist"
        linked_dist.symlink_to(real_dist, target_is_directory=True)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", linked_dist):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            resp = client.get("/stemtrace/favicon.svg")
            assert resp.status_code == 200
            assert resp.text == "<svg>icon</svg>"

    def test_serve_spa_with_dot_segments_inside_dist_serves_file(
        self, tmp_path: Path
    ) -> None:
        """Dot-segments that stay inside dist/ still resolve to the real file."""
        dist_dir, _secret = self._create_layout(tmp_path)
        (dist_dir / "icons").mkdir()

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            resp = client.get("/stemtrace/icons%2F..%2Ffavicon.svg")
            assert resp.status_code == 200
            assert resp.text == "<svg>icon</svg>"

    def test_serve_spa_legit_file_and_deep_link_still_work(
        self, tmp_path: Path
    ) -> None:
        """Real top-level files are served and client routes fall back to index."""
        dist_dir, _secret = self._create_layout(tmp_path)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            router = get_static_router()
            assert router is not None
            app = FastAPI()
            app.include_router(router, prefix="/stemtrace")
            client = TestClient(app)

            icon = client.get("/stemtrace/favicon.svg")
            assert icon.status_code == 200
            assert icon.text == "<svg>icon</svg>"

            for route in (
                "tasks/abc-123",
                "graph/root-1",
                "registry",
                "tasks/myapp.tasks.add",
            ):
                resp = client.get(f"/stemtrace/{route}")
                assert resp.status_code == 200, route
                assert "SPA-INDEX" in resp.text, route
                assert 'window.__STEMTRACE_BASE__="/stemtrace"' in resp.text, route


class TestStaticPathContainmentRawAsgi:
    """Containment tests using uvicorn-equivalent (single) path decoding."""

    def _create_app(self, tmp_path: Path, mount: str) -> tuple[FastAPI, Path]:
        """Build an app serving a temp dist/ with a file outside of it."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "assets").mkdir()
        (dist_dir / "index.html").write_text(
            "<html><head></head><body>SPA-INDEX</body></html>"
        )
        (dist_dir / "favicon.svg").write_text("<svg>icon</svg>")
        secret = tmp_path / "secret.txt"
        secret.write_text(_SECRET_MARKER)

        router = get_static_router()
        assert router is not None
        app = FastAPI()
        app.include_router(router, prefix=mount)
        return app, secret

    @pytest.mark.parametrize("mount", ["/stemtrace", ""])
    async def test_absolute_path_param_returns_404(
        self, tmp_path: Path, mount: str
    ) -> None:
        """A path parameter that is itself absolute is not served."""
        with patch("stemtrace.server.ui.static._FRONTEND_DIR", tmp_path / "dist"):
            app, secret = self._create_app(tmp_path, mount)
            status, body = await _asgi_get(app, f"{mount}/{secret}")

        assert _SECRET_MARKER not in body
        assert status == 404

    @pytest.mark.parametrize(
        "suffix", ["..%2Fsecret.txt", "%2e%2e%2fsecret.txt", "%2E%2E/secret.txt"]
    )
    async def test_encoded_dot_segments_return_404(
        self, tmp_path: Path, suffix: str
    ) -> None:
        """Encoded dot-segments that leave dist/ are rejected."""
        with patch("stemtrace.server.ui.static._FRONTEND_DIR", tmp_path / "dist"):
            app, _secret = self._create_app(tmp_path, "/stemtrace")
            status, body = await _asgi_get(app, f"/stemtrace/{suffix}")

        assert _SECRET_MARKER not in body
        assert status == 404

    @pytest.mark.parametrize(
        "suffix", ["%252e%252e%252fsecret.txt", "..%255Csecret.txt", "..%5Csecret.txt"]
    )
    async def test_double_encoded_or_backslash_falls_back_to_index(
        self, tmp_path: Path, suffix: str
    ) -> None:
        """Single-decoded literal names stay inside dist/ and hit the SPA index."""
        with patch("stemtrace.server.ui.static._FRONTEND_DIR", tmp_path / "dist"):
            app, _secret = self._create_app(tmp_path, "/stemtrace")
            status, body = await _asgi_get(app, f"/stemtrace/{suffix}")

        assert _SECRET_MARKER not in body
        assert status == 200
        assert "SPA-INDEX" in body

    async def test_legit_file_is_served(self, tmp_path: Path) -> None:
        """Regular files inside dist/ are still served."""
        with patch("stemtrace.server.ui.static._FRONTEND_DIR", tmp_path / "dist"):
            app, _secret = self._create_app(tmp_path, "/stemtrace")
            status, body = await _asgi_get(app, "/stemtrace/favicon.svg")

        assert status == 200
        assert body == "<svg>icon</svg>"


class TestContainedFrontendPath:
    """Unit tests for the lexical containment helper behind the SPA route."""

    @pytest.mark.parametrize(
        ("request_path", "expected_rel"),
        [
            ("favicon.svg", "favicon.svg"),
            ("assets/index-abc.js", "assets/index-abc.js"),
            ("icons/../favicon.svg", "favicon.svg"),
            ("a/./b//c", "a/b/c"),
            ("tasks/myapp.tasks.add", "tasks/myapp.tasks.add"),
            ("", ""),
        ],
    )
    def test_contained_path_inside_dist_returns_normalized_path(
        self, tmp_path: Path, request_path: str, expected_rel: str
    ) -> None:
        """Paths that stay inside dist/ are normalized but not resolved."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            result = _contained_frontend_path(request_path)

        base = os.path.realpath(dist_dir)
        expected = os.path.normpath(Path(base) / expected_rel)
        assert result == expected

    @pytest.mark.parametrize(
        "request_path",
        [
            "../secret.txt",
            "icons/../../secret.txt",
            "/etc/passwd",
            "../dist-other/file.txt",
            "..",
        ],
    )
    def test_contained_path_escaping_dist_returns_none(
        self, tmp_path: Path, request_path: str
    ) -> None:
        """Dot-segments, absolute paths and sibling-prefix names are rejected."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            assert _contained_frontend_path(request_path) is None

    def test_contained_path_does_not_follow_symlinks(self, tmp_path: Path) -> None:
        """A symlink inside dist/ is kept as-is, not replaced by its target."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        target = tmp_path / "cache" / "favicon.svg"
        target.parent.mkdir()
        target.write_text("<svg/>")
        (dist_dir / "favicon.svg").symlink_to(target)

        with patch("stemtrace.server.ui.static._FRONTEND_DIR", dist_dir):
            result = _contained_frontend_path("favicon.svg")

        assert result == str(Path(os.path.realpath(dist_dir)) / "favicon.svg")
