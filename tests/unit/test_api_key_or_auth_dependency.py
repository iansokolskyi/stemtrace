"""With a custom ``auth_dependency``, the API key or the dependency lets a request in.

Matrix: dependency kind (``X-API-Key`` helper, HTTP Basic helper, an OAuth2-style
Bearer token check, a sync check) x credentials (key, dependency's, both,
neither, wrong key of the key's length or another) on the JSON API and the
liveness probe; FastAPI dependency features (sub-dependencies with
``auto_error``, ``dependency_overrides``, ``yield``, callable objects); the
lockout; form login; the OpenAPI schema.
"""

# No ``from __future__ import annotations``: FastAPI 0.112 resolves a callable
# object's string annotations without its module globals.

import base64
from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from fastapi.testclient import TestClient

import stemtrace
from stemtrace.server.fastapi.api_key import API_KEY_ENV_VAR, ApiKeyGuard
from stemtrace.server.fastapi.key_or_dependency import _copy_routes, _KeyRoute
from stemtrace.server.fastapi.login_throttle import LOGIN_IP_MAX_FAILURES, LoginThrottle

KEY = "0123456789abcdef0123456789abcdef"
BEARER = {"Authorization": f"Bearer {KEY}"}
# Same length as the key: a key attempt. Other lengths can't be the key.
WRONG = {"Authorization": "Bearer " + "f" * len(KEY)}
OTHER_LENGTH = {"Authorization": "Bearer not-the-key-and-not-a-token"}
X_KEY = {"X-API-Key": "app-secret-value"}
BASIC = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
JWT = {"Authorization": "Bearer app-issued-token"}

_oauth2 = OAuth2PasswordBearer(tokenUrl="token")


async def _current_user(token: Annotated[str, Depends(_oauth2)]) -> str:
    """An app's own Bearer-token check (stands in for JWT validation)."""
    if token != "app-issued-token":
        raise HTTPException(status_code=401, detail="Bad token")
    return "user"


def _sync_user(
    x_user: Annotated[
        str | None, Depends(APIKeyHeader(name="X-User", auto_error=False))
    ],
) -> str:
    """A synchronous check, to cover both kinds of dependency function."""
    if x_user != "me":
        raise HTTPException(status_code=401, detail="No user")
    return x_user


DEPENDENCIES: dict[str, Any] = {
    "x-api-key": stemtrace.require_api_key("app-secret-value"),
    "basic": stemtrace.require_basic_auth("admin", "pw"),
    "oauth2": Depends(_current_user),
    "sync": Depends(_sync_user),
}
OWN_CREDENTIALS: dict[str, dict[str, str]] = {
    "x-api-key": X_KEY,
    "basic": BASIC,
    "oauth2": JWT,
    "sync": {"X-User": "me"},
}


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)


def _client(kind: str, *, api_key: str | None = KEY) -> TestClient:
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=api_key,
        auth_dependency=DEPENDENCIES[kind],
    )
    return TestClient(app)


@pytest.mark.parametrize("kind", sorted(DEPENDENCIES))
def test_key_or_dependency_lets_a_request_in(kind: str) -> None:
    with _client(kind) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        own = client.get("/stemtrace/api/tasks", headers=OWN_CREDENTIALS[kind])
        assert own.status_code == 200
        assert client.get("/stemtrace/api/tasks").status_code == 401


@pytest.mark.parametrize("kind", sorted(DEPENDENCIES))
@pytest.mark.parametrize("wrong", [WRONG, OTHER_LENGTH], ids=["key-length", "other"])
def test_wrong_key_without_dependency_credentials_is_refused(
    kind: str, wrong: dict[str, str]
) -> None:
    with _client(kind) as client:
        assert client.get("/stemtrace/api/tasks", headers=wrong).status_code == 401


@pytest.mark.parametrize("kind", sorted(DEPENDENCIES))
def test_liveness_probe_takes_the_key_or_the_dependency(kind: str) -> None:
    """The key gate exempts the probe; the dependency still covers it."""
    with _client(kind) as client:
        assert client.get("/stemtrace/api/health/live").status_code == 401
        keyed = client.get("/stemtrace/api/health/live", headers=BEARER)
        assert keyed.status_code == 200
        own = client.get("/stemtrace/api/health/live", headers=OWN_CREDENTIALS[kind])
        assert own.status_code == 200


def test_app_bearer_tokens_never_count_as_failed_keys() -> None:
    """An app whose own auth uses Bearer tokens must not lock itself out."""
    with _client("oauth2") as client:
        codes = {
            client.get("/stemtrace/api/tasks", headers=JWT).status_code
            for _ in range(LOGIN_IP_MAX_FAILURES * 3)
        }
        assert codes == {200}
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200


def test_refused_tokens_of_another_length_never_count_as_failed_keys() -> None:
    """An expired app token is refused by the app, not a guess at the key."""
    with _client("oauth2") as client:
        codes = {
            client.get("/stemtrace/api/tasks", headers=OTHER_LENGTH).status_code
            for _ in range(LOGIN_IP_MAX_FAILURES * 3)
        }
        assert codes == {401}
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200


def test_app_tokens_of_the_key_length_count_only_when_refused() -> None:
    accepted = "a" * len(KEY)

    async def user(token: Annotated[str, Depends(_oauth2)]) -> str:
        if token != accepted:
            raise HTTPException(status_code=401, detail="Bad token")
        return "user"

    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=Depends(user),
    )
    app_token = {"Authorization": f"Bearer {accepted}"}
    with TestClient(app) as client:
        codes = {
            client.get("/stemtrace/api/tasks", headers=app_token).status_code
            for _ in range(LOGIN_IP_MAX_FAILURES * 3)
        }
        assert codes == {200}
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200


@pytest.mark.parametrize("kind", sorted(DEPENDENCIES))
def test_wrong_keys_the_dependency_also_refuses_lock_the_key_out(kind: str) -> None:
    with _client(kind) as client:
        codes = [
            client.get("/stemtrace/api/tasks", headers=WRONG).status_code
            for _ in range(LOGIN_IP_MAX_FAILURES)
        ]
        locked_valid = client.get("/stemtrace/api/tasks", headers=BEARER)
        locked_wrong = client.get("/stemtrace/api/tasks", headers=WRONG)
        own = client.get("/stemtrace/api/tasks", headers=OWN_CREDENTIALS[kind])

    assert codes[:-1] == [401] * (LOGIN_IP_MAX_FAILURES - 1)
    # Right and wrong keys get the same answer while locked out.
    assert locked_valid.status_code == 429
    assert locked_wrong.status_code == 429
    assert "retry-after" in locked_valid.headers
    # The app's own credentials don't depend on the key's lockout.
    assert own.status_code == 200


def test_wrong_keys_the_dependency_accepts_are_not_counted() -> None:
    """A request the dependency lets in is no failed attempt, whatever its Bearer."""
    with _client("x-api-key") as client:
        both = {**WRONG, **X_KEY}
        codes = {
            client.get("/stemtrace/api/tasks", headers=both).status_code
            for _ in range(LOGIN_IP_MAX_FAILURES * 3)
        }
        assert codes == {200}
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200


def test_dependency_check_is_skipped_for_a_valid_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def counting_user(token: Annotated[str, Depends(_oauth2)]) -> str:
        calls.append(token)
        return await _current_user(token)

    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=Depends(counting_user),
    )
    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        assert calls == []
        assert client.get("/stemtrace/api/tasks", headers=JWT).status_code == 200
        assert calls == ["app-issued-token"]


def test_without_a_key_the_dependency_alone_decides() -> None:
    with _client("x-api-key", api_key=None) as client:
        assert client.get("/stemtrace/api/tasks", headers=X_KEY).status_code == 200
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 401


def test_dependency_security_schemes_skip_for_a_valid_key() -> None:
    """``APIKeyHeader(auto_error=True)`` in the dependency doesn't refuse a key request."""

    def strict(key: Annotated[str, Depends(APIKeyHeader(name="X-App-Key"))]) -> str:
        return key

    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=Depends(strict),
    )
    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        own = client.get("/stemtrace/api/tasks", headers={"X-App-Key": "anything"})
        assert own.status_code == 200
        assert client.get("/stemtrace/api/tasks").status_code in (401, 403)


def test_dependency_overrides_still_apply_to_the_wrapped_check() -> None:
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=Depends(_current_user),
    )
    app.dependency_overrides[_current_user] = lambda: "test-user"

    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=JWT).status_code == 200
        wrong = {"Authorization": "Bearer anything-the-override-accepts"}
        assert client.get("/stemtrace/api/tasks", headers=wrong).status_code == 200


def test_generator_dependency_works_with_a_key() -> None:
    events: list[str] = []

    def session_user(
        x_user: Annotated[
            str | None, Depends(APIKeyHeader(name="X-User", auto_error=False))
        ],
    ) -> Iterator[str]:
        if x_user != "me":
            raise HTTPException(status_code=401, detail="No user")
        events.append("open")
        yield x_user
        events.append("close")

    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=Depends(session_user),
    )
    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        assert events == []
        mine = client.get("/stemtrace/api/tasks", headers={"X-User": "me"})
        assert mine.status_code == 200
        assert events == ["open", "close"]
        assert client.get("/stemtrace/api/tasks").status_code == 401


class _HeaderCheck:
    """A callable-object dependency (like FastAPI's security classes)."""

    async def __call__(
        self,
        x_user: Annotated[
            str | None, Depends(APIKeyHeader(name="X-User", auto_error=False))
        ],
    ) -> str:
        if x_user != "me":
            raise HTTPException(status_code=401, detail="No user")
        return x_user


def test_callable_object_dependency_works_with_a_key() -> None:
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=Depends(_HeaderCheck()),
    )

    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        assert (
            client.get("/stemtrace/api/tasks", headers={"X-User": "me"}).status_code
            == 200
        )
        assert client.get("/stemtrace/api/tasks").status_code == 401


def test_form_login_lets_only_the_key_skip_the_session_check() -> None:
    """A request that isn't the key needs a session and the dependency; a 404 means no route checked it."""
    app = FastAPI()
    stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=KEY,
        auth_dependency=Depends(_current_user),
        login_username="admin",
        login_password="secret",  # NOSONAR - test credential only
        login_secret="test-secret-test-secret",
    )
    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        assert client.get("/stemtrace/api/tasks", headers=JWT).status_code == 401
        assert client.get("/stemtrace/api/tasks", headers=WRONG).status_code == 401
        assert client.get("/stemtrace/api/tasks").status_code == 401
        client.post(
            "/stemtrace/login",
            data={"username": "admin", "password": "secret", "next": "/stemtrace/"},
            follow_redirects=False,
        )
        assert client.get("/stemtrace/api/tasks", headers=JWT).status_code == 200


def test_openapi_is_the_same_as_with_the_dependency_alone() -> None:
    """The key's copies of the routes stay out of the schema."""

    def schema(api_key: str | None) -> dict[str, Any]:
        app = FastAPI()
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            api_key=api_key,
            auth_dependency=Depends(_current_user),
        )
        with TestClient(app) as client:
            resp = client.get("/openapi.json")
        assert resp.status_code == 200
        result: dict[str, Any] = resp.json()
        return result

    with_key = schema(KEY)
    assert "/stemtrace/api/tasks" in with_key["paths"]
    assert with_key == schema(None)


def test_copying_a_router_with_a_non_api_route_fails_loudly() -> None:
    """A route the copies can't carry must not silently drop out of the API."""
    source = APIRouter()
    source.add_route("/plain", lambda request: None)
    guard = ApiKeyGuard(KEY, LoginThrottle())

    with pytest.raises(TypeError, match="only API routes"):
        _copy_routes(source, _KeyRoute, guard)
