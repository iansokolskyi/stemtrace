"""Unit tests for UI static prefix sanitization and HTML rewriting."""

from __future__ import annotations

import json

from stemtrace.server.ui.static import (
    _rewrite_html_for_prefix,
    _sanitize_derived_prefix,
)


class TestSanitizeDerivedPrefix:
    """Tests for _sanitize_derived_prefix()."""

    def test_sanitize_returns_empty_for_root_and_empty(self) -> None:
        """Root/empty prefixes are treated as 'no prefix'."""
        assert _sanitize_derived_prefix("") == ""
        assert _sanitize_derived_prefix("/") == ""
        assert _sanitize_derived_prefix("   ") == ""

    def test_sanitize_rejects_missing_leading_slash(self) -> None:
        """Derived prefixes must start with '/'."""
        assert _sanitize_derived_prefix("stemtrace") == ""
        assert _sanitize_derived_prefix("stemtrace/") == ""

    def test_sanitize_rejects_invalid_segments(self) -> None:
        """Unsafe characters are rejected (strict allowlist)."""
        assert _sanitize_derived_prefix("/stemtrace<script>") == ""
        assert _sanitize_derived_prefix("/stemtrace/..") == ""
        assert _sanitize_derived_prefix("/stemtrace/%2F") == ""

    def test_sanitize_allows_safe_multi_segment_prefix(self) -> None:
        """Valid multi-segment mount prefixes are preserved."""
        assert _sanitize_derived_prefix("/stemtrace") == "/stemtrace"
        assert _sanitize_derived_prefix("/stemtrace/") == "/stemtrace"
        assert _sanitize_derived_prefix("/api/monitoring") == "/api/monitoring"
        assert _sanitize_derived_prefix("/api/monitoring/") == "/api/monitoring"


class TestRewriteHtmlForPrefix:
    """Tests for _rewrite_html_for_prefix()."""

    def test_rewrite_assets_when_enabled(self) -> None:
        """Asset paths are rewritten to include the mount prefix."""
        html = '<html><head></head><body><script src="/assets/index.js"></script></body></html>'
        rewritten = _rewrite_html_for_prefix(html, "/stemtrace", rewrite_assets=True)
        assert 'src="/stemtrace/assets/index.js"' in rewritten

    def test_does_not_rewrite_assets_when_disabled(self) -> None:
        """Asset paths are not rewritten when rewrite_assets=False."""
        html = '<html><head></head><body><script src="/assets/index.js"></script></body></html>'
        rewritten = _rewrite_html_for_prefix(html, "/stemtrace", rewrite_assets=False)
        assert 'src="/assets/index.js"' in rewritten

    def test_injects_base_as_json_string_literal(self) -> None:
        """The injected base is JSON encoded so it is valid JS and safe."""
        html = "<html><head></head><body>OK</body></html>"
        prefix = "/stemtrace"
        rewritten = _rewrite_html_for_prefix(html, prefix, rewrite_assets=False)
        assert f"window.__STEMTRACE_BASE__={json.dumps(prefix)};" in rewritten

    def test_injects_logout_when_enabled(self) -> None:
        """Logout injection emits a fixed-position POST form wired to logout_path."""
        html = "<html><head></head><body>OK</body></html>"
        rewritten = _rewrite_html_for_prefix(
            html,
            "/stemtrace",
            rewrite_assets=False,
            show_logout=True,
            logout_path="/logout",
        )
        assert 'window.__STEMTRACE_LOGOUT_PATH__="/logout";' in rewritten
        assert "stemtrace-logout" in rewritten
