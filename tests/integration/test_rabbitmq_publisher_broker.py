"""The publisher connection against a real RabbitMQ with its management API.

Skipped when RabbitMQ (with the management plugin) isn't reachable. Run with:

    RABBITMQ_URL=amqp://guest:guest@localhost:5672// \\
    RABBITMQ_MANAGEMENT_URL=http://guest:guest@localhost:15672 \\
    pytest tests/integration/test_rabbitmq_publisher_broker.py -v

Forking and monkey-patched (gevent, eventlet) cases run in their own interpreter.
"""

import base64
import contextlib
import http.client
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from amqp.exceptions import AMQPError
from kombu import Connection
from kombu.exceptions import KombuError

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports.rabbitmq import RabbitMQTransport

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")
RABBITMQ_MANAGEMENT_URL = os.environ.get(
    "RABBITMQ_MANAGEMENT_URL", "http://guest:guest@localhost:15672"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _management(method: str, path: str) -> Any:
    """Call the management HTTP API; return the decoded JSON body."""
    parsed = urllib.parse.urlsplit(RABBITMQ_MANAGEMENT_URL)
    base = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 15672}"
    credentials = f"{parsed.username or 'guest'}:{parsed.password or 'guest'}"
    request = urllib.request.Request(f"{base}/api/{path}", method=method)
    request.add_header(
        "Authorization", "Basic " + base64.b64encode(credentials.encode()).decode()
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read()
    return json.loads(body) if body else None


def _available() -> bool:
    try:
        with Connection(RABBITMQ_URL, connect_timeout=1) as conn:
            conn.connect()
        _management("GET", "overview")
    except (OSError, AMQPError, KombuError, ValueError, http.client.HTTPException):
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _available(), reason="RabbitMQ with management API not available"
    ),
]


def _event(task_id: str) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.publisher_broker",
        state=TaskState.STARTED,
        timestamp=datetime.now(timezone.utc),
    )


def _name_suffix(pid: int) -> str:
    return f" pid {pid}"


def _wait_for(condition: Callable[[], Any], what: str, timeout: float = 20.0) -> Any:
    """Poll until ``condition()`` is truthy (the management stats lag ~5s)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def _publisher_connections(pid: int) -> list[dict[str, Any]]:
    """Management view of the publisher connections opened by process ``pid``."""
    return [
        c
        for c in _management("GET", "connections")
        if str(c.get("user_provided_name", "")).startswith("stemtrace publisher ")
        and str(c.get("user_provided_name", "")).endswith(_name_suffix(pid))
    ]


def _exactly(pid: int, count: int) -> Callable[[], list[dict[str, Any]] | None]:
    """Condition for _wait_for: ``count`` publisher connections of ``pid``."""

    def condition() -> list[dict[str, Any]] | None:
        connections = _publisher_connections(pid)
        return connections if len(connections) == count else None

    return condition


def _current_name(pid: int) -> str:
    """The management name of the only publisher connection of ``pid``."""
    connections = _wait_for(_exactly(pid, 1), "one publisher connection")
    name: str = connections[0]["name"]
    return name


@pytest.fixture
def prefix() -> Iterator[str]:
    name = f"pubconn_{uuid.uuid4().hex[:10]}"
    yield name
    probe = RabbitMQTransport(RABBITMQ_URL, prefix=name, ttl=60)
    with contextlib.suppress(Exception), Connection(RABBITMQ_URL) as conn:
        channel = conn.channel()
        channel.queue_delete(probe.queue_name)
        channel.exchange_delete(probe.exchange_name)


@pytest.fixture
def transport(prefix: str) -> Iterator[RabbitMQTransport]:
    publisher = RabbitMQTransport(RABBITMQ_URL, prefix=prefix, ttl=60)
    publisher._declare_exchange_and_queue()
    yield publisher
    publisher._publisher.close()


def _received(queue_name: str, count: int, timeout: float = 10.0) -> list[str]:
    """Task ids of up to ``count`` messages from the queue, in arrival order."""
    ids: list[str] = []
    deadline = time.monotonic() + timeout
    with Connection(RABBITMQ_URL, connect_timeout=2) as conn:
        channel = conn.channel()
        while len(ids) < count and time.monotonic() < deadline:
            message = channel.basic_get(queue_name, no_ack=True)
            if message is None:
                time.sleep(0.05)
                continue
            ids.append(json.loads(message.body)["task_id"])
    return ids


class TestOneConnectionPerProcess:
    def test_publish_many_events_uses_one_broker_connection(
        self, transport: RabbitMQTransport
    ) -> None:
        ids = [f"t-{i}" for i in range(300)]

        for task_id in ids:
            transport.publish(_event(task_id))

        # 300 events, one connection (and it is still the same one).
        first = _current_name(os.getpid())
        transport.publish(_event("one-more"))
        assert _current_name(os.getpid()) == first
        assert _received(transport.queue_name, len(ids) + 1) == [*ids, "one-more"]

    def test_publish_from_many_threads_shares_one_connection_and_delivers_all(
        self, transport: RabbitMQTransport
    ) -> None:
        threads_n, per_thread = 16, 50

        def run(n: int) -> None:
            for i in range(per_thread):
                transport.publish(_event(f"t-{n}-{i}"))

        threads = [threading.Thread(target=run, args=(n,)) for n in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert [t.is_alive() for t in threads] == [False] * threads_n
        got = _received(transport.queue_name, threads_n * per_thread)
        assert len(got) == threads_n * per_thread
        assert len(set(got)) == threads_n * per_thread
        _current_name(os.getpid())

    def test_connect_with_heartbeat_in_url_negotiates_no_heartbeat(
        self, prefix: str
    ) -> None:
        url = f"{RABBITMQ_URL}{'&' if '?' in RABBITMQ_URL else '?'}heartbeat=10"
        publisher = RabbitMQTransport(url, prefix=prefix, ttl=60)
        try:
            publisher.publish(_event("t-hb"))

            connection = _wait_for(_exactly(os.getpid(), 1), "the listing")[0]

            # The management API reports the negotiated heartbeat as "timeout".
            assert connection["timeout"] == 0
        finally:
            publisher._publisher.close()


class TestBrokerClosesTheConnection:
    def test_publish_after_broker_closed_connection_reconnects_and_delivers(
        self, transport: RabbitMQTransport
    ) -> None:
        transport.publish(_event("before"))
        name = _current_name(os.getpid())

        _management("DELETE", f"connections/{urllib.parse.quote(name, safe='')}")
        # The broker sent Connection.Close and waits for our reply, which an
        # idle publisher only gives when it next reads the socket: the publish.
        transport.publish(_event("after"))

        assert _current_name(os.getpid()) != name
        assert transport._publish_breaker.is_open is False
        assert _received(transport.queue_name, 2) == ["before", "after"]

    def test_publish_after_exchange_deleted_loses_one_event_then_recovers(
        self, transport: RabbitMQTransport
    ) -> None:
        transport.publish(_event("before"))
        first = _current_name(os.getpid())
        _management(
            "DELETE", f"exchanges/%2F/{urllib.parse.quote(transport.exchange_name)}"
        )
        # The broker closes the channel on this publish (404): lost, no error.
        transport.publish(_event("lost"))
        time.sleep(0.3)
        transport._declare_exchange_and_queue()

        transport.publish(_event("after-1"))
        transport.publish(_event("after-2"))

        assert _current_name(os.getpid()) != first
        assert _received(transport.queue_name, 3) == ["before", "after-1", "after-2"]


# --- fork, gevent and eventlet: each in its own interpreter ------------------


def _run_while_alive(
    script: str, *args: str, inspect: Callable[[Any], None]
) -> list[Any]:
    """Run ``script``; call ``inspect`` with its first JSON line while it waits.

    The script prints one JSON line, then waits for its stdin to close, so the
    connections it opened are still there for the management API to count. It
    prints a second JSON line before exiting. Returns both lines' values.
    """
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        first = json.loads(process.stdout.readline())
        inspect(first)
        process.stdin.close()
        rest = process.stdout.read()
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
        stderr = process.stderr.read() if process.stderr else ""
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()
    assert process.returncode == 0, stderr
    return [first, json.loads(rest.strip().splitlines()[-1])]


class TestForkAndGreenPools:
    def test_forked_children_get_own_connections_and_parent_keeps_its_own(
        self, transport: RabbitMQTransport, prefix: str
    ) -> None:
        def inspect(first: Any) -> None:
            pids = [first["parent_pid"], *first["child_pids"]]
            assert len(set(pids)) == 4
            # One connection per process: four processes, four connections.
            for pid in pids:
                assert len(_wait_for(_exactly(pid, 1), f"pid {pid}")) == 1

        _, last = _run_while_alive(
            """
            import json, os, sys
            from stemtrace.transports.rabbitmq import RabbitMQTransport
            from tests.integration.test_rabbitmq_publisher_broker import _event

            url, prefix = sys.argv[1:3]
            transport = RabbitMQTransport(url, prefix=prefix, ttl=60)
            transport.publish(_event("parent-1"))
            go_read, go_write = os.pipe()
            reports, children = [], []
            for n in range(3):
                report_read, report_write = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(go_write)
                    os.close(report_read)
                    transport.publish(_event(f"child-{n}"))
                    os.write(report_write, str(os.getpid()).encode())
                    os.read(go_read, 1)  # stay alive, like a pool child
                    os._exit(0)
                os.close(report_write)
                reports.append(report_read)
                children.append(pid)
            child_pids = [int(os.read(fd, 16)) for fd in reports]
            print(json.dumps({"parent_pid": os.getpid(), "child_pids": child_pids}), flush=True)
            sys.stdin.read()
            os.close(go_write)
            for pid in children:
                os.waitpid(pid, 0)
            transport.publish(_event("parent-2"))
            print(json.dumps({"done": True}))
            """,
            RABBITMQ_URL,
            prefix,
            inspect=inspect,
        )

        assert last == {"done": True}
        got = _received(transport.queue_name, 5)
        assert sorted(got) == sorted(
            ["parent-1", "parent-2", "child-0", "child-1", "child-2"]
        )

    @pytest.mark.parametrize(
        "patch",
        [
            "from gevent import monkey; monkey.patch_all(); import gevent",
            "import eventlet; eventlet.monkey_patch()",
        ],
        ids=["gevent", "eventlet"],
    )
    def test_publish_from_many_greenlets_shares_one_connection_and_delivers_all(
        self, transport: RabbitMQTransport, prefix: str, patch: str
    ) -> None:
        def inspect(first: Any) -> None:
            assert first["done"] == 50
            assert len(_wait_for(_exactly(first["pid"], 1), "one connection")) == 1

        _run_while_alive(
            f"""
            {patch}
            import json, os, sys
            from stemtrace.transports.rabbitmq import RabbitMQTransport
            from tests.integration.test_rabbitmq_publisher_broker import _event

            url, prefix = sys.argv[1:3]
            transport = RabbitMQTransport(url, prefix=prefix, ttl=60)
            done = []

            def run(n):
                for i in range(10):
                    transport.publish(_event(f"g-{{n}}-{{i}}"))
                done.append(n)

            if "gevent" in sys.modules:
                gevent.joinall([gevent.spawn(run, n) for n in range(50)], timeout=60)
            else:
                pool = eventlet.GreenPool(50)
                for n in range(50):
                    pool.spawn(run, n)
                pool.waitall()
            print(json.dumps({{"done": len(done), "pid": os.getpid()}}), flush=True)
            sys.stdin.read()
            print(json.dumps({{"exit": True}}))
            """,
            RABBITMQ_URL,
            prefix,
            inspect=inspect,
        )

        assert len(_received(transport.queue_name, 500, timeout=20)) == 500
