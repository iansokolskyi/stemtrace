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

from stemtrace.server.fastapi.paths import is_misplaced_asset_path, strip_root_path

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"

_PREFIX_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_unsafe_root_path_warned = False


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


def is_usable_mount_prefix(prefix: str) -> bool:
    """Whether the UI can serve under ``prefix`` (e.g. an ASGI root_path).

    The UI only writes prefixes that pass its sanitizer into the page (see
    ``_sanitize_derived_prefix``); anything else is dropped, so the UI would
    load its assets and API from the wrong URLs.

    Args:
        prefix: Absolute path prefix such as "/monitoring"; "" means none.

    Returns:
        True if the prefix is empty or survives sanitization unchanged.
    """
    normalized = prefix.rstrip("/")
    return _sanitize_derived_prefix(normalized) == normalized


def _warn_if_unsafe_root_path(root_path: object) -> None:
    """Log once per process when the ASGI root_path can't be used by the UI."""
    global _unsafe_root_path_warned
    if _unsafe_root_path_warned or not isinstance(root_path, str):
        return
    if is_usable_mount_prefix(root_path):
        return
    _unsafe_root_path_warned = True
    logger.warning(
        "Ignoring ASGI root_path %r in the stemtrace UI: only segments of "
        "letters, digits, '.', '_' and '-' (up to 64 characters each) are "
        "supported, so UI links will miss the reverse-proxy prefix.",
        root_path,
    )


def _rewrite_html_for_prefix(
    html: str,
    prefix: str,
    *,
    asset_prefix: str | None = None,
    show_logout: bool = False,
    logout_path: str | None = None,
) -> str:
    r"""Rewrite asset paths and inject base path config for the given mount prefix.

    Args:
        html: The HTML content to rewrite.
        prefix: The base path prefix (e.g., "/stemtrace").
        asset_prefix: Path the bundle's ``/assets/`` URLs are rewritten under
            (None: same as ``prefix``; "": left as ``/assets/``). Must be a
            sanitized prefix: it is written into HTML attributes unescaped.
        show_logout: If True, inject ``window.__STEMTRACE_LOGOUT_PATH__`` so the
            SPA renders a Logout control in its header (and knows form login is
            enabled, e.g. to redirect to the login page on 401).
        logout_path: Absolute logout endpoint path. If None and show_logout=True,
            defaults to f"{prefix}/logout" (or "/logout" if prefix is empty).
    """
    assets_base = prefix if asset_prefix is None else asset_prefix
    if assets_base:
        html = html.replace('"/assets/', f'"{assets_base}/assets/')
        html = html.replace("'/assets/", f"'{assets_base}/assets/")
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

    The user-controlled path is joined and normalized as a string (no symlink
    resolution) and accepted only if still under the directory; the filesystem
    isn't touched before that. Symlinks inside the directory are trusted: they
    come from the installed package (e.g. symlink-mode installs).

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


def get_static_router_with_base(  # noqa: C901  sanitizer kept next to the HTML sink in each handler so taint analysis can see it
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
            # Explicit API base: UI at the app root, behind the ASGI root_path
            # (reverse proxy prefix) when there is one.
            root = _sanitize_derived_prefix(request.scope.get("root_path") or "")
            prefix = root + api_base
            asset_prefix = root
            effective_logout = root + logout_path if logout_path else None
        else:
            # Derive the mount point from the route path (root_path removed as
            # routing removes it), then put the browser's root_path in front:
            # uvicorn < 0.26 doesn't prepend root_path to the scope path.
            root_path = request.scope.get("root_path") or ""
            route_path = strip_root_path(request.scope["path"], root_path)
            prefix = _sanitize_derived_prefix(root_path) + _sanitize_derived_prefix(
                route_path.rstrip("/")
            )
            asset_prefix = prefix
            effective_logout = logout_path
        _warn_if_unsafe_root_path(request.scope.get("root_path"))
        return HTMLResponse(
            _rewrite_html_for_prefix(
                index_path.read_text(),
                prefix,
                asset_prefix=asset_prefix,
                show_logout=show_logout,
                logout_path=effective_logout,
            )
        )

    @router.get("/{path:path}", response_model=None)
    async def serve_spa(path: str, request: Request) -> FileResponse | HTMLResponse:
        """Serve static files or fall back to index.html for SPA routing.

        Only regular files under the frontend directory are served. Escaping
        paths, non-file paths under ``assets/`` (served without a session) and
        misplaced ``assets`` segments get a 404, never the app shell; any other
        non-file path falls back to index.html for client-side routes.
        """
        file_path = _contained_frontend_path(path)
        if file_path is None:
            return HTMLResponse("<h1>Not found</h1>", status_code=404)
        if _is_regular_file(file_path):
            return FileResponse(file_path)
        if _is_under_assets(file_path) or is_misplaced_asset_path(path):
            return HTMLResponse("<h1>Not found</h1>", status_code=404)

        index_path = _FRONTEND_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse("<h1>Not found</h1>", status_code=404)

        if api_base is not None:
            # Explicit API base: UI at the app root, behind the ASGI root_path
            # (reverse proxy prefix) when there is one.
            root = _sanitize_derived_prefix(request.scope.get("root_path") or "")
            prefix = root + api_base
            asset_prefix = root
            effective_logout = root + logout_path if logout_path else None
        else:
            # Extract the mount point from the route path (/stemtrace/tasks/123
            # -> /stemtrace), then put the browser's root_path in front (see
            # serve_index).
            root_path = request.scope.get("root_path") or ""
            route_path = strip_root_path(request.scope["path"], root_path)
            derived = (
                route_path[: -len(path)].rstrip("/")
                if path and route_path.endswith(path)
                else route_path.rstrip("/")
            )
            prefix = _sanitize_derived_prefix(root_path) + _sanitize_derived_prefix(
                derived
            )
            asset_prefix = prefix
            effective_logout = logout_path
        _warn_if_unsafe_root_path(request.scope.get("root_path"))
        return HTMLResponse(
            _rewrite_html_for_prefix(
                index_path.read_text(),
                prefix,
                asset_prefix=asset_prefix,
                show_logout=show_logout,
                logout_path=effective_logout,
            )
        )

    return router


def is_ui_available() -> bool:
    """Check if built UI assets exist."""
    return (_FRONTEND_DIR / "index.html").exists()
