"""Tests for throttling failed sign-ins on the built-in login page."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import stemtrace
from stemtrace.server.fastapi import login_throttle
from stemtrace.server.fastapi.form_auth import FormAuthConfig, sign_session
from stemtrace.server.fastapi.login_routes import create_login_router
from stemtrace.server.fastapi.login_throttle import (
    DEVICE_COOKIE_TTL_SECONDS,
    LOGIN_DEVICE_MAX_FAILURES,
    LOGIN_FAILURE_MEMORY_S,
    LOGIN_IP_MAX_FAILURES,
    LOGIN_LOCKOUT_BASE_S,
    LOGIN_LOCKOUT_MAX_S,
    LOGIN_USERNAME_MAX_FAILURES,
    LoginAttempt,
    LoginThrottle,
    create_device_cookie_value,
    device_id_from_cookie,
)

if TYPE_CHECKING:
    import httpx
    from starlette.types import ASGIApp, Receive, Scope, Send

_IP = "203.0.113.7"
_USER = "admin"
_PASSWORD = "secret"  # NOSONAR - test credential only
_SECRET = "test-secret"
_SESSION = "stemtrace_session"
_DEVICE = "stemtrace_session_device"


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _attempt(
    ip: str | None = _IP, user: str = _USER, device: str | None = None
) -> LoginAttempt:
    return LoginAttempt(client_ip=ip, username=user, device_id=device)


def _fail(throttle: LoginThrottle, times: int, attempt: LoginAttempt) -> None:
    for _ in range(times):
        throttle.record_failure(attempt)


class TestLoginThrottle:
    def test_retry_after_below_limit_is_zero(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        _fail(throttle, LOGIN_IP_MAX_FAILURES - 1, _attempt())

        assert throttle.retry_after(_attempt()) == 0

    def test_retry_after_at_limit_locks_for_base_lockout(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt())

        assert throttle.retry_after(_attempt()) == LOGIN_LOCKOUT_BASE_S
        clock.now += 10.5
        assert throttle.retry_after(_attempt()) == LOGIN_LOCKOUT_BASE_S - 10
        assert throttle.retry_after(_attempt("198.51.100.1", "other")) == 0

    def test_lockout_doubles_per_failure_and_is_capped(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt())
        lockouts = []
        for _ in range(7):
            lockouts.append(throttle.retry_after(_attempt()))
            clock.now += lockouts[-1]
            throttle.record_failure(_attempt())

        assert lockouts == [30, 60, 120, 240, 480, 900, 900]
        assert lockouts[-1] == LOGIN_LOCKOUT_MAX_S

    def test_thousands_of_failures_stay_at_cap_without_overflow(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        for _ in range(2000):
            throttle.record_failure(_attempt(user="x"))
            clock.now += LOGIN_LOCKOUT_MAX_S

        throttle.record_failure(_attempt(user="x"))
        assert throttle.retry_after(_attempt(user="x")) == LOGIN_LOCKOUT_MAX_S

    def test_lock_expires_then_next_failure_locks_again(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt())
        clock.now += LOGIN_LOCKOUT_BASE_S

        assert throttle.retry_after(_attempt()) == 0
        throttle.record_failure(_attempt())
        assert throttle.retry_after(_attempt()) == 2 * LOGIN_LOCKOUT_BASE_S

    def test_failures_are_forgotten_after_memory_window(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt())
        clock.now += LOGIN_FAILURE_MEMORY_S

        assert throttle.retry_after(_attempt()) == 0
        assert throttle.tracked_keys == 0
        _fail(throttle, LOGIN_IP_MAX_FAILURES - 1, _attempt())
        assert throttle.retry_after(_attempt()) == 0

    def test_failure_after_memory_window_starts_a_new_count(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES - 1, _attempt())
        clock.now += LOGIN_FAILURE_MEMORY_S
        throttle.record_failure(_attempt())  # no retry_after in between

        assert throttle.retry_after(_attempt()) == 0

    def test_username_locked_from_many_ips_without_device(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        for i in range(LOGIN_USERNAME_MAX_FAILURES):
            throttle.record_failure(_attempt(f"10.0.0.{i}"))

        assert throttle.retry_after(_attempt("10.0.1.1")) == LOGIN_LOCKOUT_BASE_S
        assert throttle.retry_after(_attempt("10.0.1.1", "someone-else")) == 0

    def test_known_device_bypasses_username_lock(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        for i in range(LOGIN_USERNAME_MAX_FAILURES):
            throttle.record_failure(_attempt(f"10.0.0.{i}"))

        assert throttle.retry_after(_attempt("10.0.1.1", device="dev-1")) == 0

    def test_known_device_has_its_own_limit(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        for i in range(LOGIN_DEVICE_MAX_FAILURES):
            throttle.record_failure(_attempt(f"10.0.0.{i}", device="dev-1"))

        assert throttle.retry_after(_attempt("10.0.1.1", device="dev-1")) == 30
        assert throttle.retry_after(_attempt("10.0.1.1", device="dev-2")) == 0
        # Device failures don't count against the username.
        assert throttle.retry_after(_attempt("10.0.1.1")) == 0

    def test_known_device_skips_ip_lock_but_its_failures_count(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt(user="x"))

        assert throttle.retry_after(_attempt(user="y")) == 30
        assert throttle.retry_after(_attempt(device="dev-1")) == 0
        throttle.record_failure(_attempt(device="dev-1"))
        # The device's failure escalated the IP lock (6th failure: 60s).
        assert throttle.retry_after(_attempt(user="y")) == 60

    @pytest.mark.parametrize(
        ("first", "same_client", "other_client"),
        [
            ("2001:db8::1", "2001:db8::ffff:1", "2001:db8:0:1::1"),
            ("::ffff:203.0.113.7", "203.0.113.7", "203.0.113.8"),
        ],
    )
    def test_ipv6_clients_are_limited_per_64_network(
        self, first: str, same_client: str, other_client: str
    ) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        for i in range(LOGIN_IP_MAX_FAILURES):
            ip = first if i == 0 else same_client
            throttle.record_failure(_attempt(ip, f"user-{i}"))

        assert throttle.retry_after(_attempt(same_client, "u")) == 30
        assert throttle.retry_after(_attempt(first, "u")) == 30
        assert throttle.retry_after(_attempt(other_client, "u")) == 0

    def test_non_ip_client_names_are_used_verbatim(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt("testclient", "x"))

        assert throttle.retry_after(_attempt("testclient", "y")) == 30
        assert throttle.retry_after(_attempt("testclient2", "y")) == 0

    def test_no_client_address_skips_ip_limit(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        for i in range(LOGIN_IP_MAX_FAILURES * 2):
            throttle.record_failure(_attempt(None, f"user-{i}"))

        assert throttle.retry_after(_attempt(None, "someone")) == 0
        assert len(throttle._by_ip) == 0
        _fail(throttle, LOGIN_USERNAME_MAX_FAILURES, _attempt(None))
        assert throttle.retry_after(_attempt(None)) == 30

    def test_record_success_clears_ip_username_and_device(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt())
        _fail(throttle, LOGIN_DEVICE_MAX_FAILURES, _attempt(device="dev-1"))

        throttle.record_success(_attempt(device="dev-1"))

        assert throttle.retry_after(_attempt()) == 0
        assert throttle.retry_after(_attempt(device="dev-1")) == 0
        assert throttle.tracked_keys == 0

    def test_memory_is_capped_under_flood_of_distinct_keys(self) -> None:
        cap = 50
        throttle = LoginThrottle(clock=FakeClock(), max_keys=cap, max_locked=cap)
        for i in range(10 * cap):
            throttle.record_failure(_attempt(f"ip-{i}", f"user-{i}"))

        assert throttle.tracked_keys == 2 * cap

    def test_flood_of_new_keys_never_evicts_a_lock(self) -> None:
        throttle = LoginThrottle(clock=FakeClock(), max_keys=10)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt("attacker", "a"))
        for i in range(1000):
            throttle.record_failure(_attempt(f"ip-{i}", f"u-{i}"))

        assert throttle.retry_after(_attempt("attacker", "zzz")) == 30

    def test_full_locked_table_fails_closed_for_new_keys(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock, max_keys=10, max_locked=2)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt("a", "ua"))
        clock.now += 10
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt("b", "ub"))

        # Refused until the earliest lock ("a", 30s from t=0) expires.
        assert throttle.retry_after(_attempt("new", "un")) == 20
        # A known device isn't refused for the IP table being full.
        assert throttle.retry_after(_attempt("new", "un", device="dev")) == 0
        clock.now += 20
        assert throttle.retry_after(_attempt("new", "un")) == 0

    def test_lock_beyond_locked_cap_keeps_counting_without_locking(self) -> None:
        throttle = LoginThrottle(clock=FakeClock(), max_keys=10, max_locked=1)
        _fail(throttle, LOGIN_DEVICE_MAX_FAILURES, _attempt("a", device="dev-a"))
        _fail(throttle, LOGIN_DEVICE_MAX_FAILURES, _attempt("b", device="dev-b"))

        assert throttle.retry_after(_attempt("c", device="dev-a")) == 30
        assert throttle.retry_after(_attempt("c", device="dev-b")) == 0
        assert len(throttle._by_device) == 2

    def test_success_releases_lock_slot(self) -> None:
        throttle = LoginThrottle(clock=FakeClock(), max_keys=10, max_locked=1)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt("a", "ua"))

        throttle.record_success(_attempt("a", "ua"))

        assert throttle.retry_after(_attempt("new", "un")) == 0

    def test_forgotten_keys_make_room_before_low_count_ones(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock, max_keys=2)
        _fail(throttle, 3, _attempt(None, "old"))
        clock.now += LOGIN_FAILURE_MEMORY_S
        throttle.record_failure(_attempt(None, "fresh"))
        throttle.record_failure(_attempt(None, "new"))

        held = set(throttle._by_username._bucket_of)
        assert held == {
            login_throttle._username_key("fresh"),
            login_throttle._username_key("new"),
        }

    def test_long_usernames_are_stored_as_fixed_size_keys(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        throttle.record_failure(_attempt(user="x" * 1_000_000))

        keys = list(throttle._by_username._bucket_of)
        assert [len(k) for k in keys] == [64]


def _flood(throttle: LoginThrottle, count: int, *, tag: str) -> None:
    """Junk usernames, one failure each, as an attacker without a client IP."""
    for i in range(count):
        junk = _attempt(None, f"{tag}-{i}")
        if throttle.retry_after(junk) == 0:
            throttle.record_failure(junk)


class TestUsernameEvictionResistance:
    _CAP = 100

    def test_junk_username_flood_cannot_reset_a_near_lock_count(self) -> None:
        throttle = LoginThrottle(clock=FakeClock(), max_keys=self._CAP)
        _fail(throttle, LOGIN_USERNAME_MAX_FAILURES - 1, _attempt(None))
        _flood(throttle, 30 * self._CAP, tag="junk")
        throttle.record_failure(_attempt(None))

        assert throttle.retry_after(_attempt(None)) == LOGIN_LOCKOUT_BASE_S

    def test_reviewer_attack_gets_no_more_than_the_limit_of_guesses(self) -> None:
        # Guess, flood junk usernames to push the count out, repeat.
        throttle = LoginThrottle(clock=FakeClock(), max_keys=self._CAP)
        guesses = 0
        for round_ in range(5):
            for _ in range(LOGIN_USERNAME_MAX_FAILURES - 1):
                if throttle.retry_after(_attempt(None)) == 0:
                    throttle.record_failure(_attempt(None))
                    guesses += 1
            _flood(throttle, 3 * self._CAP, tag=f"junk{round_}")

        assert guesses == LOGIN_USERNAME_MAX_FAILURES
        assert throttle.retry_after(_attempt(None)) > 0

    @pytest.mark.parametrize("username", [_USER, "no-such-user"])
    def test_eviction_sequence_is_the_same_for_any_username(
        self, username: str
    ) -> None:
        # No oracle: fail X 19 times, flood, fail X once -> locked either way.
        throttle = LoginThrottle(clock=FakeClock(), max_keys=self._CAP)
        _fail(throttle, LOGIN_USERNAME_MAX_FAILURES - 1, _attempt(None, username))
        _flood(throttle, 3 * self._CAP, tag="junk")
        throttle.record_failure(_attempt(None, username))

        assert throttle.retry_after(_attempt(None, username)) == 30

    def test_low_count_keys_are_evicted_before_higher_ones(self) -> None:
        throttle = LoginThrottle(clock=FakeClock(), max_keys=3)
        _fail(throttle, 3, _attempt(None, "warm"))
        throttle.record_failure(_attempt(None, "cold-1"))
        throttle.record_failure(_attempt(None, "cold-2"))
        throttle.record_failure(_attempt(None, "new"))

        held = set(throttle._by_username._bucket_of)
        assert len(held) == 3
        assert login_throttle._username_key("warm") in held
        assert login_throttle._username_key("cold-1") not in held

    def test_table_full_of_warm_keys_fails_closed_for_new_usernames(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock, max_keys=3)
        warm = LOGIN_USERNAME_MAX_FAILURES // 2 + 1
        for name in ("w1", "w2", "w3"):
            _fail(throttle, warm, _attempt(None, name))
            clock.now += 10

        # Refused until the oldest ("w1", 20s ago) is forgotten.
        assert (
            throttle.retry_after(_attempt(None, "new")) == LOGIN_FAILURE_MEMORY_S - 30
        )
        assert throttle.retry_after(_attempt(None, "w2")) == 0
        assert throttle.retry_after(_attempt(None, "new", device="dev")) == 0
        throttle.record_failure(_attempt(None, "new"))
        assert len(throttle._by_username) == 3
        clock.now += LOGIN_FAILURE_MEMORY_S
        assert throttle.retry_after(_attempt(None, "new")) == 0

    def test_released_lock_keeps_its_count_when_table_is_full(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock, max_keys=2)
        _fail(throttle, LOGIN_USERNAME_MAX_FAILURES, _attempt(None))
        warm = LOGIN_USERNAME_MAX_FAILURES // 2 + 1
        _fail(throttle, warm, _attempt(None, "w1"))
        _fail(throttle, warm, _attempt(None, "w2"))
        clock.now += LOGIN_LOCKOUT_BASE_S

        assert throttle.retry_after(_attempt(None)) == 0
        throttle.record_failure(_attempt(None))
        assert throttle.retry_after(_attempt(None)) == 2 * LOGIN_LOCKOUT_BASE_S
        assert throttle.tracked_keys == 3


def _config() -> FormAuthConfig:
    return FormAuthConfig(username=_USER, password=_PASSWORD, secret=_SECRET)


class TestDeviceCookie:
    def test_valid_cookie_for_username_yields_device_id(self) -> None:
        config = _config()
        cookie = create_device_cookie_value(config)

        device_id = device_id_from_cookie(cookie, config, _USER)

        assert isinstance(device_id, str)
        assert len(device_id) >= 16

    @pytest.mark.parametrize("submitted", ["other", "admin ", ""])
    def test_cookie_for_another_username_is_ignored(self, submitted: str) -> None:
        config = _config()
        cookie = create_device_cookie_value(config)

        assert device_id_from_cookie(cookie, config, submitted) is None

    def test_cookie_signed_with_another_secret_is_ignored(self) -> None:
        other = FormAuthConfig(username=_USER, password=_PASSWORD, secret="other")
        cookie = create_device_cookie_value(other)

        assert device_id_from_cookie(cookie, _config(), _USER) is None

    def test_cookie_does_not_carry_the_username_in_clear(self) -> None:
        config = FormAuthConfig(
            username="ops-admin@example.com", password=_PASSWORD, secret=_SECRET
        )
        cookie = create_device_cookie_value(config)
        payload_b64 = cookie.split(".", 1)[0]
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))

        assert b"ops-admin" not in payload
        assert device_id_from_cookie(cookie, config, "ops-admin@example.com")

    def test_session_cookie_is_not_a_device_cookie(self) -> None:
        config = _config()
        cookie = config.create_session_cookie_value()

        assert device_id_from_cookie(cookie, config, _USER) is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"u": _USER, "exp": 4_000_000_000},
            {"d": "", "u": _USER, "exp": 4_000_000_000},
        ],
    )
    def test_cookie_without_device_id_is_ignored(
        self, payload: dict[str, object]
    ) -> None:
        config = _config()
        forged = sign_session(payload, login_throttle._device_secret(config))

        assert device_id_from_cookie(forged, config, _USER) is None


class _ClientFromHeader:
    """ASGI shim: take scope["client"] from an x-test-client header.

    Stands in for different client addresses (TestClient always reports the
    same one); "none" removes the address, like uvicorn on a Unix socket.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = dict(scope.get("headers", []))
        value = headers.get(b"x-test-client")
        if value is not None:
            host = value.decode()
            scope = {**scope, "client": None if host == "none" else (host, 50000)}
        await self.app(scope, receive, send)


def _router_app(throttle: LoginThrottle) -> Any:
    app = FastAPI()
    app.include_router(
        create_login_router(_config(), default_next_path="/", throttle=throttle)
    )
    return app


def _embedded_app() -> FastAPI:
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        login_username=_USER,
        login_password=_PASSWORD,
        login_secret=_SECRET,
    )
    return app


def _post(
    client: TestClient,
    username: str,
    password: str,
    *,
    path: str = "/login",
    ip: str | None = None,
) -> httpx.Response:
    headers = {} if ip is None else {"x-test-client": ip}
    return client.post(
        path,
        data={"username": username, "password": password, "next": "/graphs"},
        headers=headers,
        follow_redirects=False,
    )


class TestLoginEndpointThrottling:
    def test_repeated_failures_return_429_with_retry_after(self) -> None:
        with TestClient(_embedded_app()) as client:
            first = [
                _post(client, _USER, "wrong", path="/stemtrace/login")
                for _ in range(LOGIN_IP_MAX_FAILURES - 1)
            ]
            locking = _post(client, _USER, "wrong", path="/stemtrace/login")
            locked = _post(client, _USER, _PASSWORD, path="/stemtrace/login")
            page = client.get("/stemtrace/login")

        assert [r.status_code for r in first] == [303] * (LOGIN_IP_MAX_FAILURES - 1)
        assert locking.status_code == 429
        assert locking.headers["retry-after"] == "30"
        assert locked.status_code == 429
        assert "set-cookie" not in locked.headers
        assert "Too many failed sign-in attempts" in locked.text
        assert 'action="/stemtrace/login"' in locked.text
        assert 'value="/graphs"' in locked.text
        assert page.status_code == 200

    def test_lock_expiry_allows_sign_in_and_success_resets(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        with TestClient(_router_app(throttle)) as client:
            for _ in range(LOGIN_IP_MAX_FAILURES):
                _post(client, _USER, "wrong")
            locked = _post(client, _USER, _PASSWORD)
            clock.now += LOGIN_LOCKOUT_BASE_S
            ok = _post(client, _USER, _PASSWORD)
            after_reset = [
                _post(client, "other", "wrong")
                for _ in range(LOGIN_IP_MAX_FAILURES - 1)
            ]

        assert locked.status_code == 429
        assert ok.status_code == 303
        assert ok.headers["location"] == "/graphs"
        assert [r.status_code for r in after_reset] == [303] * (
            LOGIN_IP_MAX_FAILURES - 1
        )

    def test_success_sets_long_lived_device_cookie(self) -> None:
        with TestClient(_router_app(LoginThrottle(clock=FakeClock()))) as client:
            ok = _post(client, _USER, _PASSWORD)

        device = [
            c for c in ok.headers.get_list("set-cookie") if c.startswith(f"{_DEVICE}=")
        ]
        assert len(device) == 1
        assert f"Max-Age={DEVICE_COOKIE_TTL_SECONDS}" in device[0]
        assert "HttpOnly" in device[0]
        assert "Path=/" in device[0]

    def test_known_browser_signs_in_while_strangers_locked_username(self) -> None:
        app = _ClientFromHeader(_router_app(LoginThrottle(clock=FakeClock())))
        with TestClient(app) as owner, TestClient(app) as stranger:
            _post(owner, _USER, _PASSWORD, ip="192.0.2.1")
            owner.cookies.delete(_SESSION)  # signed out; device cookie kept
            for i in range(LOGIN_USERNAME_MAX_FAILURES):
                _post(stranger, _USER, "wrong", ip=f"10.0.0.{i}")
            stranger_try = _post(stranger, _USER, _PASSWORD, ip="10.0.1.1")
            owner_try = _post(owner, _USER, _PASSWORD, ip="192.0.2.1")

        assert stranger_try.status_code == 429
        assert owner_try.status_code == 303
        assert owner_try.headers["location"] == "/graphs"

    def test_known_browser_signs_in_while_strangers_locked_shared_ip(self) -> None:
        # Behind a proxy whose address isn't trusted, everyone shares its IP.
        app = _router_app(LoginThrottle(clock=FakeClock()))
        with TestClient(app) as owner, TestClient(app) as stranger:
            _post(owner, _USER, _PASSWORD)
            owner.cookies.delete(_SESSION)
            for i in range(LOGIN_IP_MAX_FAILURES):
                _post(stranger, f"guess-{i}", "wrong")
            stranger_try = _post(stranger, _USER, _PASSWORD)
            owner_try = _post(owner, _USER, _PASSWORD)

        assert stranger_try.status_code == 429
        assert owner_try.status_code == 303

    def test_device_cookie_failures_lock_only_that_device(self) -> None:
        app = _ClientFromHeader(_router_app(LoginThrottle(clock=FakeClock())))
        with TestClient(app) as owner, TestClient(app) as other:
            _post(owner, _USER, _PASSWORD, ip="192.0.2.1")
            statuses = [
                _post(owner, _USER, "wrong", ip=f"10.0.0.{i}").status_code
                for i in range(LOGIN_DEVICE_MAX_FAILURES)
            ]
            other_try = _post(other, _USER, _PASSWORD, ip="10.0.1.1")

        assert statuses == [303] * (LOGIN_DEVICE_MAX_FAILURES - 1) + [429]
        assert other_try.status_code == 303

    def test_device_cookie_is_not_accepted_as_session(self) -> None:
        with TestClient(_embedded_app()) as client:
            resp = _post(client, _USER, _PASSWORD, path="/stemtrace/login")
            device = resp.cookies.get(_DEVICE)
            client.cookies.clear()
            client.cookies.set(_SESSION, device or "")
            api = client.get("/stemtrace/api/tasks")

        assert isinstance(device, str)
        assert api.status_code == 401

    def test_requests_without_client_address_do_not_share_ip_bucket(self) -> None:
        app = _ClientFromHeader(_router_app(LoginThrottle(clock=FakeClock())))
        with TestClient(app) as client:
            for i in range(LOGIN_IP_MAX_FAILURES * 2):
                _post(client, f"user-{i}", "wrong", ip="none")
            other = _post(client, _USER, _PASSWORD, ip="none")

        assert other.status_code == 303

    def test_wrong_username_and_wrong_password_answer_identically(self) -> None:
        with TestClient(_router_app(LoginThrottle(clock=FakeClock()))) as client:
            unknown_user = _post(client, "nobody", "wrong")
            wrong_password = _post(client, _USER, "wrong")

        assert unknown_user.status_code == wrong_password.status_code == 303
        assert unknown_user.headers["location"] == wrong_password.headers["location"]
        assert unknown_user.text == wrong_password.text

    def test_locked_responses_do_not_depend_on_username_or_password(self) -> None:
        with TestClient(_router_app(LoginThrottle(clock=FakeClock()))) as client:
            for _ in range(LOGIN_IP_MAX_FAILURES):
                _post(client, "nobody", "wrong")
            responses = [
                _post(client, "nobody", "wrong"),
                _post(client, _USER, "wrong"),
                _post(client, _USER, _PASSWORD),
            ]

        assert {r.status_code for r in responses} == {429}
        assert len({r.text for r in responses}) == 1
        assert len({r.headers["retry-after"] for r in responses}) == 1

    def test_forwarded_for_header_is_ignored_without_trusted_proxy(self) -> None:
        with TestClient(_router_app(LoginThrottle(clock=FakeClock()))) as client:
            statuses = [
                client.post(
                    "/login",
                    data={"username": f"u{i}", "password": "wrong"},
                    headers={"x-forwarded-for": f"10.0.0.{i}"},
                    follow_redirects=False,
                ).status_code
                for i in range(LOGIN_IP_MAX_FAILURES)
            ]

        assert statuses[-1] == 429

    @pytest.mark.parametrize("trusted", [True, False])
    def test_trusted_proxy_headers_separate_client_ips(self, trusted: bool) -> None:
        # What uvicorn does with --forwarded-allow-ips: rewrite scope["client"]
        # from X-Forwarded-For only for a trusted peer (here 127.0.0.1).
        app = _ClientFromHeader(
            ProxyHeadersMiddleware(
                _router_app(LoginThrottle(clock=FakeClock())),
                trusted_hosts="127.0.0.1" if trusted else "192.0.2.1",
            )
        )
        headers = {"x-test-client": "127.0.0.1"}
        with TestClient(app) as client:
            for i in range(LOGIN_IP_MAX_FAILURES):
                client.post(
                    "/login",
                    data={"username": f"a{i}", "password": "wrong"},
                    headers={**headers, "x-forwarded-for": "198.51.100.1"},
                    follow_redirects=False,
                )
            other_client = client.post(
                "/login",
                data={"username": "b", "password": "wrong"},
                headers={**headers, "x-forwarded-for": "198.51.100.2"},
                follow_redirects=False,
            )

        assert other_client.status_code == (303 if trusted else 429)
