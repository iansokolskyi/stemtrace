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
