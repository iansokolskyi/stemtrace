"""Every shape of ``auth_dependency``: a Depends or Security works, anything else is refused at setup."""

# No ``from __future__ import annotations``: FastAPI 0.112 resolves a callable
# object's string annotations without its module globals.

from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.testclient import TestClient

import stemtrace
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server.fastapi.api_key import API_KEY_ENV_VAR

KEY = "0123456789abcdef0123456789abcdef"
BEARER = {"Authorization": f"Bearer {KEY}"}
CREDENTIALS = {"X-Cred": "ok"}
API = "/stemtrace/api/tasks"

runs: list[str] = []


def _check(request: Request) -> None:
    runs.append("ran")
    if request.headers.get("x-cred") != "ok":
        raise HTTPException(status_code=401, detail="no credentials")


async def _async_check(request: Request) -> None:
    _check(request)


class _CallableObject:
    def __call__(self, request: Request) -> None:
        _check(request)


ACCEPTED: dict[str, Any] = {
    "Depends-sync": Depends(_check),
    "Depends-async": Depends(_async_check),
    "Security-with-scopes": Security(_check, scopes=["read"]),
    "Depends-callable-object": Depends(_CallableObject()),
}

# Fail closed at setup, as FastAPI 0.112 does on main (an AttributeError at
# include time); newer FastAPI accepts some of these and fails per request.
REFUSED: dict[str, Any] = {
    "plain-sync-function": _check,
    "plain-async-function": _async_check,
    "callable-object": _CallableObject(),
    "lambda": lambda request: None,
    "non-callable": 5,
    "string": "require_admin",
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    runs.clear()


def _app(**options: Any) -> FastAPI:
    app = FastAPI()
    stemtrace.init_app(
        app, broker_url="memory://", embedded_consumer=False, serve_ui=False, **options
    )
    return app


@pytest.mark.parametrize("shape", sorted(ACCEPTED))
def test_a_dependency_alone_runs_for_every_request(shape: str) -> None:
    with TestClient(_app(auth_dependency=ACCEPTED[shape])) as client:
        assert client.get(API).status_code == 401
        assert client.get(API, headers=CREDENTIALS).status_code == 200
        assert client.get(API, headers=BEARER).status_code == 401

    assert runs == ["ran"] * 3


@pytest.mark.parametrize("shape", sorted(ACCEPTED))
def test_the_key_or_the_dependency_lets_a_request_in(shape: str) -> None:
    with TestClient(_app(auth_dependency=ACCEPTED[shape], api_key=KEY)) as client:
        assert client.get(API).status_code == 401
        assert runs == ["ran"]
        assert client.get(API, headers=BEARER).status_code == 200
        assert runs == ["ran"]  # the key never runs the dependency
        assert client.get(API, headers=CREDENTIALS).status_code == 200
        assert runs == ["ran", "ran"]


def test_a_key_alone_has_no_dependency_to_run() -> None:
    with TestClient(_app(api_key=KEY)) as client:
        assert client.get(API).status_code == 401
        assert client.get(API, headers=BEARER).status_code == 200
        assert client.get(API, headers=CREDENTIALS).status_code == 401

    assert runs == []


@pytest.mark.parametrize("api_key", [None, KEY], ids=["dependency-only", "key-too"])
@pytest.mark.parametrize("shape", sorted(REFUSED))
def test_anything_but_a_depends_is_refused_at_setup(
    shape: str, api_key: str | None
) -> None:
    with pytest.raises(ConfigurationError, match=r"Depends\(\.\.\.\)"):
        _app(auth_dependency=REFUSED[shape], api_key=api_key)


@pytest.mark.parametrize("shape", sorted(REFUSED))
def test_create_router_refuses_anything_but_a_depends(shape: str) -> None:
    with pytest.raises(ConfigurationError, match=r"Depends\(\.\.\.\)"):
        stemtrace.create_router(auth_dependency=REFUSED[shape], api_key=KEY)
