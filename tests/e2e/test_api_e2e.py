"""E2E tests for celery-flow API.

These tests verify the full flow:
- Celery task submission
- Event capture via Redis Streams
- API visibility of tasks and graphs
- WebSocket real-time updates

Prerequisites:
    docker compose -f docker-compose.e2e.yml up -d --wait
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

    import httpx

from tests.e2e.conftest import wait_for_task
from tests.e2e.tasks import add, always_fails, create_workflow, flaky_task, slow_task


def get_event_by_state(events: list, state: str) -> dict | None:
    """Find the first event with a given state."""
    for event in events:
        if event.get("state") == state:
            return event
    return None


def get_latest_event(events: list) -> dict | None:
    """Get the most recent event (last in list)."""
    return events[-1] if events else None


@pytest.mark.e2e
class TestTaskVisibility:
    """Test that submitted tasks become visible via the API."""

    def test_simple_task_appears_in_api(self, api_client: httpx.Client) -> None:
        """Submit a simple task and verify it appears in the API."""
        # Submit task
        result = add.delay(2, 3)
        task_id = result.id

        # Wait for task to complete
        data = wait_for_task(api_client, task_id, ["SUCCESS"])

        # Verify task data
        task = data["task"]
        assert task["task_id"] == task_id
        assert task["name"] == "e2e.add"
        assert task["state"] == "SUCCESS"

        # Result is in the SUCCESS event
        success_event = get_event_by_state(task["events"], "SUCCESS")
        assert success_event is not None
        assert success_event["result"] == 5

    def test_task_captures_arguments(self, api_client: httpx.Client) -> None:
        """Verify task arguments are captured."""
        result = add.delay(10, 20)
        task_id = result.id

        data = wait_for_task(api_client, task_id, ["SUCCESS"])

        task = data["task"]
        # Args are captured in PENDING or STARTED events
        pending_event = get_event_by_state(task["events"], "PENDING")
        assert pending_event is not None
        assert pending_event["args"] == [10, 20]

    def test_failed_task_captures_exception(self, api_client: httpx.Client) -> None:
        """Verify failed tasks capture exception details."""
        result = always_fails.delay("E2E test failure")
        task_id = result.id

        data = wait_for_task(api_client, task_id, ["FAILURE"])

        task = data["task"]
        assert task["state"] == "FAILURE"

        # Exception details are in the FAILURE event
        failure_event = get_event_by_state(task["events"], "FAILURE")
        assert failure_event is not None
        assert failure_event["exception"] is not None
        assert "E2E test failure" in failure_event["exception"]
        assert failure_event["traceback"] is not None
        assert "ValueError" in failure_event["traceback"]

    def test_retry_task_shows_retry_count(self, api_client: httpx.Client) -> None:
        """Verify retrying tasks show retry count."""
        # This task fails once then succeeds
        result = flaky_task.delay(fail_count=1)
        task_id = result.id

        data = wait_for_task(api_client, task_id, ["SUCCESS"], timeout=60)

        task = data["task"]
        assert task["state"] == "SUCCESS"

        # Retry count is captured in events - check that we have RETRY events
        retry_events = [e for e in task["events"] if e["state"] == "RETRY"]
        assert len(retry_events) >= 1

    def test_task_appears_in_tasks_list(self, api_client: httpx.Client) -> None:
        """Verify task appears in the tasks list endpoint."""
        result = add.delay(1, 1)
        task_id = result.id

        wait_for_task(api_client, task_id, ["SUCCESS"])

        # Check tasks list
        response = api_client.get("/tasks")
        assert response.status_code == 200

        tasks = response.json()["tasks"]
        task_ids = [t["task_id"] for t in tasks]
        assert task_id in task_ids


@pytest.mark.e2e
class TestWorkflowGraphs:
    """Test workflow visibility and graph API endpoints.

    Note: Celery chords/groups don't create parent-child relationships
    in events. Each task appears as an independent root. This tests
    that workflow tasks are properly tracked and visible.
    """

    def test_workflow_tasks_are_tracked(self, api_client: httpx.Client) -> None:
        """Submit a workflow and verify all tasks are tracked."""
        import time

        # Create workflow: group of 3 process_item tasks -> aggregate
        workflow = create_workflow([1, 2, 3])
        workflow.apply_async()

        # Poll for workflow completion (all 3 process_item + 1 aggregate)
        timeout = 30
        start = time.time()
        process_count = 0
        aggregate_count = 0

        while time.time() - start < timeout:
            response = api_client.get("/tasks?limit=50")
            assert response.status_code == 200

            tasks = response.json()["tasks"]
            task_names = [t["name"] for t in tasks]

            process_count = sum(1 for n in task_names if n == "e2e.process_item")
            aggregate_count = sum(1 for n in task_names if n == "e2e.aggregate")

            if process_count >= 3 and aggregate_count >= 1:
                break
            time.sleep(0.5)

        assert (
            process_count >= 3
        ), f"Expected 3+ process_item tasks, got {process_count}"
        assert (
            aggregate_count >= 1
        ), f"Expected 1+ aggregate tasks, got {aggregate_count}"

    def test_graphs_list_shows_root_tasks(self, api_client: httpx.Client) -> None:
        """Verify graphs endpoint lists root tasks."""
        # Submit a simple task
        result = add.delay(100, 200)
        task_id = result.id

        # Wait for task to complete
        wait_for_task(api_client, task_id, ["SUCCESS"])

        # Check graphs list includes root tasks
        response = api_client.get("/graphs")
        assert response.status_code == 200

        data = response.json()
        assert "graphs" in data
        assert "total" in data
        assert data["total"] > 0

        # Find our task in graphs
        graph_ids = [g["task_id"] for g in data["graphs"]]
        assert task_id in graph_ids

    def test_graph_detail_endpoint(self, api_client: httpx.Client) -> None:
        """Verify individual graph can be retrieved."""
        result = add.delay(50, 60)
        task_id = result.id

        wait_for_task(api_client, task_id, ["SUCCESS"])

        # Get graph detail
        response = api_client.get(f"/graphs/{task_id}")
        assert response.status_code == 200

        data = response.json()
        assert data["root_id"] == task_id
        assert "nodes" in data
        assert task_id in data["nodes"]


@pytest.mark.e2e
class TestWebSocketUpdates:
    """Test WebSocket real-time event streaming."""

    @pytest.fixture
    def ws_messages(self, ws_url: str) -> Generator[list[dict], None, None]:
        """Collect WebSocket messages in background."""
        import threading
        import time

        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        messages: list[dict] = []
        stop_event = threading.Event()

        def collect_messages() -> None:
            try:
                with connect(ws_url, close_timeout=1) as ws:
                    while not stop_event.is_set():
                        try:
                            msg = ws.recv(timeout=0.5)
                            if msg:
                                messages.append(json.loads(msg))
                        except TimeoutError:
                            continue
                        except ConnectionClosed:
                            break
            except OSError:
                # Connection failed
                pass

        thread = threading.Thread(target=collect_messages, daemon=True)
        thread.start()

        # Give WebSocket time to connect
        time.sleep(0.5)

        yield messages

        stop_event.set()
        thread.join(timeout=2)

    def test_websocket_receives_task_events(
        self, api_client: httpx.Client, ws_messages: list[dict]
    ) -> None:
        """Verify WebSocket receives events when tasks are submitted."""
        # Submit a slow task to give time for events
        result = slow_task.delay(0.5)
        task_id = result.id

        # Wait for task to complete
        wait_for_task(api_client, task_id, ["SUCCESS"], timeout=10)

        # Give WebSocket time to receive events
        import time

        time.sleep(1)

        # Check that we received events for this task
        task_events = [msg for msg in ws_messages if msg.get("task_id") == task_id]

        # Should have received at least one event
        # (PENDING, RECEIVED, STARTED, SUCCESS)
        assert (
            len(task_events) >= 1
        ), f"Expected events for {task_id}, got {len(ws_messages)} messages"


@pytest.mark.e2e
class TestAPIEndpoints:
    """Test various API endpoints."""

    def test_health_endpoint(self, api_client: httpx.Client) -> None:
        """Health endpoint returns OK."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_registry_endpoint(self, api_client: httpx.Client) -> None:
        """Registry endpoint returns registered tasks."""
        # First, submit a task to ensure registry is populated
        result = add.delay(1, 1)
        wait_for_task(api_client, result.id, ["SUCCESS"])

        response = api_client.get("/tasks/registry")
        assert response.status_code == 200

        data = response.json()
        task_names = [t["name"] for t in data["tasks"]]

        # Should include our E2E tasks
        assert any("e2e.add" in name for name in task_names)

    def test_task_filtering_by_state(self, api_client: httpx.Client) -> None:
        """Tasks can be filtered by state."""
        # Submit and wait for a task
        result = add.delay(5, 5)
        wait_for_task(api_client, result.id, ["SUCCESS"])

        # Filter by SUCCESS state
        response = api_client.get("/tasks", params={"state": "SUCCESS"})
        assert response.status_code == 200

        tasks = response.json()["tasks"]
        for task in tasks:
            assert task["state"] == "SUCCESS"

    def test_task_filtering_by_name(self, api_client: httpx.Client) -> None:
        """Tasks can be filtered by name."""
        # Submit and wait for a task
        result = add.delay(6, 6)
        wait_for_task(api_client, result.id, ["SUCCESS"])

        # Filter by name
        response = api_client.get("/tasks", params={"name": "e2e.add"})
        assert response.status_code == 200

        tasks = response.json()["tasks"]
        for task in tasks:
            assert task["name"] == "e2e.add"
