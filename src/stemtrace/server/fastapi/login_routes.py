"""Server-rendered login routes for stemtrace UI protection."""

from __future__ import annotations

import html
import importlib.resources
import secrets
import urllib.parse
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

if TYPE_CHECKING:
    from stemtrace.server.fastapi.form_auth import FormAuthConfig

_DEFAULT_TITLE: Final[str] = "stemtrace"
_LOGIN_TEMPLATE_PATH: Final[str] = "templates/login.html"


@lru_cache(maxsize=1)
def _load_login_template() -> str:
    """Load the login page HTML template from package resources."""
    return (
        importlib.resources.files("stemtrace.server.ui")
        .joinpath(_LOGIN_TEMPLATE_PATH)
        .read_text(encoding="utf-8")
    )


def _safe_next(next_path: str | None, *, default: str) -> str:
    """Validate a next redirect path to avoid open redirects."""
    if not next_path:
        return default
    if not next_path.startswith("/"):
        return default
    if next_path.startswith("//"):
        return default
    return next_path


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


def create_login_router(
    config: FormAuthConfig,
    *,
    default_next_path: str,
) -> APIRouter:
    """Create router that serves /login and /logout for stemtrace.

    Args:
        config: Form auth configuration.
        default_next_path: Default redirect target after login (absolute path).

    Returns:
        A configured APIRouter.
    """
    router = APIRouter(tags=["stemtrace-auth"])

    @router.get(
        "/login",
        response_class=HTMLResponse,
        include_in_schema=False,
        name="stemtrace_login_page",
    )
    async def login_page(request: Request) -> HTMLResponse:
        """Render sign-in page."""
        error = request.query_params.get("error")
        next_path = _safe_next(
            request.query_params.get("next"), default=default_next_path
        )
        action_path = str(request.url_for("stemtrace_login_submit"))
        rendered_html = _login_page_html(
            action_path=action_path, error=error, next_path=next_path
        )
        return HTMLResponse(rendered_html, status_code=200)

    @router.post("/login", include_in_schema=False, name="stemtrace_login_submit")
    async def login_submit(request: Request) -> RedirectResponse:
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

        username_ok = secrets.compare_digest(
            username.encode("utf-8"), config.username.encode("utf-8")
        )
        password_ok = secrets.compare_digest(
            password.encode("utf-8"), config.password.encode("utf-8")
        )
        if not (username_ok and password_ok):
            # Preserve next parameter if present.
            next_param = (next_path or request.query_params.get("next")) or None
            qs = {"error": "Invalid username or password"}
            if next_param:
                qs["next"] = next_param
            login_url = str(request.url_for("stemtrace_login_page"))
            return RedirectResponse(
                url=login_url + "?" + urllib.parse.urlencode(qs),
                status_code=303,
            )

        effective_next = _safe_next(
            next_path or request.query_params.get("next"), default=default_next_path
        )

        response = RedirectResponse(url=effective_next, status_code=303)
        response.set_cookie(
            key=config.cookie_name,
            value=config.create_session_cookie_value(),
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path=config.cookie_path,
        )
        return response

    @router.post("/logout", include_in_schema=False, name="stemtrace_logout")
    async def logout(request: Request) -> RedirectResponse:
        """Clear the session cookie and redirect to login."""
        login_url = str(request.url_for("stemtrace_login_page"))
        response = RedirectResponse(url=login_url, status_code=303)
        response.delete_cookie(key=config.cookie_name, path=config.cookie_path)
        return response

    return router
