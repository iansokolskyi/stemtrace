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

import starlette._utils

if TYPE_CHECKING:
    import types
    from collections.abc import Callable, Mapping


def _literal_route_path(scope: Mapping[str, Any]) -> str:
    """Copy of ``get_route_path`` from starlette 0.39.2+ (checked to 1.7.0)."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def _resolve_route_path(
    module: types.ModuleType,
) -> Callable[[Mapping[str, Any]], str]:
    """Starlette's own ``get_route_path`` if it has one, else the literal copy.

    ``get_route_path`` is private, but present in every supported version
    (starlette>=0.38.6 through 1.7.0) and it is what routing uses, so it is
    preferred (see strip_root_path). The fallback keeps ``import stemtrace``
    working if a future Starlette drops the symbol; it matches what 0.39.2+
    does.
    """
    found = getattr(module, "get_route_path", None)
    return found if callable(found) else _literal_route_path


_get_route_path = _resolve_route_path(starlette._utils)


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
    """Remove ``root_path`` from ``path`` exactly as the installed Starlette does.

    Delegates to ``starlette._utils.get_route_path`` when it exists, the function
    ``Router``/``Mount`` match routes with, so auth decisions always see the
    path routing sees. Its behavior differs across supported versions:
    starlette 0.38.6-0.39.1 strip with an unescaped regex (a "." in root_path
    matches any character), 0.39.2+ (checked up to 1.7.0) strip a literal
    prefix at a segment boundary. A private copy of either would let a request
    route to a protected endpoint while the middleware sees another path
    (e.g. root_path "/api.v1" and path "/apiXv1/stemtrace/api/tasks" on 0.38).

    Args:
        path: Decoded (or raw) request path from the scope.
        root_path: ``scope["root_path"]`` as given by the server.

    Returns:
        The path routing matches against.
    """
    return _get_route_path({"type": "http", "path": path, "root_path": root_path})


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


def is_misplaced_asset_path(path: str) -> bool:
    """Whether a path reaches into an ``assets`` segment that isn't the bundle's.

    The UI bundle is only served from the UI root's ``assets/`` (e.g.
    ``/assets/x.js`` for ``stemtrace server``, ``/stemtrace/assets/x.js``
    embedded); callers check that location first. Any other path with an
    ``assets`` segment before its last one (``/stemtrace/assets/x.js`` on the
    CLI, ``/stemtrace/tasks/assets/x.js``) is never a UI page, so it gets a
    404 rather than the HTML shell or a login redirect, which a browser would
    try to run as a script or stylesheet. Only apply it to UI/static paths:
    API paths can legitimately contain "assets" (a task id).

    Args:
        path: UI request path, absolute or relative to the UI root.

    Returns:
        True if an ``assets`` segment appears anywhere but last.
    """
    return "assets" in path.split("/")[:-1]
