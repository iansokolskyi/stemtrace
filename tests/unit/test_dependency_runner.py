"""DependencyRunner runs a FastAPI dependency on demand, as FastAPI would on a route.

The fakes are real FastAPI apps: a gate dependency calls the runner, and the route
it guards has dependencies of its own, so teardown order and shared state are real.
There is no ``from __future__ import annotations`` here: FastAPI must resolve the
annotations of the locally defined dependencies.
"""

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    Security,
)
from fastapi.security import SecurityScopes
from fastapi.testclient import TestClient

from stemtrace.server.fastapi.dependency_runner import DependencyRunner


def _gated_app(
    depends: Any,
    *,
    route_path: str = "/items",
    endpoint_dependencies: list[Any] | None = None,
    prefix: str = "",
) -> FastAPI:
    """An app whose one GET route is guarded by a gate that runs ``depends`` on demand."""
    runner = DependencyRunner(depends)

    async def gate(
        request: Request, response: Response, background_tasks: BackgroundTasks
    ) -> None:
        await runner.run(request, response, background_tasks)

    router = APIRouter(dependencies=[Depends(gate)])

    @router.get(route_path, dependencies=endpoint_dependencies or [])
    async def endpoint() -> dict[str, str]:
        return {"ok": "yes"}

    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return app


def test_run_executes_the_dependency_and_its_sub_dependency_chain() -> None:
    calls: list[str] = []

    def settings(request: Request) -> str:
        calls.append("settings")
        return str(request.app.state.expected)

    def user(
        expected: Annotated[str, Depends(settings)],
        x_user: Annotated[str | None, Header()] = None,
    ) -> None:
        calls.append("user")
        if x_user != expected:
            raise HTTPException(status_code=401, detail="who are you")

    app = _gated_app(Depends(user))
    app.state.expected = "ian"

    with TestClient(app) as client:
        assert client.get("/items", headers={"x-user": "ian"}).status_code == 200
        assert calls == ["settings", "user"]
        assert client.get("/items").status_code == 401


def test_run_dependency_error_reaches_the_client_unchanged() -> None:
    def refuse() -> None:
        raise HTTPException(
            status_code=403, detail="no entry", headers={"WWW-Authenticate": "Custom"}
        )

    with TestClient(_gated_app(Depends(refuse))) as client:
        resp = client.get("/items")

    assert resp.status_code == 403
    assert resp.json() == {"detail": "no entry"}
    assert resp.headers["www-authenticate"] == "Custom"


def test_run_dependency_validation_error_is_a_422_like_on_a_route() -> None:
    def needs_tenant(x_tenant: Annotated[str, Header()]) -> None:
        del x_tenant

    with TestClient(_gated_app(Depends(needs_tenant))) as client:
        resp = client.get("/items")

    assert resp.status_code == 422
    where, name = resp.json()["detail"][0]["loc"]
    # FastAPI 0.112 reports the parameter as x_tenant, later versions as x-tenant.
    assert (where, name.replace("-", "_")) == ("header", "x_tenant")


def test_run_passes_the_routes_path_parameters_to_the_dependency() -> None:
    seen: list[str] = []

    def owner(task_id: str) -> None:
        seen.append(task_id)

    with TestClient(
        _gated_app(Depends(owner), route_path="/tasks/{task_id}", prefix="/api")
    ) as client:
        assert client.get("/api/tasks/abc-1").status_code == 200

    assert seen == ["abc-1"]


def test_run_honors_dependency_overrides_of_the_app() -> None:
    def real() -> None:
        raise HTTPException(status_code=401, detail="real dependency ran")

    app = _gated_app(Depends(real))
    app.dependency_overrides[real] = lambda: None

    with TestClient(app) as client:
        assert client.get("/items").status_code == 200


def test_run_passes_security_scopes_to_the_dependency() -> None:
    scopes_seen: list[list[str]] = []

    def scoped(security_scopes: SecurityScopes) -> None:
        scopes_seen.append(security_scopes.scopes)

    with TestClient(_gated_app(Security(scoped, scopes=["read"]))) as client:
        assert client.get("/items").status_code == 200

    assert scopes_seen == [["read"]]


def test_run_yield_dependencies_clean_up_once_with_the_endpoint_ones() -> None:
    events: list[str] = []

    def auth_session() -> Iterator[str]:
        events.append("auth open")
        yield "session"
        events.append("auth close")

    def endpoint_session() -> Iterator[str]:
        events.append("endpoint open")
        yield "session"
        events.append("endpoint close")

    def auth(session: Annotated[str, Depends(auth_session)]) -> None:
        del session

    app = _gated_app(Depends(auth), endpoint_dependencies=[Depends(endpoint_session)])

    with TestClient(app) as client:
        assert client.get("/items").status_code == 200

    assert sorted(events) == [
        "auth close",
        "auth open",
        "endpoint close",
        "endpoint open",
    ]
    assert events.count("auth close") == 1
    assert events.count("endpoint close") == 1


def test_run_relays_headers_and_cookies_the_dependency_sets() -> None:
    def refresh_session(response: Response) -> None:
        response.headers["X-Session-Refreshed"] = "yes"
        response.set_cookie("session", "one")
        response.set_cookie("csrf", "two")

    with TestClient(_gated_app(Depends(refresh_session))) as client:
        resp = client.get("/items")

    assert resp.json() == {"ok": "yes"}
    assert resp.headers["x-session-refreshed"] == "yes"
    assert resp.headers.get_list("set-cookie") == [
        "session=one; Path=/; SameSite=lax",
        "csrf=two; Path=/; SameSite=lax",
    ]
    # The probe's own body headers must not leak onto the endpoint's response.
    assert resp.headers["content-length"] == str(len(resp.content))


def test_run_relays_background_tasks_the_dependency_adds() -> None:
    audit: list[str] = []

    def audited(background_tasks: BackgroundTasks) -> None:
        background_tasks.add_task(audit.append, "logged")

    with TestClient(_gated_app(Depends(audited))) as client:
        assert client.get("/items").status_code == 200

    assert audit == ["logged"]
