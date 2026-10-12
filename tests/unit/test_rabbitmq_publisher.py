"""The RabbitMQ publisher connection: one per process, reused, replaced when dead.

``tests/fake_kombu.py`` stands in for kombu and py-amqp (its docstring lists the
real behavior it models and where that was read). Scripts that fork, exit or
monkey-patch run in their own interpreter.
"""

import errno
import gc
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
import warnings
import weakref
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from amqp.connection import Connection as AmqpConnection
from amqp.transport import TCPTransport

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports import rabbitmq, rabbitmq_publisher
from stemtrace.transports.rabbitmq import (
    CONNECT_TIMEOUT,
    PUBLISH_FAILURES_TO_OPEN,
    PUBLISH_IO_TIMEOUT,
    RabbitMQTransport,
)
from stemtrace.transports.rabbitmq_publisher import (
    CLOSE_TIMEOUT,
    DISCARD_TIMEOUT,
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


class TestPublisherConnectFailures:
    def test_publish_when_channel_cannot_open_discards_connection_and_rotates_host(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        broker.channel_error = RuntimeError("channel refused")
        transport = _transport(f"{H1};{H2}", clock)

        transport.publish(_event("t-0"))
        broker.channel_error = None
        transport.publish(_event("t-1"))

        assert broker.connections[0].connection.calls == ["collect"]
        assert broker.connections[1].attempted == [H2]
        assert _ids(broker) == ["t-1"]

    def test_bound_io_without_a_socket_logs_and_returns_none(
        self, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(
            logging.DEBUG, logger="stemtrace.transports.rabbitmq_publisher"
        )
        publisher = _transport(H1, clock)._publisher

        sock = publisher._bound_io(
            SimpleNamespace(transport=SimpleNamespace(sock=None))
        )

        assert sock is None
        assert "no socket to bound (got NoneType)" in caplog.text


class TestPublisherModuleHooks:
    def test_close_all_closes_every_publisher_of_the_process(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        first, second = _transport(H1, clock), _transport(H2, clock)
        first.publish(_event("t-1"))
        second.publish(_event("t-2"))

        rabbitmq_publisher.close_all()

        assert broker.live() == []

    def test_blocked_callback_after_owner_is_gone_does_nothing(self) -> None:
        proc = rabbitmq_publisher._Process()
        proc_ref = weakref.ref(proc)
        del proc

        rabbitmq_publisher._set_blocked(proc_ref, True, "reason")


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

    def test_publish_waits_for_connection_in_use_then_publishes_without_failure(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        proc = transport._publisher._process()
        proc.lock.acquire()
        proc.held_since = time.monotonic()  # another publish is in progress
        threading.Timer(0.2, proc.lock.release).start()

        started = time.monotonic()
        transport.publish(_event("t-2"))

        assert 0.15 <= time.monotonic() - started < 5.0
        assert _ids(broker) == ["t-1", "t-2"]
        assert _warnings(caplog) == []

    def test_publish_skips_attempt_when_breaker_moved_on_while_waiting(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        publisher = _transport(H1, clock)._publisher

        with pytest.raises(PublishSkippedError):
            publisher.publish({"task_id": "t"}, should_attempt=lambda: False)

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
                # Before publishing anything: did the fork hook already drop
                # the parent's descriptor (so the child can't keep it open)?
                dropped_at_fork = parent.connection.transport.sock.detached
                transport.publish(_event("child-1"))
                report = {
                    "dropped_at_fork": dropped_at_fork,
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

        assert out["child"]["dropped_at_fork"] is hook
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


class TestPyAmqpReadSemantics:
    """What the publisher's read relies on, checked on the installed py-amqp.

    These use py-amqp's real transport on a socketpair; they ground the fake
    (``tests/fake_kombu.py``), whose frame handling copies this behavior.
    """

    HEADER = struct.pack(">BHI", 1, 0, 1)  # type, channel, payload size

    @staticmethod
    def _transport(sock: socket.socket) -> TCPTransport:
        transport = TCPTransport("localhost:5672")
        transport.sock = sock
        transport._setup_transport()
        transport.connected = True
        return transport

    def test_zero_timeout_read_after_frame_header_spins_until_peer_closes(
        self,
    ) -> None:
        # py-amqp Transport._read(): EAGAIN is "continue" after the first
        # read of a frame, so a zero-timeout read of a partial frame never
        # returns (5.0.6 and 5.3.1).
        ours, theirs = socket.socketpair()
        transport = self._transport(ours)
        theirs.sendall(self.HEADER)
        outcome: list[BaseException] = []

        def read() -> None:
            try:
                with transport.having_timeout(0):
                    transport.read_frame()
            except BaseException as exc:  # noqa: BLE001  recorded for the assertion
                outcome.append(exc)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        spinning = thread.is_alive()
        theirs.close()  # the read ends once the peer closes
        thread.join(timeout=10)
        ours.close()

        assert spinning is True
        assert isinstance(outcome[0], OSError)

    def test_positive_timeout_read_keeps_partial_frame_for_the_next_read(self) -> None:
        ours, theirs = socket.socketpair()
        transport = self._transport(ours)
        theirs.sendall(self.HEADER)

        with pytest.raises(socket.timeout), transport.having_timeout(0.05):
            transport.read_frame()
        theirs.sendall(b"\x00\xce")
        with transport.having_timeout(0.05):
            frame = transport.read_frame()
        ours.close()
        theirs.close()

        assert frame == (1, 0, b"\x00")


class TestResolvingTransportsAreBounded:
    """Real py-amqp transports, as the publisher builds them."""

    def test_connect_to_peer_that_resets_leaves_no_unclosed_socket(self) -> None:
        # py-amqp 5.0.6 _AbstractTransport.close() calls sock.shutdown()
        # without a try: after an RST it raises ENOTCONN and skips
        # sock.close(), and connect() calls it when the handshake fails.
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]

            def reset_first_connection() -> None:
                conn, _ = server.accept()
                conn.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                conn.close()

            acceptor = threading.Thread(target=reset_first_connection)
            acceptor.start()
            connection = rabbitmq._resolving_amqp_connection_class(AmqpConnection)(
                host=f"127.0.0.1:{port}", connect_timeout=2
            )
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    connection.connect()
                except OSError:
                    failed = True
                else:
                    failed = False
                del connection
                gc.collect()
            acceptor.join(timeout=5)

        assert failed is True
        assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []

    def test_publish_to_silent_tls_peer_returns_within_the_connect_timeout(
        self,
    ) -> None:
        # py-amqp 5.0.6 SSLTransport._setup_transport() runs the handshake on a
        # socket without a timeout (_init_socket just removed it), so a peer
        # that accepts and never answers hangs the publisher under its lock.
        script = _script(
            """
            import json, socket, time
            from datetime import datetime, timezone
            from stemtrace.core.events import TaskEvent, TaskState
            from stemtrace.transports import rabbitmq
            from stemtrace.transports.rabbitmq import RabbitMQTransport

            rabbitmq.CONNECT_TIMEOUT = 0.5
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            port = server.getsockname()[1]
            held = []
            import threading
            def accept():
                while True:
                    held.append(server.accept()[0])
            threading.Thread(target=accept, daemon=True).start()
            transport = RabbitMQTransport(
                f"amqps://guest:guest@127.0.0.1:{port}//", prefix="t", ttl=60
            )
            started = time.monotonic()
            transport.publish(TaskEvent(
                task_id="t", name="n", state=TaskState.STARTED,
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ))
            print(json.dumps({"elapsed": time.monotonic() - started}))
            """
        )

        out = _run(script)

        assert out["elapsed"] < 5.0


class TestPublisherPartialFrames:
    """A half-received frame must neither spin nor stall the publisher."""

    def test_publish_with_half_a_frame_pending_keeps_connection_and_does_not_spin(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        # The broker started a Connection.Close and froze (docker pause): only
        # the 7-byte frame header arrived. py-amqp's _read() spins on EAGAIN
        # after the first read of a frame, so a zero-timeout read never returns.
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        amqp = broker.connections[0].connection
        amqp.deliver("close", send_bytes=7)

        started = time.monotonic()
        transport.publish(_event("t-2"))
        elapsed = time.monotonic() - started

        assert elapsed < 0.5
        assert len(broker.connections) == 1
        assert _ids(broker) == ["t-1", "t-2"]

    def test_publish_after_half_frame_completes_processes_the_whole_frame(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        amqp = broker.connections[0].connection
        amqp.deliver("close", send_bytes=7)
        transport.publish(_event("t-2"))

        amqp.finish_frame()  # the broker thaws; the buffered header is kept
        transport.publish(_event("t-3"))

        # The close frame was read intact: the connection was replaced and
        # the event still went out.
        assert len(broker.connections) == 2
        assert _ids(broker) == ["t-1", "t-2", "t-3"]

    def test_publish_with_no_frame_pending_does_not_read_the_connection(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))

        for i in range(20):
            transport.publish(_event(f"t-{i + 2}"))

        # Nothing to read: no read attempt (and no socket timeout changes).
        assert broker.connections[0].connection.reads == 0


class TestPublisherLockedStepsAreBounded:
    """The connection lock is held by steps whose real worst case is known."""

    def test_publish_while_connect_is_slower_than_one_timeout_does_not_open_breaker(
        self, broker: FakeBroker, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A slow but healthy broker: connecting takes 0.4s, several
        # connect/IO timeouts' worth, but each step is within its own timeout.
        monkeypatch.setattr(rabbitmq, "CONNECT_TIMEOUT", 0.05)
        monkeypatch.setattr(rabbitmq, "PUBLISH_IO_TIMEOUT", 0.05)
        broker.connect_delay = 0.4
        transport = _transport(H1, clock)
        threads = [
            threading.Thread(target=transport.publish, args=(_event(f"t-{i}"),))
            for i in range(4)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(_ids(broker)) == [f"t-{i}" for i in range(4)]
        assert transport._publish_breaker.is_open is False
        assert len(broker.connections) == 1

    def test_lock_wait_covers_every_step_of_a_connect_pass(
        self, clock: FakeClock
    ) -> None:
        publisher = _transport(f"{H1};{H2};amqp://h3", clock)._publisher

        # Per host: resolve, TCP connect, TLS handshake and the Start, Tune and
        # Open-Ok replies each wait up to the connect timeout; then channel
        # open, exchange declare and the write each wait up to the IO timeout.
        per_host = 6 * CONNECT_TIMEOUT
        assert publisher._lock_timeout >= 3 * per_host + 3 * PUBLISH_IO_TIMEOUT

    def test_breaker_probe_timeout_outlasts_the_longest_publish(
        self, clock: FakeClock
    ) -> None:
        transport = _transport(f"{H1};{H2};amqp://h3;amqp://h4", clock)

        one_pass = 4 * 6 * CONNECT_TIMEOUT + 3 * PUBLISH_IO_TIMEOUT
        # A probe may wait for the connection, then connect: a probe still
        # running is not presumed lost.
        assert transport._publish_breaker._probe_timeout >= 2 * one_pass

    def test_publish_skipped_after_breaker_closed_again_still_publishes(
        self,
        broker: FakeBroker,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-0"))
        breaker = transport._publish_breaker
        issued = threading.Event()
        real_allow = breaker.allow

        def allow_and_signal() -> int | None:
            ticket = real_allow()
            issued.set()
            return ticket

        breaker.allow = allow_and_signal  # type: ignore[method-assign]
        proc = transport._publisher._process()
        waiter = threading.Thread(target=transport.publish, args=(_event("t-wait"),))
        with proc.lock:  # another publish holds the connection
            waiter.start()
            assert issued.wait(5.0)
            breaker.allow = real_allow  # type: ignore[method-assign]
            # While it waits, the breaker opens and closes again.
            tickets = [breaker.allow() for _ in range(PUBLISH_FAILURES_TO_OPEN)]
            for ticket in tickets:
                assert ticket is not None
                breaker.record_failure(ticket, "x", OSError("down"))
            clock.advance(1.0)
            probe = breaker.allow()
            assert probe is not None
            breaker.record_success(probe)
            assert breaker.is_open is False
        waiter.join(timeout=10)

        # The broker is healthy again: the waiting event is published, and
        # nothing is logged as a failure for it.
        assert _ids(broker) == ["t-0", "t-wait"]
        assert not [r for r in _warnings(caplog) if "t-wait" in r.getMessage()]


class _Interrupt(BaseException):
    """What a signal handler raises (SoftTimeLimitExceeded, SystemTerminate)."""


class _InterruptedAfterAcquire:
    """Lock whose first acquire() is interrupted right after it took the lock.

    A signal handler runs between ``acquire()`` returning and the next line of
    the caller; Python can't close that window, so the exception is raised
    from inside the caller while the lock is held.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.armed = True

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        taken = self._lock.acquire(blocking, timeout)
        if taken and self.armed:
            self.armed = False
            raise _Interrupt
        return taken

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


class TestPublisherHealsStuckLock:
    def test_publish_after_lock_leaked_by_interrupt_recovers_with_new_connection(
        self,
        broker: FakeBroker,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        publisher = transport._publisher
        publisher._lock_timeout = 0.05  # a whole worst-case publish, shrunk
        publisher._process().lock = _InterruptedAfterAcquire()  # type: ignore[assignment]
        with pytest.raises(_Interrupt):
            transport.publish(_event("t-interrupted"))

        started = time.monotonic()
        transport.publish(_event("t-2"))
        transport.publish(_event("t-3"))

        assert time.monotonic() - started < 5.0
        assert _ids(broker) == ["t-1", "t-2", "t-3"]
        assert "not released" in caplog.text
        # The old connection is dropped with its state, not reused.
        assert len(broker.connections) == 2

    def test_publish_waiting_behind_ever_younger_holders_drops_without_failure(
        self, broker: FakeBroker, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = _transport(H1, clock)
        transport.publish(_event("t-1"))
        publisher = transport._publisher
        publisher._lock_timeout = 0.05
        proc = publisher._process()
        stop = threading.Event()

        def busy_holders() -> None:
            # Back-to-back publishes by other threads: the lock is never free
            # and each holder is young.
            with proc.lock:
                while not stop.is_set():
                    proc.held_since = time.monotonic()
                    time.sleep(0.005)
                proc.held_since = None

        holders = threading.Thread(target=busy_holders)
        holders.start()
        time.sleep(0.05)
        try:
            transport.publish(_event("t-busy"))
        finally:
            stop.set()
            holders.join(timeout=5)

        # Contention, not a broker failure: nothing logged, breaker untouched.
        assert _warnings(caplog) == []
        assert transport._publish_breaker.is_open is False


class TestPublisherDiscardClosesSocket:
    def test_discard_closes_socket_even_when_collect_fails_before_closing_it(
        self, broker: FakeBroker, clock: FakeClock
    ) -> None:
        # py-amqp 5.0.6 _AbstractTransport.close() calls sock.shutdown()
        # without a try: after an RST it raises ENOTCONN and skips close().
        broker.shutdown_error = OSError(errno.ENOTCONN, "not connected")
        broker.publish_error = TimeoutError("timed out")
        transport = _transport(H1, clock)

        transport.publish(_event("t-0"))  # connects, fails, discards

        amqp = broker.connections[0].connection
        assert amqp.collect_failed is True
        assert amqp.leaked_sock is not None
        assert amqp.leaked_sock.fileno() == -1


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


_PARTIAL_SCRIPT = """
import json, time
{patch}
from stemtrace.transports.rabbitmq import RabbitMQTransport
from tests import fake_kombu
from tests.fake_kombu import sample_event

broker = fake_kombu.FakeBroker(fake_kombu.FakeClock())
fake_kombu.install(None, broker)
transport = RabbitMQTransport("amqp://guest:guest@h1:5672//", prefix="t", ttl=60)
transport.publish(sample_event("t-0"))
# The broker started a Connection.Close and froze: only the header arrived.
broker.connections[0].connection.deliver("close", send_bytes=7)

gaps = []
def ticker():
    last = time.monotonic()
    while True:
        {sleep}(0.01)
        now = time.monotonic()
        gaps.append(now - last)
        last = now

{spawn_ticker}
{run_publish}
print(json.dumps({{
    "published": [b["task_id"] for _, b, _ in broker.published],
    "max_gap": max(gaps) if gaps else None,
    "connections": len(broker.connections),
}}))
"""

_GEVENT_PARTIAL = (
    "from gevent import monkey; monkey.patch_all(); import gevent",
    "gevent.sleep",
    "gevent.spawn(ticker)",
    "pub = gevent.spawn(transport.publish, sample_event('t-1')); pub.join(timeout=10)",
)
_EVENTLET_PARTIAL = (
    "import eventlet; eventlet.monkey_patch()",
    "eventlet.sleep",
    "eventlet.spawn(ticker)",
    "pub = eventlet.spawn(transport.publish, sample_event('t-1')); pub.wait()",
)


class TestPublisherPartialFrameUnderGreenPools:
    @pytest.mark.parametrize(
        "variant", [_GEVENT_PARTIAL, _EVENTLET_PARTIAL], ids=["gevent", "eventlet"]
    )
    def test_publish_with_half_a_frame_pending_never_stalls_the_hub(
        self, variant: tuple[str, str, str, str]
    ) -> None:
        patch, sleep, spawn_ticker, run_publish = variant
        script = _script(
            _PARTIAL_SCRIPT.format(
                patch=patch,
                sleep=sleep,
                spawn_ticker=spawn_ticker,
                run_publish=run_publish,
            )
        )

        out = _run(script)

        assert out["published"] == ["t-0", "t-1"]
        assert out["connections"] == 1
        # Other greenlets kept running while the partial frame was handled.
        assert out["max_gap"] < 0.5


_KILL_CONNECT_SCRIPT = """
import json, os, socket
{patch}
from datetime import datetime, timezone
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports.rabbitmq import RabbitMQTransport

server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(4)
port = server.getsockname()[1]
held = []
{spawn}(lambda: held.append(server.accept()[0]))


def sockets():
    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            count += "socket" in os.readlink("/proc/self/fd/" + name)
        except OSError:
            pass
    return count


transport = RabbitMQTransport(f"amqp://guest:guest@127.0.0.1:{{port}}//", prefix="t", ttl=60)
before = sockets()
event = TaskEvent(task_id="a", name="n", state=TaskState.STARTED,
                  timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
# The broker accepts and never answers: the connect waits in the handshake.
victim = {spawn}(transport.publish, event)
{sleep}(0.5)
mid_connect = sockets() - before
{kill}
print(json.dumps({{"mid_connect": mid_connect, "after_kill": sockets() - before,
                  "accepted": len(held)}}))
"""


class TestPublisherKilledMidConnect:
    @pytest.mark.parametrize(
        ("patch", "spawn", "sleep", "kill"),
        [
            (
                "from gevent import monkey; monkey.patch_all(); import gevent",
                "gevent.spawn",
                "gevent.sleep",
                "victim.kill(block=True, timeout=5)",
            ),
            (
                "import eventlet; eventlet.monkey_patch()",
                "eventlet.spawn",
                "eventlet.sleep",
                "victim.kill(); eventlet.sleep(0.1)",
            ),
        ],
        ids=["gevent", "eventlet"],
    )
    def test_greenlet_killed_while_connecting_closes_its_half_open_socket(
        self, patch: str, spawn: str, sleep: str, kill: str
    ) -> None:
        script = _script(
            _KILL_CONNECT_SCRIPT.format(
                patch=patch, spawn=spawn, sleep=sleep, kill=kill
            )
        )

        out = _run(script)

        # Mid-connect: the client's socket and the one the listener accepted.
        assert out["mid_connect"] == 2
        # After the kill only the accepted one is left (the dead stack no
        # longer holds the other, and nothing waits for the garbage collector).
        assert out["after_kill"] == 1


def _script(body: str) -> str:
    return textwrap.dedent(body)


def _run(script: str, *args: str) -> Any:
    """Run ``script`` in a fresh interpreter; return its last stdout line as JSON."""
    result = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-c", script, *args],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(REPO_ROOT), os.environ.get("PYTHONPATH", "")]
            ),
        },
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])
