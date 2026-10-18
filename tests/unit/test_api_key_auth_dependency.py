"""API key plus a host ``auth_dependency``: a valid key or the host's own auth passes.

The key skips the host's dependency (and its sub-dependencies) entirely; everything
else goes through the dependency as it would without a key. The fake dependency
counts its runs, so "skipped" is a number, not an assumption.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import stemtrace
from stemtrace.server.fastapi.api_key import API_KEY_ENV_VAR
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.fastapi.login_throttle import LOGIN_IP_MAX_FAILURES

if TYPE_CHECKING:
    from collections.abc import Iterator

KEY = "0123456789abcdef0123456789abcdef"
BEARER = {"Authorization": f"Bearer {KEY}"}
WRONG = {"Authorization": "Bearer not-the-key-at-all-0000"}
HOST_SECRET = "host-secret"  # NOSONAR - test credential only
HOST = {"X-Host-Credentials": HOST_SECRET}
API_PATHS = ["/stemtrace/api/tasks", "/stemtrace/api/health", "/stemtrace/api/graphs"]


class HostAuth:
    """A host dependency with a sub-dependency, counting how often each runs."""

    def __init__(self, header: str = "x-host-credentials", prefix: str = "") -> None:
        self.reads = 0
        self.checks = 0

        def read_credentials(request: Request) -> str:
            self.reads += 1
            return request.headers.get(header, "").removeprefix(prefix)

        def check(credentials: str = Depends(read_credentials)) -> None:
            self.checks += 1
            if credentials != HOST_SECRET:
                raise HTTPException(
                    status_code=401,
                    detail="Host login required",
                    headers={"WWW-Authenticate": "Host"},
                )

        self.depends = Depends(check)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        API_KEY_ENV_VAR,
        "STEMTRACE_LOGIN_USERNAME",
        "STEMTRACE_LOGIN_PASSWORD",
        "STEMTRACE_LOGIN_SECRET",
        "STEMTRACE_TRANSPORT_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _app(**kwargs: Any) -> FastAPI:
    app = FastAPI()
    stemtrace.init_app(
        app, broker_url="memory://", embedded_consumer=False, serve_ui=False, **kwargs
    )
    return app


def _form_kwargs() -> dict[str, str]:
    return {
        "login_username": "admin",
        "login_password": "secret",  # NOSONAR - test credential only
        "login_secret": "test-secret-test-secret",
    }


def _login(client: TestClient) -> None:
    resp = client.post(
        "/stemtrace/login",
        data={"username": "admin", "password": "secret", "next": "/stemtrace/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _ws_subprotocols(key: str) -> list[str]:
    encoded = base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
    return ["stemtrace", f"stemtrace.bearer.{encoded}"]


def _assert_ws_denied(client: TestClient, **kwargs: Any) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/stemtrace/ws", **kwargs),
    ):
        pytest.fail("the WebSocket handshake was accepted")
    assert exc_info.value.code == 1008


@pytest.fixture
def host() -> HostAuth:
    return HostAuth()


@pytest.fixture
def key_and_host(host: HostAuth) -> Iterator[TestClient]:
    with TestClient(_app(api_key=KEY, auth_dependency=host.depends)) as client:
        yield client


@pytest.fixture
def form_key_and_host(host: HostAuth) -> Iterator[TestClient]:
    app = _app(api_key=KEY, auth_dependency=host.depends, **_form_kwargs())
    with TestClient(app) as client:
        yield client


# --- key + auth_dependency ----------------------------------------------------


@pytest.mark.parametrize("path", API_PATHS)
def test_key_and_host_valid_key_skips_the_dependency(
    key_and_host: TestClient, host: HostAuth, path: str
) -> None:
    resp = key_and_host.get(path, headers=BEARER)

    assert resp.status_code == 200
    assert (host.checks, host.reads) == (0, 0)


def test_key_and_host_host_credentials_pass_without_a_key(
    key_and_host: TestClient, host: HostAuth
) -> None:
    """The dashboard keeps working for users of the host's own auth."""
    resp = key_and_host.get("/stemtrace/api/tasks", headers=HOST)

    assert resp.status_code == 200
    assert (host.checks, host.reads) == (1, 1)


def test_key_and_host_no_credentials_get_the_dependencys_own_401(
    key_and_host: TestClient, host: HostAuth
) -> None:
    resp = key_and_host.get("/stemtrace/api/tasks")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Host login required"}
    assert resp.headers["www-authenticate"] == "Host"
    assert host.checks == 1


def test_key_and_host_wrong_key_falls_through_to_the_dependency(
    key_and_host: TestClient, host: HostAuth
) -> None:
    refused = key_and_host.get("/stemtrace/api/tasks", headers=WRONG)
    admitted = key_and_host.get("/stemtrace/api/tasks", headers={**WRONG, **HOST})

    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == "Host"
    assert admitted.status_code == 200
    assert host.checks == 2


def test_key_and_host_wrong_key_admitted_by_the_dependency_is_not_a_failed_attempt(
    key_and_host: TestClient,
) -> None:
    for _ in range(LOGIN_IP_MAX_FAILURES * 2):
        resp = key_and_host.get("/stemtrace/api/tasks", headers={**WRONG, **HOST})
        assert resp.status_code == 200

    assert key_and_host.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
    assert key_and_host.get("/stemtrace/api/tasks", headers=WRONG).status_code == 401


def test_key_and_host_bearer_scheme_of_the_dependency_never_locks_the_key_out() -> None:
    """A host that authenticates with Bearer JWTs sends them to the key check too."""
    host = HostAuth(header="authorization", prefix="Bearer ")
    jwt = {"Authorization": f"Bearer {HOST_SECRET}"}

    with TestClient(_app(api_key=KEY, auth_dependency=host.depends)) as client:
        for _ in range(LOGIN_IP_MAX_FAILURES * 3):
            assert client.get("/stemtrace/api/tasks", headers=jwt).status_code == 200
        checks = host.checks

        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        assert host.checks == checks


def test_key_and_host_wrong_keys_the_dependency_rejects_lock_out_the_ip(
    key_and_host: TestClient, host: HostAuth
) -> None:
    codes = [
        key_and_host.get("/stemtrace/api/tasks", headers=WRONG).status_code
        for _ in range(LOGIN_IP_MAX_FAILURES)
    ]

    assert codes == [401] * (LOGIN_IP_MAX_FAILURES - 1) + [429]
    runs = host.checks
    locked = key_and_host.get("/stemtrace/api/tasks", headers=BEARER)
    # Locked out: the key isn't compared, so even the right one doesn't skip the dependency.
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "30"
    assert locked.json() == {"detail": "Too many failed API key attempts"}
    assert host.checks == runs + 1
    # The host's own credentials still pass: the lockout is for keys only.
    assert (
        key_and_host.get("/stemtrace/api/tasks", headers={**WRONG, **HOST}).status_code
        == 200
    )
    # A request without a key is no attempt: the dependency's 401, not a 429.
    assert key_and_host.get("/stemtrace/api/tasks").status_code == 401


def test_key_and_host_liveness_probe_takes_a_key_or_the_dependency(
    key_and_host: TestClient, host: HostAuth
) -> None:
    assert key_and_host.get("/stemtrace/api/health/live").status_code == 401
    assert host.checks == 1

    assert key_and_host.get("/stemtrace/api/health/live", headers=BEARER).json() == {
        "status": "ok"
    }
    assert host.checks == 1
    assert (
        key_and_host.get("/stemtrace/api/health/live", headers=HOST).status_code == 200
    )


def test_key_and_host_websocket_needs_the_key_and_never_runs_the_dependency(
    key_and_host: TestClient, host: HostAuth
) -> None:
    """The dependency never covered the WebSocket: the key (or a session) does."""
    with key_and_host.websocket_connect("/stemtrace/ws", headers=BEARER):
        pass
    with key_and_host.websocket_connect(
        "/stemtrace/ws", subprotocols=_ws_subprotocols(KEY)
    ) as ws:
        assert ws.accepted_subprotocol == "stemtrace"
    _assert_ws_denied(key_and_host)
    _assert_ws_denied(key_and_host, headers=HOST)
    _assert_ws_denied(key_and_host, headers=WRONG)

    assert (host.checks, host.reads) == (0, 0)


def test_key_and_basic_auth_helper_both_open_the_api() -> None:
    """``require_basic_auth`` reads the Authorization header the key shares."""
    app = _app(api_key=KEY, auth_dependency=stemtrace.require_basic_auth("ian", "pw"))
    basic = {"Authorization": "Basic " + base64.b64encode(b"ian:pw").decode()}

    with TestClient(app) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 200
        assert client.get("/stemtrace/api/tasks", headers=basic).status_code == 200
        refused = client.get("/stemtrace/api/tasks")
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == "Basic"


def test_host_only_is_unchanged_by_a_bearer_header() -> None:
    host = HostAuth()
    with TestClient(_app(auth_dependency=host.depends)) as client:
        assert client.get("/stemtrace/api/tasks", headers=BEARER).status_code == 401
        assert client.get("/stemtrace/api/tasks", headers=HOST).status_code == 200
        assert client.get("/stemtrace/api/health/live").status_code == 401

    assert host.checks == 3


# --- form login + key + auth_dependency ---------------------------------------


def test_form_key_and_host_valid_key_needs_no_cookie_and_skips_the_dependency(
    form_key_and_host: TestClient, host: HostAuth
) -> None:
    resp = form_key_and_host.get("/stemtrace/api/tasks", headers=BEARER)

    assert resp.status_code == 200
    assert (host.checks, host.reads) == (0, 0)


def test_form_key_and_host_session_cookie_still_goes_through_the_dependency(
    form_key_and_host: TestClient, host: HostAuth
) -> None:
    _login(form_key_and_host)

    refused = form_key_and_host.get("/stemtrace/api/tasks")
    admitted = form_key_and_host.get("/stemtrace/api/tasks", headers=HOST)

    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == "Host"
    assert admitted.status_code == 200
    assert host.checks == 2


def test_form_key_and_host_host_credentials_without_a_session_are_refused(
    form_key_and_host: TestClient, host: HostAuth
) -> None:
    resp = form_key_and_host.get("/stemtrace/api/tasks", headers=HOST)

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Authentication required"}
    assert host.checks == 0


def test_form_key_and_host_wrong_key_still_needs_a_session(
    form_key_and_host: TestClient, host: HostAuth
) -> None:
    """A Bearer request skips the login middleware, so the gate checks the session."""
    resp = form_key_and_host.get("/stemtrace/api/tasks", headers={**WRONG, **HOST})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Authentication required"}
    assert resp.headers["www-authenticate"] == "Bearer"
    assert host.checks == 0

    _login(form_key_and_host)
    with_session = form_key_and_host.get(
        "/stemtrace/api/tasks", headers={**WRONG, **HOST}
    )
    assert with_session.status_code == 200
    assert host.checks == 1


def test_form_key_and_host_websocket_takes_the_key_or_a_session(
    form_key_and_host: TestClient, host: HostAuth
) -> None:
    _assert_ws_denied(form_key_and_host, headers=HOST)
    with form_key_and_host.websocket_connect("/stemtrace/ws", headers=BEARER):
        pass
    _login(form_key_and_host)
    with form_key_and_host.websocket_connect("/stemtrace/ws"):
        pass

    assert host.checks == 0


# --- embedded app with a dependency chain -------------------------------------


def test_embedded_dependency_chain_runs_for_requests_without_a_key() -> None:
    chain: list[str] = []

    def settings(request: Request) -> str:
        chain.append("settings")
        return str(request.app.state.host_secret)

    def current_user(secret: str = Depends(settings), credentials: str = "") -> None:
        chain.append("current_user")
        if credentials != secret:
            raise HTTPException(status_code=401, detail="host login required")

    app = FastAPI()
    app.state.host_secret = HOST_SECRET
    extension = StemtraceExtension(
        "memory://",
        embedded_consumer=False,
        serve_ui=False,
        prefix="/monitoring",
        api_key=KEY,
        auth_dependency=Depends(current_user),
    )
    extension.init_app(app)

    with TestClient(app) as client:
        assert (
            client.get("/monitoring/api/tasks/nope", headers=BEARER).status_code == 404
        )
        assert chain == []

        assert client.get("/monitoring/api/tasks/nope").status_code == 401
        assert chain == ["settings", "current_user"]

        chain.clear()
        resp = client.get(f"/monitoring/api/tasks/nope?credentials={HOST_SECRET}")
        assert resp.status_code == 404  # past auth: the task doesn't exist
        assert chain == ["settings", "current_user"]

        # Overriding the host's dependency, as host test suites do, applies too.
        app.dependency_overrides[current_user] = lambda: None
        chain.clear()
        assert client.get("/monitoring/api/tasks/nope").status_code == 404
        assert chain == []
