"""ASGI ``root_path`` handling shared by form login and its routes.

Behind a path-prefixing reverse proxy (``uvicorn --root-path /proxy``), the
browser-visible URL is ``root_path + route path``. uvicorn puts the prefix in
``scope["root_path"]`` and also prepends it to ``scope["path"]``; Starlette
strips it again before matching routes. Auth decisions must therefore use the
route path (what routing matches), while redirects and cookie paths must use
the browser path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def root_path_of(scope: Mapping[str, Any]) -> str:
    """Return the browser-visible mount prefix of the app, without a trailing '/'.

    Args:
        scope: ASGI connection scope.

    Returns:
        ``scope["root_path"]`` without trailing slashes ("" when unset).
    """
    root_path = scope.get("root_path") or ""
    return str(root_path).rstrip("/")


def strip_root_path(path: str, root_path: str) -> str:
    """Remove ``root_path`` from ``path`` exactly as Starlette routing does.

    Mirrors ``starlette._utils.get_route_path`` (starlette 1.7.0), which
    ``Router``/``Mount`` use to match routes: the prefix is removed only when
    the path starts with it at a segment boundary; otherwise the path is
    returned unchanged (servers that don't prepend root_path to the path).

    Args:
        path: Decoded (or raw) request path from the scope.
        root_path: ``scope["root_path"]`` as given by the server.

    Returns:
        The path routing matches against.
    """
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def browser_path(scope: Mapping[str, Any], path: str) -> str:
    """Prefix an app-absolute path (e.g. "/stemtrace/login") with root_path.

    Args:
        scope: ASGI connection scope.
        path: Absolute path as the app's routes see it.

    Returns:
        The path the browser must use to reach it.
    """
    return root_path_of(scope) + path


def cookie_path_for(scope: Mapping[str, Any], cookie_path: str) -> str:
    """Return the cookie ``Path`` attribute for a route-level cookie path.

    Args:
        scope: ASGI connection scope.
        cookie_path: Configured path as the app's routes see it ("/" or a
            mount prefix like "/stemtrace").

    Returns:
        ``root_path + cookie_path`` without a trailing slash, or "/" at the
        site root. A cookie scoped to ``/stemtrace`` would never be sent for
        the browser URL ``/proxy/stemtrace/...``.
    """
    joined = (root_path_of(scope) + cookie_path).rstrip("/")
    return joined or "/"
