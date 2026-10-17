"""Sensitive data scrubbing for task arguments and results.

Implements Sentry-style data scrubbing with a configurable denylist of
sensitive key patterns. Values matching sensitive keys are replaced with
a filtered placeholder.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

FILTERED = "[Filtered]"

# Default sensitive key patterns (case-insensitive, partial match)
DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        # Authentication
        "password",
        "passwd",
        "pwd",
        "pass",
        "secret",
        "api_key",
        "apikey",
        "api_secret",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer",
        "authorization",
        "auth",
        "credentials",
        "private_key",
        "private",
        # Financial/PII
        "credit_card",
        "card_number",
        "cvv",
        "ssn",
        "social_security",
        # Session
        "cookie",
        "session",
        "sessionid",
        "csrf",
        "csrf_token",
    }
)


def _key_names(key: object) -> list[str]:
    """The names a dict key carries, lowercased.

    A task's dicts can have any hashable key. A str is a name, and so is bytes
    (redis-py returns bytes-keyed dicts without ``decode_responses``), decoded
    as UTF-8 with replacement so it never raises. A tuple or frozenset key
    carries the names of its parts, and an Enum member its own name plus its
    value's names (checked first: str- and int-mixin members are str or int).
    Other keys (an int, None, any other object) carry none. ``str(key)`` is
    never used: it is a repr for bytes and tuples, and a key's ``__str__`` can
    raise.
    """
    match key:
        case Enum():
            return [key.name.lower(), *_key_names(key.value)]
        case str():
            return [key.lower()]
        case bytes():
            return [key.decode("utf-8", "replace").lower()]
        case tuple() | frozenset():
            return [name for part in key for name in _key_names(part)]
        case _:
            return []


def _is_sensitive_key(
    key: object,
    sensitive_keys: frozenset[str],
    safe_keys: frozenset[str] | None = None,
) -> bool:
    """Check if a key matches any sensitive pattern.

    Uses case-insensitive partial matching:
    - 'user_password' matches 'password'
    - 'API_KEY' matches 'api_key'

    A key that isn't a str is matched by the names it carries (see
    ``_key_names``): ``b"password"`` and ``("user", "api_key")`` are
    sensitive, ``1`` and ``None`` never are. The values under keys that aren't
    sensitive are still scrubbed.

    Args:
        key: The dict key to check.
        sensitive_keys: Set of sensitive key patterns.
        safe_keys: Optional set of keys to never scrub.

    Returns:
        True if any name the key carries is sensitive and not a safe key.
    """
    safe = {k.lower() for k in safe_keys} if safe_keys else set()
    return any(
        name not in safe and any(pattern in name for pattern in sensitive_keys)
        for name in _key_names(key)
    )


def scrub_dict(
    data: dict[Any, Any],
    sensitive_keys: frozenset[str] = DEFAULT_SENSITIVE_KEYS,
    additional_keys: frozenset[str] | None = None,
    safe_keys: frozenset[str] | None = None,
) -> dict[Any, Any]:
    """Recursively scrub sensitive keys from a dictionary.

    Args:
        data: Dictionary to scrub.
        sensitive_keys: Base set of sensitive key patterns.
        additional_keys: Extra keys to treat as sensitive.
        safe_keys: Keys to never scrub (overrides sensitive).

    Returns:
        New dictionary with sensitive values replaced by FILTERED.

    Example:
        >>> scrub_dict({"password": "secret123", "name": "Alice"})
        {'password': '[Filtered]', 'name': 'Alice'}
    """
    all_sensitive = (
        sensitive_keys | additional_keys if additional_keys else sensitive_keys
    )

    result: dict[Any, Any] = {}
    for key, value in data.items():
        if _is_sensitive_key(key, all_sensitive, safe_keys):
            result[key] = FILTERED
        else:
            result[key] = _scrub_value(value, all_sensitive, safe_keys)

    return result


def _scrub_value(
    value: Any,
    sensitive_keys: frozenset[str],
    safe_keys: frozenset[str] | None = None,
) -> Any:
    """Recursively scrub a value (handles nested dicts and lists)."""
    if isinstance(value, dict):
        return scrub_dict(value, sensitive_keys, safe_keys=safe_keys)
    if isinstance(value, list):
        return [_scrub_value(item, sensitive_keys, safe_keys) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, sensitive_keys, safe_keys) for item in value)
    return value


def scrub_args(
    args: tuple[Any, ...],
    sensitive_keys: frozenset[str] = DEFAULT_SENSITIVE_KEYS,
    additional_keys: frozenset[str] | None = None,
    safe_keys: frozenset[str] | None = None,
) -> list[Any]:
    """Scrub positional arguments (recursively handles nested structures).

    Args:
        args: Tuple of positional arguments.
        sensitive_keys: Base set of sensitive key patterns.
        additional_keys: Extra keys to treat as sensitive.
        safe_keys: Keys to never scrub.

    Returns:
        List of scrubbed arguments.
    """
    all_sensitive = (
        sensitive_keys | additional_keys if additional_keys else sensitive_keys
    )
    return [_scrub_value(arg, all_sensitive, safe_keys) for arg in args]


def safe_serialize(
    value: Any,
    max_size: int = 10240,
    sensitive_keys: frozenset[str] = DEFAULT_SENSITIVE_KEYS,
    additional_keys: frozenset[str] | None = None,
    safe_keys: frozenset[str] | None = None,
) -> Any:
    """Serialize a value to JSON-compatible format with size limit and scrubbing.

    Args:
        value: Value to serialize.
        max_size: Maximum size in bytes before truncation.
        sensitive_keys: Base set of sensitive key patterns.
        additional_keys: Extra keys to treat as sensitive.
        safe_keys: Keys to never scrub.

    Returns:
        JSON-serializable value, or string representation if serialization fails.
    """
    all_sensitive = (
        sensitive_keys | additional_keys if additional_keys else sensitive_keys
    )

    # First scrub the value
    scrubbed = _scrub_value(value, all_sensitive, safe_keys)

    # Try to serialize to check size and ensure it's JSON-compatible
    try:
        serialized = json.dumps(scrubbed, default=str)
        if len(serialized) > max_size:
            return f"[Truncated: {len(serialized)} bytes > {max_size} max]"
        # Preserve the original scrubbed shape (not the JSON string).
        return scrubbed
    except (TypeError, ValueError) as e:
        logger.debug("Failed to serialize value: %s", e)
        # Fall back to string representation
        str_repr = str(scrubbed)
        if len(str_repr) > max_size:
            return f"[Truncated: {len(str_repr)} bytes > {max_size} max]"
        return str_repr
