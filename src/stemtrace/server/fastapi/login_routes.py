"""Server-rendered login routes for stemtrace UI protection."""

from __future__ import annotations

import html
import importlib.resources
import secrets
import urllib.parse
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from stemtrace.server.fastapi.form_auth import LOGIN_REASON_SESSION_MISMATCH
from stemtrace.server.fastapi.login_throttle import LoginThrottle
from stemtrace.server.fastapi.paths import browser_path, cookie_path_for

if TYPE_CHECKING:
    from stemtrace.server.fastapi.form_auth import FormAuthConfig

_DEFAULT_TITLE: Final[str] = "stemtrace"

# Fixed messages for known ``reason`` codes; the query value is never echoed.
_REASON_MESSAGES: Final[dict[str, str]] = {
    LOGIN_REASON_SESSION_MISMATCH: (
        "Your session was rejected because it was signed by a different server "
        "process (another worker or replica). If stemtrace "
        "runs with several workers or replicas, set STEMTRACE_LOGIN_SECRET to the "
        "same long random value on all of them."
    ),
}
_LOGIN_TEMPLATE_PATH: Final[str] = "templates/login.html"
_THROTTLED_MESSAGE: Final[str] = (
    "Too many failed sign-in attempts. Try again in {seconds} seconds."
)


@lru_cache(maxsize=1)
def _load_login_template() -> str:
    """Load the login page HTML template from package resources."""
    return (
        importlib.resources.files("stemtrace.server.ui")
        .joinpath(_LOGIN_TEMPLATE_PATH)
        .read_text(encoding="utf-8")
    )


def _safe_next(next_path: str | None, *, default: str) -> str:
    r"""Validate a next redirect path to avoid open redirects.

    Only same-origin absolute paths pass. Backslashes and control characters
    are rejected too, as defense in depth: browsers read "/\evil.example" as
    "//evil.example" and drop tabs/newlines, although Starlette's
    RedirectResponse (0.38.6 through 1.7.0) percent-encodes both in Location.
    """
    if not next_path or not next_path.startswith("/"):
        return default
    if next_path.startswith("//"):
        return default
    if "\\" in next_path or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in next_path):
        return default
    return next_path


def _request_is_https(request: Request) -> bool:
    """Best-effort HTTPS detection that survives a TLS-terminating proxy.

    Falls back to the ``X-Forwarded-Proto`` header when the ASGI scheme is
    ``http`` because proxy headers weren't applied to the scope (e.g. uvicorn
    without ``--forwarded-allow-ips``). The first value of a comma-separated
    list is the original client scheme.
    """
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def _login_page_html(*, action_path: str, error: str | None, next_path: str) -> str:
    """Render the login page HTML from a template."""
    escaped_title = html.escape(_DEFAULT_TITLE, quote=True)
    escaped_action = html.escape(action_path, quote=True)
    escaped_next = html.escape(next_path, quote=True)

    if error is None:
        error_html = ""
    else:
        error_html = (
            '<div class="error" role="alert">'
            + html.escape(error, quote=False)
            + "</div>"
        )

    return (
        _load_login_template()
        .replace("{{title}}", escaped_title)
        .replace("{{action_path}}", escaped_action)
        .replace("{{next_path}}", escaped_next)
        .replace("{{error_html}}", error_html)
    )


def _throttled_response(
    request: Request, next_path: str | None, wait_s: int, default_next_path: str
) -> HTMLResponse:
    """Render the sign-in page with a fixed lockout message, as a 429.

    The same response for every username and password, so it reveals nothing
    about which part was wrong.
    """
    safe_next = _safe_next(
        next_path or request.query_params.get("next"),
        default=browser_path(request.scope, default_next_path),
    )
    page = _login_page_html(
        action_path=request.url_for("stemtrace_login_submit").path,
        error=_THROTTLED_MESSAGE.format(seconds=wait_s),
        next_path=safe_next,
    )
    return HTMLResponse(page, status_code=429, headers={"Retry-After": str(wait_s)})


def create_login_router(
    config: FormAuthConfig,
    *,
    default_next_path: str,
    throttle: LoginThrottle | None = None,
) -> APIRouter:
    """Create router that serves /login and /logout for stemtrace.

    Failed sign-ins are throttled per client IP and per username (see
    :class:`LoginThrottle`): once over the limit, ``POST /login`` answers 429
    with ``Retry-After`` without checking the credentials.

    Args:
        config: Form auth configuration.
        default_next_path: Default redirect target after login (absolute route
            path; the ASGI root_path is prepended per request).
        throttle: Failed-attempt throttle; a new in-memory one if None.

    Returns:
        A configured APIRouter.
    """
    router = APIRouter(tags=["stemtrace-auth"])
    login_throttle = throttle or LoginThrottle()

    @router.get(
        "/login",
        response_class=HTMLResponse,
        include_in_schema=False,
        name="stemtrace_login_page",
    )
    async def login_page(request: Request) -> HTMLResponse:
        """Render sign-in page."""
        error = request.query_params.get("error")
        if error is None:
            error = _REASON_MESSAGES.get(request.query_params.get("reason") or "")
        next_path = _safe_next(
            request.query_params.get("next"),
            default=browser_path(request.scope, default_next_path),
        )
        # Relative path (not an absolute URL): the browser submits using the
        # page's own scheme/host, so a TLS-terminating proxy that doesn't
        # forward the scheme can't downgrade the form action to http.
        action_path = request.url_for("stemtrace_login_submit").path
        rendered_html = _login_page_html(
            action_path=action_path, error=error, next_path=next_path
        )
        return HTMLResponse(rendered_html, status_code=200)

    @router.post("/login", include_in_schema=False, name="stemtrace_login_submit")
    async def login_submit(request: Request) -> Response:
        """Handle login form submit and set session cookie."""
        # Avoid python-multipart dependency: our login form uses the default
        # application/x-www-form-urlencoded encoding, so we can parse it ourselves.
        body = (await request.body()).decode("utf-8", errors="replace")
        form: dict[str, list[str]] = urllib.parse.parse_qs(body, keep_blank_values=True)

        username = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[
            0
        ]  # NOSONAR - form field name, not a hard-coded credential
        next_values = form.get("next")
        next_path = next_values[0] if next_values else None

        client_ip = request.client.host if request.client else ""
        wait = login_throttle.retry_after(client_ip, username)
        if wait > 0:
            return _throttled_response(request, next_path, wait, default_next_path)

        username_ok = secrets.compare_digest(
            username.encode("utf-8"), config.username.encode("utf-8")
        )
        password_ok = secrets.compare_digest(
            password.encode("utf-8"), config.password.encode("utf-8")
        )
        if not (username_ok and password_ok):
            login_throttle.record_failure(client_ip, username)
            wait = login_throttle.retry_after(client_ip, username)
            if wait > 0:
                return _throttled_response(request, next_path, wait, default_next_path)
            # Preserve next parameter if present.
            next_param = (next_path or request.query_params.get("next")) or None
            qs = {"error": "Invalid username or password"}
            if next_param:
                qs["next"] = next_param
            login_url = request.url_for("stemtrace_login_page").path
            return RedirectResponse(
                url=login_url + "?" + urllib.parse.urlencode(qs),
                status_code=303,
            )

        login_throttle.record_success(client_ip, username)
        effective_next = _safe_next(
            next_path or request.query_params.get("next"),
            default=browser_path(request.scope, default_next_path),
        )

        response = RedirectResponse(url=effective_next, status_code=303)
        response.set_cookie(
            key=config.cookie_name,
            value=config.create_session_cookie_value(),
            httponly=True,
            samesite="lax",
            secure=_request_is_https(request),
            path=cookie_path_for(request.scope, config.cookie_path),
        )
        return response

    @router.post("/logout", include_in_schema=False, name="stemtrace_logout")
    async def logout(request: Request) -> RedirectResponse:
        """Clear the session cookie and redirect to login."""
        login_url = request.url_for("stemtrace_login_page").path
        response = RedirectResponse(url=login_url, status_code=303)
        response.delete_cookie(
            key=config.cookie_name,
            path=cookie_path_for(request.scope, config.cookie_path),
        )
        return response

    return router
