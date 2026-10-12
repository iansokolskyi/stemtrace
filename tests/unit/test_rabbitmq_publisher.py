"""The RabbitMQ publisher connection: one per process, reused, replaced when dead.

``tests/fake_kombu.py`` stands in for kombu and py-amqp (its docstring lists the
real behavior it models and where that was read). Scripts that fork, exit or
monkey-patch run in their own interpreter.
"""

import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports import rabbitmq_publisher
from stemtrace.transports.rabbitmq import (
    CONNECT_TIMEOUT,
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_IO_TIMEOUT,
    RabbitMQTransport,
)
from stemtrace.transports.rabbitmq_publisher import (
    CLOSE_TIMEOUT,
    DISCARD_TIMEOUT,
    PublisherBusyError,
    PublishSkippedError,
)
from tests import fake_kombu
from tests.fake_kombu import FakeBroker, FakeClock

LOGGER_NAME = "stemtrace.transports.rabbitmq"
H1 = "amqp://guest:guest@h1:5672//"
H2 = "amqp://guest:guest@h2:5672//"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_TIMEOUT_S = 60


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> Iterator[FakeBroker]:
    fake = FakeBroker(clock)
    fake_kombu.install(monkeypatch, fake)
    yield fake
    fake.release_sockets()


def _event(task_id: str = "t-1") -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="tests.publisher",
        state=TaskState.STARTED,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _transport(url: str, clock: FakeClock) -> RabbitMQTransport:
    return RabbitMQTransport(url, prefix="test", ttl=60, monotonic=clock)


def _ids(broker: FakeBroker) -> list[str]:
    return [body["task_id"] for _, body, _ in broker.published]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == LOGGER_NAME and r.levelno == logging.WARNING
    ]


class TestPublisherNoticesDeadConnections:
    """A connection the broker closed is replaced before it can lose an event."""

    def test_publish_after_broker_closed_idle_connection_reconnects_and_delivers(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        broker.close_idle_connections()  # a broker restart: the peer sees EOF

        transport.publish(_event("t-2"))
        transport.publish(_event("t-3"))

        assert _ids(broker) == ["t-1", "t-2", "t-3"]
        assert len(broker.connections) == 2
        assert broker.connections[0].connection.calls == ["collect"]
        # Replacing it isn't a failure: nothing counts against the breaker.
        assert _warnings(caplog) == []
        assert transport._publish_breaker.is_open is False

    def test_publish_after_connection_close_frame_reconnects_and_delivers(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        broker.force_close(broker.connections[0])  # e.g. closed via management

        transport.publish(_event("t-2"))

        assert _ids(broker) == ["t-1", "t-2"]
        assert len(broker.connections) == 2

    def test_publish_after_exchange_deleted_loses_one_event_then_redeclares(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        broker.delete_exchange()
        # The broker closes the channel once it sees this publish: its event
        # is lost without an error, the next publish finds the channel gone.
        transport.publish(_event("t-lost"))
        broker.exchange_exists = True

        transport.publish(_event("t-2"))
        transport.publish(_event("t-3"))

        assert _ids(broker) == ["t-1", "t-2", "t-3"]
        assert len(broker.connections) == 2
        assert broker.connections[1].declare_calls == 1

    def test_publish_when_replacement_cannot_connect_counts_one_failure(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        broker.close_idle_connections()
        broker.down = {H1}

        transport.publish(_event("t-2"))

        assert _ids(broker) == ["t-1"]
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].getMessage() == (
            "Failed to publish event t-2 to RabbitMQ; publishing continues "
            "(1 event(s) dropped since the last report)"
        )

    def test_discard_never_touches_transport_of_collected_connection(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        # py-amqp 5.3 Connection.transport on a collected connection
        # reconnects: the fake fails the test if the publisher asks for it.
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        broker.force_close(broker.connections[0])  # collects it, then raises

        transport.publish(_event("t-2"))

        assert _ids(broker) == ["t-1", "t-2"]


class TestPublisherWhileBrokerBlocksConnection:
    """A memory or disk alarm: drop events, keep the connection, resume."""

    def test_publish_during_alarm_sends_one_event_then_drops_the_rest(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-0"))
        broker.raise_alarm()

        for i in range(1, 30):
            transport.publish(_event(f"t-{i}"))

        # RabbitMQ blocks a connection once the publish after the alarm
        # arrives; that one is read after the alarm clears, later ones were
        # never sent.
        assert _ids(broker) == ["t-0"]
        assert [body["task_id"] for _, body, _ in broker.unread] == ["t-1"]
        assert len(broker.connections) == 1

    def test_publish_during_alarm_opens_breaker_and_probes_without_connecting(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-0"))
        broker.raise_alarm()
        transport.publish(_event("t-1"))  # blocks the connection
        for i in range(PUBLISH_FAILURES_TO_OPEN):
            transport.publish(_event(f"t-fail-{i}"))
        assert transport._publish_breaker.is_open is True

        for cooldown in (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0):
            clock.advance(cooldown)
            transport.publish(_event("t-probe"))

        # Every probe found the connection still blocked and made no new one
        # (a blocked connection per probe stays on the broker until the alarm ends).
        assert len(broker.connections) == 1
        assert len(broker.live()) == 1
        failures = _warnings(caplog)
        assert failures[0].exc_info is not None
        assert "blocked" in str(failures[0].exc_info[1])

    def test_publish_after_alarm_clears_resumes_on_same_connection(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-0"))
        broker.raise_alarm()
        transport.publish(_event("t-1"))
        for i in range(PUBLISH_FAILURES_TO_OPEN):
            transport.publish(_event(f"t-fail-{i}"))
        broker.clear_alarm()

        clock.advance(1.0)
        transport.publish(_event("t-probe"))
        transport.publish(_event("t-after"))

        assert len(broker.connections) == 1
        # t-1 was sent before the broker blocked the connection.
        assert _ids(broker) == ["t-0", "t-1", "t-probe", "t-after"]
        assert transport._publish_breaker.is_open is False
        assert "recovered" in _warnings(caplog)[-1].getMessage()


class TestPublisherDiscardsOnError:
    """Any failure drops the connection; the next allowed attempt reconnects."""

    def test_publish_after_error_discards_with_bounded_socket_timeout(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.publish_error = TimeoutError("timed out")
        transport = _transport(H1, clock)

        transport.publish(_event())

        amqp = broker.connections[0].connection
        assert amqp.calls == ["collect"]
        # Over TLS the unwrap in collect() waits this long for close_notify.
        assert amqp.timeout_at_collect == DISCARD_TIMEOUT

    def test_publish_interrupted_mid_write_discards_and_releases_connection_lock(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        class Killed(BaseException):
            """Like a killed greenlet or a signal handler raising mid-write."""

        broker.publish_error = Killed()
        transport = _transport(H1, clock)

        with pytest.raises(Killed):
            transport.publish(_event("t-killed"))
        broker.publish_error = None
        transport.publish(_event("t-2"))

        # Half a frame may be on the wire: the next publish uses a new connection.
        assert broker.connections[0].connection.calls == ["collect"]
        assert _ids(broker) == ["t-2"]
        assert len(broker.connections) == 2

    def test_publish_failure_after_connect_starts_next_attempt_on_next_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.publish_error = RuntimeError("channel error")
        broker.publish_error_hosts = {H1}
        transport = _transport(f"{H1};{H2}", clock)
        transport.publish(_event("t-0"))
        transport.publish(_event("t-1"))

        assert broker.connections[1].attempted == [H2]
        assert _ids(broker) == ["t-1"]


class TestPublisherHeartbeat:
    def test_connect_with_heartbeat_in_url_negotiates_none(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        # Nothing sends heartbeats between publishes; a negotiated interval
        # would make RabbitMQ close the idle connection after two of them.
        transport = _transport(f"{H1}?heartbeat=10", clock)

        transport.publish(_event())

        assert broker.connections[0].connection.heartbeat == 0


class TestPublisherThreads:
    """kombu connections aren't thread-safe: one publish at a time."""

    def test_publish_from_many_threads_never_enters_connection_concurrently(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        threads_n, per_thread = 8, 40
        state = {"active": 0, "max_active": 0}
        guard = threading.Lock()

        def on_publish() -> None:
            with guard:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.0005)  # long enough for another thread to get in
            with guard:
                state["active"] -= 1

        broker.on_publish = on_publish
        transport = _transport(H1, clock)

        def run(n: int) -> None:
            for i in range(per_thread):
                transport.publish(_event(f"t-{n}-{i}"))

        threads = [threading.Thread(target=run, args=(n,)) for n in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert state["max_active"] == 1
        assert len(broker.published) == threads_n * per_thread
        assert len(broker.connections) == 1

    def test_publish_waits_no_longer_than_lock_timeout_for_busy_connection(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        publisher = transport._publisher
        publisher._lock_timeout = 0.05
        proc = publisher._process()

        with proc.lock:  # another thread is stuck in a publish
            started = time.monotonic()
            transport.publish(_event("t-busy"))
            waited = time.monotonic() - started

        assert 0.05 <= waited < 2.0
        assert _ids(broker) == ["t-1"]
        warnings = _warnings(caplog)
        assert warnings[0].exc_info is not None
        assert isinstance(warnings[0].exc_info[1], PublisherBusyError)

    def test_lock_timeout_covers_a_connect_pass_over_every_host(
        self, clock: FakeClock
    ) -> None:
        publisher = _transport(f"{H1};{H2};amqp://h3", clock)._publisher

        assert publisher._lock_timeout == 3 * CONNECT_TIMEOUT + PUBLISH_IO_TIMEOUT

    def test_publish_skips_attempt_when_breaker_moved_on_while_waiting(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        publisher = _transport(H1, clock)._publisher

        with pytest.raises(PublishSkippedError):
            publisher.publish({"task_id": "t"}, current=lambda: False)

        assert broker.connections == []


class TestPublisherFork:
    """A child never uses the parent's socket or its (possibly held) lock."""

    @pytest.fixture
    def parent_connection(
        self, broker: FakeBroker, clock: FakeClock
    ) -> tuple[RabbitMQTransport, fake_kombu.FakeConnection]:
        transport = _transport(H1, clock)
        transport.publish(_event("parent-1"))
        return transport, broker.connections[0]

    def test_publish_in_child_without_fork_hook_builds_own_connection(
        self,
        broker: FakeBroker,
        parent_connection: tuple[RabbitMQTransport, fake_kombu.FakeConnection],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, parent = parent_connection
        closed_fds: list[int] = []
        parent_fd = parent.connection.transport.sock.fileno()
        monkeypatch.setattr(rabbitmq_publisher.os, "close", closed_fds.append)
        parent_pid = os.getpid()
        # A pool child that forked without Python's fork hooks (uWSGI).
        monkeypatch.setattr(rabbitmq_publisher.os, "getpid", lambda: parent_pid + 1)

        transport.publish(_event("child-1"))

        assert len(broker.connections) == 2
        assert [body["task_id"] for _, body, _ in broker.published] == [
            "parent-1",
            "child-1",
        ]
        assert broker.published[1][0] == H1
        # The parent's connection: no AMQP close, no shutdown (that would end
        # it for the parent too); only the child's copy of the fd is closed.
        assert parent.connection.calls == []
        assert parent.connection.transport.sock.detached is True
        assert closed_fds == [parent_fd]
        assert broker.connections[1].connection.calls == []

    def test_publish_in_child_replaces_lock_held_by_parent_thread_at_fork(
        self,
        broker: FakeBroker,
        parent_connection: tuple[RabbitMQTransport, fake_kombu.FakeConnection],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, _ = parent_connection
        monkeypatch.setattr(rabbitmq_publisher.os, "close", lambda fd: None)
        parent_lock = transport._publisher._process().lock
        parent_lock.acquire()  # a parent thread was mid-publish at fork time
        parent_pid = os.getpid()
        monkeypatch.setattr(rabbitmq_publisher.os, "getpid", lambda: parent_pid + 1)
        published: list[bool] = []

        def child_publish() -> None:
            transport.publish(_event("child-1"))
            published.append(True)

        thread = threading.Thread(target=child_publish)
        thread.start()
        thread.join(timeout=10)
        parent_lock.release()

        assert published == [True]
        assert [body["task_id"] for _, body, _ in broker.published][-1] == "child-1"

    def test_fork_hook_in_child_drops_inherited_connection_without_closing_it(
        self,
        broker: FakeBroker,
        parent_connection: tuple[RabbitMQTransport, fake_kombu.FakeConnection],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, parent = parent_connection
        closed_fds: list[int] = []
        monkeypatch.setattr(rabbitmq_publisher.os, "close", closed_fds.append)
        parent_fd = parent.connection.transport.sock.fileno()

        rabbitmq_publisher._forget_in_child()

        assert transport._publisher._proc is None
        assert parent.connection.calls == []
        assert closed_fds == [parent_fd]

    @pytest.mark.parametrize("hook", [True, False], ids=["fork-hook", "no-fork-hook"])
    def test_real_fork_child_gets_own_connection_and_parent_keeps_its_own(
        self, hook: bool
    ) -> None:
        script = _script(
            """
            import os, sys, json
            from stemtrace.transports import rabbitmq_publisher
            from stemtrace.transports.rabbitmq import RabbitMQTransport
            from tests import fake_kombu
            from tests.fake_kombu import sample_event as _event

            HOOK = bool(int(sys.argv[1]))
            broker = fake_kombu.FakeBroker(fake_kombu.FakeClock())
            fake_kombu.install(None, broker)
            transport = RabbitMQTransport("amqp://guest:guest@h1:5672//", prefix="t", ttl=60)
            transport.publish(_event("parent-1"))
            parent = broker.connections[0]
            if not HOOK:
                # What uWSGI does: fork in C, without Python's fork hooks.
                rabbitmq_publisher._publishers.discard(transport._publisher)

            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                transport.publish(_event("child-1"))
                report = {
                    "connections": len(broker.connections),
                    "child_conn_calls": broker.connections[-1].connection.calls,
                    "child_used_parent": broker.connections[-1] is parent,
                    "published": [b["task_id"] for _, b, _ in broker.published],
                }
                os.write(write_fd, json.dumps(report).encode())
                os._exit(0)
            os.close(write_fd)
            child_report = json.loads(os.read(read_fd, 65536))
            os.waitpid(pid, 0)
            # The child is gone; the parent's socket must still be usable.
            transport.publish(_event("parent-2"))
            print(json.dumps({
                "child": child_report,
                "parent_connections": len(broker.connections),
                "parent_calls": parent.connection.calls,
                "parent_published": [b["task_id"] for _, b, _ in broker.published],
            }))
            """
        )

        out = _run(script, str(int(hook)))

        assert out["child"]["connections"] == 2
        assert out["child"]["child_used_parent"] is False
        assert out["child"]["child_conn_calls"] == []
        assert out["child"]["published"] == ["parent-1", "child-1"]
        assert out["parent_connections"] == 1
        assert out["parent_calls"] == []
        assert out["parent_published"] == ["parent-1", "parent-2"]


class TestPublisherExit:
    def test_close_sends_amqp_close_and_discards_connection(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event())

        transport._publisher.close()

        amqp = broker.connections[0].connection
        assert amqp.calls == ["close", "collect"]
        assert amqp.timeout_at_close == CLOSE_TIMEOUT
        transport._publisher.close()  # idempotent
        assert amqp.calls == ["close", "collect"]

    def test_close_when_broker_never_answers_returns_without_raising(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.close_error = TimeoutError("timed out")  # no Close-Ok (blocked broker)
        transport = _transport(H1, clock)
        transport.publish(_event())

        transport._publisher.close()

        assert broker.live() == []

    def test_close_leaves_connection_in_use_by_another_thread(
        self, broker: FakeBroker, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event())
        monkeypatch.setattr(rabbitmq_publisher, "CLOSE_TIMEOUT", 0.05)
        proc = transport._publisher._process()

        with proc.lock:
            transport._publisher.close()

        assert broker.connections[0].connection.calls == []

    def test_close_in_forked_child_does_not_touch_parent_connection(
        self, broker: FakeBroker, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event())
        parent_pid = os.getpid()
        monkeypatch.setattr(rabbitmq_publisher.os, "getpid", lambda: parent_pid + 1)

        transport._publisher.close()

        assert broker.connections[0].connection.calls == []

    def test_interpreter_exit_closes_connection(self) -> None:
        script = _script(
            """
            import atexit, json
            # Registered before stemtrace's own, so it runs after it.
            report = []
            atexit.register(lambda: print(json.dumps(report[0].calls)))

            from stemtrace.transports.rabbitmq import RabbitMQTransport
            from tests import fake_kombu
            from tests.fake_kombu import sample_event

            broker = fake_kombu.FakeBroker(fake_kombu.FakeClock())
            fake_kombu.install(None, broker)
            transport = RabbitMQTransport("amqp://guest:guest@h1:5672//", prefix="t", ttl=60)
            transport.publish(sample_event())
            report.append(broker.connections[0].connection)
            """
        )

        out = _run(script)

        assert out == ["close", "collect"]


# --- gevent and eventlet -----------------------------------------------------

_GREEN_SCRIPT = """
import json, sys
{patch}
from stemtrace.transports.rabbitmq import RabbitMQTransport
from tests import fake_kombu
from tests.fake_kombu import sample_event as _event

broker = fake_kombu.FakeBroker(fake_kombu.FakeClock())
fake_kombu.install(None, broker)
state = {{"active": 0, "max_active": 0}}


def on_publish():
    # Yield to the hub while the publisher holds its lock, as a greenlet
    # doing socket I/O does.
    state["active"] += 1
    state["max_active"] = max(state["max_active"], state["active"])
    {sleep}(0)
    state["active"] -= 1


broker.on_publish = on_publish
transport = RabbitMQTransport("amqp://guest:guest@h1:5672//", prefix="t", ttl=60)
{body}
"""

_GEVENT_BODY = """
greenlets = [gevent.spawn(transport.publish, _event(f"t-{i}")) for i in range(100)]
gevent.joinall(greenlets, timeout=30)
done_all = all(g.ready() for g in greenlets)

# A greenlet killed while it holds the connection (e.g. a task time limit).
def killed_publisher():
    broker.on_publish = lambda: gevent.sleep(30)
    transport.publish(_event("t-killed"))

victim = gevent.spawn(killed_publisher)
gevent.sleep(0.05)
victim.kill(block=True, timeout=5)
broker.on_publish = None
transport.publish(_event("t-after-kill"))
print(json.dumps({
    "all_done": done_all,
    "max_active": state["max_active"],
    "published": len(broker.published),
    "connections": len(broker.connections),
    "first_calls": broker.connections[0].connection.calls,
    "last_published": broker.published[-1][1]["task_id"],
}))
"""

_EVENTLET_BODY = """
pool = eventlet.GreenPool(100)
for i in range(100):
    pool.spawn(transport.publish, _event(f"t-{i}"))
pool.waitall()

def killed_publisher():
    broker.on_publish = lambda: eventlet.sleep(30)
    transport.publish(_event("t-killed"))

victim = eventlet.spawn(killed_publisher)
eventlet.sleep(0.05)
victim.kill()
eventlet.sleep(0.05)
broker.on_publish = None
transport.publish(_event("t-after-kill"))
print(json.dumps({
    "all_done": True,
    "max_active": state["max_active"],
    "published": len(broker.published),
    "connections": len(broker.connections),
    "first_calls": broker.connections[0].connection.calls,
    "last_published": broker.published[-1][1]["task_id"],
}))
"""


class TestPublisherGreenPools:
    """Greenlets share the connection through the cooperative (patched) lock."""

    @pytest.mark.parametrize(
        ("patch", "sleep", "body"),
        [
            (
                "import gevent\nfrom gevent import monkey\nmonkey.patch_all()",
                "gevent.sleep",
                _GEVENT_BODY,
            ),
            (
                "import eventlet\neventlet.monkey_patch()",
                "eventlet.sleep",
                _EVENTLET_BODY,
            ),
        ],
        ids=["gevent", "eventlet"],
    )
    def test_publish_from_many_greenlets_shares_one_connection_without_deadlock(
        self, patch: str, sleep: str, body: str
    ) -> None:
        script = _script(_GREEN_SCRIPT.format(patch=patch, sleep=sleep, body=body))

        out = _run(script)

        assert out["all_done"] is True
        assert out["max_active"] == 1
        # 100 events, the killed greenlet's (its publish was interrupted
        # before reaching the broker) and the one after it.
        assert out["published"] == 101
        assert out["last_published"] == "t-after-kill"
        # The killed publish left half a frame on the wire: its connection
        # was dropped and the next event went out on a new one.
        assert out["connections"] == 2
        assert out["first_calls"] == ["collect"]


def _script(body: str) -> str:
    return textwrap.dedent(body)


def _run(script: str, *args: str) -> Any:
    """Run ``script`` in a fresh interpreter; return its last stdout line as JSON."""
    result = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-c", script, *args],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])
