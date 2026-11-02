"""A server restarted after its workers rebuilds the worker timeline from Redis."""

import contextlib
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import redis

from stemtrace.core.events import WorkerEvent, WorkerEventType
from stemtrace.server.consumer import EventConsumer
from stemtrace.server.store import GraphStore, WorkerTimeline, WorkerTimelineEntry
from stemtrace.transports import get_transport
from stemtrace.transports.redis import RedisTransport

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _redis_available() -> bool:
    try:
        return bool(redis.from_url(REDIS_URL).ping())
    except redis.RedisError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _redis_available(), reason="Redis not available"),
]


def test_consumer_replay_from_redis_rebuilds_worker_timeline() -> None:
    prefix = f"test_timeline_{int(time.time() * 1000)}"
    transport = get_transport(REDIS_URL, prefix=prefix, ttl=60)
    assert isinstance(transport, RedisTransport)
    sent = [
        WorkerEvent(
            event_type=event_type,
            hostname="celery@w1",
            pid=100 + i,
            timestamp=T0 + timedelta(seconds=i),
        )
        for i, event_type in enumerate(WorkerEventType)
    ]
    expected = [WorkerTimelineEntry(e.timestamp, e.event_type, e.pid) for e in sent]
    timeline = WorkerTimeline()
    consumer = EventConsumer(
        REDIS_URL, GraphStore(), prefix=prefix, ttl=60, worker_timeline=timeline
    )
    try:
        for event in sent:
            transport.publish(event)
        # The consumer starts after the events were published: a replay.
        consumer.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if timeline.events_for("celery@w1") == expected:
                break
            time.sleep(0.05)
        assert timeline.events_for("celery@w1") == expected
    finally:
        consumer.stop(timeout=5.0)
        with contextlib.suppress(Exception):
            transport._client.delete(transport.stream_key)
