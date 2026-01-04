"""Static file serving for bundled React UI assets."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"

_PREFIX_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_LOGOUT_INJECTION_JS_TEMPLATE = """
window.__STEMTRACE_LOGOUT_PATH__=__LOGOUT_PATH__;
document.addEventListener("DOMContentLoaded", () => {
  try {
    const action = window.__STEMTRACE_LOGOUT_PATH__;
    if (!action) return;

    const style = document.createElement("style");
    style.textContent = `
      #stemtrace-logout {
        position: fixed;
        top: 12px;
        right: 12px;
        z-index: 9999;
        margin: 0;
        padding: 0;
      }

      #stemtrace-logout button {
        all: unset;
        cursor: pointer;
        user-select: none;
        padding: 6px 10px;
        border-radius: 999px;
        font: 600 12px ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica,
          Arial;
        letter-spacing: 0.01em;
        border: 1px solid rgba(255, 255, 255, 0.18);
        color: rgba(255, 255, 255, 0.92);
        background: rgba(17, 24, 39, 0.7);
        backdrop-filter: blur(8px);
        box-shadow: 0 6px 18px rgba(0, 0, 0, 0.2);
        transition: filter 0.12s ease, transform 0.12s ease, background 0.12s ease;
      }

      #stemtrace-logout button:hover {
        filter: brightness(1.06);
      }

      #stemtrace-logout button:active {
        transform: translateY(1px);
      }

      @media (prefers-color-scheme: light) {
        #stemtrace-logout button {
          border: 1px solid rgba(0, 0, 0, 0.1);
          color: rgba(17, 24, 39, 0.92);
          background: rgba(255, 255, 255, 0.85);
        }
      }
    `;
    document.head.appendChild(style);

    const form = document.createElement("form");
    form.id = "stemtrace-logout";
    form.method = "post";
    form.action = action;

    const btn = document.createElement("button");
    btn.type = "submit";
    btn.textContent = "Logout";

    form.appendChild(btn);
    document.body.appendChild(form);
  } catch {}
});
"""


def _sanitize_derived_prefix(prefix: str) -> str:
    """Sanitize a prefix derived from request paths.

    This is intentionally strict: request paths are user-controlled. We only allow
    predictable mount prefixes composed of URL-safe segments (letters, numbers,
    '.', '_', '-') separated by '/'. Dot-segments ('.' and '..') are rejected.

    Args:
        prefix: Candidate prefix derived from request.url.path.

    Returns:
        A safe prefix (e.g. "/stemtrace", "/api/monitoring") or "" for root/invalid.
    """
    normalized = prefix.strip().rstrip("/")
    if normalized in ("", "/"):
        return ""

    if not normalized.startswith("/"):
        return ""

    segments = [s for s in normalized.split("/") if s]

    if any(seg in (".", "..") for seg in segments):
        return ""

    if not all(_PREFIX_SEGMENT_RE.fullmatch(seg) for seg in segments):
        return ""

    return "/" + "/".join(segments)


def _rewrite_html_for_prefix(
    html: str,
    prefix: str,
    *,
    rewrite_assets: bool = True,
    show_logout: bool = False,
    logout_path: str | None = None,
) -> str:
    r"""Rewrite asset paths and inject base path config for the given mount prefix.

    Args:
        html: The HTML content to rewrite.
        prefix: The base path prefix (e.g., "/stemtrace").
        rewrite_assets: If True, rewrite asset paths. If False, only inject API base.
        show_logout: If True, inject a small Logout button into the UI.
        logout_path: Absolute logout endpoint path. If None and show_logout=True,
            defaults to f"{prefix}/logout" (or "/logout" if prefix is empty).
    """
    if prefix and rewrite_assets:
        html = html.replace('"/assets/', f'"{prefix}/assets/')
        html = html.replace("'/assets/", f"'{prefix}/assets/")
    # Inject base path safely. The prefix may be user-provided (e.g. init_app(prefix=...))
    # or derived from a request path, so it must be serialized as a JS string literal.
    # Also, prevent `</script>` inside the string from terminating the script tag.
    prefix_js = json.dumps(prefix).replace("</", "<\\/")

    head_open = "<head><script>"
    base_config = f"window.__STEMTRACE_BASE__={prefix_js};"

    if show_logout:
        effective_logout = logout_path or (f"{prefix}/logout" if prefix else "/logout")
        logout_js = json.dumps(effective_logout).replace("</", "<\\/")
        logout_injection = _LOGOUT_INJECTION_JS_TEMPLATE.replace(
            "__LOGOUT_PATH__", logout_js
        )
    else:
        logout_injection = ""

    script = f"{head_open}{base_config}{logout_injection}</script>"

    return html.replace("<head>", script)


def get_static_router(*, show_logout: bool = False) -> APIRouter | None:
    """Create router for UI static files. Returns None if dist/ missing."""
    return get_static_router_with_base(None, show_logout=show_logout)


def get_static_router_with_base(
    api_base: str | None,
    *,
    show_logout: bool = False,
    logout_path: str | None = None,
) -> APIRouter | None:
    """Create router for UI with explicit API base path.

    Args:
        api_base: Fixed base path for API/WebSocket connections.
                  If None, derives from request URL path.
        show_logout: If True, inject a Logout button into the UI.
        logout_path: Absolute logout endpoint path (see _rewrite_html_for_prefix).

    Returns:
        APIRouter for UI, or None if dist/ missing.
    """
    if not _FRONTEND_DIR.exists():
        logger.warning("Frontend dist not found at %s", _FRONTEND_DIR)
        return None

    router = APIRouter(tags=["stemtrace-ui"])
    router.mount(
        "/assets",
        StaticFiles(directory=_FRONTEND_DIR / "assets"),
        name="stemtrace-assets",
    )

    @router.get("/", response_class=HTMLResponse)
    async def serve_index(request: Request) -> HTMLResponse:
        """Serve the main index.html page."""
        index_path = _FRONTEND_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse("<h1>UI not built</h1>", status_code=503)

        if api_base is not None:
            # Explicit API base: don't rewrite assets (they're served at current mount)
            prefix = api_base
            rewrite_assets = False
        else:
            # Derive from URL: rewrite assets to match mount point
            prefix = _sanitize_derived_prefix(request.url.path.rstrip("/") or "")
            rewrite_assets = True
        return HTMLResponse(
            _rewrite_html_for_prefix(
                index_path.read_text(),
                prefix,
                rewrite_assets=rewrite_assets,
                show_logout=show_logout,
                logout_path=logout_path,
            )
        )

    @router.get("/{path:path}", response_model=None)
    async def serve_spa(path: str, request: Request) -> FileResponse | HTMLResponse:
        """Serve static files or fall back to index.html for SPA routing."""
        file_path = _FRONTEND_DIR / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)

        index_path = _FRONTEND_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse("<h1>Not found</h1>", status_code=404)

        if api_base is not None:
            # Explicit API base: don't rewrite assets (they're served at current mount)
            prefix = api_base
            rewrite_assets = False
        else:
            # Extract prefix: /stemtrace/tasks/123 -> /stemtrace
            url_path = request.url.path
            derived = (
                url_path[: -len(path)].rstrip("/")
                if path and url_path.endswith(path)
                else url_path.rstrip("/")
            )
            prefix = _sanitize_derived_prefix(derived)
            rewrite_assets = True
        return HTMLResponse(
            _rewrite_html_for_prefix(
                index_path.read_text(),
                prefix,
                rewrite_assets=rewrite_assets,
                show_logout=show_logout,
                logout_path=logout_path,
            )
        )

    return router


def is_ui_available() -> bool:
    """Check if built UI assets exist."""
    return (_FRONTEND_DIR / "index.html").exists()
