"""E2E test fixtures.

Provides fixtures for interacting with the stemtrace server and Celery tasks
running in Docker containers.

Prerequisites:
    docker compose -f docker-compose.e2e.yml up -d --wait
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

# Import the E2E Celery app
from tests.e2e.tasks import (
    add,
    aggregate,
    always_fails,
    app,
    create_workflow,
    flaky_task,
    multiply,
    process_item,
    slow_task,
)

# Server URL - defaults to Docker Compose setup
SERVER_URL = os.environ.get("CELERY_FLOW_SERVER_URL", "http://localhost:8000")
# CLI serves API at /stemtrace prefix
API_URL = f"{SERVER_URL}/stemtrace/api"
WS_URL = (
    SERVER_URL.replace("http://", "ws://").replace("https://", "wss://")
    + "/stemtrace/ws"
)


@pytest.fixture(scope="session")
def celery_app():
    """Provide the E2E Celery app."""
    return app


@pytest.fixture(scope="session")
def api_client() -> Generator[httpx.Client, None, None]:
    """Create an HTTP client for the stemtrace API."""
    with httpx.Client(base_url=API_URL, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session")
def server_url() -> str:
    """Return the server URL."""
    return SERVER_URL


@pytest.fixture(scope="session")
def api_url() -> str:
    """Return the API URL."""
    return API_URL


@pytest.fixture(scope="session")
def ws_url() -> str:
    """Return the WebSocket URL."""
    return WS_URL


@pytest.fixture(scope="session")
def check_services_running(api_client: httpx.Client) -> None:
    """Verify that all services are running before tests start."""
    max_retries = 30
    retry_delay = 1.0

    for attempt in range(max_retries):
        try:
            response = api_client.get("/health")
            if response.status_code == 200:
                return
        except httpx.ConnectError:
            pass

        if attempt < max_retries - 1:
            time.sleep(retry_delay)

    pytest.skip(
        f"stemtrace server not available at {API_URL}. "
        "Run: docker compose -f docker-compose.e2e.yml up -d --wait"
    )


def wait_for_task(
    api_client: httpx.Client,
    task_id: str,
    target_states: list[str] | None = None,
    timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Poll the API until task reaches a target state.

    Args:
        api_client: HTTP client for API calls.
        task_id: Task ID to poll.
        target_states: States to wait for. Defaults to terminal states.
        timeout: Maximum time to wait in seconds.
        poll_interval: Time between polls in seconds.

    Returns:
        Task data from the API.

    Raises:
        TimeoutError: If task doesn't reach target state within timeout.
    """
    if target_states is None:
        target_states = ["SUCCESS", "FAILURE", "REVOKED"]

    start_time = time.time()
    last_response = None

    while time.time() - start_time < timeout:
        response = api_client.get(f"/tasks/{task_id}")
        if response.status_code == 200:
            data = response.json()
            last_response = data
            task = data.get("task", {})
            if task.get("state") in target_states:
                return data
        elif response.status_code == 404:
            # Task not yet visible, keep polling
            pass

        time.sleep(poll_interval)

    state = last_response.get("task", {}).get("state") if last_response else "NOT_FOUND"
    raise TimeoutError(
        f"Task {task_id} did not reach {target_states} within {timeout}s. "
        f"Current state: {state}"
    )


def wait_for_graph(
    api_client: httpx.Client,
    root_id: str,
    min_nodes: int = 1,
    timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Poll the API until graph has expected number of nodes.

    Args:
        api_client: HTTP client for API calls.
        root_id: Root task ID of the graph.
        min_nodes: Minimum number of nodes expected.
        timeout: Maximum time to wait in seconds.
        poll_interval: Time between polls in seconds.

    Returns:
        Graph data from the API.

    Raises:
        TimeoutError: If graph doesn't have enough nodes within timeout.
    """
    start_time = time.time()
    last_response = None

    while time.time() - start_time < timeout:
        response = api_client.get(f"/graphs/{root_id}")
        if response.status_code == 200:
            data = response.json()
            last_response = data
            nodes = data.get("nodes", [])
            if len(nodes) >= min_nodes:
                return data

        time.sleep(poll_interval)

    node_count = len(last_response.get("nodes", [])) if last_response else 0
    raise TimeoutError(
        f"Graph {root_id} did not reach {min_nodes} nodes within {timeout}s. "
        f"Current count: {node_count}"
    )


# Export task references for tests
__all__ = [
    "add",
    "aggregate",
    "always_fails",
    "api_client",
    "app",
    "celery_app",
    "check_services_running",
    "create_workflow",
    "flaky_task",
    "multiply",
    "process_item",
    "server_url",
    "slow_task",
    "wait_for_graph",
    "wait_for_task",
]
