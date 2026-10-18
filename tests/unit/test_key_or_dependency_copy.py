"""The route copies keep every option of their routes, on every FastAPI version."""

import inspect
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from stemtrace.server.fastapi.api_key import ApiKeyGuard
from stemtrace.server.fastapi.key_or_dependency import (
    COPIED_OPTIONS,
    SET_OPTIONS,
    _copy_routes,
    _KeyRoute,
)
from stemtrace.server.fastapi.login_throttle import LoginThrottle

KEY = "0123456789abcdef0123456789abcdef"

# Parameters of the installed FastAPI, so each CI job checks its own version.
ADD_API_ROUTE_OPTIONS = set(inspect.signature(APIRouter.add_api_route).parameters)

# Options of ``APIRoute`` that ``add_api_route`` doesn't take: FastAPI sets
# ``dependency_overrides_provider`` itself when the app includes the router.
ROUTE_ONLY_OPTIONS = {"dependency_overrides_provider"}


class _Body(BaseModel):
    field: str = ""


def _unique_id(route: APIRoute) -> str:
    return f"copied-{route.name}"


def _noop() -> None:
    """A dependency that does nothing."""


def _endpoint() -> _Body:
    return _Body()


# A non-default value for each copied option: the test fails for an option
# that is missing here, so a new one can't be added without a value to check.
NON_DEFAULTS: dict[str, Any] = {
    "response_model": _Body,
    "status_code": 201,
    "tags": ["tag"],
    "dependencies": [Depends(_noop)],
    "summary": "summary",
    "description": "description",
    "response_description": "response description",
    "responses": {404: {"description": "missing"}},
    "deprecated": True,
    "methods": ["GET", "PUT"],
    "operation_id": "operation",
    "response_model_include": {"field"},
    "response_model_exclude": {"other"},
    "response_model_by_alias": False,
    "response_model_exclude_unset": True,
    "response_model_exclude_defaults": True,
    "response_model_exclude_none": True,
    "response_class": PlainTextResponse,
    "name": "named",
    "callbacks": [APIRouter()],
    "openapi_extra": {"x-extra": 1},
    "generate_unique_id_function": _unique_id,
    "strict_content_type": False,
}


def test_every_add_api_route_option_is_copied_or_set_by_hand() -> None:
    """A FastAPI release that adds an option fails here instead of dropping it silently."""
    unknown = ADD_API_ROUTE_OPTIONS - set(COPIED_OPTIONS) - SET_OPTIONS

    assert unknown == set(), (
        f"add_api_route takes {sorted(unknown)}, which the route copies drop: "
        "add each to COPIED_OPTIONS (and NON_DEFAULTS here) or to SET_OPTIONS"
    )


def test_every_route_only_option_is_ignored_on_purpose() -> None:
    route_options = set(inspect.signature(APIRoute.__init__).parameters) - {"self"}

    assert route_options - ADD_API_ROUTE_OPTIONS == ROUTE_ONLY_OPTIONS


def test_a_copy_keeps_every_option_of_its_route() -> None:
    options = [name for name in COPIED_OPTIONS if name in ADD_API_ROUTE_OPTIONS]
    assert set(NON_DEFAULTS) >= set(options)
    source = APIRouter()
    source.add_api_route(
        "/things", _endpoint, **{name: NON_DEFAULTS[name] for name in options}
    )
    guard = ApiKeyGuard(KEY, LoginThrottle())

    (copy,) = _copy_routes(source, _KeyRoute, guard).routes
    (original,) = source.routes

    assert isinstance(copy, APIRoute)
    assert isinstance(original, APIRoute)
    for name in options:
        assert getattr(copy, name) == getattr(original, name), name
    assert copy.path == original.path
    assert copy.endpoint is original.endpoint
