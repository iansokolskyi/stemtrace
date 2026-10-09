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

from stemtrace.server.fastapi.form_auth import (
    LOGIN_REASON_SESSION_MISMATCH,
    SESSION_SECRET_MISMATCH_REASON,
    FormAuthConfig,
    SessionStatus,
    check_session_cookie,
    warn_session_secret_mismatch,
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


def create_form_auth_middleware(
    config: FormAuthConfig,
    *,
    login_path: str,
    logout_path: str,
    api_prefix: str,
    assets_prefix: str,
    protected_prefix: str | None = None,
    public_paths: tuple[str, ...] = (),
) -> FormAuthMiddleware:
    """Create an HTTP middleware that requires a valid session cookie.

    Paths containing '.' or '..' segments (in the decoded path or the raw,
    possibly percent-encoded one) are rejected with 404 before any other
    decision, so prefix checks can never be bypassed with ``assets/../``.

    Behaviour for unauthenticated requests:

    - login/logout paths, ``public_paths`` (e.g. the liveness probe) and the
      static bundle under ``assets_prefix`` are always served. The bundle is
      the public open-source UI build and holds no data; leaving it open keeps
      the SPA bootable (and styled) even when a request lands on a worker that
      rejects the session.
    - API paths get a JSON 401. If the cookie was signed by another process's
      generated secret, the body carries ``reason: "session_secret_mismatch"``.
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
        path = request.url.path
        raw = request.scope.get("raw_path")
        raw_path = raw.decode("latin-1") if isinstance(raw, bytes) else ""
        in_scope = (
            protected_prefix is None
            or path.startswith(protected_prefix)
            or urllib.parse.unquote(raw_path).startswith(protected_prefix)
        )
        if not in_scope:
            # Routing doesn't normalize paths, so these can't reach our routes.
            return await call_next(request)
        if _has_dot_segment(path) or _has_dot_segment(raw_path):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if path in open_paths or path.startswith(assets_dir):
            return await call_next(request)

        status = check_session_cookie(request.cookies.get(config.cookie_name), config)
        if status is SessionStatus.VALID:
            return await call_next(request)

        mismatch = status is SessionStatus.SECRET_MISMATCH
        if mismatch:
            warn_session_secret_mismatch()

        if path.startswith(api_prefix):
            body = {"detail": "Authentication required"}
            if mismatch:
                body["reason"] = SESSION_SECRET_MISMATCH_REASON
            return JSONResponse(body, status_code=401)

        next_target = path
        if request.url.query:
            next_target = f"{path}?{request.url.query}"
        login_qs = {"next": next_target}
        if mismatch:
            login_qs["reason"] = LOGIN_REASON_SESSION_MISMATCH
        return RedirectResponse(
            url=f"{login_path}?{urllib.parse.urlencode(login_qs)}", status_code=303
        )

    return form_auth_middleware
