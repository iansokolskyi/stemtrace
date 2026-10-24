"""Argument capture costs O(max_data_size), not O(size of the arguments)."""

import json
import random
from typing import Any

import pytest

from stemtrace.library import scrubbing
from stemtrace.library.scrubbing import (
    DEFAULT_SENSITIVE_KEYS,
    FILTERED,
    TRUNCATED_PREFIX,
    _lowercase_patterns,
    _scrub_value,
    safe_serialize,
)
from stemtrace.library.signals import (
    _scrub_and_serialize_args,
    _scrub_and_serialize_kwargs,
)

MAX_SIZE = 10240
# Generous: the walk charges at least 1 per node, so MAX_SIZE nodes plus a few.
VISIT_BOUND = MAX_SIZE + 16


class CountingList(list[Any]):
    """A list that counts the items handed out by iteration."""

    visits = 0

    def __iter__(self) -> Any:
        for item in super().__iter__():
            type(self).visits += 1
            yield item


class CountingDict(dict[Any, Any]):
    """A dict that counts the entries handed out by items()."""

    visits = 0

    def items(self) -> Any:  # type: ignore[override]  # counting wrapper
        for entry in super().items():
            type(self).visits += 1
            yield entry


@pytest.fixture(autouse=True)
def _reset_counters() -> None:
    CountingList.visits = 0
    CountingDict.visits = 0


@pytest.fixture
def no_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail if anything serializes: an oversize value must never get that far."""

    def fail(*args: object, **kwargs: object) -> str:
        raise AssertionError("json.dumps called on an oversize value")

    monkeypatch.setattr(scrubbing.json, "dumps", fail)


def _marker(max_size: int = MAX_SIZE) -> str:
    return f"[Truncated: over {max_size} bytes > {max_size} max]"


class TestCostIsBounded:
    def test_safe_serialize_with_huge_flat_list_visits_few_nodes(
        self, no_json: None
    ) -> None:
        value = CountingList(["x"] * 500_000)

        assert safe_serialize(value, MAX_SIZE) == _marker()
        assert CountingList.visits <= VISIT_BOUND

    def test_safe_serialize_with_many_keys_visits_few_entries(
        self, no_json: None
    ) -> None:
        value = CountingDict({f"key{i}": i for i in range(500_000)})

        assert safe_serialize(value, MAX_SIZE) == _marker()
        assert CountingDict.visits <= VISIT_BOUND

    def test_safe_serialize_with_wide_nesting_visits_few_nodes(
        self, no_json: None
    ) -> None:
        value = CountingList(CountingList(["x"] * 400) for _ in range(400))

        assert safe_serialize(value, MAX_SIZE) == _marker()
        assert CountingList.visits <= VISIT_BOUND

    def test_safe_serialize_with_deep_nesting_and_wide_leaf_visits_few_nodes(
        self, no_json: None
    ) -> None:
        leaf: Any = CountingList(["x"] * 100_000)
        for _ in range(200):
            leaf = CountingList([leaf, "pad"])

        assert safe_serialize(leaf, MAX_SIZE) == _marker()
        assert CountingList.visits <= VISIT_BOUND

    @pytest.mark.parametrize(
        "value",
        ["z" * 5_000_000, b"z" * 5_000_000, ["z" * 5_000_000], {"k": b"z" * 5_000_000}],
        ids=["str", "bytes", "str_in_list", "bytes_in_dict"],
    )
    def test_safe_serialize_with_one_huge_leaf_never_serializes(
        self, no_json: None, value: Any
    ) -> None:
        assert safe_serialize(value, MAX_SIZE) == _marker()

    def test_safe_serialize_with_huge_dict_key_never_serializes(
        self, no_json: None
    ) -> None:
        assert safe_serialize({"k" * 5_000_000: 1}, MAX_SIZE) == _marker()

    def test_capture_args_with_huge_list_skips_scrubbing(self, no_json: None) -> None:
        args = (CountingList(["x"] * 500_000),)

        assert _scrub_and_serialize_args(args) == [_marker()]
        assert CountingList.visits <= VISIT_BOUND

    def test_capture_kwargs_with_huge_dict_skips_scrubbing(self, no_json: None) -> None:
        kwargs = {"payload": CountingDict({f"k{i}": i for i in range(500_000)})}

        assert _scrub_and_serialize_kwargs(kwargs) == {"_truncated": _marker()}
        assert CountingDict.visits <= VISIT_BOUND


class _CountingKey(tuple[Any, ...]):
    """A tuple dict key that counts the parts handed out by iteration."""

    visits = 0

    def __iter__(self) -> Any:
        for part in super().__iter__():
            type(self).visits += 1
            yield part


class _LyingBytes(bytes):
    def __len__(self) -> int:
        return 10**9


class TestBoundCoversEveryShape:
    def test_safe_serialize_with_huge_tuple_key_visits_few_parts(
        self, no_json: None
    ) -> None:
        _CountingKey.visits = 0
        value = {_CountingKey((tuple(range(300_000)),)): 1}

        assert safe_serialize(value, MAX_SIZE) == _marker()
        assert _CountingKey.visits <= 1

    def test_safe_serialize_with_wide_tuple_key_visits_few_parts(
        self, no_json: None
    ) -> None:
        _CountingKey.visits = 0
        value = {_CountingKey(range(300_000)): 1}

        assert safe_serialize(value, MAX_SIZE) == _marker()
        assert _CountingKey.visits <= VISIT_BOUND

    def test_safe_serialize_with_bytes_that_lie_about_length_keeps_them(self) -> None:
        assert safe_serialize([_LyingBytes(b"ab")], MAX_SIZE) == [b"ab"]
        # A bytes key is not JSON, so the text of the dict comes back.
        assert safe_serialize({_LyingBytes(b"k"): 1}, MAX_SIZE) == "{b'k': 1}"


class TestUnderLimitCostMatchesUnbounded:
    @pytest.mark.parametrize("rows", [1, 10, 100])
    def test_capture_args_checks_each_key_no_more_often_than_before(
        self, monkeypatch: pytest.MonkeyPatch, rows: int
    ) -> None:
        # Before the bound, capture scrubbed twice (scrub_args, then
        # safe_serialize): two key checks per dict key.
        calls: list[object] = []
        real = scrubbing._is_sensitive_key

        def counting(key: object, *args: Any, **kwargs: Any) -> bool:
            calls.append(key)
            return real(key, *args, **kwargs)

        monkeypatch.setattr(scrubbing, "_is_sensitive_key", counting)
        args = ([{"a": i, "b": {"c": i}} for i in range(rows)],)

        assert _scrub_and_serialize_args(args) == [args[0]]
        assert len(calls) <= 2 * (3 * rows)

    def test_capture_kwargs_checks_each_key_no_more_often_than_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        real = scrubbing._is_sensitive_key

        def counting(key: object, *args: Any, **kwargs: Any) -> bool:
            calls.append(key)
            return real(key, *args, **kwargs)

        monkeypatch.setattr(scrubbing, "_is_sensitive_key", counting)
        kwargs = {"rows": [{"a": i, "b": {"c": i}} for i in range(20)]}

        assert _scrub_and_serialize_kwargs(kwargs) == kwargs
        assert len(calls) <= 2 * (1 + 3 * 20)


class TestScrubbingStillHolds:
    def test_safe_serialize_with_huge_value_under_sensitive_key_keeps_the_rest(
        self,
    ) -> None:
        value = {"password": "x" * 5_000_000, "name": "alice"}

        assert safe_serialize(value, MAX_SIZE) == {
            "password": FILTERED,
            "name": "alice",
        }

    def test_capture_args_with_sensitive_key_in_kept_value_scrubs_it(self) -> None:
        args = ({"user": {"api_key": "k", "n": 1}}, [{"token": "t"}])

        assert _scrub_and_serialize_args(args) == [
            {"user": {"api_key": FILTERED, "n": 1}},
            [{"token": FILTERED}],
        ]

    def test_capture_kwargs_with_huge_list_has_no_unscrubbed_text(self) -> None:
        kwargs = {"rows": [{"password": "hunter2"}] * 50_000}

        assert "hunter2" not in json.dumps(_scrub_and_serialize_kwargs(kwargs))


def _reference(value: Any, max_size: int) -> Any:
    """safe_serialize as it was before the bound: scrub everything, then measure."""
    patterns = _lowercase_patterns(DEFAULT_SENSITIVE_KEYS, None)
    scrubbed = _scrub_value(value, patterns, None)
    try:
        serialized = json.dumps(scrubbed, default=str)
        if len(serialized) > max_size:
            return f"{TRUNCATED_PREFIX}{len(serialized)} bytes > {max_size} max]"
        return scrubbed
    except (TypeError, ValueError):
        text = str(scrubbed)
        if len(text) > max_size:
            return f"{TRUNCATED_PREFIX}{len(text)} bytes > {max_size} max]"
        return text


class _Obj:
    def __str__(self) -> str:
        return "obj"


_KEYS: list[Any] = [
    "a",
    "name",
    "Password",
    "user_token",
    "x" * 40,
    "é",
    1,
    2.5,
    True,
    None,
    b"k",
    b"api_key",
    ("a", "b"),
    ("auth", 1),
    frozenset({"cookie"}),
]
_LEAVES: list[Any] = [
    None,
    True,
    0,
    -12,
    3.14,
    "",
    "text",
    "ünïcode ☃" * 3,
    "q" * 100,
    b"bytes",
    bytearray(b"ba"),
    _Obj(),
    {"s"},
]


def _payload(rng: random.Random, depth: int = 0) -> Any:
    kind = rng.random()
    if depth > 4 or kind < 0.4:
        return rng.choice(_LEAVES)
    size = rng.randint(0, 6)
    if kind < 0.65:
        return [_payload(rng, depth + 1) for _ in range(size)]
    if kind < 0.75:
        return tuple(_payload(rng, depth + 1) for _ in range(size))
    return {rng.choice(_KEYS): _payload(rng, depth + 1) for _ in range(size)}


class TestDifferentialAgainstUnboundedSerialize:
    @pytest.mark.parametrize("max_size", [10, 60, 300, 1500, MAX_SIZE])
    def test_safe_serialize_matches_unbounded_reference(self, max_size: int) -> None:
        rng = random.Random(20261024 + max_size)
        kept = 0
        cut = 0
        for _ in range(1500):
            value = _payload(rng)
            expected = _reference(value, max_size)
            actual = safe_serialize(value, max_size)
            if (
                isinstance(expected, str)
                and expected.startswith(TRUNCATED_PREFIX)
                and actual == _marker(max_size)
            ):
                cut += 1
                continue
            assert actual == expected, value
            kept += 1
        # Both outcomes are exercised, or the comparison proves little.
        assert kept > 100
        assert cut > 0 or max_size >= 1500


def _reference_args(args: tuple[Any, ...], max_size: int) -> list[Any]:
    """_capture_args as it was before the bound (two scrubs, then the size check)."""
    scrubbed = scrubbing.scrub_args(args)
    result = _reference(scrubbed, max_size)
    return result if isinstance(result, list) else [result]


class TestCaptureDifferential:
    @pytest.mark.parametrize("max_size", [10, 60, 300, 1500])
    def test_capture_args_matches_unbounded_reference(
        self, monkeypatch: pytest.MonkeyPatch, max_size: int
    ) -> None:
        monkeypatch.setattr(
            "stemtrace.library.signals._get_scrub_config",
            lambda: (DEFAULT_SENSITIVE_KEYS, None, max_size, True, True, True),
        )
        rng = random.Random(7 + max_size)
        for _ in range(1000):
            args = tuple(_payload(rng) for _ in range(rng.randint(0, 3)))
            expected = _reference_args(args, max_size)
            actual = _scrub_and_serialize_args(args)
            if expected == actual:
                continue
            # The only allowed difference: the marker of a value known to be big.
            assert actual == [_marker(max_size)], args
            assert str(expected[0]).startswith(TRUNCATED_PREFIX), args
