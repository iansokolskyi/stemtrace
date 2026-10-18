"""Unit tests for the API key gate's building blocks (server/fastapi/api_key.py)."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest

from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server.fastapi.api_key import (
    API_KEY_BUSY_RETRY_AFTER_S,
    API_KEY_ENV_VAR,
    API_KEY_MIN_LENGTH,
    ApiKeyGuard,
    KeyCheck,
    KeyStatus,
    bearer_from_subprotocols,
    bearer_token,
    configured_api_key,
    normalize_api_key,
)
from stemtrace.server.fastapi.login_throttle import (
    LOGIN_IP_MAX_FAILURES,
    LOGIN_LOCKOUT_BASE_S,
    LOGIN_USERNAME_MAX_FAILURES,
    LoginAttempt,
    LoginThrottle,
)

if TYPE_CHECKING:
    from collections.abc import Callable

KEY = "k" * 8 + "0123456789abcdef"  # 24 visible ASCII characters


class FakeClock:
    """Monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _throttle(clock: FakeClock) -> LoginThrottle:
    # Fixed hash key and enough buckets that the many-IP tests can't hit a
    # chance collision lock (a random key made them flaky).
    return LoginThrottle(clock=clock, buckets=4096, hash_key=b"stemtrace-test" * 2)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def guard(clock: FakeClock) -> ApiKeyGuard:
    return ApiKeyGuard(KEY, _throttle(clock))


# --- configuration ---------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_normalize_api_key_blank_is_none(value: str | None) -> None:
    assert normalize_api_key(value) is None


def test_normalize_api_key_strips_padding() -> None:
    assert normalize_api_key(f"  {KEY}\n") == KEY


def test_normalize_api_key_accepts_minimum_length() -> None:
    key = "x" * API_KEY_MIN_LENGTH

    assert normalize_api_key(key) == key


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("x" * (API_KEY_MIN_LENGTH - 1), "at least 16 characters"),
        ("short-secret-key", None),  # exactly 16: accepted, see below
        ("has a space inside it", "visible ASCII"),
        ("ключ-ключ-ключ-ключ", "visible ASCII"),
        ("tab\tinside-the-key-value", "visible ASCII"),
    ],
)
def test_normalize_api_key_rejects_weak_or_unsendable_keys(
    key: str, message: str | None
) -> None:
    if message is None:
        assert normalize_api_key(key) == key
        return
    with pytest.raises(ConfigurationError, match=message) as info:
        normalize_api_key(key)
    assert key not in str(info.value)


def test_configured_api_key_explicit_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, "e" * 20)

    assert configured_api_key(KEY) == KEY


@pytest.mark.parametrize("explicit", [None, "", "   "])
def test_configured_api_key_blank_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch, explicit: str | None
) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, f" {KEY} ")

    assert configured_api_key(explicit) == KEY


@pytest.mark.parametrize("env", [None, "", "  "])
def test_configured_api_key_without_any_is_none(
    monkeypatch: pytest.MonkeyPatch, env: str | None
) -> None:
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    if env is not None:
        monkeypatch.setenv(API_KEY_ENV_VAR, env)

    assert configured_api_key(None) is None


def test_configured_api_key_short_env_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, "short")

    with pytest.raises(ConfigurationError, match="at least 16"):
        configured_api_key(None)


# --- parsing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "token"),
    [
        (f"Bearer {KEY}", KEY),
        (f"bearer {KEY}", KEY),
        (f"BEARER   {KEY}  ", KEY),
        ("Bearer", ""),
        ("Bearer   ", ""),
        ("Basic dXNlcjpwYXNz", None),
        (f"Bearerx {KEY}", None),
        (f"Token {KEY}", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_token_parses_scheme_case_insensitively(
    header: str | None, token: str | None
) -> None:
    assert bearer_token(header) == token


def _encoded(key: str) -> str:
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


@pytest.mark.parametrize("key", [KEY, "a" * 16, "a" * 17, "a" * 18, "~!#$%&'*+-.^_|"])
def test_bearer_from_subprotocols_round_trips_base64url(key: str) -> None:
    offered = ["stemtrace", f"stemtrace.bearer.{_encoded(key)}"]

    assert bearer_from_subprotocols(offered) == key


@pytest.mark.parametrize(
    ("offered", "expected"),
    [
        ([], None),
        (["stemtrace"], None),
        (["graphql-ws"], None),
        (["stemtrace.bearer.%%%"], ""),
        (["stemtrace.bearer.wA"], ""),  # decodes to a non-ASCII byte
        (["stemtrace.bearer."], ""),
    ],
)
def test_bearer_from_subprotocols_without_valid_key(
    offered: list[str], expected: str | None
) -> None:
    assert bearer_from_subprotocols(offered) == expected


# --- ApiKeyGuard ------------------------------------------------------------


def test_guard_without_token_is_absent(guard: ApiKeyGuard) -> None:
    assert guard.check(None, "1.2.3.4") == KeyCheck(KeyStatus.ABSENT)


def test_guard_with_right_key_is_valid(guard: ApiKeyGuard) -> None:
    assert guard.check(KEY, "1.2.3.4") == KeyCheck(KeyStatus.VALID)


@pytest.mark.parametrize("token", ["", KEY[:-1], KEY + "x", KEY.upper(), "ключ"])
def test_guard_with_wrong_key_is_invalid(guard: ApiKeyGuard, token: str) -> None:
    assert guard.check(token, "1.2.3.4") == KeyCheck(KeyStatus.INVALID)


def test_guard_locks_ip_after_max_failures_even_for_right_key(
    guard: ApiKeyGuard, clock: FakeClock
) -> None:
    statuses = [
        guard.check("wrong", "1.2.3.4").status for _ in range(LOGIN_IP_MAX_FAILURES)
    ]

    assert statuses == [KeyStatus.INVALID] * (LOGIN_IP_MAX_FAILURES - 1) + [
        KeyStatus.THROTTLED
    ]
    # Checked before comparing: a locked-out client can't tell right from wrong.
    assert guard.check(KEY, "1.2.3.4") == KeyCheck(
        KeyStatus.THROTTLED, int(LOGIN_LOCKOUT_BASE_S)
    )
    # Other clients are unaffected.
    assert guard.check(KEY, "5.6.7.8") == KeyCheck(KeyStatus.VALID)

    clock.now += LOGIN_LOCKOUT_BASE_S
    assert guard.check(KEY, "1.2.3.4") == KeyCheck(KeyStatus.VALID)


def test_attempt_wrong_key_is_uncounted_until_rejected(guard: ApiKeyGuard) -> None:
    for _ in range(LOGIN_IP_MAX_FAILURES * 2):
        with guard.attempt("wrong", "1.2.3.4") as attempt:
            assert attempt.status is KeyStatus.INVALID

    assert guard.check(KEY, "1.2.3.4") == KeyCheck(KeyStatus.VALID)


def test_attempt_reject_counts_and_locks_on_the_last_allowed_failure(
    guard: ApiKeyGuard, clock: FakeClock
) -> None:
    waits = []
    for _ in range(LOGIN_IP_MAX_FAILURES):
        with guard.attempt("wrong", "1.2.3.4") as attempt:
            waits.append(attempt.reject())

    assert waits == [0] * (LOGIN_IP_MAX_FAILURES - 1) + [int(LOGIN_LOCKOUT_BASE_S)]
    # Checked before comparing: the right key is refused during the lockout.
    with guard.attempt(KEY, "1.2.3.4") as locked:
        assert locked.status is KeyStatus.THROTTLED
        assert locked.retry_after == int(LOGIN_LOCKOUT_BASE_S)

    clock.now += LOGIN_LOCKOUT_BASE_S
    # Once the lockout ends the key works again, without any wait for the failure memory.
    with guard.attempt(KEY, "1.2.3.4") as after:
        assert after.status is KeyStatus.VALID


def test_attempt_stops_comparing_keys_while_wrong_ones_are_in_flight(
    guard: ApiKeyGuard,
) -> None:
    """A burst can't test more keys than the failures it would be allowed."""
    held = [guard.attempt("wrong", "1.2.3.4") for _ in range(LOGIN_IP_MAX_FAILURES)]
    assert {a.status for a in held} == {KeyStatus.INVALID}

    # The right key is not compared either, or the burst could find it.
    with guard.attempt(KEY, "1.2.3.4") as capped:
        assert capped.status is KeyStatus.THROTTLED
        assert capped.retry_after == API_KEY_BUSY_RETRY_AFTER_S
    # Other clients are unaffected.
    with guard.attempt(KEY, "5.6.7.8") as other:
        assert other.status is KeyStatus.VALID

    for attempt in held:
        attempt.__exit__(None, None, None)
    with guard.attempt(KEY, "1.2.3.4") as freed:
        assert freed.status is KeyStatus.VALID


def test_attempt_in_flight_slots_count_with_failures_already_recorded(
    guard: ApiKeyGuard,
) -> None:
    for _ in range(LOGIN_IP_MAX_FAILURES - 2):
        guard.record_failure("1.2.3.4")

    first = guard.attempt("wrong", "1.2.3.4")
    second = guard.attempt("wrong", "1.2.3.4")
    third = guard.attempt("wrong", "1.2.3.4")

    assert [a.status for a in (first, second, third)] == [
        KeyStatus.INVALID,
        KeyStatus.INVALID,
        KeyStatus.THROTTLED,
    ]


def test_attempt_after_a_lockout_ends_compares_one_key_at_a_time(
    guard: ApiKeyGuard, clock: FakeClock
) -> None:
    for _ in range(LOGIN_IP_MAX_FAILURES):
        guard.record_failure("1.2.3.4")
    clock.now += LOGIN_LOCKOUT_BASE_S

    first = guard.attempt("wrong", "1.2.3.4")
    second = guard.attempt("wrong", "1.2.3.4")

    assert (first.status, second.status) == (KeyStatus.INVALID, KeyStatus.THROTTLED)
    assert second.retry_after == API_KEY_BUSY_RETRY_AFTER_S


@pytest.mark.parametrize(
    "address",
    [
        lambda i: f"2001:db8::{i + 1:x}",  # every address of one IPv6 /64
        lambda i: f"203.0.113.7:{40000 + i}",  # a proxy that appends the source port
    ],
    ids=["ipv6-64", "ip-with-port"],
)
def test_attempt_burst_from_one_ip_in_any_spelling_compares_only_the_allowed_keys(
    guard: ApiKeyGuard, address: Callable[[int], str]
) -> None:
    held = [guard.attempt("wrong", address(i)) for i in range(100)]

    statuses = [a.status for a in held]
    assert statuses.count(KeyStatus.INVALID) == LOGIN_IP_MAX_FAILURES
    assert statuses.count(KeyStatus.THROTTLED) == 100 - LOGIN_IP_MAX_FAILURES


def test_attempt_slot_is_freed_when_the_block_raises(guard: ApiKeyGuard) -> None:
    for _ in range(LOGIN_IP_MAX_FAILURES * 3):
        with pytest.raises(RuntimeError), guard.attempt("wrong", "1.2.3.4"):
            raise RuntimeError("request cancelled or broke")

    assert guard.attempt(KEY, "1.2.3.4").status is KeyStatus.VALID


def test_attempt_without_client_address_is_never_capped(guard: ApiKeyGuard) -> None:
    held = [guard.attempt("wrong", None) for _ in range(LOGIN_IP_MAX_FAILURES * 3)]

    assert {a.status for a in held} == {KeyStatus.INVALID}
    assert guard.attempt(KEY, None).status is KeyStatus.VALID
    assert held[0].reject() == 0


def test_record_failure_without_client_address_never_locks(
    guard: ApiKeyGuard,
) -> None:
    assert {guard.record_failure(None) for _ in range(50)} == {0}


def test_guard_right_key_does_not_clear_ip_failures(guard: ApiKeyGuard) -> None:
    for _ in range(LOGIN_IP_MAX_FAILURES - 1):
        guard.check("wrong", "1.2.3.4")
    assert guard.check(KEY, "1.2.3.4").status is KeyStatus.VALID

    assert guard.check("wrong", "1.2.3.4").status is KeyStatus.THROTTLED


def test_guard_without_client_address_is_never_throttled(guard: ApiKeyGuard) -> None:
    statuses = {guard.check("wrong", None).status for _ in range(50)}

    assert statuses == {KeyStatus.INVALID}
    assert guard.check(KEY, None).status is KeyStatus.VALID


def test_guard_failures_from_many_ips_never_lock_out_the_key(
    guard: ApiKeyGuard,
) -> None:
    """No shared counter: strangers can't lock the key out for everyone."""
    for i in range(LOGIN_USERNAME_MAX_FAILURES * 3):
        guard.check("wrong", f"10.0.{i // 250}.{i % 250}")

    assert guard.check(KEY, "192.168.1.1").status is KeyStatus.VALID
    assert guard.check("wrong", "192.168.1.1").status is KeyStatus.INVALID


def test_guard_shares_ip_counts_with_login_failures(clock: FakeClock) -> None:
    throttle = _throttle(clock)
    shared = ApiKeyGuard(KEY, throttle)
    login = LoginAttempt(client_ip="1.2.3.4", username="admin", device_id=None)
    for _ in range(LOGIN_IP_MAX_FAILURES):
        throttle.record_failure(login)

    assert shared.check(KEY, "1.2.3.4").status is KeyStatus.THROTTLED


def test_throttle_attempt_without_username_counts_only_the_ip(
    clock: FakeClock,
) -> None:
    throttle = _throttle(clock)
    for i in range(LOGIN_USERNAME_MAX_FAILURES):
        throttle.record_failure(
            LoginAttempt(client_ip=f"10.0.0.{i}", username=None, device_id=None)
        )
    throttle.record_success(LoginAttempt(client_ip=None, username=None, device_id=None))

    fresh = LoginAttempt(client_ip="10.9.9.9", username=None, device_id=None)
    assert throttle.retry_after(fresh) == 0
    # The login's username counter wasn't touched either.
    admin = LoginAttempt(client_ip="10.9.9.9", username="admin", device_id=None)
    assert throttle.retry_after(admin) == 0
