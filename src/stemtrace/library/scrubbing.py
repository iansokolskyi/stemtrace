"""Sensitive data scrubbing for task arguments and results.

Implements Sentry-style data scrubbing with a configurable denylist of
sensitive key patterns. Values matching sensitive keys are replaced with
a filtered placeholder.
"""

from __future__ import annotations

import json
import logging
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


def _is_sensitive_key(
    key: str,
    sensitive_keys: frozenset[str],
    safe_keys: frozenset[str] | None = None,
) -> bool:
    """Check if a key matches any sensitive pattern.

    Uses case-insensitive partial matching:
    - 'user_password' matches 'password'
    - 'API_KEY' matches 'api_key'

    Args:
        key: The key name to check.
        sensitive_keys: Set of sensitive key patterns.
        safe_keys: Optional set of keys to never scrub.

    Returns:
        True if the key should be scrubbed.
    """
    if safe_keys and key.lower() in {k.lower() for k in safe_keys}:
        return False

    key_lower = key.lower()
    return any(pattern in key_lower for pattern in sensitive_keys)


def scrub_dict(
    data: dict[str, Any],
    sensitive_keys: frozenset[str] = DEFAULT_SENSITIVE_KEYS,
    additional_keys: frozenset[str] | None = None,
    safe_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
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

    result: dict[str, Any] = {}
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
    elif isinstance(value, list):
        return [_scrub_value(item, sensitive_keys, safe_keys) for item in value]
    elif isinstance(value, tuple):
        return tuple(_scrub_value(item, sensitive_keys, safe_keys) for item in value)
    else:
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
        # Return the original scrubbed value (not the JSON string)
        return scrubbed
    except (TypeError, ValueError) as e:
        logger.debug("Failed to serialize value: %s", e)
        # Fall back to string representation
        str_repr = str(scrubbed)
        if len(str_repr) > max_size:
            return f"[Truncated: {len(str_repr)} bytes > {max_size} max]"
        return str_repr
