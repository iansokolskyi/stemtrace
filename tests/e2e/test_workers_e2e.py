"""E2E tests for worker lifecycle visibility.

These tests validate Phase 8 end-to-end behavior in Docker:
- a real Celery worker starts
- stemtrace captures worker_ready
- server consumes the event and exposes it via /api/workers
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import pytest

from tests.e2e.conftest import API_URL
from tests.e2e.tasks import add

if TYPE_CHECKING:
    import httpx


def _wait_for_workers(
    api_client: httpx.Client, timeout: float = 30.0
) -> dict[str, Any]:
    """Poll /workers until at least one worker is present."""
    start = time.time()
    last: dict[str, Any] | None = None
    while time.time() - start < timeout:
        resp = api_client.get("/workers")
        if resp.status_code == 200:
            data = resp.json()
            last = data
            if data.get("total", 0) > 0 and data.get("workers"):
                return data
        time.sleep(0.5)
    raise TimeoutError(
        f"No workers visible via {API_URL}/workers within {timeout}s. Last: {last}"
    )


def _broker_url() -> str:
    """Get the E2E broker URL (defaults to Redis dev/E2E port)."""
    return os.environ.get("CELERY_BROKER_URL", "redis://localhost:16379/0")


def _wait_for_task_event_in_rabbitmq(
    broker_url: str,
    exchange_name: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Bind a probe queue to the fanout exchange and wait for a task event.

    Important: RabbitMQ does not retain messages in exchanges. The probe queue must
    be declared/bound *before* the task is submitted, or the event can be missed.
    """
    from kombu import Connection, Exchange, Queue
    from kombu.messaging import Consumer

    exchange = Exchange(exchange_name, type="fanout", durable=True)
    probe_queue = Queue(
        name=f"{exchange_name}.probe.{int(time.time() * 1000)}",
        exchange=exchange,
        routing_key="",
        durable=False,
        exclusive=True,
        auto_delete=True,
    )

    found: dict[str, Any] | None = None
    target_task_id: str | None = None

    def on_message(body: Any, message: Any) -> None:
        nonlocal found
        try:
            payload = body
            if isinstance(body, (bytes, bytearray)):
                payload = json.loads(body.decode())
            if isinstance(payload, str):
                payload = json.loads(payload)
            if (
                target_task_id is not None
                and isinstance(payload, dict)
                and payload.get("task_id") == target_task_id
            ):
                found = payload
        finally:
            message.ack()

    start = time.time()
    with Connection(broker_url, connect_timeout=3) as conn:
        channel = conn.channel()
        exchange.maybe_bind(channel)
        probe_queue.maybe_bind(channel)
        exchange.declare()
        probe_queue.declare()

        with Consumer(
            channel, queues=[probe_queue], callbacks=[on_message], accept=["json"]
        ):
            # Submit the task only after the probe queue is bound.
            result = add.delay(1, 1)
            target_task_id = result.id
            if target_task_id is None:
                raise RuntimeError("Celery did not return task id for add.delay(1, 1)")

            while time.time() - start < timeout:
                if found is not None:
                    return found
                with contextlib.suppress(TimeoutError):
                    conn.drain_events(timeout=1)

    raise TimeoutError(
        f"No task event for task_id={target_task_id!r} found in RabbitMQ exchange {exchange_name!r} "
        f"within {timeout}s."
    )


@pytest.mark.e2e
class TestWorkersLifecycleE2E:
    """Phase 8 E2E worker lifecycle tests."""

    def test_workers_endpoint_shows_live_worker(self, api_client: httpx.Client) -> None:
        """Verify /api/workers eventually shows at least one worker."""
        data = _wait_for_workers(api_client, timeout=45.0)
        worker = data["workers"][0]
        assert isinstance(worker.get("hostname"), str)
        assert isinstance(worker.get("pid"), int)
        assert worker.get("status") in {"online", "offline"}
        assert isinstance(worker.get("registered_tasks"), list)

    def test_workers_by_hostname_endpoint_filters(
        self, api_client: httpx.Client
    ) -> None:
        """Verify /api/workers/{hostname} returns only matching hostnames."""
        data = _wait_for_workers(api_client, timeout=45.0)
        hostname = data["workers"][0]["hostname"]

        resp = api_client.get(f"/workers/{hostname}")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] >= 1
        assert all(w["hostname"] == hostname for w in payload["workers"])

    def test_task_event_is_published_to_broker(self) -> None:
        """Verify we can observe a task event in the underlying broker.

        For Redis, we read the stream directly.
        For RabbitMQ, we bind a probe queue to the fanout exchange and wait for
        an event for a newly-submitted task.
        """
        broker_url = _broker_url()
        scheme = urlparse(broker_url).scheme.lower()

        if scheme in {"redis", "rediss"}:
            from redis import Redis

            # Submit a task to ensure the event is emitted after we start observing.
            result = add.delay(1, 1)
            task_id = result.id
            assert task_id is not None

            redis_client: Redis[bytes] = Redis.from_url(
                _broker_url(), decode_responses=False
            )
            stream_key = "stemtrace:events"
            try:
                start = time.time()
                while time.time() - start < 45.0:
                    entries = redis_client.xrevrange(stream_key, count=200)
                    for _msg_id, fields in entries:
                        raw = fields.get(b"data")
                        if not raw:
                            continue
                        try:
                            parsed = json.loads(raw.decode())
                        except json.JSONDecodeError:
                            continue
                        if parsed.get("task_id") == task_id:
                            return
                    time.sleep(0.5)
            finally:
                redis_client.close()
            raise TimeoutError(
                f"No task event found for task_id={task_id!r} in Redis stream {stream_key!r} within 45s."
            )

        # RabbitMQ / AMQP mode
        _wait_for_task_event_in_rabbitmq(
            broker_url=broker_url,
            exchange_name="stemtrace.events",
            timeout=45.0,
        )
