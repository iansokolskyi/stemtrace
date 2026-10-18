"""Tests for throttling failed sign-ins on the built-in login page."""

from __future__ import annotations

import base64
import time
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
    LOGIN_THROTTLE_BUCKETS,
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

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("203.0.113.7:51234", "203.0.113.7"),
            ("[2001:db8::1]:443", "2001:db8::/64"),
            ("[2001:db8::1]", "2001:db8::/64"),
            ("2001:db8::1", "2001:db8::/64"),
            ("203.0.113.7", "203.0.113.7"),
            ("testclient", "testclient"),
            ("not-an-ip:80", "not-an-ip:80"),
            ("[nonsense]:1", "[nonsense]:1"),
            ("[2001:db8::1]junk", "[2001:db8::1]junk"),
            ("203.0.113.7:http", "203.0.113.7:http"),
        ],
    )
    def test_ip_key_strips_ports_and_keeps_unparseable_names(
        self, raw: str, expected: str
    ) -> None:
        assert login_throttle._ip_key(raw) == expected

    def test_source_ports_share_one_ip_limit(self) -> None:
        # Proxies like Azure Application Gateway append the client's port.
        throttle = LoginThrottle(clock=FakeClock())
        for port in range(LOGIN_IP_MAX_FAILURES):
            throttle.record_failure(_attempt(f"203.0.113.7:{40000 + port}", f"u{port}"))

        assert throttle.retry_after(_attempt("203.0.113.7:50000", "other")) == 30

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
        assert not any(throttle._by_ip._counts)
        _fail(throttle, LOGIN_USERNAME_MAX_FAILURES, _attempt(None))
        assert throttle.retry_after(_attempt(None)) == 30

    def test_record_success_clears_ip_username_and_device(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt())
        _fail(throttle, LOGIN_DEVICE_MAX_FAILURES, _attempt(device="dev-1"))

        throttle.record_success(_attempt(device="dev-1"))

        assert throttle.retry_after(_attempt()) == 0
        assert throttle.retry_after(_attempt(device="dev-1")) == 0
        tables = (throttle._by_ip, throttle._by_username, throttle._by_device)
        assert not any(any(t._counts) for t in tables)

    def test_memory_is_constant_under_flood_of_distinct_keys(self) -> None:
        throttle = LoginThrottle(clock=FakeClock(), buckets=1024)
        before = throttle.memory_bytes
        for i in range(20_000):
            throttle.record_failure(_attempt(f"ip-{i}", f"user-{i}"))

        assert throttle.memory_bytes == before == 3 * 3 * 8 * 1024

    def test_default_tables_use_a_fixed_amount_of_memory(self) -> None:
        throttle = LoginThrottle()

        assert throttle.memory_bytes == 3 * 3 * 8 * LOGIN_THROTTLE_BUCKETS

    def test_flood_of_new_keys_never_clears_a_lock(self) -> None:
        throttle = LoginThrottle(clock=FakeClock(), buckets=256)
        _fail(throttle, LOGIN_IP_MAX_FAILURES, _attempt("attacker", "a"))
        for i in range(5000):
            throttle.record_failure(_attempt(f"ip-{i}", f"u-{i}"))

        assert throttle.retry_after(_attempt("attacker", "zzz")) >= 30

    def test_bucket_choice_depends_on_the_hash_key(self) -> None:
        a = LoginThrottle(clock=FakeClock(), hash_key=b"a" * 32)
        b = LoginThrottle(clock=FakeClock(), hash_key=b"b" * 32)
        same = LoginThrottle(clock=FakeClock(), hash_key=b"a" * 32)

        assert a._by_username._slots("admin") != b._by_username._slots("admin")
        assert a._by_username._slots("admin") == same._by_username._slots("admin")

    def test_random_hash_key_per_throttle(self) -> None:
        keys = {LoginThrottle()._by_username._hash_key for _ in range(3)}

        assert len(keys) == 3


def _flood(throttle: LoginThrottle, count: int, *, tag: str, each: int = 1) -> None:
    """Junk usernames, ``each`` failures apiece, from a client without an IP."""
    for i in range(count):
        junk = _attempt(None, f"{tag}-{i}")
        for _ in range(each):
            if throttle.retry_after(junk) == 0:
                throttle.record_failure(junk)


def _guesses_until_locked(throttle: LoginThrottle, attempt: LoginAttempt) -> int:
    """Password checks an attacker gets on ``attempt`` before a 429."""
    guesses = 0
    while throttle.retry_after(attempt) == 0:
        throttle.record_failure(attempt)
        guesses += 1
        assert guesses <= 10 * LOGIN_USERNAME_MAX_FAILURES, "never locked"
    return guesses


class TestNoResetByFlooding:
    """Nothing an attacker sends can lower another key's count."""

    @pytest.mark.parametrize(
        ("junk_keys", "junk_count_level"),
        [(3000, 1), (3000, 5), (9_999, 11), (3000, 19)],
    )
    def test_prewarmed_table_never_gives_more_than_the_limit(
        self, junk_keys: int, junk_count_level: int
    ) -> None:
        # Round-3 attack: pre-warm with junk usernames at some count level,
        # then alternate guesses on the real username with new junk keys
        # (which used to evict the real username's count).
        throttle = LoginThrottle(clock=FakeClock(), hash_key=b"k" * 32)
        _flood(throttle, junk_keys, tag="warm", each=junk_count_level)
        guesses = 0
        for round_ in range(50):
            for _ in range(10):
                if throttle.retry_after(_attempt(None)) == 0:
                    throttle.record_failure(_attempt(None))
                    guesses += 1
            _flood(throttle, 20, tag=f"evict-{round_}")

        assert guesses == LOGIN_USERNAME_MAX_FAILURES
        assert throttle.retry_after(_attempt(None)) > 0

    def test_round2_flood_attack_gets_no_more_than_the_limit(self) -> None:
        throttle = LoginThrottle(clock=FakeClock(), hash_key=b"k" * 32)
        guesses = 0
        for round_ in range(5):
            for _ in range(LOGIN_USERNAME_MAX_FAILURES - 1):
                if throttle.retry_after(_attempt(None)) == 0:
                    throttle.record_failure(_attempt(None))
                    guesses += 1
            _flood(throttle, 3000, tag=f"junk{round_}")

        assert guesses == LOGIN_USERNAME_MAX_FAILURES
        assert throttle.retry_after(_attempt(None)) > 0

    def test_without_collisions_the_limit_is_exact(self) -> None:
        throttle = LoginThrottle(clock=FakeClock())

        assert _guesses_until_locked(throttle, _attempt(None)) == (
            LOGIN_USERNAME_MAX_FAILURES
        )

    @pytest.mark.parametrize("username", [_USER, "no-such-user"])
    def test_flood_sequence_is_the_same_for_any_username(self, username: str) -> None:
        # No oracle: fail X 19 times, flood, fail X once -> locked either way.
        throttle = LoginThrottle(clock=FakeClock(), buckets=4096)
        _fail(throttle, LOGIN_USERNAME_MAX_FAILURES - 1, _attempt(None, username))
        _flood(throttle, 3000, tag="junk")
        throttle.record_failure(_attempt(None, username))

        assert throttle.retry_after(_attempt(None, username)) >= 30


def _false_lock_rate(*, buckets: int, locked_keys: int, probes: int) -> float:
    """Share of never-failed usernames that are locked by collisions alone."""
    throttle = LoginThrottle(clock=FakeClock(), buckets=buckets, hash_key=b"k" * 32)
    for i in range(locked_keys):
        _fail(throttle, LOGIN_USERNAME_MAX_FAILURES, _attempt(None, f"bad-{i}"))
    locked = sum(
        throttle.retry_after(_attempt(None, f"fresh-{i}")) > 0 for i in range(probes)
    )
    return locked / probes


class TestFalseLocks:
    """Measured false-lock rates (documented in docs/security.md)."""

    def test_realistic_load_has_no_measurable_false_locks(self) -> None:
        # 500 locked keys at once is far beyond normal traffic; the expected
        # rate with two hashes is about (2*500/65536)**2 = 0.02%.
        rate = _false_lock_rate(
            buckets=LOGIN_THROTTLE_BUCKETS, locked_keys=500, probes=20_000
        )

        assert rate <= 0.001

    def test_heavy_attack_false_lock_rate_stays_bounded(self) -> None:
        # 20,000 locked junk usernames: about (1 - e**(-40000/65536))**2,
        # roughly 21%, of other usernames are falsely locked (still only for
        # browsers without a device cookie).
        rate = _false_lock_rate(
            buckets=LOGIN_THROTTLE_BUCKETS, locked_keys=20_000, probes=5_000
        )

        assert 0.15 <= rate <= 0.28


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


def _ip_throttle(now: list[float] | None = None) -> LoginThrottle:
    clock = (lambda: now[0]) if now is not None else time.monotonic
    return LoginThrottle(clock=clock, hash_key=b"stemtrace-test" * 2)


def test_reserve_ip_attempt_grants_as_many_slots_as_failures_are_left() -> None:
    throttle = _ip_throttle()
    for _ in range(2):
        throttle.record_failure(
            LoginAttempt(client_ip=_IP, username=None, device_id=None)
        )

    grants = [throttle.reserve_ip_attempt(_IP) for _ in range(LOGIN_IP_MAX_FAILURES)]

    assert grants == [True] * (LOGIN_IP_MAX_FAILURES - 2) + [False] * 2
    assert throttle.reserve_ip_attempt("198.51.100.1") is True


def test_reserve_ip_attempt_is_freed_by_release() -> None:
    throttle = _ip_throttle()
    for _ in range(LOGIN_IP_MAX_FAILURES):
        assert throttle.reserve_ip_attempt(_IP)
    assert throttle.reserve_ip_attempt(_IP) is False

    throttle.release_ip_attempt(_IP)

    assert throttle.reserve_ip_attempt(_IP) is True


def test_reserve_ip_attempt_grants_one_slot_after_a_lockout_ends() -> None:
    """Zero failures left with nothing in flight is a lockout that expired, not a cap."""
    now = [1000.0]
    throttle = _ip_throttle(now)
    for _ in range(LOGIN_IP_MAX_FAILURES):
        throttle.record_failure(
            LoginAttempt(client_ip=_IP, username=None, device_id=None)
        )
    now[0] += LOGIN_LOCKOUT_BASE_S

    assert throttle.reserve_ip_attempt(_IP) is True
    assert throttle.reserve_ip_attempt(_IP) is False


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("2001:db8::1", "2001:db8::ffff"),  # one IPv6 /64
        ("203.0.113.7", "203.0.113.7:51234"),  # a proxy that appends the source port
        ("203.0.113.7", "::ffff:203.0.113.7"),  # IPv4-mapped IPv6
    ],
)
def test_reserve_ip_attempt_shares_slots_across_spellings_of_one_ip(
    first: str, second: str
) -> None:
    throttle = _ip_throttle()
    for i in range(LOGIN_IP_MAX_FAILURES):
        assert throttle.reserve_ip_attempt(first if i % 2 else second)

    assert throttle.reserve_ip_attempt(first) is False
    assert throttle.reserve_ip_attempt(second) is False


def test_release_ip_attempt_forgets_an_ip_with_no_slots_left() -> None:
    throttle = _ip_throttle()
    assert throttle.reserve_ip_attempt(_IP)

    throttle.release_ip_attempt(_IP)
    throttle.release_ip_attempt(_IP)  # an extra release is harmless

    assert throttle.reserve_ip_attempt(_IP) is True
