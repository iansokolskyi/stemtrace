"""HTTP middleware enforcing stemtrace's built-in form login.

Shared by the embedded integration (``stemtrace.init_app``) and the standalone
``stemtrace server`` CLI, which mount the same routes under different paths.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

from stemtrace.server.fastapi.api_key import GATE_SCOPE_KEY, bearer_token
from stemtrace.server.fastapi.form_auth import (
    LOGIN_REASON_SESSION_MISMATCH,
    SESSION_SECRET_MISMATCH_REASON,
    FormAuthConfig,
    SessionStatus,
    check_session_cookie,
    warn_session_secret_mismatch,
)
from stemtrace.server.fastapi.paths import (
    browser_path,
    is_misplaced_asset_path,
    strip_root_path,
)

FormAuthMiddleware = Callable[
    [Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]
]

# Enough to unwrap double/triple percent-encoding (e.g. %252e -> %2e -> .).
_MAX_DECODE_ROUNDS = 4


def _has_dot_segment(path: str) -> bool:
    """Whether a path has a '.' or '..' segment, after percent-decoding.

    Both '/' and backslash count as separators, and the path is decoded repeatedly
    so encoded variants (%2e, %2f, %5c, double encoding) are caught too.
    """
    candidate = path
    for _ in range(_MAX_DECODE_ROUNDS):
        segments = candidate.replace("\\", "/").split("/")
        if any(segment in (".", "..") for segment in segments):
            return True
        decoded = urllib.parse.unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return True  # Still encoded after several rounds: treat as suspicious.


def _is_ambiguous_path(path: str, raw_path: str) -> bool:
    """Whether a path could slip past prefix and open-path checks.

    True for '.'/'..' segments in the decoded or raw (possibly percent-encoded)
    path, and for a decoded '?' or '#' (sent as %3F/%23), which would make an
    exact open-path match like "health/live%3Fx" ambiguous.
    """
    return (
        "?" in path
        or "#" in path
        or _has_dot_segment(path)
        or _has_dot_segment(raw_path)
    )


def _unauthenticated_response(
    request: Request,
    path: str,
    *,
    login_path: str,
    api_prefix: str,
    secret_mismatch: bool,
    api_key_configured: bool,
) -> Response:
    """Answer a request without a valid session: JSON 401 for API, else login.

    API paths get a 401 whose body carries the mismatch ``reason`` when the
    cookie was signed by another process's secret. UI paths redirect (303) to
    the login page with ``next`` set to the requested path and query, plus the
    login-page ``reason`` for the same case. ``path`` and ``login_path`` are
    route paths; both are prefixed with the ASGI root_path for the browser.
    """
    if path.startswith(api_prefix):
        body = {"detail": "Authentication required"}
        if secret_mismatch:
            body["reason"] = SESSION_SECRET_MISMATCH_REASON
        headers = {"WWW-Authenticate": "Bearer"} if api_key_configured else None
        return JSONResponse(body, status_code=401, headers=headers)

    next_target = browser_path(request.scope, path)
    if request.url.query:
        next_target = f"{next_target}?{request.url.query}"
    login_qs = {"next": next_target}
    if secret_mismatch:
        login_qs["reason"] = LOGIN_REASON_SESSION_MISMATCH
    login_url = browser_path(request.scope, login_path)
    return RedirectResponse(
        url=f"{login_url}?{urllib.parse.urlencode(login_qs)}", status_code=303
    )


def _require_api_key_check(
    call_next: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    """Wrap ``call_next`` so a request no API route key-checked gets a 404.

    A Bearer request is let past the cookie check only for the API key
    dependency to check it. If the path isn't an API route (``api/tasks/``,
    ``apix``), something else (e.g. the UI's catch-all) would answer without
    any check; it gets a 404 instead. A 405 passes: the router rejects a
    method on an API route before any dependency runs, with no data, as it
    does without the login.
    """

    async def _call(request: Request) -> Response:
        response = await call_next(request)
        if request.scope.get(GATE_SCOPE_KEY) or response.status_code == 405:
            return response
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    return _call


def create_form_auth_middleware(
    config: FormAuthConfig,
    *,
    login_path: str,
    logout_path: str,
    api_prefix: str,
    assets_prefix: str,
    protected_prefix: str | None = None,
    public_paths: tuple[str, ...] = (),
    api_key_configured: bool = False,
) -> FormAuthMiddleware:
    """Create an HTTP middleware that requires a valid session cookie.

    All decisions use the route path: the decoded ASGI path (``scope["path"]``)
    and the raw path, each with the ASGI ``root_path`` removed the way
    Starlette routing removes it. All path arguments are route paths too; the
    login redirect and its ``next`` target get the root_path prepended, so the
    middleware works behind a path-prefixing proxy (``uvicorn --root-path``). Paths containing '.' or '..' segments (in the
    decoded path or the raw, possibly percent-encoded one), or whose decoded
    path contains '?' or '#' (sent as ``%3F``/``%23``), are rejected with 404
    before any other decision, so prefix and open-path checks can never be
    bypassed with ``assets/../`` or ``health/live%3Fx``.

    Behaviour for unauthenticated requests:

    - login/logout paths, ``public_paths`` (e.g. the liveness probe) and the
      static bundle under ``assets_prefix`` are always served. The bundle is
      the public open-source UI build and holds no data; leaving it open keeps
      the SPA bootable (and styled) even when a request lands on a worker that
      rejects the session.
    - Other non-API paths with an ``assets`` segment (e.g.
      ``/stemtrace/assets/x.js`` when the bundle is at ``/assets/``) get a
      404, as they do with a session.
    - API paths get a JSON 401. If the cookie was signed by another process's
      generated secret, the body carries ``reason: "session_secret_mismatch"``.
    - With ``api_key_configured``, API requests that carry Bearer credentials
      skip the cookie check: the API key dependency on the API router checks
      the key (and answers 401/429 itself). If no API route ran that check
      (the path isn't an API route), the answer is a 404 instead of whatever
      else matched. Without a key configured, a Bearer header proves nothing
      and the cookie is still required.
    - Everything else (UI routes) redirects to the login page with ``next``
      (and ``reason=session_mismatch`` for the same case).

    Register it with ``app.middleware("http")(middleware)``.

    Args:
        config: Form auth configuration.
        login_path: Absolute path of the login page.
        logout_path: Absolute path of the logout endpoint.
        api_prefix: Path prefix of JSON API routes (answered with 401).
        assets_prefix: Path prefix of the static UI bundle (served without auth).
        protected_prefix: Only paths under this prefix are protected; None
            protects every path.
        public_paths: Exact paths served without a session. They must not
            expose any data.
        api_key_configured: An API key is configured, and the API router
            checks it (see ``api_key.api_key_dependency``).

    Returns:
        The middleware callable.
    """
    assets_dir = assets_prefix.rstrip("/") + "/"
    open_paths = frozenset((login_path, logout_path, *public_paths))

    async def form_auth_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Let authenticated requests through; reject or redirect the rest."""
        # The decoded ASGI path, as routing sees it. Not request.url.path: that
        # re-parses the path as a URL, so a decoded '?' or '#' (from %3F/%23)
        # would cut it short and e.g. "live%3Fx" would pass as an open path.
        root_path = str(request.scope.get("root_path") or "")
        path = strip_root_path(request.scope["path"], root_path)
        raw = request.scope.get("raw_path")
        raw_path = strip_root_path(
            raw.decode("latin-1") if isinstance(raw, bytes) else "", root_path
        )
        in_scope = (
            protected_prefix is None
            or path.startswith(protected_prefix)
            or urllib.parse.unquote(raw_path).startswith(protected_prefix)
        )
        if not in_scope:
            # Routing doesn't normalize paths, so these can't reach our routes.
            return await call_next(request)
        if _is_ambiguous_path(path, raw_path):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # With a key configured, Bearer API requests go to the API key
        # dependency, which checks the key and answers 401/429 itself.
        defer_to_api_key = (
            api_key_configured
            and path.startswith(api_prefix)
            and bearer_token(request.headers.get("authorization")) is not None
        )
        if defer_to_api_key:
            call_next = _require_api_key_check(call_next)
        if path in open_paths or path.startswith(assets_dir) or defer_to_api_key:
            return await call_next(request)
        if not path.startswith(api_prefix) and is_misplaced_asset_path(path):
            # Not a page (API paths may have an "assets" segment, e.g. a task
            # id); 404 like the UI does with a session.
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        status = check_session_cookie(request.cookies.get(config.cookie_name), config)
        if status is SessionStatus.VALID:
            return await call_next(request)

        mismatch = status is SessionStatus.SECRET_MISMATCH
        if mismatch:
            warn_session_secret_mismatch()
        return _unauthenticated_response(
            request,
            path,
            login_path=login_path,
            api_prefix=api_prefix,
            secret_mismatch=mismatch,
            api_key_configured=api_key_configured,
        )

    return form_auth_middleware
