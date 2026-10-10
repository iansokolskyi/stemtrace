"""Unit tests for signed-cookie session auth primitives."""

from __future__ import annotations

import base64
import hmac
import logging
import time
from hashlib import sha256
from http.cookies import SimpleCookie
from typing import Any

import pytest

from stemtrace.server.fastapi import form_auth
from stemtrace.server.fastapi.form_auth import (
    FormAuthConfig,
    SessionStatus,
    check_session_cookie,
    is_authenticated_cookie,
    parse_cookie_header,
    resolve_session_secret,
    sign_session,
    verify_session,
    warn_session_secret_mismatch,
)

_FORM_AUTH_LOGGER = "stemtrace.server.fastapi.form_auth"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove env vars that influence multi-process detection."""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    return monkeypatch


def _form_auth_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _FORM_AUTH_LOGGER]


class TestVerifySession:
    def test_verify_session_returns_none_for_missing_cookie(self) -> None:
        assert verify_session(None, secret="s") is None
        assert verify_session("", secret="s") is None

    def test_verify_session_returns_none_for_missing_separator(self) -> None:
        assert verify_session("no-separator", secret="s") is None

    def test_verify_session_returns_none_for_bad_base64(self) -> None:
        assert verify_session("%%%.%%% ", secret="s") is None

    def test_verify_session_returns_none_for_bad_signature(self) -> None:
        payload = {"u": "admin", "exp": int(time.time()) + 60}
        cookie = sign_session(payload, "secret-a")
        assert verify_session(cookie, secret="secret-b") is None

    def test_verify_session_returns_none_for_expired_session(self) -> None:
        payload = {"u": "admin", "exp": int(time.time()) - 1}
        cookie = sign_session(payload, "secret")
        assert verify_session(cookie, secret="secret") is None

    def test_verify_session_returns_payload_for_valid_unexpired_session(self) -> None:
        payload = {"u": "admin", "exp": int(time.time()) + 60}
        cookie = sign_session(payload, "secret")
        decoded = verify_session(cookie, secret="secret")
        assert decoded is not None
        assert decoded["u"] == "admin"

    def test_verify_session_returns_none_when_payload_is_not_json(self) -> None:
        payload_bytes = b"not-json"

        sig = hmac.new(b"secret", payload_bytes, sha256).digest()
        cookie = (
            base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
            + "."
            + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        )
        assert verify_session(cookie, secret="secret") is None


class TestParseCookieHeader:
    def test_parse_cookie_header_returns_empty_dict_for_none(self) -> None:
        assert parse_cookie_header(None) == {}

    def test_parse_cookie_header_parses_simple_cookie(self) -> None:
        cookies = parse_cookie_header("a=1; b=two")
        assert cookies["a"] == "1"
        assert cookies["b"] == "two"

    def test_parse_cookie_header_returns_empty_dict_for_invalid_cookie(self) -> None:
        assert parse_cookie_header("not-a-cookie") == {}

    def test_parse_cookie_header_returns_empty_dict_when_load_raises(
        self, monkeypatch: Any
    ) -> None:
        def _boom(self: object, raw: str) -> None:
            raise ValueError("boom")

        monkeypatch.setattr(SimpleCookie, "load", _boom)
        assert parse_cookie_header("a=1") == {}


class TestIsAuthenticatedCookie:
    def test_is_authenticated_cookie_false_for_missing_cookie(self) -> None:
        assert (
            is_authenticated_cookie(None, secret="s", expected_username="admin")
            is False
        )

    def test_is_authenticated_cookie_false_for_wrong_user(self) -> None:
        cfg = FormAuthConfig("admin", "pw", "secret")
        cookie = cfg.create_session_cookie_value()
        assert (
            is_authenticated_cookie(
                cookie, secret=cfg.secret, expected_username="other"
            )
            is False
        )

    def test_is_authenticated_cookie_true_for_expected_user(self) -> None:
        cfg = FormAuthConfig("admin", "pw", "secret")
        cookie = cfg.create_session_cookie_value()
        assert (
            is_authenticated_cookie(
                cookie, secret=cfg.secret, expected_username="admin"
            )
            is True
        )

    def test_is_authenticated_cookie_false_when_username_is_not_string(self) -> None:
        payload = {"u": 123, "exp": int(time.time()) + 60}
        cookie = sign_session(payload, "secret")
        assert (
            is_authenticated_cookie(cookie, secret="secret", expected_username="admin")
            is False
        )


class TestVerifySessionEdgeCases:
    def test_verify_session_returns_none_for_non_dict_payload(self) -> None:
        # Valid signature but payload isn't a dict.
        payload_bytes = b'["not-a-dict"]'

        sig = hmac.new(b"secret", payload_bytes, sha256).digest()
        cookie = (
            base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
            + "."
            + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        )
        assert verify_session(cookie, secret="secret") is None

    def test_verify_session_returns_none_for_non_int_exp(self) -> None:
        payload = {"u": "admin", "exp": "not-int"}
        cookie = sign_session(payload, "secret")
        assert verify_session(cookie, secret="secret") is None


_TTL = 3600


def _config(
    *, secret: str, generated: bool, started_at: float, ttl: int = _TTL
) -> FormAuthConfig:
    return FormAuthConfig(
        "admin",
        "pw",
        secret,
        ttl_seconds=ttl,
        secret_is_generated=generated,
        started_at=started_at,
    )


def _cookie(
    secret: str,
    *,
    iat: float | None,
    username: str = "admin",
    ttl: int = _TTL,
) -> str:
    issued = time.time() if iat is None else iat
    payload: dict[str, object] = {"u": username, "exp": int(issued) + ttl}
    if iat is not None:
        payload["iat"] = iat
    return sign_session(payload, secret)


class TestCheckSessionCookie:
    def test_check_session_cookie_valid_cookie_returns_valid(self) -> None:
        now = time.time()
        cfg = _config(secret="secret-a", generated=True, started_at=now - 10)
        status = check_session_cookie(_cookie("secret-a", iat=now - 60), cfg)
        assert status is SessionStatus.VALID

    def test_check_session_cookie_sibling_cookie_after_start_returns_mismatch(
        self,
    ) -> None:
        now = time.time()
        cfg = _config(secret="secret-b", generated=True, started_at=now - 10)
        status = check_session_cookie(_cookie("secret-a", iat=now), cfg)
        assert status is SessionStatus.SECRET_MISMATCH

    def test_check_session_cookie_issued_before_start_returns_invalid(self) -> None:
        # E.g. a single process restarted: old cookies are simply expired.
        now = time.time()
        cfg = _config(secret="secret-b", generated=True, started_at=now)
        status = check_session_cookie(_cookie("secret-a", iat=now - 60), cfg)
        assert status is SessionStatus.INVALID

    def test_check_session_cookie_same_second_restart_returns_invalid(self) -> None:
        # Issued at T+0.1 by the old process, new process started at T+0.9.
        second = float(int(time.time()))
        cfg = _config(secret="secret-b", generated=True, started_at=second + 0.9)
        status = check_session_cookie(_cookie("secret-a", iat=second + 0.1), cfg)
        assert status is SessionStatus.INVALID

    def test_check_session_cookie_ttl_changed_across_restart_returns_invalid(
        self,
    ) -> None:
        # Old process: 2h TTL, issued a minute before the restart. The new
        # process uses a 1h TTL; exp - ttl would wrongly put issuance after
        # the restart, iat doesn't.
        now = time.time()
        old_cookie = _cookie("secret-a", iat=now - 60, ttl=7200)
        cfg = _config(secret="secret-b", generated=True, started_at=now, ttl=3600)
        assert check_session_cookie(old_cookie, cfg) is SessionStatus.INVALID

    def test_check_session_cookie_without_iat_is_never_mismatch(self) -> None:
        # Cookies from older versions carry no iat.
        cfg = _config(secret="secret-b", generated=True, started_at=0)
        legacy = _cookie("secret-a", iat=None)
        assert check_session_cookie(legacy, cfg) is SessionStatus.INVALID

    def test_check_session_cookie_non_numeric_iat_returns_invalid(self) -> None:
        cfg = _config(secret="secret-b", generated=True, started_at=0)
        exp = int(time.time()) + _TTL
        for iat in (True, "soon", None):
            cookie = sign_session({"u": "admin", "exp": exp, "iat": iat}, "secret-a")
            assert check_session_cookie(cookie, cfg) is SessionStatus.INVALID, iat

    def test_check_session_cookie_other_secret_when_configured_returns_invalid(
        self,
    ) -> None:
        # Forged cookie or rotated secret: never reported as a mismatch.
        now = time.time()
        cfg = _config(secret="secret-b", generated=False, started_at=now - 10)
        status = check_session_cookie(_cookie("secret-a", iat=now), cfg)
        assert status is SessionStatus.INVALID

    def test_check_session_cookie_mismatch_ignores_username_in_payload(
        self,
    ) -> None:
        # No username oracle: a forged cookie gets the same answer whether or
        # not it guesses the configured username.
        now = time.time()
        for generated in (True, False):
            cfg = _config(secret="secret", generated=generated, started_at=now - 10)
            statuses = {
                check_session_cookie(_cookie("forger", iat=now, username=username), cfg)
                for username in ("admin", "eve")
            }
            assert len(statuses) == 1, generated

    def test_check_session_cookie_missing_or_garbage_returns_invalid(self) -> None:
        cfg = _config(secret="secret", generated=True, started_at=0)
        for cookie in (None, "", "garbage", "%%%.%%%", "bm90LWpzb24.c2ln"):
            assert check_session_cookie(cookie, cfg) is SessionStatus.INVALID, cookie

    def test_check_session_cookie_expired_other_secret_returns_invalid(self) -> None:
        cfg = _config(secret="secret-b", generated=True, started_at=0)
        now = time.time()
        expired = sign_session(
            {"u": "admin", "exp": int(now) - 1, "iat": now - 10}, "secret-a"
        )
        assert check_session_cookie(expired, cfg) is SessionStatus.INVALID

    def test_check_session_cookie_other_user_same_secret_returns_invalid(
        self,
    ) -> None:
        now = time.time()
        cfg = _config(secret="secret", generated=True, started_at=now - 10)
        cookie = _cookie("secret", iat=now, username="eve")
        assert check_session_cookie(cookie, cfg) is SessionStatus.INVALID

    def test_form_auth_config_started_at_defaults_to_creation_time(self) -> None:
        before = time.time()
        cfg = FormAuthConfig("admin", "pw", "secret")
        assert before <= cfg.started_at <= time.time()

    def test_create_session_cookie_value_includes_signed_iat(self) -> None:
        cfg = FormAuthConfig("admin", "pw", "secret", ttl_seconds=60)
        before = time.time()
        payload = verify_session(cfg.create_session_cookie_value(), secret="secret")
        assert payload is not None
        iat = payload["iat"]
        assert isinstance(iat, float)
        assert before <= iat <= time.time()
        assert payload["exp"] == int(iat) + 60


class TestResolveSessionSecret:
    def test_resolve_session_secret_explicit_secret_wins_without_warning(
        self, clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)
        assert resolve_session_secret("explicit") == ("explicit", False)
        assert _form_auth_records(caplog) == []

    def test_resolve_session_secret_unset_returns_random_secret_and_warns(
        self, clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        first, first_generated = resolve_session_secret(None)
        second, second_generated = resolve_session_secret("")

        assert first_generated is True
        assert second_generated is True
        assert first != second
        assert len(first) == 43
        records = _form_auth_records(caplog)
        assert len(records) == 2
        message = records[0].getMessage()
        assert records[0].levelno == logging.WARNING
        assert "only work within this single process" in message
        assert "STEMTRACE_LOGIN_SECRET" in message
        assert "Note:" not in message

    def test_resolve_session_secret_web_concurrency_mentions_detection(
        self, clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        clean_env.setenv("WEB_CONCURRENCY", "4")
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        resolve_session_secret(None)

        message = _form_auth_records(caplog)[0].getMessage()
        assert "multiple worker processes detected (WEB_CONCURRENCY=4)" in message

    def test_resolve_session_secret_single_web_concurrency_mentions_nothing(
        self, clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        clean_env.setenv("WEB_CONCURRENCY", "1")
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        resolve_session_secret(None)

        assert "Note:" not in _form_auth_records(caplog)[0].getMessage()

    def test_resolve_session_secret_gunicorn_mentions_possible_workers(
        self, clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        clean_env.setenv("SERVER_SOFTWARE", "gunicorn/23.0.0")
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        resolve_session_secret(None)

        message = _form_auth_records(caplog)[0].getMessage()
        assert "gunicorn, possibly with multiple workers" in message


class TestWarnSessionSecretMismatch:
    def test_warn_session_secret_mismatch_is_rate_limited(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(form_auth, "_last_mismatch_warning_at", None)
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        warn_session_secret_mismatch()
        warn_session_secret_mismatch()

        records = _form_auth_records(caplog)
        assert len(records) == 1
        assert "signed by a different secret" in records[0].getMessage()
        assert "STEMTRACE_LOGIN_SECRET" in records[0].getMessage()

    def test_warn_session_secret_mismatch_logs_again_after_interval(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            form_auth, "_last_mismatch_warning_at", time.monotonic() - 61
        )
        caplog.set_level(logging.WARNING, logger=_FORM_AUTH_LOGGER)

        warn_session_secret_mismatch()

        assert len(_form_auth_records(caplog)) == 1
