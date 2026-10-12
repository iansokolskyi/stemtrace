"""A fake kombu publish stack that models the real semantics stemtrace relies on.

``install(monkeypatch_or_modules, broker)`` puts a fake ``kombu`` module into
``sys.modules``. What it models, with the library code it was read from
(kombu 5.6.1, py-amqp 5.3.1):

- ``Connection`` (kombu): a ``;``-separated URL becomes ``alt`` (one entry per
  host); ``maybe_switch_next()`` moves to the next host; ``ensure_connection``
  walks the hosts round-robin from the current one like ``retry_over_time``
  (raising once ``retries`` reaches ``max_retries`` *before* calling the
  errback, sleeping for the interval only after a full round of hosts has
  failed); errors kombu doesn't treat as recoverable (an authentication
  failure) are raised at once. The py-amqp connection is created by
  ``ensure_connection``. ``transport_options`` override the URL
  (``Transport.establish_connection`` merges them last), so the negotiated
  heartbeat is the option's, whatever ``?heartbeat=`` says.
- py-amqp ``Connection.blocking_read(timeout)`` reads real bytes from a
  socketpair, so readiness (``poll``), EOF and partial frames behave as on a
  TCP socket. ``Transport.read_frame()``/``_read()`` (same in 5.0.6 and 5.3.1):
  with ``timeout=0`` the *first* read of a frame raises ``socket.timeout`` on
  EAGAIN, but every later read of the same frame treats EAGAIN as ``continue``
  and spins until the frame completes or the peer closes (the fake fails the
  test instead of spinning); with a positive timeout a partial frame raises
  ``socket.timeout`` and its bytes are kept for the next read. EOF raises
  ``OSError('Server unexpectedly closed connection')``. A pending
  Connection.Close collects the connection and raises ``ConnectionForced``
  (``_on_close``), Blocked/Unblocked frames call ``on_blocked(reason)``/
  ``on_unblocked()`` (``_on_blocked``) and nothing else, a channel error frame
  raises (``Channel._on_close``). ``collect()`` shuts the socket down
  (``_AbstractTransport.close()`` calls ``shutdown(SHUT_RDWR)``, which in a
  forked child ends the parent's connection) and is safe on an already
  collected connection; where ``shutdown_error`` is set it raises before the
  socket is closed, as py-amqp 5.0.6 does after an RST (no try around it);
  ``.transport`` on a collected connection would make py-amqp reconnect, so
  the fake fails the test instead. ``close()`` waits for Close-Ok: a broker
  that stopped reading never sends it, so the wait times out (collecting the
  connection first).
- RabbitMQ: a memory/disk alarm blocks a connection only once a publish
  arrives after the alarm (Connection.Blocked is sent then), after which the
  broker stops reading it: later messages stay unread until Unblocked.
  Deleting the exchange makes the next publish close the channel (404).
  Publishing on a connection the broker already closed succeeds locally (the
  kernel accepts the write) and the event is lost.
- ``Producer.publish`` on a collected connection raises
  ``RecoverableConnectionError`` (``Connection already closed``); ``declare``
  entities are declared once per kombu connection (``maybe_declare`` caches in
  ``Connection.declared_entities``).
"""

from __future__ import annotations

import contextlib
import socket
import struct
import sys
import time
import types
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from amqp.exceptions import ConnectionForced, NotFound, RecoverableConnectionError

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.transports.rabbitmq_publisher import track_connecting_transport

# Reads of one frame that hit EAGAIN before the fake declares py-amqp spinning.
SPIN_LIMIT = 100_000

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def sample_event(task_id: str = "t-1") -> TaskEvent:
    """A task event with the given id."""
    return TaskEvent(
        task_id=task_id,
        name="tests.publish",
        state=TaskState.STARTED,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class PyAmqpSpinning(BaseException):
    """py-amqp would busy-spin here (BaseException, so no ``except Exception`` hides it)."""


class FakeClock:
    """Monotonic clock the test advances by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBroker:
    """State shared by the fake kombu module: which hosts are down, what arrived."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.down: set[str] = set()
        # Hosts that refuse the login (not a recoverable error for kombu).
        self.auth_refused: set[str] = set()
        self.publish_error: BaseException | None = None
        # Hosts where publish_error applies (None: every host).
        self.publish_error_hosts: set[str] | None = None
        # Seconds Producer.publish takes (advances the fake clock).
        self.publish_delay = 0.0
        # Raised by the AMQP close handshake, e.g. socket.timeout while a
        # blocked broker (memory/disk alarm) never sends Close-Ok.
        self.close_error: Exception | None = None
        # Called at the start of every Producer.publish (e.g. to yield to
        # another greenlet or thread while the publisher holds its lock).
        self.on_publish: Callable[[], None] | None = None
        # Seconds a connect takes (a slow but healthy broker); real time.
        self.connect_delay = 0.0
        # A transport-like object the connect "has started" (with a ``sock``)
        # when it fails: what py-amqp leaves behind in a killed connect.
        self.half_open_transport: Any = None
        # Raised by Connection.channel() (e.g. the broker refuses the channel).
        self.channel_error: Exception | None = None
        # Raised by collect() before the socket is closed (py-amqp 5.0.6 after an RST).
        self.shutdown_error: OSError | None = None
        self.alarm = False
        self.exchange_exists = True
        self.connections: list[FakeConnection] = []
        # (host, body, kwargs) of every message the broker received.
        self.published: list[tuple[str | None, Any, dict[str, Any]]] = []
        # Messages sent while blocked: received once unblocked.
        self.unread: list[tuple[str | None, Any, dict[str, Any]]] = []

    def release_sockets(self) -> None:
        """Close what a test left open (an unclosed socket fails the run)."""
        for conn in self.live():
            conn.amqp.collect()

    def live(self) -> list[FakeConnection]:
        """Connections that are open (not collected)."""
        return [c for c in self.connections if c.amqp is not None and c.amqp.open]

    def close_idle_connections(self) -> None:
        """The broker restarts or kills every connection: the peers see EOF."""
        for conn in self.live():
            conn.amqp.transport.peer.close()

    def force_close(self, conn: FakeConnection) -> None:
        """The broker sends Connection.Close (e.g. closed via management)."""
        conn.amqp.deliver("close")

    def raise_alarm(self) -> None:
        """A memory/disk alarm starts (connections block on their next publish)."""
        self.alarm = True

    def clear_alarm(self) -> None:
        """The alarm ends: blocked connections get Unblocked and are read again."""
        self.alarm = False
        for conn in self.live():
            if conn.amqp.broker_blocked:
                conn.amqp.broker_blocked = False
                conn.amqp.deliver("unblocked")
        self.published.extend(self.unread)
        self.unread.clear()

    def delete_exchange(self) -> None:
        """The exchange is deleted: the next publish closes its channel."""
        self.exchange_exists = False


class _RecordingSocket(socket.socket):
    """A socket that records ``detach()`` (how a forked child drops its copy)."""

    detached = False

    def detach(self) -> int:
        self.detached = True
        return super().detach()


class _FakeAmqpTransport:
    def __init__(self) -> None:
        sock, self.peer = socket.socketpair()
        self.sock = _RecordingSocket(fileno=sock.detach())
        # py-amqp TCPTransport._setup_transport(): reads go through this.
        self._quick_recv = self.sock.recv


class FakeAmqpConnection:
    """py-amqp Connection, as far as the publisher uses it."""

    def __init__(self, broker: FakeBroker, heartbeat: float | None) -> None:
        self.broker = broker
        self.heartbeat = heartbeat
        self._transport: _FakeAmqpTransport | None = _FakeAmqpTransport()
        self.calls: list[str] = []
        self.open = True
        self.on_blocked: Callable[[str], None] | None = None
        self.on_unblocked: Callable[[], None] | None = None
        # What each complete frame on the wire means, in order.
        self.kinds: list[tuple[str, Any]] = []
        # Bytes of a frame the broker has not finished sending.
        self._tail = b""
        # Received bytes not yet consumed as a frame (py-amqp's _read_buffer).
        self._rbuf = b""
        self.broker_blocked = False
        self.reads = 0
        # Set when collect() raised before closing the socket (shutdown_error).
        self.collect_failed = False
        self.leaked_sock: socket.socket | None = None
        # Socket timeout when collect() ran: a TLS unwrap waits that long.
        self.timeout_at_collect: float | None = None
        # Socket timeout when close() ran: the wait for Close-Ok.
        self.timeout_at_close: float | None = None

    def deliver(
        self,
        kind: str,
        arg: Any = None,
        *,
        send_bytes: int | None = None,
        payload_size: int = 1,
    ) -> None:
        """The broker sends a frame; ``send_bytes`` sends only its first bytes."""
        frame = (
            struct.pack(">BHI", 1, 0, payload_size) + b"\x00" * payload_size + b"\xce"
        )
        self.kinds.append((kind, arg))
        head = frame if send_bytes is None else frame[:send_bytes]
        self._tail = b"" if send_bytes is None else frame[send_bytes:]
        self.transport.peer.sendall(head)

    def finish_frame(self) -> None:
        """The broker sends the rest of a frame it only started."""
        tail, self._tail = self._tail, b""
        self.transport.peer.sendall(tail)

    @property
    def transport(self) -> _FakeAmqpTransport:
        if self._transport is None:
            raise AssertionError(
                "py-amqp 5.3 Connection.transport on a collected connection "
                "reconnects (with a deprecation warning)"
            )
        return self._transport

    @property
    def connected(self) -> bool:
        return self.open

    def _fill(self, n: int, *, initial: bool) -> None:
        """py-amqp Transport._read(): wait for ``n`` buffered bytes."""
        spins = 0
        while len(self._rbuf) < n:
            try:
                data = self.transport._quick_recv(n - len(self._rbuf))
            except BlockingIOError:  # the socket timeout is 0: EAGAIN
                if initial:
                    raise TimeoutError from None
                spins += 1
                if spins > SPIN_LIMIT:
                    raise PyAmqpSpinning(
                        "py-amqp Transport._read() spins on EAGAIN after the "
                        "first read of a frame (transport.py `continue`)"
                    ) from None
                continue
            if not data:
                raise OSError("Server unexpectedly closed connection")
            self._rbuf += data

    def blocking_read(self, timeout: float | None = None) -> None:
        """Read one frame and run its handler, like py-amqp's."""
        if timeout is None:
            raise AssertionError("the publisher must never wait without a bound")
        self.reads += 1
        if not self.open:
            raise RecoverableConnectionError("Connection already closed")
        sock = self.transport.sock
        previous = sock.gettimeout()
        sock.settimeout(timeout)
        try:
            self._fill(7, initial=True)
            size = struct.unpack(">BHI", self._rbuf[:7])[2]
            self._fill(7 + size + 1, initial=False)
        finally:
            sock.settimeout(previous)
        self._rbuf = self._rbuf[7 + size + 1 :]
        kind, arg = self.kinds.pop(0)
        if kind == "blocked":
            if self.on_blocked is not None:
                self.on_blocked("connection blocked, see broker logs")
        elif kind == "unblocked":
            if self.on_unblocked is not None:
                self.on_unblocked()
        elif kind == "close":
            self.collect()
            raise ConnectionForced(
                "(0, 0): (320) CONNECTION_FORCED - Closed via management plugin"
            )
        elif kind == "channel_error":
            raise arg

    def drain_events(self, timeout: float | None = None) -> None:
        """py-amqp: read frames until one handler returns something (none do here)."""
        while True:
            self.blocking_read(timeout)

    def close(self) -> None:
        if self._transport is None:
            return  # py-amqp: already closed
        self.calls.append("close")
        self.timeout_at_close = self.transport.sock.gettimeout()
        error = self.broker.close_error
        self.collect()
        if error is not None:
            raise error

    def collect(self) -> None:
        if self._transport is None:
            return  # py-amqp: nothing left to collect
        self.calls.append("collect")
        transport, self._transport = self._transport, None
        self.open = False
        if transport is not None:
            self.timeout_at_collect = transport.sock.gettimeout()
            # What _AbstractTransport.close() does.
            if self.broker.shutdown_error is not None:
                self.collect_failed = True
                self.leaked_sock = transport.sock
                transport.peer.close()
                raise self.broker.shutdown_error
            with contextlib.suppress(OSError):
                transport.sock.shutdown(socket.SHUT_RDWR)
            transport.sock.close()
            transport.peer.close()


class FakeChannel:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection


class FakeConnection:
    """kombu Connection; ``install()`` binds ``broker``."""

    broker: ClassVar[FakeBroker]
    retry_limit: ClassVar[int] = 50

    def __init__(self, url: str, **kwargs: Any) -> None:
        self.url = url
        self.kwargs = kwargs
        self.alt = url.split(";") if ";" in url else []
        self.switches = 0
        self._index = 0
        self.ensure_calls: list[dict[str, Any]] = []
        self.attempted: list[str] = []
        self.sleeps: list[float] = []
        self.host: str | None = None
        self.amqp: FakeAmqpConnection | None = None
        self.sock_timeout_at_channel: float | None = None
        self.declared: set[str] = set()
        self.declare_calls = 0
        self.broker.connections.append(self)

    @property
    def connection(self) -> FakeAmqpConnection:
        if self.amqp is None:
            raise AssertionError("connection before ensure_connection()")
        return self.amqp

    def maybe_switch_next(self) -> None:
        self.switches += 1
        if self.alt:
            self._index = (self._index + 1) % len(self.alt)

    def ensure_connection(
        self,
        *,
        errback: Callable[[Exception, float], None] | None = None,
        max_retries: int | None = None,
        interval_start: float = 2,
        interval_step: float = 2,
        interval_max: float = 30,
        reraise_as_library_errors: bool = True,
    ) -> FakeConnection:
        self.ensure_calls.append(
            {
                "errback": errback,
                "max_retries": max_retries,
                "interval_start": interval_start,
                "interval_step": interval_step,
                "interval_max": interval_max,
                "reraise_as_library_errors": reraise_as_library_errors,
            }
        )
        del interval_step, interval_max
        hosts = self.alt or [self.url]
        retries = 0
        while True:
            host = hosts[(self._index + retries) % len(hosts)]
            self.attempted.append(host)
            if host in self.broker.auth_refused:
                raise PermissionError(f"ACCESS_REFUSED on {host}")
            if self.broker.half_open_transport is not None:
                track_connecting_transport(self.broker.half_open_transport)
                raise ConnectionRefusedError("killed mid-connect")
            if host not in self.broker.down:
                if self.broker.connect_delay:
                    time.sleep(self.broker.connect_delay)
                self.host = host
                options = self.kwargs.get("transport_options") or {}
                self.amqp = FakeAmqpConnection(self.broker, options.get("heartbeat"))
                return self
            exc = ConnectionRefusedError(f"{host} refused")
            if max_retries is not None and retries >= max_retries:
                raise exc
            if retries >= self.retry_limit:
                raise RuntimeError("runaway connect loop")
            completes_round = (retries + 1) % len(hosts) == 0
            interval = interval_start if completes_round else 0
            if errback is not None:
                errback(exc, interval)
            if interval:
                self.sleeps.append(interval)
            retries += 1

    def channel(self) -> FakeChannel:
        amqp = self.connection
        if self.broker.channel_error is not None:
            raise self.broker.channel_error
        self.sock_timeout_at_channel = amqp.transport.sock.gettimeout()
        return FakeChannel(self)

    def release(self) -> None:
        raise AssertionError("the publisher must not release() (it would close)")


class FakeExchange:
    def __init__(self, name: str, *, type: str, durable: bool) -> None:
        self.name = name
        self.type = type
        self.durable = durable


class FakeProducer:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    def publish(self, body: Any, **kwargs: Any) -> None:
        connection = self.channel.connection
        broker = connection.broker
        amqp = connection.connection
        if broker.on_publish is not None:
            broker.on_publish()
        if not amqp.open:
            raise RecoverableConnectionError("connection already closed")
        # py-amqp 5.3.1 Channel._basic_publish: with the connection.blocked
        # capability (RabbitMQ), every publish first reads what is pending.
        with contextlib.suppress(socket.timeout):
            amqp.drain_events(timeout=0)
        broker.clock.advance(broker.publish_delay)
        host = connection.host
        if broker.publish_error is not None and (
            broker.publish_error_hosts is None or host in broker.publish_error_hosts
        ):
            raise broker.publish_error
        for entity in kwargs.get("declare") or []:
            if entity.name not in connection.declared:
                connection.declared.add(entity.name)
                connection.declare_calls += 1
        message = (host, body, kwargs)
        if amqp.broker_blocked:
            broker.unread.append(message)
            return
        if broker.alarm:
            amqp.broker_blocked = True
            amqp.deliver("blocked")
            broker.unread.append(message)
            return
        if not broker.exchange_exists:
            amqp.deliver(
                "channel_error",
                NotFound(
                    f"Basic.publish: (404) NOT_FOUND - no exchange "
                    f"{kwargs['exchange'].name!r} in vhost '/'"
                ),
            )
            return
        # A peer that already closed still accepts the write locally.
        broker.published.append(message)


def install(monkeypatch: pytest.MonkeyPatch | None, broker: FakeBroker) -> None:
    """Install the fake kombu module into ``sys.modules``.

    Args:
        monkeypatch: pytest's, so the module is removed after the test; None
            for scripts that run in their own process.
        broker: State shared with the test.
    """
    fake_kombu = types.ModuleType("kombu")
    # The publisher builds Connection(url, **kwargs) itself, so bind the
    # broker on a per-test subclass rather than passing it in.
    fake_kombu.Connection = type("Connection", (FakeConnection,), {"broker": broker})  # type: ignore[attr-defined]
    fake_kombu.Exchange = FakeExchange  # type: ignore[attr-defined]
    fake_kombu.Producer = FakeProducer  # type: ignore[attr-defined]
    if monkeypatch is None:
        sys.modules["kombu"] = fake_kombu
    else:
        monkeypatch.setitem(sys.modules, "kombu", fake_kombu)
