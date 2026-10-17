"""Tests for sensitive data scrubbing."""

import pytest

from stemtrace.library import scrubbing
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


PATTERN_CASES = ["mrn", "MRN", "Mrn"]
KEYS_FOR_MRN = ["mrn", "MRN", "patient_Mrn"]


class TestPatternCase:
    """Patterns match whatever case the user wrote them in."""

    @pytest.mark.parametrize("key", KEYS_FOR_MRN)
    @pytest.mark.parametrize("pattern", PATTERN_CASES)
    def test_scrub_dict_with_sensitive_key_in_any_case_filters(
        self, pattern: str, key: str
    ) -> None:
        result = scrub_dict({key: 1, "name": "alice"}, frozenset({pattern}))

        assert result == {key: FILTERED, "name": "alice"}

    @pytest.mark.parametrize("pattern", PATTERN_CASES)
    def test_scrub_dict_with_additional_key_in_any_case_filters(
        self, pattern: str
    ) -> None:
        data = dict.fromkeys(KEYS_FOR_MRN, 1) | {"name": "alice"}

        result = scrub_dict(data, additional_keys=frozenset({pattern}))

        assert result == dict.fromkeys(KEYS_FOR_MRN, FILTERED) | {"name": "alice"}

    @pytest.mark.parametrize("pattern", PATTERN_CASES)
    def test_scrub_args_and_safe_serialize_with_sensitive_key_in_any_case_filter(
        self, pattern: str
    ) -> None:
        value = [{"MRN": 1, "nested": {"mrn": 2}}]
        expected = [{"MRN": FILTERED, "nested": {"mrn": FILTERED}}]

        assert scrub_args(tuple(value), frozenset({pattern})) == expected
        assert safe_serialize(value, sensitive_keys=frozenset({pattern})) == expected

    def test_safe_key_in_any_case_still_overrides_pattern_in_any_case(self) -> None:
        data = {"MRN_PUBLIC": 1, "mrn": 2}

        result = scrub_dict(
            data,
            additional_keys=frozenset({"MRN"}),
            safe_keys=frozenset({"Mrn_Public"}),
        )

        assert result == {"MRN_PUBLIC": 1, "mrn": FILTERED}


class TestPatternsLoweredOncePerCall:
    """Each entry point lowercases its patterns once, however deep the data."""

    @pytest.fixture
    def lowercase_calls(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        calls: list[int] = []
        real = scrubbing._lowercase_patterns

        def counting(
            sensitive_keys: frozenset[str], additional_keys: frozenset[str] | None
        ) -> frozenset[str]:
            calls.append(1)
            return real(sensitive_keys, additional_keys)

        monkeypatch.setattr(scrubbing, "_lowercase_patterns", counting)
        return calls

    def test_scrub_dict_with_nested_dicts_lowercases_once(
        self, lowercase_calls: list[int]
    ) -> None:
        scrub_dict({"a": {"b": {"c": 1}}, "d": [{"e": 1}, {"f": 2}]})

        assert len(lowercase_calls) == 1

    def test_scrub_args_with_many_dicts_lowercases_once(
        self, lowercase_calls: list[int]
    ) -> None:
        scrub_args(([{"a": i} for i in range(50)],))

        assert len(lowercase_calls) == 1

    def test_safe_serialize_with_many_dicts_lowercases_once(
        self, lowercase_calls: list[int]
    ) -> None:
        safe_serialize([{"a": {"b": i}} for i in range(50)])

        assert len(lowercase_calls) == 1
