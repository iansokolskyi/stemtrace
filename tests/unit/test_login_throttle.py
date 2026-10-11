"""Tests for throttling failed sign-ins on the built-in login page."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import stemtrace
from stemtrace.server.fastapi.form_auth import FormAuthConfig
from stemtrace.server.fastapi.login_routes import create_login_router
from stemtrace.server.fastapi.login_throttle import (
    LOGIN_FAILURE_MEMORY_S,
    LOGIN_IP_MAX_FAILURES,
    LOGIN_LOCKOUT_BASE_S,
    LOGIN_LOCKOUT_MAX_S,
    LOGIN_USERNAME_MAX_FAILURES,
    LoginThrottle,
)

if TYPE_CHECKING:
    import httpx

_IP = "203.0.113.7"
_USER = "admin"
_PASSWORD = "secret"  # NOSONAR - test credential only


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _fail(
    throttle: LoginThrottle, times: int, *, ip: str = _IP, user: str = _USER
) -> None:
    for _ in range(times):
        throttle.record_failure(ip, user)


class TestLoginThrottle:
    def test_retry_after_below_limit_is_zero(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        _fail(throttle, LOGIN_IP_MAX_FAILURES - 1)

        assert throttle.retry_after(_IP, _USER) == 0

    def test_retry_after_at_limit_locks_for_base_lockout(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES)

        assert throttle.retry_after(_IP, _USER) == LOGIN_LOCKOUT_BASE_S
        clock.now += 10.5
        assert throttle.retry_after(_IP, _USER) == LOGIN_LOCKOUT_BASE_S - 10
        # Another IP with another username is unaffected.
        assert throttle.retry_after("198.51.100.1", "other") == 0

    def test_lockout_doubles_per_failure_and_is_capped(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES)
        lockouts = []
        for _ in range(7):
            lockouts.append(throttle.retry_after(_IP, _USER))
            clock.now += lockouts[-1]
            throttle.record_failure(_IP, _USER)

        assert lockouts == [30, 60, 120, 240, 480, 900, 900]
        assert lockouts[-1] == LOGIN_LOCKOUT_MAX_S

    def test_lock_expires_then_next_failure_locks_again(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES)
        clock.now += LOGIN_LOCKOUT_BASE_S

        assert throttle.retry_after(_IP, _USER) == 0
        throttle.record_failure(_IP, _USER)
        assert throttle.retry_after(_IP, _USER) == 2 * LOGIN_LOCKOUT_BASE_S

    def test_failures_are_forgotten_after_memory_window(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock)
        _fail(throttle, LOGIN_IP_MAX_FAILURES)
        clock.now += LOGIN_FAILURE_MEMORY_S

        assert throttle.retry_after(_IP, _USER) == 0
        assert throttle.tracked_keys == 0
        _fail(throttle, LOGIN_IP_MAX_FAILURES - 1)
        assert throttle.retry_after(_IP, _USER) == 0

    def test_username_locked_from_many_ips(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        for i in range(LOGIN_USERNAME_MAX_FAILURES):
            throttle.record_failure(f"10.0.0.{i}", _USER)

        assert throttle.retry_after("10.0.1.1", _USER) == LOGIN_LOCKOUT_BASE_S
        assert throttle.retry_after("10.0.1.1", "someone-else") == 0

    def test_record_success_clears_ip_and_username(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        _fail(throttle, LOGIN_IP_MAX_FAILURES)

        throttle.record_success(_IP, _USER)

        assert throttle.retry_after(_IP, _USER) == 0
        assert throttle.tracked_keys == 0

    def test_memory_is_capped_under_flood_of_distinct_keys(self) -> None:
        cap = 50
        throttle = LoginThrottle(clock=FakeClock(), max_keys=cap)
        for i in range(10 * cap):
            throttle.record_failure(f"ip-{i}", f"user-{i}")

        assert throttle.tracked_keys == 2 * cap

    def test_eviction_drops_least_recently_failed_key(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock, max_keys=3)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, ip="attacker", user="a")
        for i in range(2):
            clock.now += 1
            throttle.record_failure(f"ip-{i}", f"u-{i}")
        clock.now += 1
        throttle.record_failure("attacker", "a")  # refreshes the attacker's keys
        throttle.record_failure("ip-new", "u-new")  # evicts ip-0 / u-0, not them

        assert throttle.retry_after("attacker", "a") > 0
        assert throttle.tracked_keys == 6

    def test_expired_keys_are_purged_when_a_new_key_arrives(self) -> None:
        clock = FakeClock()
        throttle = LoginThrottle(clock=clock, max_keys=10)
        throttle.record_failure("old-1", "old-1")
        throttle.record_failure("old-2", "old-2")
        clock.now += LOGIN_FAILURE_MEMORY_S
        throttle.record_failure("new", "new")

        assert throttle.tracked_keys == 2

    def test_long_usernames_are_stored_as_fixed_size_keys(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        throttle.record_failure(_IP, "x" * 1_000_000)

        keys = list(throttle._by_username._entries)
        assert [len(k) for k in keys] == [64]


def _config() -> FormAuthConfig:
    return FormAuthConfig(username=_USER, password=_PASSWORD, secret="test-secret")


def _router_app(throttle: LoginThrottle) -> FastAPI:
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
        login_secret="test-secret",
    )
    return app


def _post(
    client: TestClient, username: str, password: str, *, path: str = "/login"
) -> httpx.Response:
    return client.post(
        path,
        data={"username": username, "password": password, "next": "/graphs"},
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
        # While locked, even the right password is not checked or accepted.
        assert locked.status_code == 429
        assert "set-cookie" not in locked.headers
        assert "Too many failed sign-in attempts" in locked.text
        assert 'action="/stemtrace/login"' in locked.text
        assert 'value="/graphs"' in locked.text
        # The sign-in page itself is never throttled.
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
                _post(client, _USER, "wrong") for _ in range(LOGIN_IP_MAX_FAILURES - 1)
            ]

        assert locked.status_code == 429
        assert ok.status_code == 303
        assert ok.headers["location"] == "/graphs"
        assert throttle.retry_after("testclient", _USER) == 0
        assert [r.status_code for r in after_reset] == [303] * (
            LOGIN_IP_MAX_FAILURES - 1
        )

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
                    data={"username": _USER, "password": "wrong"},
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
        app = ProxyHeadersMiddleware(
            _router_app(LoginThrottle(clock=FakeClock())),
            trusted_hosts="127.0.0.1" if trusted else "192.0.2.1",
        )
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            for _ in range(LOGIN_IP_MAX_FAILURES):
                client.post(
                    "/login",
                    data={"username": "a", "password": "wrong"},
                    headers={"x-forwarded-for": "198.51.100.1"},
                    follow_redirects=False,
                )
            other_client = client.post(
                "/login",
                data={"username": "b", "password": "wrong"},
                headers={"x-forwarded-for": "198.51.100.2"},
                follow_redirects=False,
            )

        assert other_client.status_code == (303 if trusted else 429)
