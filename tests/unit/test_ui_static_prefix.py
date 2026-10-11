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
        rewritten = _rewrite_html_for_prefix(html, "/stemtrace")
        assert 'src="/stemtrace/assets/index.js"' in rewritten

    def test_does_not_rewrite_assets_when_disabled(self) -> None:
        """Asset paths are not rewritten when asset_prefix is empty."""
        html = '<html><head></head><body><script src="/assets/index.js"></script></body></html>'
        rewritten = _rewrite_html_for_prefix(html, "/stemtrace", asset_prefix="")
        assert 'src="/assets/index.js"' in rewritten

    def test_injects_base_as_json_string_literal(self) -> None:
        """The injected base is JSON encoded so it is valid JS and safe."""
        html = "<html><head></head><body>OK</body></html>"
        prefix = "/stemtrace"
        rewritten = _rewrite_html_for_prefix(html, prefix, asset_prefix="")
        assert f"window.__STEMTRACE_BASE__={json.dumps(prefix)};" in rewritten

    def test_injects_logout_path_only_when_enabled(self) -> None:
        """Logout is configured via the logout path; the SPA renders the control.

        The server no longer injects a floating button (it overlapped the
        header), only the path the SPA's header Logout form posts to.
        """
        html = "<html><head></head><body>OK</body></html>"
        rewritten = _rewrite_html_for_prefix(
            html,
            "/stemtrace",
            asset_prefix="",
            show_logout=True,
            logout_path="/logout",
        )
        assert rewritten == (
            "<html><head><script>"
            'window.__STEMTRACE_BASE__="/stemtrace";'
            'window.__STEMTRACE_LOGOUT_PATH__="/logout";'
            "</script></head><body>OK</body></html>"
        )

    def test_logout_path_defaults_to_prefix_logout(self) -> None:
        """Without an explicit logout_path, the mount prefix's /logout is used."""
        html = "<html><head></head><body>OK</body></html>"
        rewritten = _rewrite_html_for_prefix(
            html, "/stemtrace", asset_prefix="", show_logout=True
        )
        assert 'window.__STEMTRACE_LOGOUT_PATH__="/stemtrace/logout";' in rewritten

    def test_logout_path_is_escaped_for_script_context(self) -> None:
        """A logout path can't terminate the inline script tag."""
        html = "<html><head></head><body>OK</body></html>"
        rewritten = _rewrite_html_for_prefix(
            html,
            "",
            asset_prefix="",
            show_logout=True,
            logout_path="/x</script><script>alert(1)//",
        )
        assert "</script><script>alert" not in rewritten
        assert rewritten.count("</script>") == 1

    def test_does_not_inject_logout_when_disabled(self) -> None:
        """No logout path is exposed when form login is off."""
        html = "<html><head></head><body>OK</body></html>"
        rewritten = _rewrite_html_for_prefix(html, "/stemtrace", asset_prefix="")
        assert "__STEMTRACE_LOGOUT_PATH__" not in rewritten
