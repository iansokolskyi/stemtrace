"""With the login page on, a request that isn't the key needs a session and the dependency."""

# No ``from __future__ import annotations``: FastAPI 0.112 resolves a callable
# object's string annotations without its module globals.

import base64
import time
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import OAuth2PasswordBearer
from fastapi.testclient import TestClient

import stemtrace
from stemtrace.server.fastapi.api_key import API_KEY_ENV_VAR
from stemtrace.server.fastapi.form_auth import (
    SESSION_SECRET_MISMATCH_REASON,
    sign_session,
)
from stemtrace.server.fastapi.login_throttle import LOGIN_IP_MAX_FAILURES

KEY = "0123456789abcdef0123456789abcdef"
BEARER = {"Authorization": f"Bearer {KEY}"}
WRONG = {"Authorization": "Bearer " + "f" * len(KEY)}
JUNK = {"Authorization": "Bearer x"}
APP_TOKEN = {"Authorization": "Bearer app-issued-token"}
BASIC = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
API = "/stemtrace/api/tasks"

_oauth2 = OAuth2PasswordBearer(tokenUrl="token")
checked: list[str] = []


async def _current_user(token: Annotated[str, Depends(_oauth2)]) -> str:
    checked.append(token)
    if token != "app-issued-token":
        raise HTTPException(status_code=401, detail="Bad token")
    return "user"


async def _admit_everything() -> None:
    checked.append("admitted")


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    checked.clear()


def _client(dependency: Any) -> TestClient:
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=dependency,
        login_username="admin",
        login_password="secret",  # NOSONAR - test credential only
        login_secret="test-secret-test-secret",
    )
    return TestClient(app)


def _sign_in(client: TestClient) -> None:
    resp = client.post(
        "/stemtrace/login",
        data={"username": "admin", "password": "secret", "next": "/stemtrace/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_a_junk_bearer_header_does_not_skip_the_session_check() -> None:
    with _client(Depends(_admit_everything)) as client:
        junk = client.get(API, headers=JUNK)
        plain = client.get(API)

    assert (junk.status_code, plain.status_code) == (401, 401)
    assert junk.json() == plain.json() == {"detail": "Authentication required"}
    assert checked == []


def test_wrong_keys_of_the_keys_length_without_a_session_are_refused_and_counted() -> (
    None
):
    with _client(Depends(_admit_everything)) as client:
        codes = [client.get(API, headers=WRONG).status_code for _ in range(10)]
        locked = client.get(API, headers=BEARER)

    assert codes == [401] * (LOGIN_IP_MAX_FAILURES - 1) + [429] * (
        10 - LOGIN_IP_MAX_FAILURES + 1
    )
    assert locked.status_code == 429
    assert checked == []


def test_a_request_that_isnt_the_key_needs_a_session_and_the_dependency() -> None:
    with _client(Depends(_current_user)) as client:
        assert client.get(API, headers=APP_TOKEN).status_code == 401
        _sign_in(client)
        assert client.get(API, headers=APP_TOKEN).status_code == 200
        refused = client.get(API, headers=JUNK)
        assert (refused.status_code, refused.json()) == (401, {"detail": "Bad token"})
        assert client.get(API).status_code == 401


def test_the_key_needs_neither_a_session_nor_the_dependency() -> None:
    with _client(Depends(_current_user)) as client:
        assert client.get(API, headers=BEARER).status_code == 200

    assert checked == []


def test_basic_auth_helper_with_the_login_page_needs_a_session_too() -> None:
    with _client(stemtrace.require_basic_auth("admin", "pw")) as client:
        assert client.get(API, headers=BASIC).status_code == 401
        _sign_in(client)
        assert client.get(API, headers=BASIC).status_code == 200
        wrong_basic = {"Authorization": "Basic " + base64.b64encode(b"a:b").decode()}
        assert client.get(API, headers=wrong_basic).status_code == 401
        assert client.get(API, headers=BEARER).status_code == 200


def test_a_session_signed_by_another_process_says_why_it_was_refused() -> None:
    """As the middleware does for a request without the key: the 401 carries the reason."""
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=Depends(_admit_everything),
        login_username="admin",
        login_password="secret",  # NOSONAR - test credential only
    )  # no login_secret: this process generates its own
    now = time.time()
    foreign = sign_session(
        {"u": "admin", "exp": int(now) + 3600, "iat": now + 1}, "another-secret"
    )

    with TestClient(app) as client:
        resp = client.get(
            API, headers={**JUNK, "Cookie": f"stemtrace_session={foreign}"}
        )

    assert resp.status_code == 401
    assert resp.json() == {
        "detail": "Authentication required",
        "reason": SESSION_SECRET_MISMATCH_REASON,
    }
