"""The RabbitMQ publisher connection against a real, disposable broker container.

Opt-in because it starts, restarts, pauses and stresses a Docker container and
takes a few minutes:

    STEMTRACE_DOCKER_TESTS=1 uv run pytest tests/integration/test_rabbitmq_publisher_docker.py -v
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from kombu import Connection

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports.rabbitmq import (
    CONNECT_TIMEOUT,
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_IO_TIMEOUT,
    RabbitMQTransport,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("STEMTRACE_DOCKER_TESTS") != "1"
        or shutil.which("docker") is None,
        reason="set STEMTRACE_DOCKER_TESTS=1 (requires Docker)",
    ),
]

IMAGE = "rabbitmq:3-management"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _wait_until(condition: Callable[[], Any], timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


class RabbitMQContainer:
    """A throwaway RabbitMQ (with management API) on random local ports."""

    def __init__(self) -> None:
        self.name = f"stemtrace-test-rabbitmq-{uuid.uuid4().hex[:8]}"
        self.amqp_port = _free_port()
        self.http_port = _free_port()
        self.url = f"amqp://guest:guest@127.0.0.1:{self.amqp_port}//"

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
                f"127.0.0.1:{self.amqp_port}:5672",
                "-p",
                f"127.0.0.1:{self.http_port}:15672",
                IMAGE,
            ],
            check=True,
            capture_output=True,
        )
        self.wait_ready()

    def wait_ready(self) -> None:
        def ready() -> bool:
            # The management plugin starts after the AMQP listeners, so its
            # answer means both are up (and probing with an AMQP connection
            # that fails mid-handshake leaks a socket in old py-amqp).
            try:
                self.management("GET", "overview")
            except (OSError, urllib.error.URLError, ValueError):
                return False
            return True

        _wait_until(ready, 90, "RabbitMQ to accept connections")

    def management(self, method: str, path: str) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.http_port}/api/{path}", method=method
        )
        request.add_header(
            "Authorization", "Basic " + base64.b64encode(b"guest:guest").decode()
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
        return json.loads(body) if body else None

    def publisher_connections(self) -> list[dict[str, Any]]:
        try:
            connections = self.management("GET", "connections")
        except (OSError, urllib.error.URLError):
            return []
        suffix = f" pid {os.getpid()}"
        return [
            c
            for c in connections
            if str(c.get("user_provided_name", "")).endswith(suffix)
        ]

    def docker(self, *args: str) -> None:
        subprocess.run(["docker", *args, self.name], check=True, capture_output=True)

    def rabbitmqctl(self, *args: str) -> None:
        subprocess.run(
            ["docker", "exec", self.name, "rabbitmqctl", *args],
            check=True,
            capture_output=True,
        )

    def remove(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


@pytest.fixture
def broker() -> Iterator[RabbitMQContainer]:
    container = RabbitMQContainer()
    try:
        container.start()
        yield container
    finally:
        container.remove()


@pytest.fixture
def transport(broker: RabbitMQContainer) -> Iterator[RabbitMQTransport]:
    publisher = RabbitMQTransport(
        broker.url, prefix=f"dock_{uuid.uuid4().hex[:8]}", ttl=300
    )
    publisher._declare_exchange_and_queue()
    yield publisher
    publisher._publisher.close()


def _event(task_id: str) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.publisher_docker",
        state=TaskState.STARTED,
        timestamp=datetime.now(timezone.utc),
    )


def _timed(transport: RabbitMQTransport, task_id: str) -> float:
    start = time.perf_counter()
    transport.publish(_event(task_id))
    return time.perf_counter() - start


def _queued(broker: RabbitMQContainer, transport: RabbitMQTransport) -> list[str]:
    """Task ids waiting in the consumer queue (read without removing them)."""
    ids: list[str] = []
    with Connection(broker.url, connect_timeout=5) as conn:
        channel = conn.channel()
        while (
            message := channel.basic_get(transport.queue_name, no_ack=False)
        ) is not None:
            ids.append(json.loads(message.body)["task_id"])
        channel.basic_recover(requeue=True)
    return ids


def _connection_name(broker: RabbitMQContainer) -> str:
    connections = _wait_until(
        lambda: (c := broker.publisher_connections()) and len(c) == 1 and c,
        30,
        "exactly one publisher connection",
    )
    name: str = connections[0]["name"]
    return name


class TestBrokerRestart:
    # A connect that fails mid-handshake (the broker is booting) leaves its
    # socket to the garbage collector in py-amqp 5.0.6 (Connection.connect()
    # only collects in later versions); the per-event connections did the same.
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    def test_publish_after_broker_restart_recovers_on_one_new_connection(
        self, broker: RabbitMQContainer, transport: RabbitMQTransport
    ) -> None:
        transport.publish(_event("before"))
        before = _connection_name(broker)

        broker.docker("restart")
        broker.wait_ready()
        # The first attempts may fail while the broker boots or the breaker
        # is open; publishing recovers within one cooldown (at most 30s).
        recovered = _wait_until(
            lambda: (
                (transport.publish(_event("after")) or True)
                and not transport._publish_breaker.is_open
                and _queued(broker, transport).count("after") >= 1
            ),
            60,
            "publishing to recover",
        )

        assert recovered
        assert _connection_name(broker) != before


class TestPausedBroker:
    """``docker pause``: the TCP connection stays up and nobody answers."""

    def test_publish_on_open_connection_while_paused_is_instant_and_delivered_after_unpause(
        self, broker: RabbitMQContainer, transport: RabbitMQTransport
    ) -> None:
        transport.publish(_event("before"))
        broker.docker("pause")
        try:
            durations = [_timed(transport, f"paused-{i}") for i in range(50)]
        finally:
            broker.docker("unpause")

        # Writes land in the socket buffers: no waiting, no failure.
        assert max(durations) < 0.5
        assert transport._publish_breaker.is_open is False
        delivered = _wait_until(
            lambda: (
                len(_queued(broker, transport)) >= 51 and _queued(broker, transport)
            ),
            30,
            "buffered events to arrive",
        )
        assert delivered[0] == "before"

    def test_connect_while_paused_is_bounded_and_opens_breaker(
        self, broker: RabbitMQContainer, transport: RabbitMQTransport
    ) -> None:
        broker.docker("pause")
        try:
            attempts = [
                _timed(transport, f"paused-{i}")
                for i in range(PUBLISH_FAILURES_TO_OPEN)
            ]
            skipped = _timed(transport, "skipped")
        finally:
            broker.docker("unpause")

        # One bounded wait per attempt (the handshake frame wait is the connect timeout).
        assert all(0.5 < d < CONNECT_TIMEOUT + PUBLISH_IO_TIMEOUT + 1 for d in attempts)
        assert skipped < 0.05
        assert transport._publish_breaker.is_open is True
        # Once the broker is back, the next probe connects and publishing resumes.
        _wait_until(
            lambda: (
                (transport.publish(_event("after")) or True)
                and not transport._publish_breaker.is_open
            ),
            60,
            "publishing to recover",
        )
        assert "after" in _queued(broker, transport)


class TestMemoryAlarm:
    def test_publish_during_memory_alarm_keeps_one_connection_and_resumes(
        self, broker: RabbitMQContainer, transport: RabbitMQTransport
    ) -> None:
        transport.publish(_event("before"))
        name = _connection_name(broker)

        broker.rabbitmqctl("set_vm_memory_high_watermark", "0.0001")
        try:
            durations = []
            for i in range(60):
                durations.append(_timed(transport, f"alarm-{i}"))
                time.sleep(0.05)
            alarm_connections = _wait_until(
                lambda: broker.publisher_connections(), 30, "the listing"
            )
        finally:
            broker.rabbitmqctl("set_vm_memory_high_watermark", "0.4")

        # No publish waits for the blocked broker, and the alarm costs no new connection.
        assert max(durations) < 0.5
        assert [c["name"] for c in alarm_connections] == [name]
        _wait_until(
            lambda: (
                (transport.publish(_event("after")) or True)
                and not transport._publish_breaker.is_open
                and "after" in _queued(broker, transport)
            ),
            90,
            "publishing to resume after the alarm",
        )
        assert _connection_name(broker) == name
        queued = _queued(broker, transport)
        # The event published right after the alarm started was held by the
        # broker and delivered when it cleared; the ones dropped meanwhile
        # were never sent.
        assert queued[0] == "before"
        assert len([i for i in queued if i.startswith("alarm-")]) <= 1
