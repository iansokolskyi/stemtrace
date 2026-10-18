"""Let the API key *or* a custom ``auth_dependency`` in to the JSON API.

FastAPI runs every dependency of a route, so the API routes are served twice: a
hidden copy behind the key, matched only by a request that presents the key,
then a copy behind ``auth_dependency`` for every other request. The app's
dependency keeps FastAPI's own rules (sub-dependencies, ``dependency_overrides``,
``yield``) and never runs for a key request. With the login page on, only the key
skips the session check.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from fastapi import APIRouter, Depends
from fastapi.exception_handlers import http_exception_handler
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.routing import Match

from stemtrace.server.fastapi.api_key import (
    GATE_SCOPE_KEY,
    PUBLIC_ROUTE_NAMES,
    ApiKeyGuard,
    KeyStatus,
    api_key_dependency,
    bearer_token,
    too_many_attempts,
)
from stemtrace.server.fastapi.form_auth import (
    SessionStatus,
    check_session_cookie,
    warn_session_secret_mismatch,
)
from stemtrace.server.fastapi.form_auth_middleware import api_unauthenticated_response

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.types import Message, Receive, Scope, Send

    from stemtrace.server.fastapi.form_auth import FormAuthConfig

#: The request's key status, worked out once while routing and shared by both copies.
VERDICT_SCOPE_KEY: Final[str] = "stemtrace.api_key_verdict"

#: Statuses that send a request to the key copy: a valid key, or a client locked
#: out for wrong keys, which the key check answers with 429.
_KEY_COPY_STATUSES: Final[frozenset[KeyStatus]] = frozenset(
    {KeyStatus.VALID, KeyStatus.THROTTLED}
)

#: The wrong-key attempt of the request, so the admitting dependency can end it.
ATTEMPT_SCOPE_KEY: Final[str] = "stemtrace.api_key_attempt"

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

    A Bearer token of the key's length counts toward the client's lockout
    unless the dependency lets the request in. A token of another length is
    never a key attempt and goes straight to the dependency.
    """

    async def _admitted(request: Request, _result: Any = auth_dependency) -> None:
        # ``auth_dependency`` is a Depends (checked in ``create_router``), or
        # FastAPI would read the default as a query parameter and never run it.
        # Runs only after ``auth_dependency`` returned: FastAPI skips a
        # dependency whose own sub-dependency failed (0.112.1 and 0.143.1,
        # ``solve_dependencies``), and caches it, so it runs once per request.
        attempt = request.scope.get(ATTEMPT_SCOPE_KEY)
        if isinstance(attempt, _WrongKey):
            attempt.admit()

    key_copies = _copy_routes(
        api_router, _KeyRoute, guard, form_auth_config, in_schema=False
    )
    dependency_copies = _copy_routes(
        api_router, _DependencyRoute, guard, form_auth_config
    )
    # Order matters: the router takes the first route that matches.
    router.include_router(
        key_copies, dependencies=[api_key_dependency(guard, form_auth_config)]
    )
    router.include_router(dependency_copies, dependencies=[Depends(_admitted)])


class _GuardedRoute(APIRoute):
    """An API route that knows the key guard (set per guard on a subclass)."""

    guard: ClassVar[ApiKeyGuard]
    form_auth: ClassVar[FormAuthConfig | None]


class _KeyRoute(_GuardedRoute):
    """Matches only a request that presents the key (or is locked out)."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        """Match as the route does, unless the request has no key."""
        match, child_scope = super().matches(scope)
        if match is Match.NONE or _verdict(scope, self.guard) in _KEY_COPY_STATUSES:
            return match, child_scope
        return Match.NONE, {}


class _DependencyRoute(_GuardedRoute):
    """Serves the requests without the key; counts wrong keys the app doesn't admit."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle the request: the session (login page on), then the dependency."""
        # The form-login middleware lets a Bearer request skip the session
        # check only if an API route checked it; this route does, below.
        scope[GATE_SCOPE_KEY] = True
        attempt = None
        reply = send
        if _verdict(scope, self.guard) is KeyStatus.INVALID:
            attempt = _WrongKey(self.guard, scope, receive, send)
            scope[ATTEMPT_SCOPE_KEY] = attempt
            reply = attempt.send
        try:
            refusal = self._session_refusal(scope)
            if refusal is None:
                await super().handle(scope, receive, reply)
            else:
                await refusal(scope, receive, reply)
        except Exception:
            # An unhandled error in the dependency (a crashing token decoder)
            # is no admission either; a cancelled request isn't a failure.
            if attempt is not None:
                attempt.fail()
            raise
        finally:
            if attempt is not None:
                attempt.end()

    def _session_refusal(self, scope: Scope) -> Response | None:
        """The 401 for a request without a session, when the login page is on."""
        if self.form_auth is None or self.name in PUBLIC_ROUTE_NAMES:
            return None
        cookie = Request(scope).cookies.get(self.form_auth.cookie_name)
        status = check_session_cookie(cookie, self.form_auth)
        if status is SessionStatus.VALID:
            return None
        mismatch = status is SessionStatus.SECRET_MISMATCH
        if mismatch:
            warn_session_secret_mismatch()
        return api_unauthenticated_response(
            secret_mismatch=mismatch, api_key_configured=True
        )


class _WrongKey:
    """A request with a wrong key of the key's length, until the app's dependency admits it.

    It is in flight until the dependency admits it or its response starts. A
    response before the admission, whatever its status, counts as a failed
    key; the failure that locks the client out is answered 429, as in
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
            wait = self._settle(refused=True)
            if wait:
                self._replaced = True
                await self._answer_locked(wait)
                return
        await self._send(message)

    def admit(self) -> None:
        """End the attempt: the dependency let the request in, so no failure."""
        self._settle(refused=False)

    def fail(self) -> None:
        """Count the failure of a request that ended in an error, if not admitted."""
        self._settle(refused=True)

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
    form_auth_config: FormAuthConfig | None = None,
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
                type(
                    f"{kind.__name__}{base.__name__}",
                    (kind, base),
                    {"guard": guard, "form_auth": form_auth_config},
                ),
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
