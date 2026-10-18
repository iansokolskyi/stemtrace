"""Static API key (``Authorization: Bearer <key>``) for the JSON API and WebSocket.

One key (``init_app(api_key=...)``, ``--api-key`` or ``STEMTRACE_API_KEY``) lets
scripts and coding agents read the API without the browser login. With form login,
a valid session cookie *or* the key passes; with a custom ``auth_dependency``, the
key *or* the dependency does (a valid key skips it). The WebSocket takes the key or
a session cookie only. Failed attempts are throttled per client IP by the login's
``LoginThrottle``. The key is never logged. See docs/security.md.
"""

from __future__ import annotations

import base64
import binascii
import enum
import os
import secrets
from dataclasses import dataclass
from typing import Any, Final

# Runtime imports: FastAPI resolves the dependencies' annotations when registering routes.
from fastapi import (
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError

from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server.fastapi.dependency_runner import DependencyRunner
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

#: ``Retry-After`` when an IP's in-flight wrong keys already cover its remaining
#: failures: they resolve as soon as the host dependency answers.
API_KEY_BUSY_RETRY_AFTER_S: Final[int] = 1

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
        """Check a presented key and count a wrong one against the client's IP.

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
        locked = self._lockout(client_ip)
        if locked is not None:
            return locked
        if self._matches(token):
            return KeyCheck(KeyStatus.VALID)
        wait = self.record_failure(client_ip)
        if wait:
            return KeyCheck(KeyStatus.THROTTLED, wait)
        return KeyCheck(KeyStatus.INVALID)

    def attempt(self, token: str | None, client_ip: str | None) -> KeyAttempt:
        """Check a presented key, counting a wrong one only when ``reject()`` is called.

        A wrong key holds an in-flight slot until the ``with`` block ends. An IP whose
        slots cover its remaining failures gets no comparison (throttled, retry after
        ``API_KEY_BUSY_RETRY_AFTER_S``), so a burst can't test more keys than a lockout allows.
        """
        ip = client_ip or None
        if token is None:
            return KeyAttempt(self, None, KeyCheck(KeyStatus.ABSENT))
        locked = self._lockout(ip)
        if locked is not None:
            return KeyAttempt(self, None, locked)
        if ip and not self._throttle.reserve_ip_attempt(ip):
            busy = KeyCheck(KeyStatus.THROTTLED, API_KEY_BUSY_RETRY_AFTER_S)
            return KeyAttempt(self, None, busy)
        if self._matches(token):
            if ip:
                self._throttle.release_ip_attempt(ip)
            return KeyAttempt(self, None, KeyCheck(KeyStatus.VALID))
        return KeyAttempt(self, ip, KeyCheck(KeyStatus.INVALID))

    def record_failure(self, client_ip: str | None) -> int:
        """Count a failed key attempt against the IP.

        Returns:
            Seconds until the IP may try again; 0 if this failure didn't lock it.
        """
        attempt = _attempt(client_ip)
        self._throttle.record_failure(attempt)
        return self._throttle.retry_after(attempt)

    def release(self, client_ip: str) -> None:
        """Free the in-flight slot ``attempt()`` took for the IP."""
        self._throttle.release_ip_attempt(client_ip)

    def _lockout(self, client_ip: str | None) -> KeyCheck | None:
        """The throttled answer for a locked-out IP, else None (decided before comparing)."""
        wait = self._throttle.retry_after(_attempt(client_ip))
        return KeyCheck(KeyStatus.THROTTLED, wait) if wait else None

    def _matches(self, token: str) -> bool:
        return secrets.compare_digest(token.encode("utf-8"), self._key)


class KeyAttempt:
    """A key check whose failure counts only if the request is then refused.

    Use as a context manager: a wrong key's in-flight slot is freed on exit,
    whatever happens inside, including a cancelled request.
    """

    def __init__(
        self, guard: ApiKeyGuard, slot_ip: str | None, check: KeyCheck
    ) -> None:
        """Wrap ``check``; ``slot_ip`` is the IP whose in-flight slot this holds."""
        self._guard = guard
        self._slot_ip = slot_ip
        self._check = check

    @property
    def status(self) -> KeyStatus:
        """Outcome of the key check."""
        return self._check.status

    @property
    def retry_after(self) -> int:
        """Seconds to wait when throttled, else 0."""
        return self._check.retry_after

    def reject(self) -> int:
        """Count the wrong key against the IP, the request having been refused.

        Returns:
            Seconds until the IP may try again; 0 if this failure didn't lock it.
        """
        client_ip = self._slot_ip
        self._free_slot()
        return self._guard.record_failure(client_ip)

    def __enter__(self) -> KeyAttempt:
        """Return the attempt."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Free the in-flight slot, if still held."""
        self._free_slot()

    def _free_slot(self) -> None:
        if self._slot_ip is not None:
            self._guard.release(self._slot_ip)
            self._slot_ip = None


def _attempt(client_ip: str | None) -> LoginAttempt:
    """A key attempt counts against the client's IP only."""
    return LoginAttempt(client_ip=client_ip or None, username=None, device_id=None)


def has_valid_session(cookies: dict[str, str], config: FormAuthConfig | None) -> bool:
    """Whether the request carries a valid form-login session cookie."""
    if config is None:
        return False
    status_ = check_session_cookie(cookies.get(config.cookie_name), config)
    return status_ is SessionStatus.VALID


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _too_many_attempts(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many failed API key attempts",
        headers={"Retry-After": str(retry_after)},
    )


def _refuses_credentials(exc: Exception) -> bool:
    """Whether ``exc`` says the request's credentials are wrong (401, 403, 422)."""
    if isinstance(exc, HTTPException):
        return exc.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )
    return isinstance(exc, RequestValidationError)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


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
        result = guard.check(
            bearer_token(request.headers.get("authorization")), _client_ip(request)
        )
        if result.status is KeyStatus.VALID:
            return
        if result.status is KeyStatus.THROTTLED:
            raise _too_many_attempts(result.retry_after)
        if result.status is KeyStatus.ABSENT and has_valid_session(
            dict(request.cookies), form_auth_config
        ):
            return
        raise _unauthorized(
            "Invalid API key"
            if result.status is KeyStatus.INVALID
            else "Authentication required"
        )

    return Depends(_verify)


def api_key_or_dependency(
    guard: ApiKeyGuard,
    form_auth_config: FormAuthConfig | None,
    auth_dependency: Any,
) -> Any:
    """Create the router dependency that admits a valid key, else defers to ``auth_dependency``.

    A valid key skips ``auth_dependency``. Any other request must pass the session
    check (with form login) and ``auth_dependency``, whose error is the response.
    A key nothing admitted counts as a failed attempt, and a lockout answers 429.
    """
    runner = DependencyRunner(auth_dependency)

    async def _verify(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
    ) -> None:
        request.scope[GATE_SCOPE_KEY] = True
        route_name = getattr(request.scope.get("route"), "name", None)
        # A wrong key is counted only once nothing else has admitted the request:
        # a Bearer token meant for auth_dependency (a JWT) is no failure.
        with guard.attempt(
            bearer_token(request.headers.get("authorization")), _client_ip(request)
        ) as attempt:
            if attempt.status is KeyStatus.VALID:
                return
            try:
                # The login middleware waives the session for the liveness probe.
                if (
                    form_auth_config is not None
                    and route_name not in PUBLIC_ROUTE_NAMES
                    and not has_valid_session(dict(request.cookies), form_auth_config)
                ):
                    raise _unauthorized("Authentication required")
                await runner.run(request, response, background_tasks)
            except (HTTPException, RequestValidationError) as exc:
                # Always re-raised: this only decides whether a refusal of the
                # credentials also counts against the client's key attempts. A
                # 5xx or the dependency's own 429 (its outage, its rate limit)
                # and any other error pass through uncounted.
                if not _refuses_credentials(exc):
                    raise
                wait = attempt.retry_after
                if attempt.status is KeyStatus.INVALID:
                    wait = attempt.reject()
                if wait:
                    raise _too_many_attempts(wait) from exc
                raise

    return Depends(_verify)
