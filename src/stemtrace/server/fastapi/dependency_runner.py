"""Run a host's FastAPI dependency on demand, for the API key gate.

FastAPI resolves every router dependency, with its sub-dependencies, before the
route runs, and a dependency can't skip another. So the gate runs the host's
``auth_dependency`` through the standard request handler of a one-route router.
That resolves sub-dependencies, ``Request`` access, dependency overrides, path
parameters, ``yield`` cleanup and security scopes as on any route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import BackgroundTasks, Request, Response
from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import params

    _RouteHandler = Callable[[Request], Awaitable[Response]]

# Headers of the probe route's own (discarded) response, not set by the dependency.
_PROBE_HEADERS = frozenset({b"content-length", b"content-type"})


async def _probe_endpoint() -> None:
    """Stand-in endpoint: the runner only wants the dependencies to run."""


class DependencyRunner:
    """Run one FastAPI dependency when asked, inside the current request."""

    def __init__(self, depends: params.Depends) -> None:
        """Hold the ``Depends(...)`` or ``Security(...)`` to run."""
        self._depends = depends
        # One handler per (app, route path): path parameters resolve against the
        # path, and dependency overrides against the app. Routes aren't hashable.
        self._handlers: dict[tuple[object, str], _RouteHandler] = {}

    async def run(
        self,
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
    ) -> None:
        """Run the dependency for ``request``; its exceptions propagate unchanged.

        Headers, cookies and background tasks the dependency sets are relayed to
        ``response`` and ``background_tasks``, the ones the calling route returns.

        Args:
            request: The request being handled, with its route in the scope.
            response: The calling route's response, shared by its dependencies.
            background_tasks: The calling route's background tasks.
        """
        path = request.scope["route"].path
        handler = self._handlers.get((request.app, path))
        if handler is None:
            # Verified on fastapi 0.112.1, 0.121.0, 0.130.0 and 0.143.1:
            # APIRoute.get_route_handler (a documented extension point) returns the
            # request -> Response callable that solves the route's dependencies;
            # solve_dependencies itself changed signature across them. A yield
            # dependency's cleanup runs when the request ends (0.130.0+) or, on
            # 0.112.1, as soon as this handler returns.
            probe = APIRoute(
                path,
                _probe_endpoint,
                dependencies=[self._depends],
                dependency_overrides_provider=request.app,
                include_in_schema=False,
            )
            handler = self._handlers[request.app, path] = probe.get_route_handler()
        probe_response = await handler(request)
        response.headers.raw.extend(
            (name, value)
            for name, value in probe_response.headers.raw
            if name not in _PROBE_HEADERS
        )
        probe_tasks = probe_response.background
        if isinstance(probe_tasks, BackgroundTasks):
            background_tasks.tasks.extend(probe_tasks.tasks)
