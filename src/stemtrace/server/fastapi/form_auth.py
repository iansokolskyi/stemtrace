"""Form-based authentication primitives for stemtrace.

This module provides a small, dependency-free (stdlib-only) signed-cookie session
mechanism intended for UI-first deployments. It is not meant to replace a full
auth system; rather, it provides a simple way to protect all stemtrace routes
out of the box.
"""

from __future__ import annotations

import base64
import enum
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256
from http.cookies import SimpleCookie
from typing import Final

logger = logging.getLogger(__name__)

_SESSION_SEPARATOR: Final[str] = "."

#: ``reason`` in the JSON body of a 401 when the session cookie was signed with
#: a different secret (another worker/replica without a shared login secret).
SESSION_SECRET_MISMATCH_REASON: Final[str] = "session_secret_mismatch"

#: ``reason`` query parameter on the login page for the same situation.
LOGIN_REASON_SESSION_MISMATCH: Final[str] = "session_mismatch"

_MISMATCH_WARNING_INTERVAL_SECONDS: Final[float] = 60.0
_last_mismatch_warning_at: float | None = None


class SessionStatus(enum.Enum):
    """Outcome of checking a session cookie."""

    VALID = "valid"
    """Correctly signed, unexpired, for the expected user."""

    INVALID = "invalid"
    """Missing, malformed, expired, or for another user."""

    SECRET_MISMATCH = "secret_mismatch"
    """Recent and unexpired, but signed with another process's generated secret."""


@dataclass(frozen=True, slots=True)
class FormAuthConfig:
    """Configuration for stemtrace form-login authentication.

    Args:
        username: Single allowed username.
        password: Password for the user.
        secret: Secret key used to sign cookies.
        ttl_seconds: Session TTL in seconds.
        cookie_name: Cookie name to store the session.
        cookie_path: Cookie path scope. Use the stemtrace mount prefix (e.g.
            "/stemtrace") in embedded mode to avoid affecting other app routes.
        secret_is_generated: True if ``secret`` was generated for this process
            because none was configured (see :func:`resolve_session_secret`).
            Only then is a badly signed session reported as a secret mismatch.
        started_at: Unix time this config was created, i.e. when the process
            started serving. Mismatches are only reported for cookies whose
            ``iat`` is after it; older ones predate this process (e.g. a
            restart).
    """

    username: str
    password: str
    secret: str
    ttl_seconds: int = 86400
    cookie_name: str = "stemtrace_session"
    cookie_path: str = "/"
    secret_is_generated: bool = False
    started_at: float = field(default_factory=time.time)

    def create_session_cookie_value(self) -> str:
        """Create a signed session cookie value for this config's user.

        The payload carries the user (``u``), the expiry (``exp``, whole
        seconds) and the exact issue time (``iat``, float seconds).
        """
        now = time.time()
        payload = {"u": self.username, "exp": int(now) + self.ttl_seconds, "iat": now}
        return sign_session(payload, self.secret)


def _multi_process_hint() -> str | None:
    """Best-effort detection of a multi-process server, for the startup warning.

    Returns:
        A short description of what was detected, or None. Several replicas
        behind a load balancer cannot be detected from inside one process.
    """
    web_concurrency = os.environ.get("WEB_CONCURRENCY", "")
    if web_concurrency.isdigit() and int(web_concurrency) > 1:
        return f"multiple worker processes detected (WEB_CONCURRENCY={web_concurrency})"
    if "gunicorn" in os.environ.get("SERVER_SOFTWARE", "").lower():
        return "running under gunicorn, possibly with multiple workers"
    return None


def resolve_session_secret(explicit_secret: str | None) -> tuple[str, bool]:
    """Return the cookie-signing secret, generating a per-process one if unset.

    An explicit secret (``login_secret`` / ``--login-secret`` /
    ``STEMTRACE_LOGIN_SECRET``) always takes precedence. Without one, a random
    secret is generated for this process and a warning is logged: sessions then
    only work within this single process, so with several workers or replicas
    users get logged out at random (401s) unless all of them share the same
    explicit secret.

    Args:
        explicit_secret: Configured secret, if any.

    Returns:
        ``(secret, secret_is_generated)``: the secret to sign session cookies
        with, and whether it was generated (pass this to
        :attr:`FormAuthConfig.secret_is_generated`).
    """
    if explicit_secret:
        return explicit_secret, False

    message = (
        "Built-in login is enabled without STEMTRACE_LOGIN_SECRET (login_secret / "
        "--login-secret), so a random per-process signing secret is used. Sessions "
        "only work within this single process: with uvicorn --workers N, gunicorn "
        "or several replicas, users will be logged out at random and get 401s "
        "unless STEMTRACE_LOGIN_SECRET is set to the same long random value "
        "everywhere."
    )
    hint = _multi_process_hint()
    if hint is None:
        logger.warning(message)
    else:
        logger.warning("%s Note: %s.", message, hint)
    return secrets.token_urlsafe(32), True


def _b64url_encode(raw: bytes) -> str:
    """Encode bytes as base64url without padding."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    """Decode base64url string without padding."""
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _canonical_json(obj: object) -> bytes:
    """Serialize an object to canonical JSON bytes for signing."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _signature(payload_bytes: bytes, secret: str) -> bytes:
    """Compute the HMAC-SHA256 signature of a session payload."""
    return hmac.new(secret.encode("utf-8"), payload_bytes, sha256).digest()


def sign_session(payload: dict[str, object], secret: str) -> str:
    """Sign a session payload and return cookie value.

    The cookie format is: base64url(payload_json) + '.' + base64url(hmac_sha256).

    Args:
        payload: JSON-serializable payload. Must include at least 'u' and 'exp'.
        secret: Signing secret.

    Returns:
        A cookie-safe string.
    """
    payload_bytes = _canonical_json(payload)
    signature = _signature(payload_bytes, secret)
    return f"{_b64url_encode(payload_bytes)}{_SESSION_SEPARATOR}{_b64url_encode(signature)}"


def _split_cookie(cookie_value: str | None) -> tuple[bytes, bytes] | None:
    """Decode a cookie value into (payload bytes, signature), or None."""
    if not cookie_value or _SESSION_SEPARATOR not in cookie_value:
        return None
    payload_b64, sig_b64 = cookie_value.split(_SESSION_SEPARATOR, 1)
    try:
        return _b64url_decode(payload_b64), _b64url_decode(sig_b64)
    except Exception:
        return None


def _parse_unexpired_payload(payload_bytes: bytes) -> dict[str, object] | None:
    """Parse a session payload; None unless it is a dict with a future 'exp'."""
    try:
        payload_obj = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload_obj, dict):
        return None

    exp = payload_obj.get("exp")
    if not isinstance(exp, int):
        return None

    if exp < int(time.time()):
        return None

    return payload_obj


def verify_session(
    cookie_value: str | None, *, secret: str
) -> dict[str, object] | None:
    """Verify a signed session cookie value.

    Args:
        cookie_value: Cookie value from client.
        secret: Signing secret.

    Returns:
        The decoded payload dict if valid and unexpired; otherwise None.
    """
    parts = _split_cookie(cookie_value)
    if parts is None:
        return None
    payload_bytes, provided_sig = parts

    if not secrets.compare_digest(provided_sig, _signature(payload_bytes, secret)):
        return None

    return _parse_unexpired_payload(payload_bytes)


def parse_cookie_header(cookie_header: str | None) -> dict[str, str]:
    """Parse a Cookie header value into a name->value mapping."""
    if not cookie_header:
        return {}
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


def _is_expected_username(payload: dict[str, object], expected_username: str) -> bool:
    """Check (in constant time) that a payload belongs to the expected user."""
    username = payload.get("u")
    return isinstance(username, str) and secrets.compare_digest(
        username.encode("utf-8"), expected_username.encode("utf-8")
    )


def check_session_cookie(
    cookie_value: str | None, config: FormAuthConfig
) -> SessionStatus:
    """Classify a session cookie as valid, invalid, or signed by another secret.

    ``SECRET_MISMATCH`` is reported only when all of these hold:

    - this process generated its own secret (none configured), so other
      workers/replicas necessarily sign with different secrets;
    - the cookie is well-formed and unexpired but its signature doesn't match;
    - its issue time (``iat``) is strictly after this process started, so it
      must come from a live sibling. Older cookies were signed before a
      restart, and cookies without ``iat`` come from older versions; both are
      plain ``INVALID`` (normal login redirect), not evidence of siblings.

    With a configured secret, a bad signature is always plain ``INVALID``
    (forged cookie or rotated secret). The payload of a badly signed cookie is
    unauthenticated: it never grants access, and its username is ignored so the
    outcome can't be used to probe for the configured username.

    Accepted limitation: anyone can forge an unsigned-but-plausible payload with
    a recent ``iat`` and so learn, from the 401 ``reason``, that no login secret
    is configured, and trigger the (rate-limited) mismatch warning. That
    reveals a configuration fact, not credentials, and the startup warning
    already tells operators to set a secret.

    Args:
        cookie_value: Cookie value from client.
        config: This process's form auth configuration.

    Returns:
        The session status.
    """
    parts = _split_cookie(cookie_value)
    if parts is None:
        return SessionStatus.INVALID
    payload_bytes, provided_sig = parts

    payload = _parse_unexpired_payload(payload_bytes)
    if payload is None:
        return SessionStatus.INVALID
    if secrets.compare_digest(provided_sig, _signature(payload_bytes, config.secret)):
        if _is_expected_username(payload, config.username):
            return SessionStatus.VALID
        return SessionStatus.INVALID
    if not config.secret_is_generated:
        return SessionStatus.INVALID

    iat = payload.get("iat")
    if isinstance(iat, bool) or not isinstance(iat, int | float):
        return SessionStatus.INVALID
    if iat > config.started_at:
        return SessionStatus.SECRET_MISMATCH
    return SessionStatus.INVALID


def is_authenticated_cookie(
    cookie_value: str | None,
    *,
    secret: str,
    expected_username: str,
) -> bool:
    """Check whether a session cookie is valid and belongs to expected username."""
    payload = verify_session(cookie_value, secret=secret)
    return payload is not None and _is_expected_username(payload, expected_username)


def warn_session_secret_mismatch() -> None:
    """Log that a session was signed by a different secret, at most once a minute.

    Never logs the cookie value.
    """
    global _last_mismatch_warning_at
    now = time.monotonic()
    if (
        _last_mismatch_warning_at is not None
        and now - _last_mismatch_warning_at < _MISMATCH_WARNING_INTERVAL_SECONDS
    ):
        return
    _last_mismatch_warning_at = now
    logger.warning(
        "Rejected a session cookie signed by a different secret, likely from "
        "another worker/replica (or a recent restart) without %s. Set it to the "
        "same long random value on every worker and replica.",
        "STEMTRACE_LOGIN_SECRET",
    )
