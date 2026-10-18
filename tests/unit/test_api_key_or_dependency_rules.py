"""Rules of the key-or-dependency gate, as reviews of the first attempts found them.

Lockout whatever the dependency refuses with, refusals that never count, wrong keys
in flight at once, the host's ``yield`` dependency, and the OpenAPI schema.
"""

# No ``from __future__ import annotations``: FastAPI 0.112 resolves a callable
# object's string annotations without its module globals.

import asyncio
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import stemtrace
from stemtrace.server.fastapi.api_key import (
    API_KEY_BUSY_RETRY_AFTER_S,
    API_KEY_ENV_VAR,
)
from stemtrace.server.fastapi.login_throttle import LOGIN_IP_MAX_FAILURES

KEY = "0123456789abcdef0123456789abcdef"
BEARER = {"Authorization": f"Bearer {KEY}"}
API = "/stemtrace/api/tasks"
ATTEMPTS = 30


def _wrong_key(i: int) -> dict[str, str]:
    """The ``i``-th distinct wrong key, as long as the real one."""
    return {"Authorization": f"Bearer {i:0{len(KEY)}d}"}


class _AuthRequired(Exception):
    """An app's own exception, which an exception handler turns into a 401."""


async def _fastapi_refusal() -> None:
    raise HTTPException(status_code=401, detail="no")


async def _starlette_refusal() -> None:
    raise StarletteHTTPException(status_code=401, detail="no")


async def _custom_exception_refusal() -> None:
    raise _AuthRequired


async def _forbidden() -> None:
    raise HTTPException(status_code=403, detail="no")


async def _missing_credentials(x_token: Annotated[str, Header()]) -> None:
    """A dependency whose missing credentials surface as a 422."""


async def _admit() -> None:
    """Returns normally: the request is let in."""


async def _unavailable() -> None:
    raise HTTPException(status_code=503, detail="identity provider down")


async def _rate_limited() -> None:
    raise HTTPException(status_code=429, detail="slow down")


async def _login_redirect() -> None:
    raise HTTPException(
        status_code=307, detail="sign in", headers={"Location": "/sign-in"}
    )


async def _bad_request() -> None:
    raise HTTPException(status_code=400, detail="malformed token")


async def _not_found() -> None:
    raise HTTPException(status_code=404, detail="unknown token")


async def _crashing_decoder() -> None:
    raise RuntimeError("decoder crashed")


# kind -> (dependency, what it answers a wrong key with, what the failure that
# fills the lockout is answered with: 429, but an unhandled exception stays 500)
REFUSALS: dict[str, tuple[Any, int, int]] = {
    "fastapi-401": (Depends(_fastapi_refusal), 401, 429),
    "starlette-401": (Depends(_starlette_refusal), 401, 429),
    "custom-exception-401": (Depends(_custom_exception_refusal), 401, 429),
    "403": (Depends(_forbidden), 403, 429),
    "422": (Depends(_missing_credentials), 422, 429),
    "307-login-redirect": (Depends(_login_redirect), 307, 429),
    "400": (Depends(_bad_request), 400, 429),
    "404": (Depends(_not_found), 404, 429),
    "5xx": (Depends(_unavailable), 503, 429),
    "its-own-429": (Depends(_rate_limited), 429, 429),
    "unhandled-exception": (Depends(_crashing_decoder), 500, 500),
}


def _build(
    dependency: Any, *, api_key: str | None = KEY
) -> tuple[FastAPI, stemtrace.StemtraceExtension]:
    app = FastAPI()

    @app.exception_handler(_AuthRequired)
    async def _render(request: Request, exc: _AuthRequired) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": "custom"})

    extension = stemtrace.init_app(
        app,
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        api_key=api_key,
        auth_dependency=dependency,
    )
    return app, extension


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)


@pytest.mark.parametrize("kind", sorted(REFUSALS))
def test_wrong_keys_lock_the_key_out_whatever_the_dependency_answers_with(
    kind: str,
) -> None:
    dependency, refused, filling = REFUSALS[kind]
    app, _ = _build(dependency)

    with TestClient(app, raise_server_exceptions=False) as client:
        codes = [
            client.get(API, headers=_wrong_key(i), follow_redirects=False).status_code
            for i in range(ATTEMPTS)
        ]
        locked = client.get(API, headers=BEARER)

    # The failure that fills the lockout is answered 429, as in key-only mode.
    assert codes == (
        [refused] * (LOGIN_IP_MAX_FAILURES - 1)
        + [filling]
        + [429] * (ATTEMPTS - LOGIN_IP_MAX_FAILURES)
    )
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


def test_a_request_the_dependency_admits_never_locks_the_key_out() -> None:
    app, _ = _build(Depends(_admit))

    with TestClient(app) as client:
        codes = {
            client.get(API, headers=_wrong_key(i)).status_code for i in range(ATTEMPTS)
        }
        valid = client.get(API, headers=BEARER)

    assert codes == {200}
    assert valid.status_code == 200


def test_an_endpoint_error_after_the_dependency_admitted_is_no_failed_key() -> None:
    """``limit=0`` is a 422 of the endpoint, not a refusal of the app's token."""
    app, _ = _build(Depends(_admit))

    with TestClient(app) as client:
        codes = {
            client.get(f"{API}?limit=0", headers=_wrong_key(i)).status_code
            for i in range(ATTEMPTS)
        }
        valid = client.get(API, headers=BEARER)
        own = client.get(API, headers=_wrong_key(1))

    assert codes == {422}
    assert (valid.status_code, own.status_code) == (200, 200)


def test_the_dependency_runs_once_per_request() -> None:
    """The admission marker shares the dependency's cached result instead of calling it again."""
    calls: list[str] = []

    async def counted_user(request: Request) -> str:
        calls.append(request.headers.get("authorization", ""))
        return "user"

    app, _ = _build(Depends(counted_user))
    own = _wrong_key(1)

    with TestClient(app) as client:
        assert client.get(API, headers=own).status_code == 200
        assert client.get(API, headers={"X-Other": "1"}).status_code == 200

    assert calls == [own["Authorization"], ""]


async def test_wrong_keys_in_flight_at_once_never_test_more_keys_than_a_lockout_allows() -> (
    None
):
    """The dependency awaits before it refuses, so every wrong key is in flight together."""
    entered = 0
    release = asyncio.Event()

    async def slow_refusal() -> None:
        nonlocal entered
        entered += 1
        await release.wait()
        raise HTTPException(status_code=401, detail="no")

    app, extension = _build(Depends(slow_refusal))
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tasks = [
            asyncio.create_task(client.get(API, headers=_wrong_key(i)))
            for i in range(ATTEMPTS)
        ]
        for _ in range(10_000):
            if entered + sum(task.done() for task in tasks) == ATTEMPTS:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("the burst never settled")
        in_flight = entered
        release.set()
        responses = await asyncio.gather(*tasks)
        locked = await client.get(API, headers=BEARER)

    codes = [response.status_code for response in responses]
    assert in_flight == LOGIN_IP_MAX_FAILURES
    assert codes.count(401) == LOGIN_IP_MAX_FAILURES - 1
    assert codes.count(429) == ATTEMPTS - LOGIN_IP_MAX_FAILURES + 1
    assert all(
        int(response.headers["retry-after"]) > 0
        for response in responses
        if response.status_code == 429
    )
    assert locked.status_code == 429
    throttle = extension.auth_throttle
    assert throttle is not None
    assert throttle._ip_in_flight == {}


def test_a_locked_out_ip_gets_429_even_for_a_token_the_dependency_would_accept() -> (
    None
):
    """Documented: a locked-out client's tokens of the key's length aren't judged."""
    accepted = "a" * len(KEY)

    async def accepts_one_token(request: Request) -> None:
        if request.headers.get("authorization") != f"Bearer {accepted}":
            raise HTTPException(status_code=401, detail="no")

    app, _ = _build(Depends(accepts_one_token))
    own = {"Authorization": f"Bearer {accepted}"}

    with TestClient(app) as client:
        assert client.get(API, headers=own).status_code == 200
        for i in range(LOGIN_IP_MAX_FAILURES):
            client.get(API, headers=_wrong_key(i))
        locked = client.get(API, headers=own)
        other = client.get(API, headers={"Authorization": "Bearer short"})

    assert locked.status_code == 429
    assert other.status_code == 401


async def test_tokens_in_flight_cover_the_failures_left_for_the_right_key_too() -> None:
    """The dependency admits, so nothing fails; the slots are held until the response."""
    entered = 0
    release = asyncio.Event()

    async def slow_admit() -> None:
        nonlocal entered
        entered += 1
        await release.wait()

    app, _ = _build(Depends(slow_admit))
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tasks = [
            asyncio.create_task(client.get(API, headers=_wrong_key(i)))
            for i in range(LOGIN_IP_MAX_FAILURES)
        ]
        for _ in range(10_000):
            if entered == LOGIN_IP_MAX_FAILURES:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("the requests never reached the dependency")
        # Bounded: without the cap these wait for the dependency, which is blocked.
        sixth = await asyncio.wait_for(client.get(API, headers=_wrong_key(99)), 5)
        right_key = await asyncio.wait_for(client.get(API, headers=BEARER), 5)
        release.set()
        responses = await asyncio.gather(*tasks)
        after = await client.get(API, headers=BEARER)

    busy = str(API_KEY_BUSY_RETRY_AFTER_S)
    assert (sixth.status_code, sixth.headers["retry-after"]) == (429, busy)
    assert (right_key.status_code, right_key.headers["retry-after"]) == (429, busy)
    assert [response.status_code for response in responses] == [200] * entered
    assert after.status_code == 200


async def test_a_cancelled_request_frees_its_in_flight_slot() -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def hanging_check() -> None:
        entered.set()
        await gate.wait()

    app, extension = _build(Depends(hanging_check))
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        task = asyncio.create_task(client.get(API, headers=_wrong_key(1)))
        await asyncio.wait_for(entered.wait(), timeout=5)
        throttle = extension.auth_throttle
        assert throttle is not None
        assert throttle._ip_in_flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert throttle._ip_in_flight == {}


def test_every_kind_of_request_with_a_wrong_key_frees_its_in_flight_slot() -> None:
    app, extension = _build(Depends(_fastapi_refusal))

    with TestClient(app) as client:
        assert client.get(API, headers=_wrong_key(1)).status_code == 401
        assert client.head(API, headers=_wrong_key(2)).status_code == 401
        assert client.post(API, headers=_wrong_key(3)).status_code == 405
        assert (
            client.get("/stemtrace/api/nothing", headers=_wrong_key(4)).status_code
            == 404
        )

    throttle = extension.auth_throttle
    assert throttle is not None
    assert throttle._ip_in_flight == {}


_user: ContextVar[str | None] = ContextVar("user", default=None)


@pytest.mark.parametrize("api_key", [None, KEY], ids=["no-key", "key"])
def test_yield_dependency_binds_the_user_and_sees_the_endpoints_error(
    api_key: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dependency runs natively around the endpoint, with or without a key."""
    seen_by_endpoint: list[str | None] = []
    audited: list[int] = []

    async def audited_user(
        x_user: Annotated[
            str | None, Depends(APIKeyHeader(name="X-User", auto_error=False))
        ],
    ) -> AsyncIterator[str]:
        if x_user != "me":
            raise HTTPException(status_code=401, detail="No user")
        _user.set(x_user)
        try:
            yield x_user
        except HTTPException as exc:
            audited.append(exc.status_code)
            raise

    app, extension = _build(Depends(audited_user), api_key=api_key)

    def missing(task_id: str) -> None:
        seen_by_endpoint.append(_user.get())

    monkeypatch.setattr(extension.store, "get_node_with_children", missing)

    with TestClient(app) as client:
        resp = client.get("/stemtrace/api/tasks/gone", headers={"X-User": "me"})

    assert resp.status_code == 404
    assert seen_by_endpoint == ["me"]
    assert audited == [404]


class _CountingScheme(OAuth2PasswordBearer):
    """An OAuth2 scheme that counts how often FastAPI runs it."""

    def __init__(self) -> None:
        super().__init__(tokenUrl="token")
        self.calls = 0

    async def __call__(self, request: Request) -> str | None:
        self.calls += 1
        return await super().__call__(request)


@pytest.mark.parametrize("method", ["get", "head"])
def test_a_valid_key_never_runs_the_hosts_dependency_or_its_security_scheme(
    method: str,
) -> None:
    scheme = _CountingScheme()
    checks: list[str] = []

    async def current_user(token: Annotated[str, Depends(scheme)]) -> str:
        checks.append(token)
        return "user"

    app, _ = _build(Depends(current_user))

    with TestClient(app) as client:
        keyed = client.request(method, API, headers=BEARER)
        assert (keyed.status_code, scheme.calls, checks) == (200, 0, [])
        own = client.request(method, API, headers={"Authorization": "Bearer app-token"})
        assert (own.status_code, scheme.calls, checks) == (200, 1, ["app-token"])


def _schema(api_key: str | None) -> dict[str, Any]:
    scheme = OAuth2PasswordBearer(tokenUrl="token")

    async def current_user(token: Annotated[str, Depends(scheme)]) -> str:
        return token

    app, _ = _build(Depends(current_user), api_key=api_key)
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
    assert resp.status_code == 200
    result: dict[str, Any] = resp.json()
    return result


def test_openapi_keeps_the_hosts_security_schemes_and_hides_the_key_copies() -> None:
    baseline = _schema(None)
    with_key = _schema(KEY)

    # Equality alone would pass if both schemas lost the scheme, so pin it.
    assert set(baseline["components"]["securitySchemes"]) == {"OAuth2PasswordBearer"}
    operations = [
        (path, method, operation)
        for path, item in with_key["paths"].items()
        if path.startswith("/stemtrace/api/")
        for method, operation in item.items()
    ]
    assert operations
    assert all(
        operation.get("security") == [{"OAuth2PasswordBearer": []}]
        for _, _, operation in operations
    )
    assert with_key == baseline
