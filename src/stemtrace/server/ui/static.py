"""Static file serving for bundled React UI assets."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"

_PREFIX_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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
        show_logout: If True, inject ``window.__STEMTRACE_LOGOUT_PATH__`` so the
            SPA renders a Logout control in its header (and knows form login is
            enabled, e.g. to redirect to the login page on 401).
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
        logout_injection = f"window.__STEMTRACE_LOGOUT_PATH__={logout_js};"
    else:
        logout_injection = ""

    script = f"{head_open}{base_config}{logout_injection}</script>"

    return html.replace("<head>", script)


def _contained_frontend_path(path: str) -> str | None:
    """Confine a request path to the frontend directory, lexically.

    The request path is user-controlled. It is joined onto the resolved
    frontend directory and normalized as a string (dot-segments collapsed,
    no symlink resolution), then accepted only if the result is still under
    that directory. The filesystem is never touched for the candidate before
    this check passes. Symlinks inside the frontend directory are trusted and
    followed when the file is served (they come from the installed package,
    e.g. symlink-mode installs), since only the request path is untrusted.

    Args:
        path: Path parameter taken from the request URL.

    Returns:
        The normalized absolute path if it lies under the frontend directory
        (it may not exist), or None if it escapes the directory.
    """
    base = os.path.realpath(_FRONTEND_DIR)
    candidate = os.path.normpath(Path(base) / path)
    # Plain string prefix check: an absolute request path, a drive change or
    # a UNC path (Windows) all produce a candidate outside this prefix.
    base_prefix = base if base.endswith(os.sep) else base + os.sep
    if candidate != base and not candidate.startswith(base_prefix):
        return None
    return candidate


def _is_under_assets(candidate: str) -> bool:
    """Return True if a contained, normalized path lies in the assets directory.

    Paths under ``assets/`` are served without a login session (they are the
    public UI bundle), so anything there that is not a real file must 404
    rather than fall back to the SPA shell.
    """
    assets_dir = str(Path(os.path.realpath(_FRONTEND_DIR)) / "assets")
    return candidate == assets_dir or candidate.startswith(assets_dir + os.sep)


def _is_regular_file(path: str) -> bool:
    """Return True if path is an existing regular file (symlinks followed).

    Any error while inspecting the path (missing file, name too long,
    embedded NUL byte, permission denied) is treated as "not a file".
    """
    try:
        return stat.S_ISREG(Path(path).stat().st_mode)
    except (OSError, ValueError):
        return False


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
    # follow_symlink: per-file symlinked installs (e.g. uv --link-mode symlink)
    # ship assets as links into a cache outside dist/. Starlette still confines
    # the *request* path to the directory lexically before following links.
    # The directory is resolved up front because older Starlette compares the
    # unresolved request path against the resolved directory, which would 404
    # every asset when the install path itself contains a symlink.
    router.mount(
        "/assets",
        StaticFiles(
            directory=os.path.realpath(_FRONTEND_DIR / "assets"),
            follow_symlink=True,
        ),
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
        """Serve static files or fall back to index.html for SPA routing.

        Only regular files under the frontend directory are served. Paths
        that normalize to a location outside of it get a 404, and so do
        non-file paths under ``assets/`` (served without a session, so they
        must not return the app shell); any other path that is not a servable
        file (missing, a directory, or not inspectable) falls back to
        index.html so client-side routes keep working.
        """
        file_path = _contained_frontend_path(path)
        if file_path is None:
            return HTMLResponse("<h1>Not found</h1>", status_code=404)
        if _is_regular_file(file_path):
            return FileResponse(file_path)
        if _is_under_assets(file_path):
            return HTMLResponse("<h1>Not found</h1>", status_code=404)

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
