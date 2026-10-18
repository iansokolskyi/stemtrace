"""Let the API key *or* a custom ``auth_dependency`` in to the JSON API.

FastAPI runs every dependency of a route, so the API routes are served twice: a
hidden copy behind the key, matched only by a request that presents the key,
then a copy behind ``auth_dependency`` for every other request. The app's
dependency keeps FastAPI's own rules (sub-dependencies, ``dependency_overrides``,
``yield``) and never runs for a key request.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from fastapi import APIRouter
from fastapi.exception_handlers import http_exception_handler
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.routing import Match

from stemtrace.server.fastapi.api_key import (
    GATE_SCOPE_KEY,
    ApiKeyGuard,
    KeyStatus,
    api_key_dependency,
    bearer_token,
    too_many_attempts,
)

if TYPE_CHECKING:
    from starlette.types import Message, Receive, Scope, Send

    from stemtrace.server.fastapi.form_auth import FormAuthConfig

#: The request's key status, worked out once while routing and shared by both copies.
VERDICT_SCOPE_KEY: Final[str] = "stemtrace.api_key_verdict"

#: Statuses that send a request to the key copy: a valid key, or a client locked
#: out for wrong keys, which the key check answers with 429.
_KEY_COPY_STATUSES: Final[frozenset[KeyStatus]] = frozenset(
    {KeyStatus.VALID, KeyStatus.THROTTLED}
)

#: Responses that mean the app's dependency refused the request's credentials
#: (422: a required header or query of the dependency is missing).
_REFUSED: Final[frozenset[int]] = frozenset({401, 403, 422})

#: ``add_api_route`` options copied from each route, read from the attribute of the same name.
COPIED_OPTIONS: Final[tuple[str, ...]] = (
    "response_model",
    "status_code",
    "tags",
    "dependencies",
    "summary",
    "description",
    "response_description",
    "responses",
    "deprecated",
    "methods",
    "operation_id",
    "response_model_include",
    "response_model_exclude",
    "response_model_by_alias",
    "response_model_exclude_unset",
    "response_model_exclude_defaults",
    "response_model_exclude_none",
    "response_class",
    "name",
    "callbacks",
    "openapi_extra",
    "generate_unique_id_function",
    "strict_content_type",  # FastAPI 0.132+
)

#: ``add_api_route`` parameters that ``_copy_routes`` sets itself.
SET_OPTIONS: Final[frozenset[str]] = frozenset(
    {"self", "path", "endpoint", "include_in_schema", "route_class_override"}
)

_ADD_API_ROUTE_PARAMETERS: Final[frozenset[str]] = frozenset(
    inspect.signature(APIRouter.add_api_route).parameters
)


def include_key_or_dependency(
    router: APIRouter,
    api_router: APIRouter,
    *,
    guard: ApiKeyGuard,
    form_auth_config: FormAuthConfig | None,
    auth_dependency: Any,
) -> None:
    """Include ``api_router`` in ``router`` so the key or ``auth_dependency`` lets a request in.

    A wrong key counts toward the client's lockout only when the dependency
    refuses the request too, so the app's own Bearer tokens never lock its
    users out. A token of another length than the key is never a key attempt
    and goes straight to the dependency.
    """
    key_copies = _copy_routes(api_router, _KeyRoute, guard, in_schema=False)
    dependency_copies = _copy_routes(api_router, _DependencyRoute, guard)
    # Order matters: the router takes the first route that matches.
    router.include_router(
        key_copies, dependencies=[api_key_dependency(guard, form_auth_config)]
    )
    router.include_router(dependency_copies, dependencies=[auth_dependency])


class _GuardedRoute(APIRoute):
    """An API route that knows the key guard (set per guard on a subclass)."""

    guard: ClassVar[ApiKeyGuard]


class _KeyRoute(_GuardedRoute):
    """Matches only a request that presents the key (or is locked out)."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        """Match as the route does, unless the request has no key."""
        match, child_scope = super().matches(scope)
        if match is Match.NONE or _verdict(scope, self.guard) in _KEY_COPY_STATUSES:
            return match, child_scope
        return Match.NONE, {}


class _DependencyRoute(_GuardedRoute):
    """Serves the requests without the key; counts wrong keys the app refuses too."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle the request, counting a failure if a wrong key is refused."""
        # The form-login middleware lets a Bearer request skip the session
        # check only if an API route checked it; here the dependency does.
        scope[GATE_SCOPE_KEY] = True
        if _verdict(scope, self.guard) is not KeyStatus.INVALID:
            await super().handle(scope, receive, send)
            return
        attempt = _WrongKey(self.guard, scope, receive, send)
        try:
            await super().handle(scope, receive, attempt.send)
        finally:
            attempt.end()


class _WrongKey:
    """A request with a wrong key of the key's length, which the app's dependency judges.

    It is in flight until the response starts. A refusal counts as a failed
    key, and the failure that locks the client out is answered 429, as in
    key-only mode.
    """

    def __init__(
        self, guard: ApiKeyGuard, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Hold the client's in-flight slot, before the request's first await."""
        # Routing checked the client's capacity and nothing has awaited since,
        # so on one event loop no other request slips in between check and hold.
        self._guard = guard
        self._ip = _client_ip(scope)
        self._scope, self._receive, self._send = scope, receive, send
        self._holding = True
        self._replaced = False
        guard.hold(self._ip)

    async def send(self, message: Message) -> None:
        """Pass the response on; replace the one that locks the client out."""
        if self._replaced:
            return
        # Starlette's route runs exception handlers inside itself
        # (``wrap_app_handling_exceptions``, 0.38.6 and 1.7.0), so a handler's
        # response for any exception type passes through here.
        if self._holding and message["type"] == "http.response.start":
            wait = self._settle(refused=message["status"] in _REFUSED)
            if wait:
                self._replaced = True
                await self._answer_locked(wait)
                return
        await self._send(message)

    def end(self) -> None:
        """Free the in-flight slot, whatever happened to the request."""
        self._settle(refused=False)

    def _settle(self, *, refused: bool) -> int:
        """Free the slot once, count a failure if refused; return the lockout wait."""
        if not self._holding:
            return 0
        self._holding = False
        self._guard.release(self._ip)
        return self._guard.record_failure(self._ip) if refused else 0

    async def _answer_locked(self, wait: int) -> None:
        request = Request(self._scope, self._receive)
        response = await http_exception_handler(request, too_many_attempts(wait))
        await response(self._scope, self._receive, self._send)


def _verdict(scope: Scope, guard: ApiKeyGuard) -> KeyStatus:
    """The request's key status, checked once per request and kept in the scope."""
    cached = scope.get(VERDICT_SCOPE_KEY)
    if isinstance(cached, KeyStatus):
        return cached
    token = bearer_token(Headers(scope=scope).get("authorization"))
    if token is not None and not guard.could_be_key(token):
        token = None
    status = guard.peek(token, _client_ip(scope)).status
    scope[VERDICT_SCOPE_KEY] = status
    return status


def _client_ip(scope: Scope) -> str | None:
    client = scope.get("client")
    return str(client[0]) if client else None


def _copy_routes(
    source: APIRouter,
    kind: type[_GuardedRoute],
    guard: ApiKeyGuard,
    *,
    in_schema: bool = True,
) -> APIRouter:
    """Copy every route of ``source`` as a ``kind`` route bound to ``guard``.

    Copies, not ``include_router`` twice: FastAPI 0.143 includes a router by
    reference (``_IncludedRouter``), so both would share one route object. A
    copy keeps its route's class (the 405 ``Allow`` header), and FastAPI keeps
    a route's class when the app includes this router (0.112 and 0.143).
    """
    copies = APIRouter()
    classes: dict[type[APIRoute], type[APIRoute]] = {}
    for route in source.routes:
        if not isinstance(route, APIRoute):
            raise TypeError(f"Expected only API routes, got {route!r}")
        base = type(route)
        if base not in classes:
            # type() returns a plain ``type``; the bases make it an APIRoute.
            classes[base] = cast(
                "type[APIRoute]",
                type(f"{kind.__name__}{base.__name__}", (kind, base), {"guard": guard}),
            )
        options = {
            name: getattr(route, name)
            for name in COPIED_OPTIONS
            if name in _ADD_API_ROUTE_PARAMETERS
        }
        copies.add_api_route(
            route.path,
            route.endpoint,
            include_in_schema=route.include_in_schema and in_schema,
            route_class_override=classes[base],
            **options,
        )
    return copies
