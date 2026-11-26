"""``/api/graphs`` names a group's members from their arguments off the event loop.

With ``node_alias_from_arguments`` set and every member resolving to one value, the list
resolves every member's name (an event scan and a ``str()`` each): about 175,000 profile
events on the loop for 8,800 members (see ``test_summary_routes_off_loop.py``). That work
runs in a worker thread, and with the option off nothing changes.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.server.api import routes
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
MEMBERS = 8_800


def _app(key: str | None) -> FastAPI:
    store = GraphStore(max_nodes=MEMBERS + 100)
    for i in range(MEMBERS):
        store.add_event(
            TaskEvent(
                task_id=f"m{i}",
                name="app.fetch",
                state=TaskState.SUCCESS,
                timestamp=_T0 + timedelta(seconds=i),
                group_id="G",
                kwargs={"site": "shop.eu"},
            )
        )
    app = FastAPI()
    app.include_router(create_api_router(store, node_alias_from_arguments=key))
    return app


async def _list(app: FastAPI) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/graphs?limit=50")
    assert response.status_code == 200
    return dict(response.json()["graphs"][0]["flow"])


@pytest.mark.parametrize(("key", "expected_off_loop"), [("site", True), (None, None)])
async def test_list_graphs_resolves_member_aliases_in_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch, key: str | None, expected_off_loop: bool | None
) -> None:
    loop_thread = threading.get_ident()
    member_threads: list[int] = []
    real = routes._resolve_node_alias

    def recording(node: Any, alias_key: str | None) -> str:
        if node.task_id.startswith("m"):
            member_threads.append(threading.get_ident())
        return real(node, alias_key)

    monkeypatch.setattr(routes, "_resolve_node_alias", recording)

    flow = await _list(_app(key))

    if expected_off_loop:
        assert flow["member_count"] == MEMBERS
        assert flow["member_name"] == "eu"
        assert len(member_threads) == MEMBERS
        assert loop_thread not in member_threads
    else:
        # Without a key the members are never resolved: names come from the store pass
        assert member_threads == []
        assert (flow["member_count"], flow["member_name"]) == (MEMBERS, "fetch")
