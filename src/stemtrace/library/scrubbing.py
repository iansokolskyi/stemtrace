"""Sensitive data scrubbing for task arguments and results.

Implements Sentry-style data scrubbing with a configurable denylist of
sensitive key patterns. Values matching sensitive keys are replaced with
a filtered placeholder.
"""

from __future__ import annotations

import json
import logging
from enum import Enum, Flag
from typing import Any

logger = logging.getLogger(__name__)

FILTERED = "[Filtered]"
# Start of the text safe_serialize returns instead of a value that is too big.
TRUNCATED_PREFIX = "[Truncated: "

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


def _lowercase_patterns(
    sensitive_keys: frozenset[str], additional_keys: frozenset[str] | None
) -> frozenset[str]:
    """All sensitive patterns, lowercased once per call.

    Keys are lowercased before matching; patterns come from user config as
    written (``additional_sensitive_keys={"MRN"}``), so they are too.
    """
    patterns = sensitive_keys | additional_keys if additional_keys else sensitive_keys
    return frozenset(pattern.lower() for pattern in patterns)


def _key_names(key: object) -> list[str]:
    """The names a dict key carries, lowercased.

    A str or int key, including an IntEnum, IntFlag or StrEnum member, is
    matched as that str or int (such a member equals its value). Bytes are
    decoded leniently, a tuple or frozenset carries its parts' names, another
    Enum member its name plus its value's, another Flag every member it has.
    """
    names: list[str]
    match key:
        # str and int before Enum and Flag: a mixin member equals its value.
        case str():
            # str.lower, not key.lower: the key's own text, whatever its class.
            names = [str.lower(key)]
        case int():
            names = []
        case Flag():
            own = [key.name.lower()] if isinstance(key.name, str) else []
            # __members__, not iteration: iteration skips aliases on every
            # version, and multi-bit members on 3.11+. Its keys are the
            # names: an alias's member object carries the canonical name.
            members = [
                name.lower()
                for name, member in type(key).__members__.items()
                if member.value and key.value & member.value == member.value
            ]
            names = [*own, *members]
        case Enum():
            # A custom Enum can override name; don't call str methods on None.
            name = [key.name.lower()] if isinstance(key.name, str) else []
            names = [*name, *_key_names(key.value)]
        case bytes():
            # e.g. redis-py's bytes-keyed dicts without decode_responses.
            names = [key.decode("utf-8", "replace").lower()]
        case tuple() | frozenset():
            names = [name for part in key for name in _key_names(part)]
        case _:
            # Never str(key): a repr for bytes and tuples, and __str__ can raise.
            names = []
    return names


def _is_sensitive_key(
    key: object,
    sensitive_keys: frozenset[str],
    safe_keys: frozenset[str] | None = None,
) -> bool:
    """Check if a key matches any sensitive pattern.

    Case-insensitive partial matching ('user_password' matches 'password'). A
    non-str key is matched by the names it carries (``b"password"`` and
    ``("user", "api_key")`` are sensitive, ``1`` never is). Values under keys
    that aren't sensitive are still scrubbed.

    Args:
        key: The dict key to check.
        sensitive_keys: Set of sensitive key patterns, lowercase (see
            ``_lowercase_patterns``).
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
    return _scrub_dict(
        data, _lowercase_patterns(sensitive_keys, additional_keys), safe_keys
    )


def _scrub_dict(
    data: dict[Any, Any],
    patterns: frozenset[str],
    safe_keys: frozenset[str] | None,
    budget: _Budget | None = None,
) -> dict[Any, Any]:
    """Scrub a dictionary with patterns already lowercased by the entry point."""
    result: dict[Any, Any] = {}
    for key, value in data.items():
        if budget is not None:
            budget.charge(4)  # ": " and ", "
            _charge_key(key, budget)
        shown = _as_matched(key)
        if _is_sensitive_key(key, patterns, safe_keys):
            if budget is not None:
                budget.charge(len(FILTERED) + 2)
            result[shown] = FILTERED
        else:
            result[shown] = _scrub_value(value, patterns, safe_keys, budget)

    return result


def _as_matched(key: Any) -> Any:
    """A dict key as ``_key_names`` matches it: a str or int as its own value.

    An Enum member that is a str or int otherwise prints as its name
    (``Field.PASSWORD``), which may look sensitive while its value isn't.
    """
    match key:
        case bool():
            return key
        case str():
            return str.__str__(key)
        case int():
            return int.__int__(key)
        case tuple() | frozenset():
            return _rebuilt(key)
        case _:
            return key


def _rebuilt(key: tuple[Any, ...] | frozenset[Any]) -> Any:
    """``key`` with its parts as matched, of the same kind of container."""
    parts = [_as_matched(part) for part in key]
    if isinstance(key, frozenset):
        return frozenset(parts)
    if hasattr(type(key), "_fields"):  # a namedtuple keeps its type
        # tuple.__new__ skips the subclass's own __new__, so the copy has no
        # instance state (it could hold the unscrubbed parts): a __repr__ that
        # reads such state raises, and the capture falls back to its placeholder.
        return tuple.__new__(type(key), parts)
    return tuple(parts)


def _scrub_value(
    value: Any,
    sensitive_keys: frozenset[str],
    safe_keys: frozenset[str] | None = None,
    budget: _Budget | None = None,
) -> Any:
    """Recursively scrub a value (handles nested dicts and lists).

    With a ``budget``, every node is charged its smallest output size, so the
    walk ends with ValueTooBig after O(budget) nodes instead of the whole value.
    """
    if isinstance(value, dict):
        return _scrub_dict(value, sensitive_keys, safe_keys, budget)
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            if budget is not None:
                budget.charge(2)  # an element and its ", "
            items.append(_scrub_value(item, sensitive_keys, safe_keys, budget))
        return items if isinstance(value, list) else tuple(items)
    if budget is not None:
        _charge_leaf(value, budget)
    return value


class ValueTooBig(Exception):
    """A value's smallest possible scrubbed size already exceeds the limit."""


class _Budget:
    """Running lower bound on a value's scrubbed size, shared by one walk."""

    def __init__(self, max_size: int) -> None:
        self.left = max_size

    def charge(self, cost: int) -> None:
        """Spend ``cost`` bytes, raising ValueTooBig once the limit is passed."""
        self.left -= cost
        if self.left < 0:
            raise ValueTooBig


def _charge_key(key: object, budget: _Budget) -> None:
    """Charge a dict key's minimum output size, before anything walks its parts."""
    match key:
        case str():
            budget.charge(str.__len__(key) + 2)
        case bytes():
            budget.charge(bytes.__len__(key) + 3)
        case tuple() | frozenset():
            budget.charge(1)
            for part in key:
                _charge_key(part, budget)
        case _:
            budget.charge(1)


def _charge_leaf(value: Any, budget: _Budget) -> None:
    """Charge a value that is not a dict, list or tuple its minimum output size."""
    match value:
        case str():
            budget.charge(str.__len__(value) + 2)
        case bytes():  # json falls back to str(): "b'...'"
            budget.charge(bytes.__len__(value) + 3)
        case bytearray():
            budget.charge(bytearray.__len__(value) + 3)
        case _:
            budget.charge(1)


def oversize_marker(max_size: int) -> str:
    """The text that stands in for a value known to exceed ``max_size``."""
    return f"{TRUNCATED_PREFIX}over {max_size} bytes > {max_size} max]"


def scrub_within(
    value: Any,
    max_size: int,
    sensitive_keys: frozenset[str],
    safe_keys: frozenset[str] | None = None,
) -> Any:
    """Scrub ``value`` like ``scrub_args``/``scrub_dict``, walking O(``max_size``) nodes.

    Raises:
        ValueTooBig: The value is certainly bigger than ``max_size`` once scrubbed.
    """
    patterns = _lowercase_patterns(sensitive_keys, None)
    return _scrub_value(value, patterns, safe_keys, _Budget(max_size))


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
    all_sensitive = _lowercase_patterns(sensitive_keys, additional_keys)
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
    all_sensitive = _lowercase_patterns(sensitive_keys, additional_keys)

    # A value that is certainly too big is never fully copied or serialized.
    try:
        scrubbed = _scrub_value(value, all_sensitive, safe_keys, _Budget(max_size))
    except ValueTooBig:
        return oversize_marker(max_size)

    # Try to serialize to check size and ensure it's JSON-compatible
    try:
        serialized = json.dumps(scrubbed, default=str)
        if len(serialized) > max_size:
            return f"{TRUNCATED_PREFIX}{len(serialized)} bytes > {max_size} max]"
        # Preserve the original scrubbed shape (not the JSON string).
        return scrubbed
    except (TypeError, ValueError) as e:
        logger.debug("Failed to serialize value: %s", e)
        # Fall back to string representation
        str_repr = str(scrubbed)
        if len(str_repr) > max_size:
            return f"{TRUNCATED_PREFIX}{len(str_repr)} bytes > {max_size} max]"
        return str_repr
