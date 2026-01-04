"""Integration tests for workers refresh behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import stemtrace.server.api.routes as routes
from stemtrace.server.fastapi.router import create_router
from stemtrace.server.store import GraphStore, WorkerRegistry


class _FakeInspect:
    """Minimal Celery Inspect stub for testing refresh logic."""

    def __init__(self, ping_result: dict[str, Any] | None) -> None:
        self._ping_result = ping_result

    def ping(self) -> dict[str, Any] | None:
        return self._ping_result


class TestWorkersRefresh:
    """Tests for /api/workers?refresh=true."""

    def test_workers_refresh_true_without_broker_url_does_not_error(self) -> None:
        """refresh=true is safe when broker_url is not provided."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 12345, ["tasks.add"])

        router = create_router(
            store=store, worker_registry=worker_registry, broker_url=None
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/workers?refresh=true")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    def test_workers_refresh_true_updates_status_from_inspect(
        self, monkeypatch: Any
    ) -> None:
        """refresh=true marks workers online when Celery reports them active."""
        store = GraphStore()
        worker_registry = WorkerRegistry()
        worker_registry.register_worker("worker-1", 12345, ["tasks.add"])
        worker_registry.mark_shutdown("worker-1", 12345)  # offline before refresh

        started_at = datetime.now(UTC)
        fake = _FakeInspect(
            ping_result={
                "celery@worker-1": {"ok": "pong"},
                "worker-2": {"ok": "pong"},
            }
        )
        monkeypatch.setattr(routes, "_get_inspector", lambda _url, **_: fake)

        router = create_router(
            store=store,
            worker_registry=worker_registry,
            broker_url="redis://localhost:6379/0",
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/api/workers?refresh=true")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

        worker = data["workers"][0]
        assert worker["hostname"] == "worker-1"
        assert worker["status"] == "online"
        # last_seen should be updated to "now-ish"
        assert datetime.fromisoformat(worker["last_seen"]) >= started_at
