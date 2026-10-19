"""Every shape of ``auth_dependency``: a Depends or Security works, anything else is refused at setup."""

# No ``from __future__ import annotations``: FastAPI 0.112 resolves a callable
# object's string annotations without its module globals.

import functools
from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, Security, params
from fastapi.testclient import TestClient

import stemtrace
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server.fastapi import StemtraceExtension
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
    "Depends-partial": Depends(functools.partial(_check)),
    "Depends-no-cache": Depends(_check, use_cache=False),
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
    # A Depends with no callable passes isinstance: FastAPI resolves it to the
    # wrapper's own annotation (fastapi 0.143.1) and the app boots.
    "empty-Depends": Depends(),
    "Depends-None": Depends(None),
    "empty-Security": Security(),
    "Depends-non-callable": Depends(5),  # type: ignore[arg-type]  # refused at setup
    "Depends-string": Depends("require_admin"),  # type: ignore[arg-type]
    "Security-non-callable": Security(5),  # type: ignore[arg-type]
    "Depends-subclass-without-callable": type("_Marked", (params.Depends,), {})(),
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


def _shape(app: FastAPI) -> tuple[object, ...]:
    return (
        list(app.routes),
        list(app.user_middleware),
        app.router.lifespan_context,
    )


@pytest.mark.parametrize("shape", sorted(REFUSED))
def test_a_refused_dependency_leaves_the_app_unchanged_with_form_login(
    shape: str,
) -> None:
    app = FastAPI()
    before = _shape(app)

    with pytest.raises(ConfigurationError, match=r"Depends\(\.\.\.\)"):
        stemtrace.init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
            auth_dependency=REFUSED[shape],
            login_username="admin",
            login_password="hunter2",
            login_secret="s" * 32,
        )

    assert _shape(app) == before


def test_extension_init_app_refused_dependency_leaves_the_app_unchanged() -> None:
    app = FastAPI()
    before = _shape(app)
    extension = StemtraceExtension(
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        auth_dependency=Depends(),
    )

    with pytest.raises(ConfigurationError, match=r"Depends\(\.\.\.\)"):
        extension.init_app(app)

    assert _shape(app) == before


@pytest.mark.parametrize(
    "helper",
    [
        stemtrace.require_api_key("a-long-enough-secret"),
        stemtrace.require_basic_auth("u", "p"),
    ],
    ids=["require_api_key", "require_basic_auth"],
)
def test_the_require_helpers_are_accepted_at_setup(helper: Any) -> None:
    _app(auth_dependency=helper)
    _app(auth_dependency=helper, api_key=KEY)
