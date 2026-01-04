"""Unit tests for signed-cookie session auth primitives."""

from __future__ import annotations

import time

from stemtrace.server.fastapi.form_auth import (
    FormAuthConfig,
    is_authenticated_cookie,
    parse_cookie_header,
    sign_session,
    verify_session,
)


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


class TestParseCookieHeader:
    def test_parse_cookie_header_returns_empty_dict_for_none(self) -> None:
        assert parse_cookie_header(None) == {}

    def test_parse_cookie_header_parses_simple_cookie(self) -> None:
        cookies = parse_cookie_header("a=1; b=two")
        assert cookies["a"] == "1"
        assert cookies["b"] == "two"

    def test_parse_cookie_header_returns_empty_dict_for_invalid_cookie(self) -> None:
        # SimpleCookie is fairly permissive; still, ensure we never raise.
        assert parse_cookie_header("\x00\x00") == {}


class TestIsAuthenticatedCookie:
    def test_is_authenticated_cookie_false_for_missing_cookie(self) -> None:
        assert (
            is_authenticated_cookie(None, secret="s", expected_username="admin")
            is False
        )

    def test_is_authenticated_cookie_false_for_wrong_user(self) -> None:
        cfg = FormAuthConfig(username="admin", password="pw", secret="secret")
        cookie = cfg.create_session_cookie_value()
        assert (
            is_authenticated_cookie(
                cookie, secret=cfg.secret, expected_username="other"
            )
            is False
        )

    def test_is_authenticated_cookie_true_for_expected_user(self) -> None:
        cfg = FormAuthConfig(username="admin", password="pw", secret="secret")
        cookie = cfg.create_session_cookie_value()
        assert (
            is_authenticated_cookie(
                cookie, secret=cfg.secret, expected_username="admin"
            )
            is True
        )
