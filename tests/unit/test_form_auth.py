"""Unit tests for signed-cookie session auth primitives."""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256
from http.cookies import SimpleCookie
from typing import Any

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
