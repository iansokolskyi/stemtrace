"""Redis transport resilience against a real, disposable Redis container.

Opt-in because it starts and restarts a Docker container and takes ~30s:

    STEMTRACE_DOCKER_TESTS=1 uv run pytest tests/integration/test_redis_resilience_docker.py -v
"""

import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
import redis

from stemtrace.core.events import TaskEvent, TaskState, WorkerEvent
from stemtrace.library.transports.redis import RedisTransport

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("STEMTRACE_DOCKER_TESTS") != "1"
        or shutil.which("docker") is None,
        reason="set STEMTRACE_DOCKER_TESTS=1 (requires Docker)",
    ),
]

REDIS_IMAGE = "redis:7-alpine"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _wait_until(condition: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.2)


def _ping(url: str) -> bool:
    try:
        return bool(redis.Redis.from_url(url, socket_timeout=1).ping())
    except redis.RedisError:
        return False


class RedisContainer:
    """A throwaway Redis container bound to a random local port."""

    def __init__(self) -> None:
        self.name = f"stemtrace-test-redis-{uuid.uuid4().hex[:8]}"
        self.port = _free_port()
        self.url = f"redis://127.0.0.1:{self.port}/0"

    def start(self) -> None:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                self.name,
                "-p",
                f"127.0.0.1:{self.port}:6379",
                REDIS_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
        _wait_until(lambda: _ping(self.url), 30, "redis to start")

    def restart(self) -> None:
        subprocess.run(
            ["docker", "restart", self.name], check=True, capture_output=True
        )
        _wait_until(lambda: _ping(self.url), 30, "redis to restart")

    def remove(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


@pytest.fixture
def container() -> Iterator[RedisContainer]:
    """Start a Redis container for one test and always remove it."""
    redis_container = RedisContainer()
    try:
        redis_container.start()
        yield redis_container
    finally:
        redis_container.remove()


def _event(task_id: str) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.resilience",
        state=TaskState.STARTED,
        timestamp=datetime.now(UTC),
    )


class ConsumerThread:
    """Runs transport.consume() in a thread until it sees the stop task id."""

    STOP_ID = "__stop__"

    def __init__(self, transport: RedisTransport) -> None:
        self.transport = transport
        self.seen: list[str] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        for event in self.transport.consume():
            if isinstance(event, WorkerEvent):
                continue
            if event.task_id == self.STOP_ID:
                return
            self.seen.append(event.task_id)

    def start(self) -> None:
        self.thread.start()

    def stop(self, publisher: RedisTransport) -> None:
        publisher.publish(_event(self.STOP_ID))
        self.thread.join(timeout=30)
        assert not self.thread.is_alive()


def _connections_received(url: str) -> int:
    info = redis.Redis.from_url(url).info("stats")
    return int(info["total_connections_received"])


class TestRedisResilience:
    def test_consume_when_idle_keeps_a_single_connection(
        self, container: RedisContainer
    ) -> None:
        assert int(redis.__version__.split(".")[0]) >= 8
        consumer_transport = RedisTransport.from_url(container.url, prefix="idle")
        publisher = RedisTransport.from_url(container.url, prefix="idle")
        before = _connections_received(container.url)

        consumer = ConsumerThread(consumer_transport)
        consumer.start()
        time.sleep(12)  # Longer than two XREAD block windows with no traffic.

        # Exactly one new connection: the consumer's (the stats probe adds one).
        after = _connections_received(container.url)
        assert after - before == 2
        assert consumer_transport.connected is True

        publisher.publish(_event("after-idle"))
        _wait_until(lambda: consumer.seen == ["after-idle"], 15, "event after idle")
        consumer.stop(publisher)

    def test_consume_after_redis_restart_receives_new_events(
        self, container: RedisContainer
    ) -> None:
        consumer_transport = RedisTransport.from_url(container.url, prefix="restart")
        publisher = RedisTransport.from_url(container.url, prefix="restart")

        consumer = ConsumerThread(consumer_transport)
        consumer.start()
        publisher.publish(_event("before-restart"))
        _wait_until(
            lambda: consumer.seen == ["before-restart"], 15, "event before restart"
        )

        container.restart()
        publisher.publish(_event("after-restart"))

        _wait_until(
            lambda: consumer.seen == ["before-restart", "after-restart"],
            45,
            "event after restart",
        )
        assert consumer_transport.connected is True
        consumer.stop(publisher)
