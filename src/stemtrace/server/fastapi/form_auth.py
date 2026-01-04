"""Form-based authentication primitives for stemtrace.

This module provides a small, dependency-free (stdlib-only) signed-cookie session
mechanism intended for UI-first deployments. It is not meant to replace a full
auth system; rather, it provides a simple way to protect all stemtrace routes
out of the box.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from http.cookies import SimpleCookie
from typing import Final

_SESSION_SEPARATOR: Final[str] = "."


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
    """

    username: str
    password: str
    secret: str
    ttl_seconds: int = 86400
    cookie_name: str = "stemtrace_session"
    cookie_path: str = "/"

    def create_session_cookie_value(self) -> str:
        """Create a signed session cookie value for this config's user."""
        now = int(time.time())
        payload = {"u": self.username, "exp": now + self.ttl_seconds}
        return sign_session(payload, self.secret)


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
    signature = hmac.new(secret.encode("utf-8"), payload_bytes, sha256).digest()
    return f"{_b64url_encode(payload_bytes)}{_SESSION_SEPARATOR}{_b64url_encode(signature)}"


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
    if not cookie_value:
        return None

    if _SESSION_SEPARATOR not in cookie_value:
        return None

    payload_b64, sig_b64 = cookie_value.split(_SESSION_SEPARATOR, 1)
    try:
        payload_bytes = _b64url_decode(payload_b64)
        provided_sig = _b64url_decode(sig_b64)
    except Exception:
        return None

    expected_sig = hmac.new(secret.encode("utf-8"), payload_bytes, sha256).digest()
    if not secrets.compare_digest(provided_sig, expected_sig):
        return None

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


def is_authenticated_cookie(
    cookie_value: str | None,
    *,
    secret: str,
    expected_username: str,
) -> bool:
    """Check whether a session cookie is valid and belongs to expected username."""
    payload = verify_session(cookie_value, secret=secret)
    if payload is None:
        return False
    username = payload.get("u")
    return isinstance(username, str) and secrets.compare_digest(
        username.encode("utf-8"), expected_username.encode("utf-8")
    )
