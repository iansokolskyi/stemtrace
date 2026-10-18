"""Static API key (``Authorization: Bearer <key>``) for the JSON API and WebSocket.

One key (``init_app(api_key=...)``, ``--api-key`` or ``STEMTRACE_API_KEY``) lets
scripts and coding agents read the API without the browser login. With form login,
a valid session cookie *or* the key passes; with a custom ``auth_dependency``, a
JSON API request needs both (FastAPI runs every router dependency), while the
WebSocket needs only the key. Failed attempts are throttled per client IP by the
login's ``LoginThrottle``. The key is never logged. See docs/security.md.
"""

from __future__ import annotations

import base64
import binascii
import enum
import os
import secrets
from dataclasses import dataclass
from typing import Any, Final

from fastapi import Depends, HTTPException, Request, status

from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server.fastapi.form_auth import (
    FormAuthConfig,
    SessionStatus,
    check_session_cookie,
)
from stemtrace.server.fastapi.login_throttle import LoginAttempt, LoginThrottle

API_KEY_ENV_VAR: Final[str] = "STEMTRACE_API_KEY"

#: Shortest accepted key. Throttling slows guessing, but only length makes it hopeless.
API_KEY_MIN_LENGTH: Final[int] = 16

#: Subprotocol the server accepts a WebSocket with when the key came as one.
WS_SUBPROTOCOL: Final[str] = "stemtrace"

#: Browsers can't set headers on a WebSocket, so they offer
#: ``stemtrace.bearer.<base64url(key)>`` next to ``stemtrace``. Not a query
#: parameter: uvicorn's access log prints the query string.
WS_BEARER_PREFIX: Final[str] = "stemtrace.bearer."

#: Set in the ASGI scope by the key dependency, so the form-login middleware can
#: tell a request it deferred actually reached the key check.
GATE_SCOPE_KEY: Final[str] = "stemtrace.api_key_checked"

#: Route names the key doesn't guard (the liveness probe, used by container
#: health checks that carry no credentials).
PUBLIC_ROUTE_NAMES: Final[frozenset[str]] = frozenset({"stemtrace_health_live"})


class KeyStatus(enum.Enum):
    """Outcome of checking the key a request presented."""

    ABSENT = "absent"  # no Bearer credentials
    VALID = "valid"
    INVALID = "invalid"
    THROTTLED = "throttled"  # too many failures from this client; not compared


def normalize_api_key(value: str | None) -> str | None:
    """Validate a configured key: surrounding whitespace is dropped, blank is None.

    Args:
        value: The configured key, or None.

    Returns:
        The key, or None if ``value`` is None or blank.

    Raises:
        ConfigurationError: If the key is shorter than ``API_KEY_MIN_LENGTH``
            or has characters other than visible ASCII. The message never
            contains the key.
    """
    key = (value or "").strip()
    if not key:
        return None
    if len(key) < API_KEY_MIN_LENGTH:
        raise ConfigurationError(
            f"The API key must be at least {API_KEY_MIN_LENGTH} characters; "
            "generate one with `openssl rand -hex 32`."
        )
    if not all("!" <= ch <= "~" for ch in key):
        raise ConfigurationError(
            "The API key may only contain visible ASCII characters (no spaces); "
            "generate one with `openssl rand -hex 32`."
        )
    return key


def configured_api_key(value: str | None) -> str | None:
    """The key from ``value``, or from ``STEMTRACE_API_KEY`` when ``value`` is blank.

    Raises:
        ConfigurationError: See :func:`normalize_api_key`.
    """
    return normalize_api_key(value) or normalize_api_key(os.getenv(API_KEY_ENV_VAR))


def bearer_token(authorization: str | None) -> str | None:
    """Return the credentials of a ``Bearer`` Authorization header.

    The scheme is case-insensitive (RFC 7235). Other schemes, such as
    ``Basic``, count as no key, so they reach a custom ``auth_dependency``.

    Returns:
        The token (possibly empty, which never matches), or None without
        Bearer credentials.
    """
    scheme, _, token = (authorization or "").strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip()


def bearer_from_subprotocols(subprotocols: list[str]) -> str | None:
    """Return the key a WebSocket client offered as ``stemtrace.bearer.<base64url>``.

    Returns:
        The decoded key ("" if it doesn't decode, which never matches), or
        None if no such subprotocol was offered.
    """
    for protocol in subprotocols:
        if protocol.startswith(WS_BEARER_PREFIX):
            encoded = protocol.removeprefix(WS_BEARER_PREFIX)
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                return base64.urlsafe_b64decode(padded).decode("ascii")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return ""
    return None


@dataclass(frozen=True, slots=True)
class KeyCheck:
    """A key check's outcome, and how long to wait when throttled."""

    status: KeyStatus
    retry_after: int = 0


class ApiKeyGuard:
    """Compare presented keys with the configured one, throttling failures per IP."""

    def __init__(self, api_key: str, throttle: LoginThrottle) -> None:
        """Hold the configured key and the shared failed-attempt throttle."""
        self._key = api_key.encode("ascii")
        self._throttle = throttle

    def check(self, token: str | None, client_ip: str | None) -> KeyCheck:
        """Check a presented key.

        The lockout is checked before the key, as ``POST /login`` does:
        otherwise a locked-out client could still tell right keys from wrong
        ones. A valid key doesn't clear the IP's failures, so steady valid
        traffic from a shared address can't reset someone else's guessing.

        Args:
            token: Key presented by the client (None if it sent none).
            client_ip: Client address; None skips throttling (e.g. a Unix
                socket), as the login does.
        """
        if token is None:
            return KeyCheck(KeyStatus.ABSENT)
        attempt = LoginAttempt(
            client_ip=client_ip or None, username=None, device_id=None
        )
        wait = self._throttle.retry_after(attempt)
        if wait:
            return KeyCheck(KeyStatus.THROTTLED, wait)
        if secrets.compare_digest(token.encode("utf-8"), self._key):
            return KeyCheck(KeyStatus.VALID)
        self._throttle.record_failure(attempt)
        wait = self._throttle.retry_after(attempt)
        if wait:
            return KeyCheck(KeyStatus.THROTTLED, wait)
        return KeyCheck(KeyStatus.INVALID)


def has_valid_session(cookies: dict[str, str], config: FormAuthConfig | None) -> bool:
    """Whether the request carries a valid form-login session cookie."""
    if config is None:
        return False
    status_ = check_session_cookie(cookies.get(config.cookie_name), config)
    return status_ is SessionStatus.VALID


def api_key_dependency(
    guard: ApiKeyGuard, form_auth_config: FormAuthConfig | None
) -> Any:
    """Create the router dependency that requires the key (or, with form login, a session).

    Answers 401 with ``WWW-Authenticate: Bearer`` for a missing or wrong key,
    and 429 with ``Retry-After`` while the client is locked out. A wrong key
    is refused even if a valid session cookie came with it.
    """

    async def _verify(request: Request) -> None:
        request.scope[GATE_SCOPE_KEY] = True
        route_name = getattr(request.scope.get("route"), "name", None)
        if route_name in PUBLIC_ROUTE_NAMES:
            return
        client_ip = request.client.host if request.client else None
        result = guard.check(
            bearer_token(request.headers.get("authorization")), client_ip
        )
        if result.status is KeyStatus.VALID:
            return
        if result.status is KeyStatus.THROTTLED:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed API key attempts",
                headers={"Retry-After": str(result.retry_after)},
            )
        if result.status is KeyStatus.ABSENT and has_valid_session(
            dict(request.cookies), form_auth_config
        ):
            return
        detail = (
            "Invalid API key"
            if result.status is KeyStatus.INVALID
            else "Authentication required"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )

    return Depends(_verify)
