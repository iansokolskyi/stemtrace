"""One long-lived AMQP connection per process for publishing events to RabbitMQ.

Opening a connection per event (TCP connect, AMQP handshake, channel, close)
costs about 9 ms; publishing on a connection that is already open costs about
0.3 ms. This module owns that connection for :class:`RabbitMQTransport`, and
keeps every guarantee the per-event connection gave:

**Bounded.** Connecting tries each host of the URL once (``connect_timeout``,
no kombu retry sleeps), and every later socket read or write times out after
``io_timeout``. The longest the connection can legitimately be held is
derived from the steps of one attempt (:meth:`PublisherConnection.
_attempt_bound`), and every wait for it is bounded by that.

**Never raises into a task.** :meth:`PublisherConnection.publish` raises; the
transport turns every exception into a breaker failure.

**One thread at a time.** kombu and py-amqp connections are not thread-safe
(a channel's frames must not interleave), so one lock serializes publishes.
It is held for one readability check and one socket write (microseconds),
or for one bounded connect pass. A signal handler can raise between
``acquire()`` returning and the ``try`` that releases (Python can't close
that window), and a holder can outlive its bound; a waiter that has waited a
whole bound and finds the holder gone or past it starts over with a new
connection state, leaving the old socket to its owner. Per-thread connections were rejected: under gevent and
eventlet ``threading.local`` is greenlet-local and Celery runs every task in
a new greenlet, so every task would open a connection, and a threads pool
would hold one connection per thread. Publisher confirms were rejected too:
waiting for Basic.Ack would hold the lock for a network round trip per event.

**Fork-safe.** A child never uses its parent's socket: ``os.register_at_fork``
(Python's forks, including Celery's prefork pool) and a PID check (forks that
skip the hooks, e.g. uWSGI's) start the child with no connection and a new
lock (a parent thread may hold the old one at fork time). The inherited
socket is never shut down or closed with the AMQP handshake: py-amqp 5.3
``_AbstractTransport.close()`` calls ``sock.shutdown(SHUT_RDWR)``, which
would end the parent's connection. Only the child's copy of the file
descriptor is closed.

**A dead connection is noticed before it loses an event.** Before each
publish the socket is read without waiting (``drain_events(timeout=0)``), the
only way py-amqp 5.3 processes inbound frames. A connection closed by the
broker (restart, ``rabbitmqctl close_connection``, a deleted exchange closing
the channel, EOF) raises there, and is replaced inside the same attempt: the
event is published on the new connection, and only a failure to connect
counts against the breaker. An event written in the same instant the broker
closes the connection can still be lost without an error, as can events
written in the first second after the network silently drops (no FIN or
RST): py-amqp 5.3 sets ``TCP_USER_TIMEOUT`` to 1s.

**Memory or disk alarm.** RabbitMQ sends Connection.Blocked after the first
publish that follows the alarm and then stops reading from the connection
(py-amqp 5.3 ``Connection._on_blocked`` calls ``on_blocked`` and does
nothing else; publishing on a blocked connection succeeds until the kernel's
buffers fill, thousands of events later, and the write then times out).
The pre-publish read picks the frame up: while blocked, events are dropped
without being sent, and the same connection is kept, so an alarm costs no
new connection (and no blocked connection per breaker probe) and resumes at
the Connection.Unblocked frame.

**Heartbeats are off** for this connection (``heartbeat=0``, also when the
URL says ``?heartbeat=N``): nothing runs between publishes to send them, and
the broker closes a connection that misses two intervals.

Library behavior cited here was read in kombu 5.6.1 and py-amqp 5.3.1.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import functools
import logging
import os
import select
import socket
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Longest the exit hook waits for the broker's Close-Ok (and for the lock) so
# interpreter shutdown never hangs on a broker that stopped reading.
CLOSE_TIMEOUT = 0.5

# Longest a read of one inbound frame may wait for the rest of it (it only
# starts once the socket is readable), and the most frames one check reads.
POLL_TIMEOUT = 0.05
MAX_FRAMES_PER_CHECK = 8

# Waits of one connect pass over one host, in units of connect_timeout: the
# resolver, the TCP connect, the TLS handshake, and the Start, Tune and
# Open-Ok replies (py-amqp Connection.connect() waits connect_timeout for
# each frame). The steps after connecting (channel open, exchange declare and
# the write) wait io_timeout each.
CONNECT_WAITS_PER_HOST = 6
IO_WAITS_AFTER_CONNECT = 3

# How long a waiter lets a holder that took the lock but not yet recorded it
# catch up before it counts as gone.
HOLDER_GRACE = 0.05

# Socket timeout while a connection is torn down without the AMQP handshake:
# over TLS py-amqp 5.3 SSLTransport._shutdown_transport() unwraps the socket,
# which waits for the peer's close_notify.
DISCARD_TIMEOUT = 0.2


def connection_name() -> str:
    """The name RabbitMQ lists the publisher connection of this process under."""
    return f"stemtrace publisher {socket.gethostname()} pid {os.getpid()}"


class PublishError(Exception):
    """An event was not published (the transport counts it as a failure)."""


class PublishDroppedError(PublishError):
    """The event was dropped without an attempt: says nothing about the broker."""


class PublisherBusyError(PublishDroppedError):
    """The connection stayed in use by other publishes for two whole bounds."""


class PublishSkippedError(PublishDroppedError):
    """The circuit breaker opened while this event waited for the connection."""


class PublishBlockedError(PublishError):
    """RabbitMQ reports the connection blocked (memory or disk alarm)."""


class _Process:
    """Everything that belongs to one process: the lock and the connection."""

    def __init__(self) -> None:
        """Start with no connection, owned by the current process."""
        self.pid = os.getpid()
        # Created here, not at import: gevent or eventlet may patch
        # ``threading`` after stemtrace is imported, and a real lock would
        # block the whole hub when contended.
        self.lock = threading.Lock()
        self.conn: Any = None  # kombu Connection
        self.amqp: Any = None  # its py-amqp connection
        self.sock: Any = None  # the socket of ``amqp``
        self.producer: Any = None
        self.blocked = False
        # When the holder of ``lock`` took it (None while free); lets a waiter
        # tell a stuck holder from a new one.
        self.held_since: float | None = None
        # Index of the failover host to try first (the one that worked last).
        self.first_host = 0

    def __del__(self) -> None:
        """Drop the connection when its owner goes away, without a ResourceWarning."""
        # Never raises: this may run at interpreter shutdown, with module
        # globals already gone.
        with contextlib.suppress(Exception):
            if self.pid == os.getpid():
                _discard(self)
            else:
                _abandon(self)


# The sockets the connect in progress has opened (per thread or greenlet).
_connecting: contextvars.ContextVar[list[socket.socket] | None] = (
    contextvars.ContextVar("stemtrace_connecting_sockets", default=None)
)


def track_connecting_socket(sock: socket.socket) -> None:
    """Remember a socket opened by the connect in progress, if there is one.

    py-amqp owns the socket from then on, but if the connect is abandoned
    midway (a greenlet killed by a time limit) nothing is left that can close
    it: the half-open connection stays referenced by the dead stack.
    """
    sockets = _connecting.get()
    if sockets is not None:
        sockets.append(sock)


def _abandon(proc: _Process) -> None:
    """Drop a connection inherited through fork() without touching the parent's.

    Only this process's copy of the file descriptor is closed: no AMQP close
    (it would send frames the parent's session doesn't expect), no shutdown().
    """
    sock = proc.sock
    proc.conn = proc.amqp = proc.sock = proc.producer = None
    if sock is not None:
        # detach() hands over the descriptor without closing it (-1 if the
        # socket is already closed or another thread detached it), so the
        # close below is a plain close(2).
        with contextlib.suppress(Exception):
            fd = sock.detach()
            if fd >= 0:
                os.close(fd)


def _discard(proc: _Process) -> None:
    """Drop the connection without the AMQP close handshake, never blocking.

    The handshake would wait for the broker's reply, which a stalled
    broker never sends. A short socket timeout bounds the one wait left,
    over TLS (see DISCARD_TIMEOUT).
    """
    amqp, sock = proc.amqp, proc.sock
    proc.conn = proc.amqp = proc.sock = proc.producer = None
    proc.blocked = False
    if sock is not None:
        with contextlib.suppress(Exception):
            sock.settimeout(DISCARD_TIMEOUT)
    if amqp is not None:
        # collect() reads py-amqp's private fields, so it is safe on a
        # connection that is already collected (``amqp.transport`` would
        # reconnect there).
        with contextlib.suppress(Exception):
            amqp.collect()
    if sock is not None:
        # py-amqp 5.0.6 _AbstractTransport.close() calls sock.shutdown()
        # without a try: after an RST it raises ENOTCONN and never reaches
        # sock.close(), and collect() above swallowed that.
        with contextlib.suppress(Exception):
            sock.close()


def _readable(sock: Any) -> bool:
    """Whether a read of ``sock`` would find bytes (or EOF) without waiting."""
    pending = getattr(sock, "pending", None)  # SSLSocket: decrypted, buffered
    if pending is not None and pending():
        return True
    # poll(), not select(): select() fails for descriptors above 1023.
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        return bool(poller.poll(0))
    readable, _, _ = select.select([sock], [], [], 0)  # Windows
    return bool(readable)


def _install_guarded_drain(amqp: Any) -> None:
    """Make ``amqp.drain_events(timeout=0)`` safe against a partly received frame.

    py-amqp's own ``Channel._basic_publish`` calls ``drain_events(timeout=0)``
    before every publish (when the broker has the connection.blocked
    capability, i.e. RabbitMQ), and our pre-publish check does too. Its
    ``Transport._read()`` treats EAGAIN as ``continue`` after the first read
    of a frame (5.0.6 and 5.3.1), so a zero-timeout read of a frame that is
    only partly there (a frozen broker, ``docker pause``) spins until the
    frame completes or the peer closes: unbounded under the connection lock,
    and it starves a gevent hub. The instance's ``drain_events`` is replaced
    for the zero-timeout case: read only while the socket is readable, and
    each frame with a positive timeout, so a partial frame raises
    ``socket.timeout`` and keeps its bytes for the next read.
    """
    base = type(amqp).drain_events
    # Weakly: the callable lives on the instance, and a cycle would delay
    # closing the connection when its state is dropped.
    owner = weakref.ref(amqp)

    def drain_events(timeout: float | None = None) -> None:
        """py-amqp ``drain_events``; ``timeout=0`` never spins or waits long."""
        connection = owner()
        if timeout != 0 or connection is None:
            return base(connection, timeout)  # type: ignore[no-any-return]
        for _ in range(MAX_FRAMES_PER_CHECK):
            if not _readable(connection.transport.sock):
                break
            connection.blocking_read(POLL_TIMEOUT)
        raise TimeoutError  # nothing (more) to read, as py-amqp reports it

    amqp.drain_events = drain_events


def _set_blocked(
    proc_ref: weakref.ReferenceType[_Process], blocked: bool, *_reason: str
) -> None:
    """Record Connection.Blocked or Unblocked (py-amqp passes a reason text).

    Holds the state weakly: py-amqp keeps this callback, and a strong
    reference would make a cycle that delays closing the connection.
    """
    proc = proc_ref()
    if proc is not None:
        proc.blocked = blocked
    logger.debug(
        "RabbitMQ publisher connection %s", "blocked" if blocked else "unblocked"
    )


_publishers: weakref.WeakSet[PublisherConnection] = weakref.WeakSet()


def _forget_in_child() -> None:
    """After fork() in the child: abandon every inherited publisher connection."""
    for publisher in list(_publishers):
        publisher._forget()


def close_all() -> None:
    """Close every publisher connection of this process (at interpreter exit)."""
    for publisher in list(_publishers):
        publisher.close()


atexit.register(close_all)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_in_child)


class PublisherConnection:
    """The publisher connection of one transport: one per process, reused."""

    def __init__(
        self,
        *,
        url: str,
        exchange_name: str,
        wrap_connection_class: Callable[[Any], Any],
        connect_timeout: float,
        io_timeout: float,
    ) -> None:
        """Initialize without connecting (the first publish connects).

        Args:
            url: AMQP URL; several hosts separated by ``;`` are failover
                alternates, tried in turn.
            exchange_name: The durable fanout exchange events are published to.
            wrap_connection_class: Adapts kombu's Connection class (e.g. to
                resolve host names boundedly).
            connect_timeout: Seconds each host gets to accept the connection
                (also each frame wait during the AMQP handshake).
            io_timeout: Seconds any later socket read or write may take.
        """
        self._url = url
        self._exchange_name = exchange_name
        self._wrap_connection_class = wrap_connection_class
        self._connect_timeout = connect_timeout
        self._io_timeout = io_timeout
        self._hosts = max(len(url.split(";")), 1)
        # How long a waiter waits for the lock: the longest a holder can take.
        self._lock_timeout = self._attempt_bound()
        self._proc: _Process | None = None
        _publishers.add(self)

    def _attempt_bound(self) -> float:
        """The longest one holder can keep the connection, from the steps it takes.

        The one place this is defined: the lock wait, and the breaker's probe
        timeout (via :attr:`worst_case_s`), derive from it.
        """
        connect_pass = CONNECT_WAITS_PER_HOST * self._connect_timeout * self._hosts
        after_connect = IO_WAITS_AFTER_CONNECT * self._io_timeout
        return connect_pass + after_connect + MAX_FRAMES_PER_CHECK * POLL_TIMEOUT

    @property
    def worst_case_s(self) -> float:
        """The longest :meth:`publish` can take: two waits for the lock, then an attempt."""
        return 3 * self._lock_timeout

    def publish(
        self, payload: dict[str, Any], *, should_attempt: Callable[[], bool]
    ) -> None:
        """Publish ``payload`` on this process's connection, connecting if needed.

        Args:
            payload: JSON-serializable event.
            should_attempt: Whether the circuit breaker still wants this
                attempt; a publish that waited for the connection while the
                breaker opened is skipped instead of connecting to a broker
                that is known to be down.

        Raises:
            PublisherBusyError: The connection stayed in use for two whole
                bounds, each holder young.
            PublishSkippedError: ``should_attempt()`` was false once the
                connection was free.
            PublishBlockedError: RabbitMQ reports the connection blocked.
            Exception: Whatever kombu or py-amqp raise on a failed connect or
                publish; the connection is discarded and the next publish
                connects again, starting with the next failover host.
        """
        proc = self._acquire(self._process())
        try:
            if not should_attempt():
                raise PublishSkippedError(
                    "publishing paused while this event waited for the connection"
                )
            self._publish_locked(proc, payload)
        finally:
            proc.held_since = None
            proc.lock.release()

    def _acquire(self, proc: _Process) -> _Process:
        """Take the connection lock, starting over if its holder can't release it.

        Returns:
            The state whose lock the caller now holds (a new one if the old
            lock was stuck).

        Raises:
            PublisherBusyError: Other publishes kept the lock for two bounds.
        """
        waits = 0
        while True:
            if proc.lock.acquire(timeout=self._lock_timeout):
                proc.held_since = time.monotonic()
                return proc
            if not proc.lock.locked():
                continue  # released since the wait ended
            if self._holder_is_gone(proc):
                proc = self._replace(proc)
                continue
            waits += 1
            if waits == 2:
                raise PublisherBusyError(
                    f"RabbitMQ publisher connection in use for {waits * self._lock_timeout:g}s"
                )

    def _holder_is_gone(self, proc: _Process) -> bool:
        """Whether the lock's holder can no longer release it in time.

        True if nobody recorded taking it (a signal handler raised between
        ``acquire()`` and the ``try`` that releases it) or the holder is past
        the longest an attempt can take.
        """
        since = proc.held_since
        if since is None:
            # A holder that took the lock a moment ago hasn't recorded it yet.
            time.sleep(HOLDER_GRACE)
            since = proc.held_since
        return since is None or time.monotonic() - since > self._lock_timeout

    def _replace(self, stuck: _Process) -> _Process:
        """Start over with a new state; the old one keeps its lock and socket.

        The old connection is left to whoever holds the lock (it is closed
        when the last reference to the state goes), never closed from here.
        """
        if self._proc is stuck:
            logger.warning(
                "RabbitMQ publisher connection was not released by its last user "
                "(waited %gs); starting over with a new connection",
                self._lock_timeout,
            )
            self._proc = _Process()
        return self._process()

    def close(self) -> None:
        """Close this process's connection if it is idle; never waits long.

        Sends the AMQP close and waits at most CLOSE_TIMEOUT for the broker's
        reply (a broker that stopped reading never sends it), so the broker
        doesn't log an abrupt disconnect and interpreter shutdown doesn't
        hang. A connection in use by another thread is left to the OS.
        """
        proc = self._proc
        if proc is None or proc.pid != os.getpid():
            return
        if not proc.lock.acquire(timeout=CLOSE_TIMEOUT):
            return
        try:
            amqp, sock = proc.amqp, proc.sock
            if amqp is not None:
                with contextlib.suppress(Exception):
                    sock.settimeout(CLOSE_TIMEOUT)
                    amqp.close()
            _discard(proc)
        finally:
            proc.lock.release()

    def _forget(self) -> None:
        """In a forked child: drop the parent's connection (see _abandon())."""
        proc, self._proc = self._proc, None
        if proc is not None:
            _abandon(proc)

    def _process(self) -> _Process:
        """The state of this process, starting over after a fork.

        The at-fork hook already did when Python forked; this catches forks
        that skip it (uWSGI forks in C). Two threads of a hookless child may
        each build a state: they then use separate locks and connections,
        which is safe, and the one built last wins.
        """
        proc = self._proc
        if proc is None or proc.pid != os.getpid():
            if proc is not None:
                _abandon(proc)
            proc = self._proc = _Process()
        return proc

    def _publish_locked(self, proc: _Process, payload: dict[str, Any]) -> None:
        """Publish with the lock held: check the connection, connect, send."""
        if proc.amqp is not None:
            self._check_connection(proc)
        if proc.amqp is None:
            self._connect(proc)
        # Imported with the other kombu names in _connect() (lazily, to keep
        # import-time side effects minimal).
        from kombu import Exchange

        exchange = Exchange(self._exchange_name, type="fanout", durable=True)
        try:
            proc.producer.publish(
                payload,
                exchange=exchange,
                routing_key="",
                # kombu declares once per connection (maybe_declare caches in
                # Connection.declared_entities), so this is no round trip
                # after the first publish.
                declare=[exchange],
                serializer="json",
                # Persistent, so the broker can retain messages in queues.
                delivery_mode=2,
                retry=False,
            )
        except BaseException:
            # BaseException: a greenlet killed or a signal handler raising
            # mid-write leaves half a frame on the wire, as a timeout does.
            # The fault may be local to this node: start with the next host
            # of a failover URL next time.
            proc.first_host = (proc.first_host + 1) % self._hosts
            _discard(proc)
            raise

    def _check_connection(self, proc: _Process) -> None:
        """Process the frames the broker sent since the last publish.

        Replaces a connection the broker closed (the caller then connects
        anew) and raises while the broker reports it blocked.

        Raises:
            PublishBlockedError: Connection.Blocked arrived and no
                Connection.Unblocked since.
        """
        try:
            # Each frame runs its handler (Blocked and Unblocked call
            # on_blocked and on_unblocked; Close and Channel.Close raise after
            # collecting; EOF raises OSError). drain_events is the guarded
            # one installed by _install_guarded_drain().
            proc.amqp.drain_events(timeout=0)
        except TimeoutError:
            # socket.timeout is TimeoutError since Python 3.10 (the floor). A
            # partly received frame: its bytes are kept for the next read.
            pass
        except Exception as exc:  # noqa: BLE001  any error here means the connection is unusable; py-amqp raises AMQPError, OSError, even AttributeError for a lost one
            logger.debug(
                "RabbitMQ publisher connection is gone (%s: %s); reconnecting",
                type(exc).__name__,
                exc,
            )
            _discard(proc)
            return
        if proc.blocked:
            raise PublishBlockedError(
                "RabbitMQ reports the publisher connection blocked (memory or "
                "disk alarm); not publishing until it is unblocked"
            )

    def _connect(self, proc: _Process) -> None:
        """Open the connection, channel and producer (one pass over the hosts)."""
        # kombu is imported lazily to keep import-time side effects minimal.
        from kombu import Connection, Producer

        failed_hosts = 0

        def on_host_error(exc: Exception, interval: float) -> None:
            """Count a failed host; kombu switches to the next one after this."""
            nonlocal failed_hosts
            del interval
            failed_hosts += 1
            logger.debug(
                "RabbitMQ publish: failover host unreachable (%s); trying the next",
                exc,
            )

        # Start with the host that worked last, so a dead first host doesn't
        # cost every reconnect a connect timeout.
        first = proc.first_host % self._hosts
        conn = self._connection(self._wrap_connection_class(Connection), first)
        # One connection attempt per host, without sleeping: kombu raises
        # once retries reach max_retries (before switching hosts), so
        # max_retries=0 would never try the alternates of a failover URL.
        # The zero intervals keep kombu from sleeping after a full round.
        opened: list[socket.socket] = []
        token = _connecting.set(opened)
        try:
            conn.ensure_connection(
                errback=on_host_error,
                max_retries=max(len(conn.alt) - 1, 0),
                interval_start=0,
                interval_step=0,
                interval_max=0,
                reraise_as_library_errors=False,
            )
        except BaseException:
            # Every host failed, or one failed in a way kombu doesn't retry
            # on another host (e.g. a refused login): start from the URL's
            # own order next time instead of sticking to this host.
            proc.first_host = 0
            for sock in opened:
                with contextlib.suppress(Exception):
                    sock.close()
            raise
        finally:
            _connecting.reset(token)
        proc.conn = conn
        proc.first_host = (first + failed_hosts) % self._hosts
        proc.blocked = False
        try:
            # The py-amqp connection (connected, so this doesn't reconnect).
            amqp = proc.amqp = conn.connection
            proc_ref = weakref.ref(proc)
            amqp.on_blocked = functools.partial(_set_blocked, proc_ref, True)
            amqp.on_unblocked = functools.partial(_set_blocked, proc_ref, False)
            _install_guarded_drain(amqp)
            proc.sock = self._bound_io(amqp)
            proc.producer = Producer(conn.channel())
        except BaseException:
            proc.first_host = (first + failed_hosts + 1) % self._hosts
            _discard(proc)
            raise

    def _connection(self, connection_cls: Any, first: int) -> Any:
        """Create a kombu connection for the URL, set to start at host ``first``.

        It switches hosts the way kombu's own failover does, so every entry
        gets exactly the parameters kombu resolves for it: a bare host
        (``;rabbit2``) takes credentials, port, virtual host and TLS options
        from the first entry; a URL-form entry (``;amqp://rabbit2``) gets no
        credentials (guest/guest) unless it names them, and takes port and
        virtual host from the previous entry only when it omits them.
        Rebuilding the URL with a different first entry would resolve them
        differently (e.g. a bare ``;rabbit2`` would lose the first entry's
        credentials and vhost). No I/O happens here.
        """
        conn = connection_cls(
            self._url,
            connect_timeout=self._connect_timeout,
            # Wins over ``?heartbeat=`` in the URL, which a kwarg doesn't
            # (kombu 5.6.1 Transport.establish_connection merges
            # transport_options last): see the module docstring.
            transport_options={
                "heartbeat": 0,
                # Shown as the connection's name in RabbitMQ's management UI.
                "client_properties": {"connection_name": connection_name()},
            },
        )
        for _ in range(first):
            conn.maybe_switch_next()
        return conn

    def _bound_io(self, amqp: Any) -> Any:
        """Make every later socket read/write of a py-amqp connection time out.

        py-amqp's ``read_timeout`` transport option doesn't bound reads (its
        reader retries on the resulting EAGAIN), and its frame waits outside
        the handshake use the socket's own timeout. Setting that timeout on
        the connected socket bounds channel open, declare, publish and the
        close. A no-op for transports without a socket attribute.

        Returns:
            The socket (None for such transports).
        """
        transport = getattr(amqp, "transport", None)
        sock = getattr(transport, "sock", None)
        # socket.socket is looked up at call time, so gevent's monkey-patched
        # socket class matches too.
        if isinstance(sock, socket.socket):
            sock.settimeout(self._io_timeout)
            return sock
        logger.debug(
            "RabbitMQ publish: no socket to bound (got %s); reads and writes "
            "after connecting may block",
            type(sock).__name__,
        )
        return None
