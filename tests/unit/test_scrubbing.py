"""Tests for sensitive data scrubbing."""

from enum import Enum, Flag, IntEnum, IntFlag

import pytest

from stemtrace.library.scrubbing import (
    DEFAULT_SENSITIVE_KEYS,
    FILTERED,
    _is_sensitive_key,
    safe_serialize,
    scrub_args,
    scrub_dict,
)


class TestIsSensitiveKey:
    """Tests for _is_sensitive_key function."""

    def test_exact_match(self) -> None:
        """Exact key name matches."""
        assert _is_sensitive_key("password", DEFAULT_SENSITIVE_KEYS) is True
        assert _is_sensitive_key("token", DEFAULT_SENSITIVE_KEYS) is True
        assert _is_sensitive_key("api_key", DEFAULT_SENSITIVE_KEYS) is True

    def test_case_insensitive(self) -> None:
        """Matching is case-insensitive."""
        assert _is_sensitive_key("PASSWORD", DEFAULT_SENSITIVE_KEYS) is True
        assert _is_sensitive_key("Password", DEFAULT_SENSITIVE_KEYS) is True
        assert _is_sensitive_key("API_KEY", DEFAULT_SENSITIVE_KEYS) is True

    def test_partial_match(self) -> None:
        """Keys containing sensitive patterns are matched."""
        assert _is_sensitive_key("user_password", DEFAULT_SENSITIVE_KEYS) is True
        assert _is_sensitive_key("db_password_hash", DEFAULT_SENSITIVE_KEYS) is True
        assert _is_sensitive_key("my_api_key", DEFAULT_SENSITIVE_KEYS) is True
        assert _is_sensitive_key("access_token_value", DEFAULT_SENSITIVE_KEYS) is True

    def test_non_sensitive_keys(self) -> None:
        """Non-sensitive keys are not matched."""
        assert _is_sensitive_key("username", DEFAULT_SENSITIVE_KEYS) is False
        assert _is_sensitive_key("email", DEFAULT_SENSITIVE_KEYS) is False
        assert _is_sensitive_key("data", DEFAULT_SENSITIVE_KEYS) is False

    def test_safe_keys_override(self) -> None:
        """Safe keys override sensitive matching."""
        safe = frozenset({"password"})
        assert _is_sensitive_key("password", DEFAULT_SENSITIVE_KEYS, safe) is False
        # But other sensitive keys still match
        assert _is_sensitive_key("token", DEFAULT_SENSITIVE_KEYS, safe) is True


class TestScrubDict:
    """Tests for scrub_dict function."""

    def test_scrubs_sensitive_keys(self) -> None:
        """Sensitive key values are replaced with FILTERED."""
        data = {
            "username": "alice",
            "password": "secret123",
            "email": "alice@example.com",
        }
        result = scrub_dict(data)

        assert result["username"] == "alice"
        assert result["password"] == FILTERED
        assert result["email"] == "alice@example.com"

    def test_scrubs_nested_dicts(self) -> None:
        """Nested dictionaries are scrubbed recursively."""
        data = {
            "user": {
                "name": "Bob",
                "login": {
                    "password": "hunter2",
                    "api_key": "sk-123",
                },
            },
        }
        result = scrub_dict(data)

        assert result["user"]["name"] == "Bob"
        assert result["user"]["login"]["password"] == FILTERED
        assert result["user"]["login"]["api_key"] == FILTERED

    def test_scrubs_lists_of_dicts(self) -> None:
        """Lists containing dicts are scrubbed recursively."""
        data = {
            "users": [
                {"name": "Alice", "token": "abc"},
                {"name": "Bob", "token": "def"},
            ],
        }
        result = scrub_dict(data)

        assert result["users"][0]["name"] == "Alice"
        assert result["users"][0]["token"] == FILTERED
        assert result["users"][1]["token"] == FILTERED

    def test_additional_keys(self) -> None:
        """Additional sensitive keys can be provided."""
        data = {"internal_id": "12345", "custom_secret": "value"}
        additional = frozenset({"custom_secret"})
        result = scrub_dict(data, additional_keys=additional)

        assert result["internal_id"] == "12345"
        assert result["custom_secret"] == FILTERED

    def test_safe_keys(self) -> None:
        """Safe keys are not scrubbed."""
        data = {"password": "secret", "session": "important"}
        safe = frozenset({"session"})
        result = scrub_dict(data, safe_keys=safe)

        assert result["password"] == FILTERED
        assert result["session"] == "important"

    def test_empty_dict(self) -> None:
        """Empty dict returns empty dict."""
        assert scrub_dict({}) == {}

    def test_preserves_non_sensitive_values(self) -> None:
        """Non-sensitive values of various types are preserved."""
        data = {
            "count": 42,
            "ratio": 3.14,
            "active": True,
            "items": [1, 2, 3],
            "meta": None,
        }
        result = scrub_dict(data)
        assert result == data


class TestScrubArgs:
    """Tests for scrub_args function."""

    def test_scrubs_dict_args(self) -> None:
        """Dict arguments are scrubbed."""
        args = ({"password": "secret", "name": "test"},)
        result = scrub_args(args)

        assert result[0]["name"] == "test"
        assert result[0]["password"] == FILTERED

    def test_preserves_simple_args(self) -> None:
        """Simple argument values are preserved."""
        args = (1, "hello", 3.14, True, None)
        result = scrub_args(args)
        assert result == [1, "hello", 3.14, True, None]

    def test_scrubs_nested_structures(self) -> None:
        """Nested structures in args are scrubbed."""
        args = ([{"token": "abc"}], {"users": [{"password": "x"}]})
        result = scrub_args(args)

        assert result[0][0]["token"] == FILTERED
        assert result[1]["users"][0]["password"] == FILTERED

    def test_empty_args(self) -> None:
        """Empty args returns empty list."""
        assert scrub_args(()) == []

    def test_scrubs_tuple_values(self) -> None:
        """Tuples inside args are scrubbed recursively and preserved as tuples."""
        args = (("a", {"password": "secret"}),)
        result = scrub_args(args)

        assert isinstance(result[0], tuple)
        assert result[0][0] == "a"
        assert result[0][1]["password"] == FILTERED


class TestSafeSerialize:
    """Tests for safe_serialize function."""

    def test_serializes_dict(self) -> None:
        """Dicts are serialized and scrubbed."""
        value = {"name": "test", "password": "secret"}
        result = safe_serialize(value)

        assert result["name"] == "test"
        assert result["password"] == FILTERED

    def test_serializes_list(self) -> None:
        """Lists are serialized."""
        value = [1, 2, 3]
        result = safe_serialize(value)
        assert result == [1, 2, 3]

    def test_truncates_large_values(self) -> None:
        """Large values are truncated."""
        # Create a value that serializes to > 100 bytes
        value = {"data": "x" * 200}
        result = safe_serialize(value, max_size=100)

        assert isinstance(result, str)
        assert "Truncated" in result

    def test_handles_non_json_types(self) -> None:
        """Non-JSON-serializable types are passed through (json.dumps with default=str checks size)."""

        # Custom class - json.dumps uses str() as fallback for size check
        class Custom:
            def __str__(self) -> str:
                return "CustomObject"

        obj = Custom()
        result = safe_serialize(obj)
        # The original value is returned (scrubbing doesn't transform non-dict/list)
        assert result is obj

    def test_respects_max_size(self) -> None:
        """Values under max_size are not truncated."""
        value = {"small": "data"}
        result = safe_serialize(value, max_size=10240)
        assert result == {"small": "data"}

    def test_falls_back_to_string_when_default_str_raises(self) -> None:
        """If json.dumps triggers ValueError, safe_serialize should fall back to str()."""

        class BadStr:
            def __str__(self) -> str:
                raise ValueError("boom")

            def __repr__(self) -> str:
                return "<BadStr>"

        result = safe_serialize([BadStr()])
        assert isinstance(result, str)

    def test_truncates_string_fallback_when_too_large(self) -> None:
        """If string fallback exceeds max_size, it should be truncated."""

        class BadStr:
            def __str__(self) -> str:
                raise ValueError("boom")

            def __repr__(self) -> str:
                return "<BadStr>"

        result = safe_serialize([BadStr()], max_size=5)
        assert isinstance(result, str)
        assert "Truncated" in result


class TestDefaultSensitiveKeys:
    """Tests for DEFAULT_SENSITIVE_KEYS coverage."""

    @pytest.mark.parametrize(
        "key",
        [
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
            "credit_card",
            "card_number",
            "cvv",
            "ssn",
            "social_security",
            "cookie",
            "session",
            "sessionid",
            "csrf",
            "csrf_token",
        ],
    )
    def test_default_keys_are_scrubbed(self, key: str) -> None:
        """All default sensitive keys are scrubbed."""
        data = {key: "sensitive_value"}
        result = scrub_dict(data)
        assert result[key] == FILTERED


# Dict keys that aren't strings: Celery's after_task_publish gets the body before
# it is serialized, the pickle serializer keeps them on the worker, and a task
# result is never serialized before task_postrun (redis-py returns bytes-keyed
# dicts without decode_responses). A key is matched by the names it carries:
# bytes decoded as UTF-8, and the str/bytes parts of a tuple or frozenset key.


class _StrIsPassword:
    """A key that is no name, though its str() looks like one."""

    def __str__(self) -> str:
        return "password"

    def __repr__(self) -> str:
        return "<_StrIsPassword>"


class _Cred(Enum):
    """Enum members are names: matched by their name and their value."""

    PASSWORD = "pw"
    LOOKUP = "api_key"
    COLOR = 1


class _StrCred(str, Enum):
    """A str-mixin Enum is a str, but its name is matched too."""

    SECRET = "s"


class _Slot(IntEnum):
    """An IntEnum is an int, but its members still have names."""

    TOKEN = 1
    OTHER = 2


class _Perm(Flag):
    """Flag pseudo-members (empty, and on 3.10 composite) have no name."""

    READ = 1
    WRITE = 2
    SECRET = 4


class _IPerm(IntFlag):
    """An IntFlag value with bits outside its members has no name."""

    READ = 1
    WRITE = 2
    TOKEN = 8


class _Combo(Flag):
    """A multi-bit named member: Python 3.11+ leaves it out of iteration."""

    A = 1
    B = 2
    TOKEN_PAIR = 3
    C = 4


class _Alias(Flag):
    """An alias is another name for a member's bits."""

    READ = 1
    TOKEN = 1


class _Nameless(Enum):
    """An Enum may override name; a None name carries nothing."""

    A = "x"

    @property
    def name(self) -> None:  # type: ignore[override]  # the case under test
        return None


class _Mode(Flag):
    """A zero-valued member is in no Flag value, whatever its name."""

    NO_TOKEN = 0
    READ = 1


UNNAMED_KEYS = [
    1,
    None,
    1.5,
    (1, 2),
    b"name",
    ("user", 1),
    _StrIsPassword(),
    _Cred.COLOR,
    _Slot.OTHER,
    _Perm(0),
    _Perm.READ | _Perm.WRITE,
    _IPerm(0),
    _IPerm(4),
    _Mode.READ,
    _Combo.A | _Combo.C,
    _Nameless.A,
]
UNNAMED_KEY_IDS = [
    "int",
    "none",
    "float",
    "tuple",
    "bytes",
    "tuple_mixed",
    "object",
    "enum",
    "int_enum",
    "flag_empty",
    "flag_composite",
    "int_flag_empty",
    "int_flag_unknown_bits",
    "flag_with_zero_member",
    "flag_without_multi_bit_member",
    "enum_name_none",
]
NAMED_KEYS = [
    b"password",
    b"\xffpassword",
    ("api_key",),
    ("user", b"token"),
    (("nested", "cookie"),),
    frozenset({"secret"}),
    _Cred.PASSWORD,
    _Cred.LOOKUP,
    _Slot.TOKEN,
    _StrCred.SECRET,
    _Perm.SECRET,
    # Composites have no name on Python 3.10: matched by their members.
    _Perm.READ | _Perm.SECRET,
    _IPerm.READ | _IPerm.TOKEN,
    _IPerm(4) | _IPerm.TOKEN,
    _Combo.TOKEN_PAIR | _Combo.C,
    _Alias.READ,
]
NAMED_KEY_IDS = [
    "bytes",
    "bytes_not_utf8",
    "tuple",
    "tuple_bytes",
    "tuple_nested",
    "frozenset",
    "enum_name",
    "enum_value",
    "int_enum",
    "str_enum",
    "flag_member",
    "flag_composite",
    "int_flag_composite",
    "int_flag_unknown_bits",
    "flag_multi_bit_member",
    "flag_alias",
]
ALL_KEYS = [(key, False) for key in UNNAMED_KEYS] + [(key, True) for key in NAMED_KEYS]
ALL_KEY_IDS = [f"unnamed_{i}" for i in UNNAMED_KEY_IDS] + [
    f"named_{i}" for i in NAMED_KEY_IDS
]


def _with_key(key: object, secret: object) -> dict[object, object]:
    """A dict with a non-string key next to a sensitive one."""
    return {key: {"password": secret, "v": 1}, "password": secret}


def _scrubbed(key: object, named: bool) -> dict[object, object]:
    """``_with_key`` after scrubbing: a named key's whole value is filtered."""
    value = FILTERED if named else {"password": FILTERED, "v": 1}
    return {key: value, "password": FILTERED}


def _wrapped(where: str, value: object) -> object:
    """Put a value where a task's data can hold a dict."""
    match where:
        case "top":
            return value
        case "in_list":
            return [value]
        case "in_tuple":
            return (value,)
        case _:
            return {"outer": value}


WRAPPERS = ["top", "in_list", "in_tuple", "in_dict"]


class TestNonStringKeys:
    """Dicts with non-string keys are scrubbed by the names the keys carry."""

    @pytest.mark.parametrize("safe_keys", [None, frozenset({"public_key"})])
    @pytest.mark.parametrize(("key", "named"), ALL_KEYS, ids=ALL_KEY_IDS)
    def test_is_sensitive_key_with_non_string_key_matches_names_only(
        self, key: object, named: bool, safe_keys: frozenset[str] | None
    ) -> None:
        assert _is_sensitive_key(key, DEFAULT_SENSITIVE_KEYS, safe_keys) is named

    @pytest.mark.parametrize(
        ("key", "sensitive"),
        [
            (b"session_id", False),
            (("session_id",), False),
            (("session_id", "token"), True),
        ],
        ids=["bytes", "tuple", "tuple_with_unsafe_part"],
    )
    def test_is_sensitive_key_with_safe_name_in_non_string_key(
        self, key: object, sensitive: bool
    ) -> None:
        safe = frozenset({"SESSION_ID"})

        assert _is_sensitive_key(key, DEFAULT_SENSITIVE_KEYS, safe) is sensitive

    @pytest.mark.parametrize("where", WRAPPERS)
    @pytest.mark.parametrize(("key", "named"), ALL_KEYS, ids=ALL_KEY_IDS)
    def test_scrub_args_with_non_string_key_scrubs_by_name(
        self, key: object, named: bool, where: str
    ) -> None:
        args = (_wrapped(where, _with_key(key, "x")),)

        assert scrub_args(args) == [_wrapped(where, _scrubbed(key, named))]

    @pytest.mark.parametrize("where", WRAPPERS)
    @pytest.mark.parametrize(("key", "named"), ALL_KEYS, ids=ALL_KEY_IDS)
    def test_scrub_dict_with_non_string_key_scrubs_by_name(
        self, key: object, named: bool, where: str
    ) -> None:
        kwargs = {"data": _wrapped(where, _with_key(key, "x"))}

        assert scrub_dict(kwargs, safe_keys=frozenset({"public_key"})) == {
            "data": _wrapped(where, _scrubbed(key, named))
        }

    @pytest.mark.parametrize(("key", "named"), ALL_KEYS, ids=ALL_KEY_IDS)
    def test_scrub_dict_with_top_level_non_string_key_scrubs_by_name(
        self, key: object, named: bool
    ) -> None:
        assert scrub_dict(_with_key(key, "x")) == _scrubbed(key, named)

    @pytest.mark.parametrize("where", WRAPPERS)
    @pytest.mark.parametrize(("key", "named"), ALL_KEYS, ids=ALL_KEY_IDS)
    def test_safe_serialize_with_non_string_key_scrubs_result(
        self, key: object, named: bool, where: str
    ) -> None:
        expected = _wrapped(where, _scrubbed(key, named))

        result = safe_serialize(_wrapped(where, _with_key(key, "x")))

        # json.dumps takes str, int, float and None keys (subclasses too, such
        # as mixin Enums); other keys make it raise TypeError, and
        # safe_serialize falls back to str() of the scrubbed value.
        json_key = key is None or isinstance(key, str | int | float)
        assert result == (expected if json_key else str(expected))

    def test_safe_serialize_with_scrubbing_off_and_non_string_key_keeps_values(
        self,
    ) -> None:
        # The signal handlers pass an empty pattern set when scrubbing is off.
        value = [{1: "a", b"password": "x", "password": "x"}]

        assert safe_serialize(value, sensitive_keys=frozenset()) == str(value)

    def test_safe_serialize_with_scrubbing_off_and_int_key_keeps_shape(
        self,
    ) -> None:
        value = [{1: "a", "password": "x"}]

        assert safe_serialize(value, sensitive_keys=frozenset()) == value
