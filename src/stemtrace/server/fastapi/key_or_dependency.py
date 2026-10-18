"""Let the API key *or* a custom ``auth_dependency`` in to the JSON API.

FastAPI runs every dependency of a route, so the API routes are served twice: a
hidden copy behind the key, matched only by a request that presents the key,
then a copy behind ``auth_dependency`` for every other request. The app's
dependency keeps FastAPI's own rules (sub-dependencies, ``dependency_overrides``,
``yield``) and never runs for a key request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from fastapi import APIRouter
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.routing import Match

from stemtrace.server.fastapi.api_key import (
    GATE_SCOPE_KEY,
    ApiKeyGuard,
    KeyStatus,
    api_key_dependency,
    bearer_token,
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

#: Responses that mean the app's dependency refused the request.
_REFUSED: Final[frozenset[int]] = frozenset({401, 403})


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
    refuses the request too (401 or 403), so the app's own Bearer tokens never
    lock its users out. A token of another length than the key is never a key
    attempt and goes straight to the dependency.
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

        async def _send(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message["status"] in _REFUSED
            ):
                self.guard.record_failure(_client_ip(scope))
            await send(message)

        await super().handle(scope, receive, _send)


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
        copies.add_api_route(
            route.path,
            route.endpoint,
            response_model=route.response_model,
            status_code=route.status_code,
            tags=route.tags,
            dependencies=route.dependencies,
            summary=route.summary,
            description=route.description,
            response_description=route.response_description,
            responses=route.responses,
            deprecated=route.deprecated,
            methods=route.methods,
            operation_id=route.operation_id,
            response_model_include=route.response_model_include,
            response_model_exclude=route.response_model_exclude,
            response_model_by_alias=route.response_model_by_alias,
            response_model_exclude_unset=route.response_model_exclude_unset,
            response_model_exclude_defaults=route.response_model_exclude_defaults,
            response_model_exclude_none=route.response_model_exclude_none,
            include_in_schema=route.include_in_schema and in_schema,
            response_class=route.response_class,
            name=route.name,
            route_class_override=classes[base],
            callbacks=route.callbacks,
            openapi_extra=route.openapi_extra,
            generate_unique_id_function=route.generate_unique_id_function,
        )
    return copies
